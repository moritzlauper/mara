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
