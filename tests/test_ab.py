from mixmatch import ab
from mixmatch.actions import Executor
from mixmatch.sim import demo_project


def test_before_and_after_switch_parameters_and_bypass_new_devices(tmp_path):
    song, hub, env, live = demo_project()
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    ab.save(ex.set_state, str(tmp_path), "1", "before")
    vol_before = song.tracks[0].mixer_device.volume.value
    ex.apply([{"type": "volume", "track": "t0", "delta_db": -2.0, "reason": "t"},
              {"type": "eq", "track": "t1", "freq_hz": 80, "gain_db": -3.0, "q": 1.0, "shape": "bell", "reason": "t"}])
    ex.refresh()
    ab.save(ex.set_state, str(tmp_path), "1", "after")
    vol_after = song.tracks[0].mixer_device.volume.value
    eq = song.tracks[1].devices[-1]

    assert ab.switch(live, "before", str(tmp_path)) >= 2
    assert abs(song.tracks[0].mixer_device.volume.value - vol_before) < 1e-6
    assert eq.p("Device On").value == 0                      # added by the run: bypassed for «before»

    ab.switch(live, "after", str(tmp_path))
    assert abs(song.tracks[0].mixer_device.volume.value - vol_after) < 1e-6
    assert eq.p("Device On").value == 1


def test_devices_are_matched_by_name_not_position():
    state = {"tracks": [{"key": "t0", "name": "Pad", "mixer": [],
                         "devices": [{"name": "EQ Eight", "params": [{"name": "1 Gain A", "value": -3.0}]}]}]}
    snap = ab.snapshot(state)
    moved = {"tracks": [{"key": "t0", "name": "Pad", "mixer": [],
                         "devices": [{"name": "Utility", "params": [{"name": "Device On", "value": 1.0}]},
                                     {"name": "EQ Eight", "params": [{"name": "1 Gain A", "value": 0.0}]}]}]}
    changes = ab.changes_to(snap, moved, bypass_new=True)
    assert {"track": "t0", "device": 1, "param": "1 Gain A", "value": -3.0} in changes
    assert {"track": "t0", "device": 0, "param": "Device On", "value": 0.0} in changes
