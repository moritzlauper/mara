import math
from dataclasses import replace

import numpy as np
import pytest

from mixmatch.analysis import masking, masking_frames
from mixmatch.bands import BAND_COUNT, CENTRES
from mixmatch.taps import Frame


def frames(levels, frequency=63, offset=0.0):
    band = int(np.argmin(abs(np.array(CENTRES) - frequency)))
    out = []
    for i, level in enumerate(levels):
        rms = 10 ** (level / 20)
        bands = [0.0] * BAND_COUNT
        bands[band] = rms
        out.append(Frame(10 + i / 10 + offset, rms, 0, 0, rms * 2, bands))
    return out


def conflict(a, b, gain=0):
    return masking(masking_frames(a), masking_frames(b, gain))


def test_alternating_notes_are_not_masking():
    a = frames([-20 if i % 8 < 4 else -100 for i in range(80)])
    b = frames([-100 if i % 8 < 4 else -20 for i in range(80)], offset=0.025)
    assert conflict(a, b) is None


def test_simultaneous_notes_detect_regions_timing_and_dominant_maskers():
    a = frames([-20] * 40)
    equal = conflict(a, frames([-20] * 40, offset=0.03))
    loud = conflict(a, frames([-8] * 40))
    assert equal["overlap_fraction"] == 1 and equal["collision_frames"] == 40
    assert equal["freq_hz"] == round(CENTRES[np.argmin(abs(np.array(CENTRES) - 63))])
    assert loud["severity"] > equal["severity"] > 0.5
    assert conflict(a, frames([-20] * 40), gain=-6)["severity"] < equal["severity"]
    assert conflict(a, frames([-20] * 40), gain=-math.inf) is None


def test_existing_ducking_reduces_remaining_masking():
    pattern = [i % 10 < 3 for i in range(100)]
    kick = frames([-20 if on else -100 for on in pattern])
    flat = conflict(kick, frames([-20] * 100))
    ducked = conflict(kick, frames([-26 if on else -20 for on in pattern]))
    assert flat["intermittent"] and flat["overlap_fraction"] == 0.3
    assert flat["observed_ducking_db"] == 0
    assert ducked["observed_ducking_db"] == 6
    assert ducked["severity"] < flat["severity"] * 0.6
    assert conflict(kick, frames([-35 if on else -20 for on in pattern])) is None


def test_different_bands_silence_tails_and_single_accidents_are_not_conflicts():
    a = frames([-20] * 30)
    assert conflict(a, frames([-20] * 30, frequency=2000)) is None
    assert conflict(a, frames([-100] * 30)) is None
    assert conflict(a, frames([-20] + [-55] * 29)) is None
    assert conflict(frames([-80] * 30), frames([-80] * 30)) is None


def test_packet_gaps_duplicates_and_stale_windows_are_not_invented_overlap():
    a = frames([-20] * 40)
    assert conflict(a, a[::4]) is None
    assert conflict(a[::4], a[::4]) is None
    assert conflict(a, [replace(f, t=f.t + 10) for f in a]) is None
    assert conflict(a[:2] * 20, a[:2] * 20) is None
    assert conflict(a, [replace(f, bands=[float("nan")] * BAND_COUNT) for f in a]) is None
    assert conflict(a, a[::4] + [replace(f, m_rms=0, bands=[0] * BAND_COUNT)
                                for i, f in enumerate(a) if i % 4]) is not None


def setup_agent():
    from mixmatch.agent import Agent, Settings
    from mixmatch.planner import RulePlanner
    from mixmatch.sim import demo_project

    song, hub, env, live = demo_project()
    env.jitter = 0
    env.wait(2)
    agent = Agent(live, hub, RulePlanner(), Settings(fine=False, settle=0.2, min_gain=0.01),
                  env=env, log=lambda *_: None)
    agent.understand()
    return song, hub, env, live, agent


