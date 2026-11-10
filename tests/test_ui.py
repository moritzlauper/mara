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


def test_measurements_do_not_compare_different_channels_or_resumed_baselines():
    state = parse_log([
        "Taps: 3 channels", "━━ Channel pass: 3 channels",
        "▶ Piano (keys, priority 3), bars 98–114", "distance 8.0 (tonal 8.0)",
        "✓ 8.0 → 7.0 (tonal 7.0)",
        "▶ Intro (pad, priority 2), bars 17–25 (outside the loudest part: overlap and mono bass only)",
        "distance 0.5 (masking 0.5)",
        "Taps: 3 channels", "━━ Channel pass, resumed at Piano: 2 channels left",
        "▶ Piano (keys, priority 3), bars 98–114", "distance 9.0 (tonal 9.0)",
        "✓ 9.0 → 9.1 (tonal 9.1)", "verified: less masking, protected punch",
        "━━ Effects: what does nothing or harms",
    ])
    groups = state["measurement_groups"]
    assert [g["values"] for g in groups] == [[8.0, 7.0], [0.5], [9.0, 9.1]]
    assert [g["scoring"] for g in groups] == ["balance", "local", "balance"]
    assert [g["run"] for g in groups] == [1, 1, 2]
    assert groups[-1]["verification"] == "less masking, protected punch"
    assert state["section"] is None  # effects must not be labelled as the previous channel


def test_reverted_run_clears_section_comparisons():
    state = parse_log(["━━ Master against the reference", "▶ loudest part", "distance 8.0 (tonal 8.0)",
                       "Stopped: reverted to the state before the run."])
    assert state["measurement_groups"] == []


def test_restarted_ui_can_pause_its_existing_run(tmp_path):
    import subprocess
    import sys
    from mixmatch.ui import _AttachedProcess

    child = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"  # macOS Python's launcher has finished execing
        folder = tmp_path / "runs"
        folder.mkdir()
        log_path = str(folder / "ui-test.log")
        (folder / "ui-process.json").write_text(json.dumps({
            "pid": child.pid, "identity": _AttachedProcess.identify(child.pid), "log_path": log_path}))
        runner = Runner(str(tmp_path))
        assert runner.running()
        assert runner.latest_log() == log_path
        runner.stop()
        child.wait(timeout=5)
        assert not runner.running()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_ui_does_not_signal_a_reused_pid(tmp_path, monkeypatch):
    from mixmatch.ui import _AttachedProcess

    monkeypatch.setattr(_AttachedProcess, "identify", staticmethod(lambda pid: "different process"))
    signals = []
    monkeypatch.setattr("mixmatch.ui.os.kill", lambda *args: signals.append(args))
    proc = _AttachedProcess(123, "old MARA process")
    proc.terminate()
    assert proc.poll() == 0
    assert signals == []


def test_ui_mode_follows_the_resumed_run_and_whole_song_silence():
    lines = ["Taps: 34 channels", "Mode: song with vocals", "Taps: 34 channels"]
    assert parse_log(lines)["mode"] is None
    lines.append("Mode: beat (no audible vocals; muted channels do not count)")
    assert parse_log(lines)["mode"] == "beat"
    lines += ["Taps: 34 channels", "Mode: song (audible vocals)",
              "The vocal channels are silent in the whole song: treating it as a beat (no vocals)."]
    assert parse_log(lines)["mode"] == "beat"


def test_activity_reports_waiting_and_updates_scan_without_duplicate_events():
    from mixmatch.activity import report

    lines = []
    report(lines.append, "Listening to the whole song", started=100, current=1, total=120)
    report(lines.append, "Listening to the whole song", started=100, current=10, total=120)
    state = parse_log(lines)
    assert state["activity"]["current"] == 10 and state["activity"]["started"] == 100
    assert len(state["events"]) == 1
    report(lines.append, "Planning the next changes · waiting for AI", started=140)
    state = parse_log(lines)
    assert state["activity"]["started"] == 140 and state["activity"]["total"] is None
    assert len(state["events"]) == 2


