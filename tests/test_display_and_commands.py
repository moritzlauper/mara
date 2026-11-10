import math
import pytest

from MixMatch.commands import parse_display as live_parse
from mixmatch.display import parse_display
from mixmatch.sim import demo_project


def test_set_reports_ableton_document_name_and_supports_older_live():
    song, _, _, live = demo_project()
    assert live.get_set()["name"] == ""
    assert live.get_set()["file_path"] == ""
    song.name = "Cleopatra"
    song.file_path = "/Music/Cleopatra Project/Cleopatra.als"
    state = live.get_set()
    assert state["name"] == "Cleopatra"
    assert state["file_path"] == song.file_path


def test_role_context_reads_pad_mapping_samples_and_only_played_unmuted_notes():
    from types import SimpleNamespace as NS
    from mixmatch.roles import guess
    song, _, _, live = demo_project()
    t = song.tracks[0]
    sample = NS(name="Simpler", class_name="OriginalSimpler", type=1, parameters=[],
                sample=NS(file_path="/Samples/Crash_Cymbal.wav"))
    chain = NS(name="C1", mute=False, solo=False, devices=[sample])
    pad = NS(name="C1", note=36, mute=False, solo=False, chains=[chain])
    rack = NS(name="DS Drum Rack", class_name="DrumGroupDevice", type=1, parameters=[],
              can_have_drum_pads=True, drum_pads=[pad])
    t.name, t.devices, t.has_midi_input = "23-DS Drum Rack", [rack], True
    def clip(notes, muted=False):
        return NS(is_midi_clip=True, muted=muted, start_marker=0, end_marker=16,
                  get_notes_extended=lambda *args: [NS(pitch=p, mute=m, velocity=v) for p, m, v in notes])
    t.arrangement_clips = [clip([(36, False, 100), (38, True, 100), (49, False, 0)]),
                          clip([(38, False, 100)], muted=True)]
    t.clip_slots = []
    result = live.request("role_context")["tracks"]["t0"]
    assert result["played_notes"] == {"36": 1}
    assert result["devices"][0]["drum_pads"][0]["note"] == 36
    role = guess({"key": "t0", "name": t.name, "devices": result["devices"],
                  "played_notes": result["played_notes"]}, None)
    assert role.role == "cymbals"  # pitch 36 must not imply a kick
    assert len(t.devices) == 1 and not t.mute and not chain.mute


def test_run_uses_configured_api_billing(monkeypatch):
    import mixmatch.cli as cli
    import os

    monkeypatch.setenv("MIXMATCH_PLANNER", "api")
    monkeypatch.setattr(cli, "_stoppable", lambda: None)
    seen = []
    monkeypatch.setattr(cli, "cmd_run", lambda args: seen.append((args.planner, os.environ["MIXMATCH_PLANNER"])))
    cli.main(["run", "--continue"])
    assert seen == [("api", "api")]


