import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mixmatch import monitoring, workflow, songs, ui
from mixmatch.agent import Agent, Settings
from mixmatch.planner import RulePlanner
from mixmatch.sim import demo_project


def test_ui_reattaches_after_python_launcher_replaces_itself(monkeypatch):
    saved = json.dumps([100.5, ["/venv/bin/python", "-m", "mixmatch.cli", "run"]])
    process = ui._AttachedProcess(123, saved)
    actual = [100.5, ["/framework/Python", "-m", "mixmatch.cli", "run"]]
    monkeypatch.setattr(process, "identify", lambda pid: json.dumps(actual))
    assert process.poll() is None
    actual[0] = 101.5  # same PID, new process: must never attach
    assert process.poll() == 0


def test_output_toggle_restores_saved_level_and_tracks_actual_fader(tmp_path):
    song, hub, env, live = demo_project()
    folder = str(tmp_path)
    workflow.begin(folder)
    try:
        original = monitoring.main_volume(live.get_set())["value"]
        monitoring.begin(folder, original, True)
        assert monitoring.set_silent(live, folder, True)["silent"]
        assert monitoring.main_volume(live.get_set())["value"] == 0
        assert not monitoring.set_silent(live, folder, False)["silent"]
        assert monitoring.main_volume(live.get_set())["value"] == original
        # A user's manual listening-level adjustment becomes the next restore level.
        live.set("m", "mixer", "Volume", value=0.6)
        monitoring.set_silent(live, folder, True)
        monitoring.set_silent(live, folder, False)
        assert monitoring.main_volume(live.get_set())["value"] == 0.6
        workflow.begin(folder)
        assert monitoring.read(folder) == {}  # previous run's level is never reused
    finally:
        workflow.end()


def test_finished_run_sets_unity_and_remembers_it_for_listening(tmp_path):
    from mixmatch.display import parse_display

    song, hub, env, live = demo_project()
    folder = str(tmp_path)
    workflow.begin(folder)
    try:
        monitoring.begin(folder, 0.4, True)
        live.set("m", "mixer", "Volume", value=0)
        raw = monitoring.finish(live, folder)
        assert raw > 0  # 0 dB is not the raw value zero, which mutes Live.
        assert parse_display(monitoring.main_volume(live.get_set())["display"]) == 0
        assert monitoring.read(folder)["volume"] == raw
        monitoring.set_silent(live, folder, True)
        monitoring.set_silent(live, folder, False)
        assert monitoring.main_volume(live.get_set())["value"] == raw
    finally:
        workflow.end()


@pytest.mark.parametrize("outcome", ["finished", "paused", "failed"])
def test_run_cleanup_resets_main_only_after_success(tmp_path, monkeypatch, outcome):
    from types import SimpleNamespace
    from mixmatch import cli
    from mixmatch.display import parse_display

    song, hub, env, live = demo_project()
    live.set("m", "mixer", "Volume", target=-6)
    original = monitoring.main_volume(live.get_set())["value"]

    class FakeAgent:
        _mix_complete = outcome != "paused"
        _failure = "planner failed" if outcome == "failed" else None

        def __init__(self, *args, **kwargs):
            self.ex = SimpleNamespace(refresh=lambda: None)

        def mute_main(self):
            live.set("m", "mixer", "Volume", value=0)
            return original

        def restore_main_when_quiet(self, value):
            live.set("m", "mixer", "Volume", value=value)

        def run(self, **kwargs):
            if outcome == "paused":
                raise KeyboardInterrupt()

    args = SimpleNamespace(**dict.fromkeys([
        "rules", "planner", "auto_ref", "ref", "minutes", "assist", "roles", "mode", "loudness_from",
        "focus", "focus_bars", "quick", "cont", "resume", "review_references", "replace_references",
        "no_suggest", "library", "jamendo", "no_audit"], False), silent=True, wait=0, no_auto_ref=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_connect", lambda: (live, hub))
    monkeypatch.setattr(cli, "make_planner", lambda *_: RulePlanner())
    monkeypatch.setattr(cli, "_refset", lambda *_: None)
    monkeypatch.setattr(cli, "set_taps_on", lambda *_: False)
    monkeypatch.setattr(cli, "Agent", FakeAgent)
    workflow.begin("runs")
    try:
        if outcome == "paused":
            with pytest.raises(KeyboardInterrupt):
                cli._cmd_run(args)
        else:
            cli._cmd_run(args)
        level = parse_display(monitoring.main_volume(live.get_set())["display"])
        assert level == (0 if outcome == "finished" else -6)
        assert workflow.read("runs")["output_restored"]
    finally:
        workflow.end()


def test_scan_acknowledges_pause_during_first_second(tmp_path):
    song, hub, env, live = demo_project()
    agent = Agent(live, hub, RulePlanner(), Settings(), env=env, log=lambda *_: None)
    agent.ex.refresh()
    workflow.begin(str(tmp_path))
    wait = env.wait
    def request_during_wait(seconds):
        wait(seconds)
        (tmp_path / "pause.request").touch()
    env.wait = request_during_wait
    start = env.now()
    try:
        with pytest.raises(KeyboardInterrupt):
            agent.scan([("Song", 0, 480)])
        assert env.now() - start < 1
    finally:
        workflow.end()


def test_running_ui_accepts_output_toggle_without_stopping_run(tmp_path, monkeypatch):
    from mixmatch import live_client, live_identity

    song, hub, env, live = demo_project()
    live.connect = lambda: live
    live.close = lambda: None
    monkeypatch.setattr(live_client, "LiveClient", lambda **kw: live)
    monkeypatch.setattr(live_identity, "document_path", lambda: "/tmp/monitor-test.als")
    songs.activate_live_set(str(tmp_path), "Monitor test", ["Kick"], "/tmp/monitor-test.als")
    runner = ui.Runner(str(tmp_path))
    monkeypatch.setattr(runner, "running", lambda: True)
    folder = str(tmp_path / "songs" / "Monitor-test" / "runs")
    workflow.begin(folder)
    original = monitoring.main_volume(live.get_set())["value"]
    monitoring.begin(folder, original, True)
    monitoring.set_silent(live, folder, True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), ui.make_handler(runner, live_set_fn=lambda: None))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/output",
                                         data=json.dumps({"silent": False}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["silent"] is False
        assert runner.running()
        assert monitoring.main_volume(live.get_set())["value"] == original
    finally:
        server.shutdown()
        server.server_close()
        workflow.end()


def test_unmute_recovers_zero_monitor_after_live_restart(tmp_path):
    from mixmatch import ab
    _,_,_,live=demo_project()
    folder=str(tmp_path)
    before=ab.save(live.get_set(),folder,'original','before')
    expected=monitoring.main_volume(live.get_set())['value']
    workflow.begin(folder)
    workflow.update(before=before.split('/')[-1])
    live.set('m','mixer','Volume',value=0)
    monitoring._write(folder, {'run_id':workflow.read(folder)['id'],'volume':0,'managed':False})
    assert monitoring.set_silent(live,folder,False)['silent'] is False
    assert monitoring.main_volume(live.get_set())['value']==expected
    monitoring.set_silent(live,folder,True)
    monitoring.set_silent(live,folder,False)
    assert monitoring.main_volume(live.get_set())['value']==expected
    workflow.end()