def test_interrupted_logs_show_a_stop_instead_of_a_traceback():
    old = ('Traceback (most recent call last):\n'
           '  File "agent.py", line 41, in wait\n'
           '    time.sleep(seconds)\n'
           '    ~~~~~~~~~~^^^^^^^^^\n'
           'KeyboardInterrupt\n')
    for log in (old, "Stopped: run interrupted.\n"):
        state = parse_log(log.splitlines(keepends=True))
        assert state["done"] == "Stopped: run interrupted."
        assert state["events"] == [{"kind": "neutral", "text": "Stopped: run interrupted."}]


def test_tracebacks_show_the_actual_error_and_stop_stale_progress():
    log = ('  progress {"done": 1, "total": 10, "t": 100}\n'
           'Traceback (most recent call last):\n'
           '  File "taps.py", line 44, in window\n'
           '    return [f for f in self.frames]\n')
    partial = parse_log(log.splitlines(keepends=True))
    assert partial["done"] and "Traceback" not in partial["events"][-1]["text"]
    log += "RuntimeError: deque mutated during iteration\n"
    state = parse_log(log.splitlines(keepends=True))
    assert state["done"] == "Stopped: RuntimeError: deque mutated during iteration"
    assert state["events"] == [{"kind": "warning", "text": state["done"]}]
    resumed = parse_log((log + 'Taps: 3 channels\nprogress {"done": 2, "total": 10, "t": 200}\n').splitlines(True))
    assert resumed["done"] is None and resumed["progress"]["done"] == 2


def test_buy_list(tmp_path, monkeypatch):
    import mixmatch.style as style
    monkeypatch.setattr(style, "BASE", str(tmp_path / "cache"))          # no previews looked up before
    monkeypatch.setattr(style, "apple_preview", lambda *a, **k: {})      # and no network
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
        assert b"<title>MARA</title>" in urllib.request.urlopen(base + "/").read()
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


def test_download_watcher_takes_only_finished_matching_downloads(tmp_path):
    import os
    import time as _t
    import zipfile

    from mixmatch.ui import DownloadWatcher, matches

    sugg = [{"title": "SOPHIE – Immaterial", "free": False}, {"title": "Charli XCX, Kim Petras – Unlock It", "free": False}]
    assert matches("SOPHIE - OIL OF EVERY PEARL'S UN-INSIDES - 09 Immaterial.flac", sugg[0])
    assert not matches("Kick Sample 01.wav", sugg[0])

    dl = tmp_path / "Downloads"
    dl.mkdir()
    w = DownloadWatcher(str(tmp_path), str(dl))
    w.started -= 5
    (dl / "SOPHIE - Immaterial.wav").write_bytes(b"RIFF")
    (dl / "random beat.wav").write_bytes(b"RIFF")
    (dl / "Charli XCX - Album.zip.crdownload").write_bytes(b"...")
    with zipfile.ZipFile(dl / "Charli XCX - Album.zip", "w") as z:
        z.writestr("Charli XCX - Album - 03 Unlock It.flac", b"fLaC")
        z.writestr("Charli XCX - Album - 04 Other Song.flac", b"fLaC")
    assert w.scan(sugg) == []                        # first look: sizes not settled yet
    added = w.scan(sugg)
    assert sorted(added) == ["Charli XCX - Album - 03 Unlock It.flac", "SOPHIE - Immaterial.wav"]
    assert sorted(os.listdir(tmp_path / "references")) == sorted(added)
    assert w.scan(sugg) == []                        # never twice


