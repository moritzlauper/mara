import math

from MixMatch.commands import parse_display as live_parse
from mixmatch.display import parse_display
from mixmatch.sim import demo_project


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
    assert add_taps(live, log=lambda *_: None) == len(song.tracks) + 1
    assert song.tracks[0].devices[-1].name == "MixMatch Tap"   # after the EQ, not in front of it
    assert song.master_track.devices[-1].name == "MixMatch Tap"
    assert add_taps(live, log=lambda *_: None) == 0             # second run adds nothing


def test_load_device_through_browser():
    song, hub, env, live = demo_project()
    assert live.load_device("t1", "EQ Eight") == 0
    assert song.tracks[1].devices[0].class_name == "Eq8"
