import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from mixmatch.ui import Runner, buy_list, make_handler, parse_log

LOG = """Planner: Claude Code: opus for the master, haiku for channels
Key: Ebm (minor, from MIDI) · chords: Ebm7 – Dbmaj7
━━ Master against the reference
▶ loudest part
  distance 5.06   (loudness 1.76 · masking 1.50 · tonal 1.23)
  round 1: The master is quiet.
    · limiter master: raise it
    ✓ 5.06 → 4.05   (masking 1.58 · tonal 1.13 · loudness 1.02)
    ↺ undone: 4.05 → 4.20
  ⊘ bypassed Redux on 6-Drum Rack: does nothing here (level +0.0 dB)
  explain {"loudness": "1.3 LU quieter than the reference", "masking": "Kick ↔ Sub at 40 Hz"}
Done. 3 rounds, 2 kept, 1 undone.
""".splitlines(keepends=True)


def test_parse_log():
    s = parse_log(LOG)
    assert s["phase"] == "Master against the reference" and s["section"] == "loudest part"
    assert s["history"] == [5.06, 4.05] and s["parts"]["loudness"] == 1.02
    assert s["key"].startswith("Ebm") and s["effects"] and s["done"].startswith("Done.")
    assert s["explain"]["masking"] == "Kick ↔ Sub at 40 Hz"
    assert [e["kind"] for e in s["events"]][:4] == ["phase", "section", "round", "action"]


def test_buy_list(tmp_path):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "20261109-1-buy.md").write_text(
        "# References (hyperpop)\n\n## To get\n\n- **SOPHIE – Immaterial**: bright.  \n"
        "  [Spotify](https://s/1) · [Bandcamp](https://b/1)\n\n## Free\n\n"
        "- **Artist – Free One** (NCS): free and close.  \n  [Spotify](https://s/2) · [NCS](https://n/2)\n")
    assert buy_list(str(tmp_path)) == [
        {"title": "SOPHIE – Immaterial", "why": "bright.", "free": False,
         "links": {"Spotify": "https://s/1", "Bandcamp": "https://b/1"}},
        {"title": "Artist – Free One", "why": "free and close.", "free": True, "source": "NCS",
         "links": {"Spotify": "https://s/2", "NCS": "https://n/2"}},
    ]


def test_server_state_and_roles(tmp_path):
    (tmp_path / "roles.json").write_text(json.dumps({"Kick": {"role": "kick", "priority": 5, "note": ""}}))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Runner(str(tmp_path))))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert b"<title>MixMatch</title>" in urllib.request.urlopen(base + "/").read()
        state = json.load(urllib.request.urlopen(base + "/api/state"))
        assert state["running"] is False and state["reference_files"] == []
        req = urllib.request.Request(base + "/api/roles", data=json.dumps({"Kick": {"role": "sub", "priority": 4}}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req)
        assert json.loads((tmp_path / "roles.json").read_text())["Kick"]["role"] == "sub"
    finally:
        server.shutdown()


def test_references_can_be_added_and_removed_to_the_trash(tmp_path, monkeypatch):
    monkeypatch.setenv("MIXMATCH_TRASH", str(tmp_path / "trash"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Runner(str(tmp_path))))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, data=b"", ctype="application/octet-stream"):
        req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": ctype})
        try:
            return json.load(urllib.request.urlopen(req))
        except urllib.error.HTTPError as e:
            return json.load(e)

    try:
        assert call("PUT", "/api/references/song%20one.wav", b"RIFF....")["saved"] == "song one.wav"
        assert (tmp_path / "references" / "song one.wav").exists()
        assert "error" in call("PUT", "/api/references/..%2Fevil.wav", b"x")           # stays inside references/
        assert "error" in call("PUT", "/api/references/notes.txt", b"x")                # audio only
        out = call("POST", "/api/references/remove", json.dumps({"name": "song one.wav"}).encode(), "application/json")
        assert out["trash"].endswith("song one.wav") and not (tmp_path / "references" / "song one.wav").exists()
        assert (tmp_path / "trash" / "song one.wav").exists()
    finally:
        server.shutdown()


def test_cached_keeps_the_last_answer_when_live_is_busy():
    from mixmatch.ui import _Cached

    answers = iter([[{"track": "ref", "file": "a.wav"}], None])
    c = _Cached(lambda: next(answers))
    c._refresh()
    assert c.value == [{"track": "ref", "file": "a.wav"}] and c.reachable is True
    c._refresh()                                           # Live busy: the last answer stays
    assert c.value == [{"track": "ref", "file": "a.wav"}] and c.reachable is False


def test_description_is_saved_in_the_project(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Runner(str(tmp_path))))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/api/project", data=json.dumps({"description": " Hyperpop, happy "}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req)
        assert json.loads((tmp_path / "mixmatch.json").read_text())["description"] == "Hyperpop, happy"
        assert json.load(urllib.request.urlopen(base + "/api/project"))["description"] == "Hyperpop, happy"
    finally:
        server.shutdown()


def test_import_from_downloads_takes_new_audio_and_zips(tmp_path):
    import zipfile

    from mixmatch.ui import download_reference, import_downloads

    dl = tmp_path / "Downloads"
    dl.mkdir()
    (dl / "free track.wav").write_bytes(b"RIFF")
    (dl / "notes.pdf").write_bytes(b"%PDF")
    with zipfile.ZipFile(dl / "album.zip", "w") as z:
        z.writestr("Artist - Album/01 Song.flac", b"fLaC")
        z.writestr("__MACOSX/._01 Song.flac", b"x")
    added = import_downloads(str(tmp_path), str(dl))
    assert sorted(added) == ["01 Song.flac", "free track.wav"]
    assert import_downloads(str(tmp_path), str(dl)) == []                 # nothing twice
    import pytest

    with pytest.raises(ValueError):                                      # only Jamendo is fetched directly
        download_reference(str(tmp_path), "https://ncs.io/track.mp3", "x")