@pytest.mark.parametrize("interrupted", [False, True])
def test_silent_run_mutes_before_playback_and_stops_before_restoring(monkeypatch, tmp_path, interrupted):
    import json
    import mixmatch.cli as cli
    from mixmatch.agent import Agent

    song, hub, env, live = demo_project()
    env.wait(5)
    volume = song.master_track.mixer_device.volume
    original = volume.value
    events = []
    transport, set_param = live.transport, live.set

    def watch_transport(**kw):
        if kw.get("play"):
            assert volume.value == 0, "playback started before muting"
            events.append("play")
        return transport(**kw)

    def watch_set(track, device, param, **kw):
        if track == "m" and param == "Volume" and kw.get("value", 0) > 0:
            assert not song.is_playing, "output restored while playback was running"
            events.append("restore")
        return set_param(track, device, param, **kw)

    def wait(seconds):
        assert volume.value == 0
        if interrupted:
            raise KeyboardInterrupt
        env.wait(seconds)

    def run(agent, deadline):
        assert volume.value == 0
        agent.play_loop(0, 16)

    monkeypatch.setattr(live, "transport", watch_transport)
    monkeypatch.setattr(live, "set", watch_set)
    monkeypatch.setattr(cli, "_connect", lambda: (live, hub))
    monkeypatch.setattr(cli, "Agent", lambda *a, **kw: Agent(*a, **kw, env=env))
    monkeypatch.setattr(cli, "_stoppable", lambda: None)
    monkeypatch.setattr(cli.time, "sleep", wait)
    monkeypatch.setattr(Agent, "_run", run)
    if interrupted:
        with pytest.raises(SystemExit) as exc:
            cli.main(["run", "--silent", "--rules", "--no-auto-ref", "--wait", "1"])
        assert exc.value.code == 130
    else:
        cli.main(["run", "--silent", "--rules", "--no-auto-ref", "--wait", "1"])
        assert events.count("play") == 2
        before = next((tmp_path / "runs").glob("*-before.json"))
        assert json.loads(before.read_text())["tracks"]["m"]["mixer"]["Volume"] == original
    assert events[0] == "play" and events[-1] == "restore"
    assert volume.value == original and not song.is_playing


def test_both_parsers_agree():
    for text in ["-3.0 dB", "1.20 kHz", "254 Hz", "-inf dB", "50L", "12R", "C", "0.71", "100 %", "On"]:
        a, b = parse_display(text), live_parse(text)
        assert a == b or (a is None and b is None) or (math.isinf(a) and math.isinf(b))


def test_set_by_display_target_and_batch_undo():
    song, hub, env, live = demo_project()
    res = live.set("t0", "mixer", "Volume", target=-6.0)
    assert res["display"] == "-6.0 dB"
    old = song.tracks[0].mixer_device.volume.value
    live.set("t0", "mixer", "Volume", target=-3.0)
    live.batch([{"track": "t0", "device": "mixer", "param": "Volume", "value": old}])
    assert song.tracks[0].mixer_device.volume.value == old


def test_get_set_lists_tracks_cues_and_master():
    *_, live = demo_project()
    s = live.get_set()
    names = [t["name"] for t in s["tracks"]]
    assert names[0] == "Kick" and "REF Song" in names and s["tracks"][-1]["key"] == "m"
    assert [c["name"] for c in s["cue_points"]] == ["Drop", "End"]


def test_add_taps_appends_to_channels_and_master_only():
    from mixmatch.cli import add_taps

    song, hub, env, live = demo_project()
    live.load_device("t0", "EQ Eight")
    channels = [tr for tr in song.tracks if not tr.name.startswith("REF")]
    assert add_taps(live, log=lambda *_: None) == len(channels) + 1          # no tap on the reference track
    assert song.tracks[0].devices[-1].name == "MixMatch Tap"   # after the EQ, not in front of it
    assert song.master_track.devices[-1].name == "MixMatch Tap"
    assert add_taps(live, log=lambda *_: None) == 0             # second run adds nothing
    assert add_taps(live, log=lambda *_: None, replace=True) == len(channels) + 1
    assert [d.name for d in song.tracks[0].devices] == ["EQ Eight", "MixMatch Tap"]


def test_new_devices_go_before_the_tap():
    from mixmatch.cli import add_taps

    song, hub, env, live = demo_project()
    add_taps(live, log=lambda *_: None)
    assert live.load_device("t0", "EQ Eight") == 0               # the tap was the only device
    assert live.load_device("t0", "Utility") == 1
    assert live.load_device("m", "Limiter") == 0
    assert [d.name for d in song.tracks[0].devices] == ["EQ Eight", "Utility", "MixMatch Tap"]
    assert [d.name for d in song.master_track.devices] == ["Limiter", "MixMatch Tap"]


def test_load_device_through_browser():
    song, hub, env, live = demo_project()
    assert live.load_device("t1", "EQ Eight") == 0
    assert song.tracks[1].devices[0].class_name == "Eq8"


