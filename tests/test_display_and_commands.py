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


def test_load_device_through_browser():
    song, hub, env, live = demo_project()
    assert live.load_device("t1", "EQ Eight") == 0
    assert song.tracks[1].devices[0].class_name == "Eq8"
