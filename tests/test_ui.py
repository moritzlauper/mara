import json
import threading
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
Done. 3 rounds, 2 kept, 1 undone.
""".splitlines(keepends=True)


def test_parse_log():
    s = parse_log(LOG)
    assert s["phase"] == "Master against the reference" and s["section"] == "loudest part"
    assert s["history"] == [5.06, 4.05] and s["parts"]["loudness"] == 1.02
    assert s["key"].startswith("Ebm") and s["effects"] and s["done"].startswith("Done.")
    assert [e["kind"] for e in s["events"]][:4] == ["phase", "section", "round", "action"]


def test_buy_list(tmp_path):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "20261109-1-buy.md").write_text(
        "# x\n\n- **SOPHIE – Immaterial**: bright.  \n  [Spotify](https://s/1) · [Bandcamp](https://b/1)\n")
    assert buy_list(str(tmp_path)) == [{"title": "SOPHIE – Immaterial", "why": "bright.",
                                        "links": {"spotify": "https://s/1", "bandcamp": "https://b/1"}}]


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