def test_reference_track_names():
    from mixmatch.taps import is_reference_name

    for name in ("REF Song", "Reference", "31-Ref", "[ref] Wish You Well", "my reference track"):
        assert is_reference_name(name), name
    for name in ("Prefix Pad", "Refrain Vox", "Reflections", "Kick"):
        assert not is_reference_name(name), name


def test_reference_clips_are_read_from_a_muted_reference_track(tmp_path):
    import numpy as np
    import soundfile as sf

    song, hub, env, live = demo_project()
    path = tmp_path / "wish.wav"
    sf.write(path, np.zeros((8000, 2), np.float32), 8000)

    class Clip:
        is_audio_clip, file_path = True, str(path)

    ref = next(tr for tr in song.tracks if tr.name.startswith("REF"))
    ref.mute, ref.arrangement_clips = True, [Clip()]
    clips = live.request("reference_clips")["clips"]
    assert [c["file"] for c in clips] == [str(path)] and clips[0]["track"] == ref.name


def test_reading_a_tap_while_frames_arrive():
    import threading
    import time as _time

    from mixmatch.taps import FRAME_LEN, TapHub

    hub = TapHub()
    hub.on_frame(1, *([0.1] * FRAME_LEN))
    stop = False

    def feed():
        while not stop:
            hub.on_frame(1, *([0.1] * FRAME_LEN))

    th = threading.Thread(target=feed)
    th.start()
    try:
        tap = hub.snapshot()[0]
        for _ in range(3000):
            tap.window(0, _time.time() + 10)      # used to raise «deque mutated during iteration»
    finally:
        stop = True
        th.join()


def test_taps_sleep_between_runs_and_can_be_removed():
    from mixmatch.cli import TAP, remove_taps, set_taps_on
    from mixmatch.sim import demo_project

    song, hub, env, live = demo_project()
    live.load_device("t0", TAP, source="user_library")
    live.load_device("t1", TAP, source="user_library")
    processing = lambda: [(t['key'], d) for t in live.get_set()['tracks'] for d in t['devices']
                          if d['name'] != TAP]
    before = processing()
    taps = lambda: [d for t in live.get_set()["tracks"] for d in t["devices"] if d["name"] == TAP]
    on = lambda: [next(p["value"] for p in d["params"] if p["name"] == "Device On") for d in taps()]
    assert set_taps_on(live, False) == 2 and on() == [0.0, 0.0]
    assert set_taps_on(live, False) == 0                     # already asleep: nothing to do
    assert set_taps_on(live, True) == 2 and on() == [1.0, 1.0]
    assert remove_taps(live, log=lambda *_: None) == 2 and not taps()
    assert processing() == before
    assert remove_taps(live, log=lambda *_: None) == 0


def test_a_run_started_with_sigint_ignored_still_stops_in_order(tmp_path):
    """Background shells start processes with SIGINT ignored; «Stop» must still end a run through its finally."""
    import signal
    import subprocess
    import sys
    import time

    script = tmp_path / "run.py"
    marker = tmp_path / "cleaned"
    script.write_text(
        "import time, sys\n"
        "import mixmatch.cli as cli\n"
        "def run(args):\n"
        "    try:\n"
        "        print('ready', flush=True)\n"
        "        time.sleep(30)\n"
        "    finally:\n"
        f"        open({str(marker)!r}, 'w').write('ok')\n"
        "cli.cmd_run = run\n"
        "cli.main(['run'])\n")
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
    assert proc.stdout.readline().strip() == "ready"
    proc.send_signal(signal.SIGINT)
    stdout, stderr = proc.communicate(timeout=10)
    assert marker.read_text() == "ok"
    assert proc.returncode == 130 and "Stopped:" in stdout
    assert "Traceback" not in stdout + stderr and "KeyboardInterrupt" not in stderr
