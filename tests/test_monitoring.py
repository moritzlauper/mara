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