def test_all_pairs_count_and_channel_focus_has_its_own_conflicts():
    song, hub, env, live, agent = setup_agent()
    # All six tracks play the same band: 15 distinct pairs, including pairs beyond the old top four.
    for t in song.tracks[:6]:
        t.spectrum[:] = -140
        t.spectrum[6] = -20
    m = agent.measure()
    assert len(m["masking"]) == 15
    expected = sum(x["severity"] * min(x["priority_a"], x["priority_b"]) / 5 for x in m["masking"])
    assert m["parts"]["masking"] == pytest.approx(expected)
    ctx = agent.context("channel", m, "t5")
    assert all("t5" in (x["track_a"], x["track_b"]) for x in ctx["masking"][:5])
    action = agent.planner.plan(ctx)["actions"][0]
    assert action["type"] == "eq" and action["track"] == "t5"
    song.tracks[5].mute = True
    assert all("t5" not in (x["track_a"], x["track_b"]) for x in agent.measure()["masking"])


def test_rule_planner_ducks_intermittent_pairs_but_respects_existing_processing():
    from mixmatch.planner import masking_action

    pair = {"background": "t1", "foreground": "t0", "severity": 0.7, "intermittent": True, "freq_hz": 63}
    ctx = {"masking": [pair], "sidechain_ducking": True, "channels": [{"track": "t1", "devices": []}]}
    assert masking_action(ctx)["type"] == "duck"
    assert masking_action({**ctx, "sidechain_ducking": False})["type"] == "eq"
    assert masking_action({**ctx, "focus_track": "t0"}) is None
    assert masking_action({**ctx, "final_check": "master only"}) is None
    ctx["channels"][0]["devices"] = [{"name": "Compressor", "params": ["S/C On=On"], "enabled": True}]
    assert masking_action(ctx)["type"] == "eq"
    ctx["history"] = [{"undone": True, "actions": [{"type": "eq", "track": "t1", "freq_hz": 63}]}]
    assert masking_action(ctx) is None
    ctx["history"] = []
    ctx["channels"][0]["devices"] += [{"name": "EQ Eight", "enabled": True, "params": [],
                                       "eq_bands": [{"on": True, "kind": "Bell", "freq": 63,
                                                     "gain": -4, "q": 1.4}]}]
    assert masking_action(ctx) is None


def duck_action(**extra):
    return {"type": "duck", "track": "t1", "source_track": "t0", "source_level_dbfs": -20,
            "reduction_db": 2, "release_ms": 120, **extra}


def test_ducker_is_routed_bounded_and_removed_on_undo():
    from mixmatch.sim import track_output

    song, hub, env, live, agent = setup_agent()
    ex = agent.ex
    before = track_output(song.tracks[1])[0]
    rr = ex.apply([duck_action(reduction_db=20, release_ms=5),
                   {"type": "eq", "track": "t1", "freq_hz": 300, "gain_db": -1, "q": 1.4}])
    assert not rr.skipped
    d = song.tracks[1].devices[0]
    assert d.input_routing_type.target is song.tracks[0]
    assert d.input_routing_channel.display_name == "Post FX"
    assert d.p("Makeup").value == 0 and d.p("Release").value == pytest.approx(50, abs=0.1)
    assert 0 < d.p("Dry/Wet").value <= 1 - 10 ** (-3 / 20) + 0.001
    ex.undo(rr)
    assert all(not d.name.startswith("MARA Duck") for d in song.tracks[1].devices)
    assert track_output(song.tracks[1])[0] == pytest.approx(before)
    assert not ex.locks


@pytest.mark.parametrize("failure", ["missing_param", "routing", "post_fx", "duplicate_source", "off", "existing_sidechain"])
def test_failed_duck_leaves_existing_devices_and_parameters_unchanged(monkeypatch, failure):
    from mixmatch.sim import FACTORIES, compressor

    song, hub, env, live, agent = setup_agent()
    if failure in ("off", "existing_sidechain"):
        d = compressor()
        d.p("Device On" if failure == "off" else "S/C On").value = 0 if failure == "off" else 1
        song.tracks[1].devices.append(d)
    elif failure == "duplicate_source":
        song.tracks[2].name = song.tracks[0].name
    else:
        def broken():
            d = compressor()
            if failure == "missing_param":
                d.parameters = [p for p in d.parameters if p.name != "Dry/Wet"]
            elif failure == "routing":
                del d.available_input_routing_types
            elif failure == "post_fx":
                d.available_input_routing_channels = []
            return d
        monkeypatch.setitem(FACTORIES, "compressor", broken)
    agent.ex.refresh()
    before = live.get_set()
    rr = agent.ex.apply([duck_action()])
    assert rr.skipped and not rr.changes
    assert live.get_set() == before


