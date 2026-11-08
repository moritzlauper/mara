from mixmatch.actions import Executor
from mixmatch.sim import demo_project


def make():
    song, hub, env, live = demo_project()
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    return song, ex


def test_eq_adds_band_then_adjusts_the_same_band():
    song, ex = make()
    ex.apply([{"type": "eq", "track": "t1", "freq_hz": 250, "gain_db": -2, "q": 1.0, "shape": "bell"}])
    ex.apply([{"type": "eq", "track": "t1", "freq_hz": 280, "gain_db": -2, "q": 1.0, "shape": "bell"}])
    eq = song.tracks[1].devices[0]
    assert eq.p("1 Filter On A").value == 1 and eq.p("2 Filter On A").value == 0
    assert abs(eq.p("1 Gain A").value - -4.0) < 0.06


def test_steps_are_clamped():
    song, ex = make()
    ex.apply([{"type": "volume", "track": "t0", "delta_db": -10}])
    assert ex._param("t0", "mixer", "Volume")["display"] == "-2.0 dB"


def test_undo_restores_everything_without_locking_itself():
    song, ex = make()
    before = song.tracks[1].mixer_device.volume.value
    rr = ex.apply([{"type": "volume", "track": "t1", "delta_db": -1},
                   {"type": "volume", "track": "t1", "delta_db": -1},
                   {"type": "eq", "track": "t1", "freq_hz": 70, "gain_db": -2, "q": 1, "shape": "bell"}])
    ex.undo(rr)
    ex.refresh()
    assert song.tracks[1].mixer_device.volume.value == before
    assert song.tracks[1].devices[0].p("1 Filter On A").value == 0
    assert not ex.locks


def test_a_hand_on_a_fader_locks_it():
    song, ex = make()
    ex.apply([{"type": "volume", "track": "t2", "delta_db": -1}])
    song.tracks[2].mixer_device.volume.value = 0.5     # the user moves the fader
    ex.refresh()
    rr = ex.apply([{"type": "volume", "track": "t2", "delta_db": -1}])
    assert ("t2", "mixer", "Volume") in ex.locks and rr.skipped and not rr.changes


def test_limiter_and_bass_mono_on_master():
    song, ex = make()
    ex.apply([{"type": "limiter", "delta_db": 1.5}, {"type": "width", "track": "m", "bass_mono": True}])
    devs = {d.class_name: d for d in song.master_track.devices}
    assert abs(devs["Limiter"].p("Gain").value - 1.5) < 0.06 and abs(devs["Limiter"].p("Ceiling").value + 1) < 0.06
    assert devs["StereoGain"].p("Bass Mono").value == 1
