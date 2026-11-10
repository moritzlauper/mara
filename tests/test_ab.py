from mixmatch import ab
from mixmatch.actions import Executor
from mixmatch.sim import demo_project
import pytest


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


def test_blind_choice_is_independent_can_repeat_and_requires_matching_snapshots(tmp_path, monkeypatch):
    song, hub, env, live = demo_project()
    ab.save(live.get_set(), str(tmp_path), "1", "before")
    trial = ab.BlindComparison()
    assert not trial.ready(str(tmp_path))
    with pytest.raises(RuntimeError):
        trial.choose(live, str(tmp_path))
    live.set("t0", "mixer", "Volume", target=-2)
    after = song.tracks[0].mixer_device.volume.value
    ab.save(live.get_set(), str(tmp_path), "1", "after")
    choices = iter(["after", "after", "before"])
    monkeypatch.setattr(ab.secrets, "choice", lambda options: next(choices))
    for expected in ["after", "after", "before"]:
        assert trial.choose(live, str(tmp_path)) is None
        assert trial.reveal(str(tmp_path)) == expected
        assert trial.reveal(str(tmp_path / "another-song")) is None
        assert (song.tracks[0].mixer_device.volume.value == after) == (expected == "after")
    # A new incomplete run must not mix its "before" with the previous run's "after".
    ab.save(live.get_set(), str(tmp_path), "2", "before")
    assert not trial.ready(str(tmp_path))
    with pytest.raises(RuntimeError):
        trial.choose(live, str(tmp_path))
    assert trial.reveal(str(tmp_path)) is None


def test_failed_switch_does_not_reveal_a_trial(tmp_path, monkeypatch):
    song, hub, env, live = demo_project()
    ab.save(live.get_set(), str(tmp_path), "1", "before")
    live.set("t0", "mixer", "Volume", target=-2)
    ab.save(live.get_set(), str(tmp_path), "1", "after")
    monkeypatch.setattr(ab.secrets, "choice", lambda options: "before")
    monkeypatch.setattr(live, "batch", lambda changes: {"errors": ["unavailable parameter"]})
    trial = ab.BlindComparison()
    with pytest.raises(RuntimeError):
        trial.choose(live, str(tmp_path))
    assert trial.reveal(str(tmp_path)) is None


def test_snapshots_preserve_automation_including_legacy_snapshots():
    import copy
    state = {'tracks': [{'key': 't0', 'name': 'Pad',
        'mixer': [{'name': 'Volume', 'value': .5, 'automated': True}],
        'devices': [{'name': 'EQ', 'params': [
            {'name': 'Frequency', 'value': .2, 'automated': True},
            {'name': 'Gain', 'value': .4, 'automated': False}]}]}]}
    snapshot = ab.snapshot(state)
    now = copy.deepcopy(state)
    now['tracks'][0]['mixer'][0]['value'] = .8
    for p in now['tracks'][0]['devices'][0]['params']:
        p['value'] = .8
    expected = [{'track': 't0', 'device': 0, 'param': 'Gain', 'value': .4}]
    assert ab.changes_to(snapshot, now, True) == expected
    legacy = copy.deepcopy(snapshot)
    legacy.pop('automation')
    assert ab.changes_to(legacy, now, True) == expected
    # Snapshot metadata also protects a lane when the bridge temporarily reports it inactive.
    now['tracks'][0]['mixer'][0]['automated'] = False
    now['tracks'][0]['devices'][0]['params'][0]['automated'] = False
    assert ab.changes_to(snapshot, now, True) == expected