def test_a_resumed_run_shows_the_whole_history_and_its_progress(tmp_path):
    from mixmatch.ui import log_chain
    first = tmp_path / "ui-20260101-100000.log"
    first.write_text("Taps: 3 channels\n━━ Master against the reference\n  distance 6.0   (tonal 6.0)\n"
                     "    ✓ 6.0 → 4.0   (tonal 4.0)\n━━ Channel pass: 3 channels\n  distance 2.5   (masking 2.5)\n")
    second = tmp_path / "ui-20260101-110000.log"
    second.write_text("Taps: 3 channels\n  progress {\"done\": 0, \"total\": 10, \"t\": 100.0, \"step\": \"x\"}\n"
                      "━━ Channel pass, resumed at B: 2 channels left\n  distance 2.4   (masking 2.4)\n"
                      "  progress {\"done\": 4, \"total\": 10, \"t\": 220.0, \"step\": \"B\"}\n")
    chain = log_chain(str(second))
    assert chain == [str(first), str(second)]
    s = parse_log([l for p in chain for l in open(p)])
    assert s["history"] == [6.0, 4.0, 2.5, 2.4]
    assert s["history_phase"] == ["master", "master", "channels", "channels"] and s["history_run"] == [1, 1, 1, 2]
    assert s["progress"]["done"] == 4 and s["progress_start"]["t"] == 100.0


def test_resume_retains_comparison_and_song_after_failed_tap_start(tmp_path):
    from mixmatch.ui import log_chain

    first = tmp_path / "ui-20260101-100000.log"
    first.write_text("Taps: 3 channels\nKey: G minor\nsounds like: atmospheric house\n"
                     "━━ Master against the reference\n  distance 2.5   (tonal 2.5)\n")
    failed = tmp_path / "ui-20260101-110000.log"
    failed.write_text("Taps: 0 channels\nStopped: no channel reports through a MixMatch Tap, "
                      "so there is nothing to measure.\n")
    resumed = tmp_path / "ui-20260101-120000.log"
    resumed.write_text("Taps: 3 channels\n↻ Continuing the run from 2026-01-01 10:00 at effects\n"
                       "━━ Effects: what does nothing or harms\n")
    chain = log_chain(str(resumed))
    assert chain == [str(first), str(resumed)]
    state = parse_log([line for path in chain for line in open(path)])
    assert state["distance"] == 2.5
    assert state["key"] == "G minor"
    assert state["style"] == "atmospheric house"
    assert state["phase"] == "Effects: what does nothing or harms"
    assert state["done"] is None

    # A new run must never inherit the previous run's comparison or song.
    resumed.write_text("Taps: 3 channels\n━━ Master against the reference\n")
    assert log_chain(str(resumed)) == [str(resumed)]


def test_buy_list_adds_a_preview_from_the_cache(tmp_path, monkeypatch):
    import json as _json
    import mixmatch.style as style
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "apple_previews.json").write_text(_json.dumps({"sophie|immaterial": {"preview": "https://p/1"}}))
    monkeypatch.setattr(style, "BASE", str(tmp_path / "cache"))
    monkeypatch.setattr(style, "apple_preview", lambda *a, **k: {})
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "20261109-1-buy.md").write_text(
        "# References (x)\n\n## To get\n\n- **SOPHIE – Immaterial**: bright.  \n  [Spotify](https://s/1)\n")
    assert buy_list(str(tmp_path))[0]["links"]["Preview"] == "https://p/1"


def test_checkpoint_for_the_continue_button(tmp_path):
    import json as _json
    from mixmatch.ui import checkpoint
    assert checkpoint(str(tmp_path)) is None
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "checkpoint.json").write_text(_json.dumps(
        {"phase": "channels", "next_channel": "1-Group", "done": 12.0, "total": 48.0, "stamp": "x"}))
    assert checkpoint(str(tmp_path)) == {"phase": "channels", "next_channel": "1-Group", "stamp": "x", "percent": 25,
                                         "complete": True}