def test_ducker_does_not_stack_or_touch_references_and_old_scripts_fail_safely():
    song, hub, env, live, agent = setup_agent()
    rr = agent.ex.apply([duck_action()])
    assert rr.changes
    assert agent.ex.apply([duck_action()]).skipped
    assert len(song.tracks[1].devices) == 1
    assert agent.ex.apply([{"type": "param", "track": "t1", "device_index": 0,
                            "param": "Dry/Wet", "target": 100}]).skipped
    assert agent.ex.apply([duck_action(track="t6")]).skipped
    agent.ex.set_state.pop("capabilities")
    assert agent.ex.apply([duck_action(track="t2")]).skipped


def test_partial_configuration_failure_rolls_back_and_before_after_bypasses_ducker(monkeypatch):
    from mixmatch import ab

    song, hub, env, live, agent = setup_agent()
    before = live.get_set()
    search = live.cmd._search
    def fail(p, target):
        if p.name == "Output":
            raise RuntimeError("failed after routing and threshold changes")
        return search(p, target)
    monkeypatch.setattr(live.cmd, "_search", fail)
    assert agent.ex.apply([duck_action()]).skipped
    assert live.get_set() == before
    monkeypatch.setattr(live.cmd, "_search", search)
    assert not agent.ex.apply([duck_action()]).skipped
    after = live.get_set()
    live.batch(ab.changes_to(ab.snapshot(before), after, bypass_new=True))
    assert song.tracks[1].devices[0].p("Device On").value == 0
    live.batch(ab.changes_to(ab.snapshot(after), live.get_set(), bypass_new=False))
    assert song.tracks[1].devices[0].p("Device On").value == 1


def test_lost_tap_data_cannot_make_a_round_look_better(monkeypatch):
    song, hub, env, live, agent = setup_agent()
    original = live.get_set()
    measure = agent.measure
    count = 0
    def dropout(scoring="full"):
        nonlocal count
        count += 1
        m = measure(scoring)
        if count > 1:
            return {**m, "objective": 0, "masking": [], "masking_coverage": {}}
        return m
    monkeypatch.setattr(agent, "measure", dropout)
    agent.section("missing observations", env.now() + 200, max_rounds=1)
    assert agent.history[0].undone
    # Existing mix parameters are restored, even though neutral added EQs can remain.
    from mixmatch.ab import changes_to, snapshot
    assert not changes_to(snapshot(original), live.get_set(), bypass_new=False)


def test_dynamic_ducking_only_changes_background_during_trigger_activity():
    from mixmatch.sim import track_output

    song, hub, env, live, agent = setup_agent()
    bass = song.tracks[1]
    before = track_output(bass)[0]
    rr = agent.ex.apply([duck_action()])
    assert not rr.skipped
    after = track_output(bass)[0]
    assert np.all(after < before) and np.max(before - after) <= 2.01
    song.tracks[0].signal_db = -100
    assert track_output(bass)[0] == pytest.approx(before)


def test_channel_loop_keeps_verified_duck_and_rejects_invented_sources(monkeypatch):
    song, hub, env, live, agent = setup_agent()
    env.jitter = 0.0  # verify the duck itself independently of random simulated spectrum noise
    # Equal narrow-band sources; a repeating kick against sustained bass.
    for t in song.tracks[:6]:
        t.spectrum[:] = -140
    song.tracks[0].spectrum[6] = song.tracks[1].spectrum[6] = -20
    wait = env.wait
    def pulsed_wait(seconds):
        for _ in range(max(1, round(seconds * 10))):
            song.tracks[0].signal_db = 0 if round(env.now() * 10) % 10 < 3 else -100
            wait(0.1)
    monkeypatch.setattr(env, "wait", pulsed_wait)
    agent.section("channel Bass", env.now() + 200, focus="t1", max_rounds=1, scoring="local")
    assert agent.history and agent.history[0].applied[0]["type"] == "duck"
    assert not agent.history[0].undone
    assert agent.history[0].after < agent.history[0].before
    class Invented:
        def plan(self, ctx):
            return {"actions": [duck_action(track="t2", source_track="t3")]}
    agent.planner = Invented()
    agent.section("invented", env.now() + 200, max_rounds=1, scoring="local")
    assert not song.tracks[2].devices