def test_reference_choice_is_required_and_search_only_prepares(tmp_path, monkeypatch):
    import mixmatch.ui as ui
    from mixmatch import songs

    monkeypatch.setattr(ui, "project_references", lambda: [])
    monkeypatch.setattr(ui, "run_process_alive", lambda: False)
    songs.create(str(tmp_path), "Song")
    workdir = tmp_path / "songs" / "Song"
    shared = tmp_path / "references"
    shared.mkdir()
    (shared / "chosen.wav").write_bytes(b"test audio")
    runner, starts = Runner(str(tmp_path)), []
    monkeypatch.setattr(runner, "start", lambda args: starts.append(args) or "test.log")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runner, live_set_fn=lambda: {
        "name": "Song", "channels": ["Kick"], "taps": 1, "total": 1, "missing": [], "playing": False}))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}" + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req))
        except urllib.error.HTTPError as e:
            return json.load(e)

    try:
        assert "Choose at least one" in call("/api/run", {"reference_mode": "selected", "continue": True})["error"]
        assert starts == []
        call("/api/run", {"reference_mode": "discover"})
        assert "--review-references" in starts[-1] and "--auto-ref" in starts[-1]
        s = call("/api/state")
        assert s["reference_files"] == [] and len(s["reference_candidates"]) == 1
        candidate = s["reference_candidates"][0]
        assert candidate["name"] == "chosen.wav" and candidate["source"] == "shared references"
        assert "path" not in candidate
        assert "error" in call("/api/references/choose", {"id": str(shared / "chosen.wav")})
        assert call("/api/references/choose", {"id": candidate["id"]}) == {"saved": "chosen.wav"}
        assert (shared / "chosen.wav").exists()
        assert (workdir / "references" / "chosen.wav").read_bytes() == b"test audio"
        (workdir / "runs" / "checkpoint.json").write_text(json.dumps({"phase": "references", "complete": False}))
        call("/api/run", {"reference_mode": "selected", "continue": True})
        args = starts[-1]
        assert all(f in args for f in ("--no-auto-ref", "--no-suggest", "--replace-references", "--continue", "--quick"))
        assert "--auto-ref" not in args and "--review-references" not in args
    finally:
        server.shutdown()


def test_reverting_clears_the_comparison_from_the_discarded_mix():
    s = parse_log(LOG + ["Stopped: reverted to the state before the run. Choose your reference tracks before mixing again."])
    assert not s["history"] and not s["parts"] and not s["explain"] and not s["effects"]
    assert s["progress"] is None and s["distance"] is None
    ready = parse_log(["Ready: choose your reference tracks, then mix with them. The song scan is saved."])
    assert ready["done"].startswith("Ready:")


def test_an_m4a_reference_becomes_a_wav(tmp_path):
    import shutil
    import subprocess

    import numpy as np
    import pytest
    import soundfile as sf

    from mixmatch.ui import duration_s, to_wav
    if not shutil.which("afconvert"):
        pytest.skip("afconvert is macOS only")
    wav = tmp_path / "src.wav"
    sf.write(wav, np.zeros((44100 * 2, 2), dtype="float32"), 44100)
    m4a = tmp_path / "song.m4a"
    subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(wav), str(m4a)], check=True)
    out = to_wav(str(m4a))
    assert out.endswith("song.wav") and not m4a.exists() and abs(duration_s(out) - 2.0) < 0.1


def test_an_apple_preview_is_named_after_its_track(tmp_path, monkeypatch):
    import json as _json
    import mixmatch.style as style
    from mixmatch.ui import preview_name
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "apple_previews.json").write_text(_json.dumps(
        {"sophie|immaterial": {"preview": "https://a/x/mzaf_161.plus.aac.p.m4a"}}))
    monkeypatch.setattr(style, "BASE", str(tmp_path / "cache"))
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "20261109-1-buy.md").write_text("## To get\n\n- **SOPHIE – Immaterial**: bright.\n")
    assert preview_name(str(tmp_path), "mzaf_161.plus.aac.p.wav") == "SOPHIE – Immaterial (Apple preview)"
    assert preview_name(str(tmp_path), "my song.wav") is None


def test_a_stopped_or_crashed_run_is_not_running_any_more():
    stopped = parse_log(["Taps: 3 channels, 0 reference(s)", 'progress {"done": 0, "total": 19, "t": 1, "step": "scan"}',
                         "Stopped: by you. Everything kept so far stays in the set."])
    assert stopped["done"].startswith("Stopped")
    crashed = parse_log(["Taps: 3 channels, 0 reference(s)", 'progress {"done": 0, "total": 19, "t": 1, "step": "scan"}',
                         "Traceback (most recent call last):", "KeyError: 'x'"])
    assert crashed["done"]
