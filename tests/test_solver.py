"""The calculated flow: predictions, limits, collision simulation, and a whole run in the simulator."""
import json
import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from mixmatch import solver
from mixmatch.agent import Agent, PlannerFailed, Settings
from mixmatch.analysis import K_WEIGHT, Stats, masking, masking_frames
from mixmatch.bands import BAND_COUNT, CENTRES, bands_between
from mixmatch.planner import RulePlanner
from mixmatch.sim import bump, demo_project, pink
from mixmatch.taps import Frame

F = np.array(CENTRES)


def stats_of(bands_db, plr=10.0):
    loud = -0.691 + 10 * math.log10(2 * float((10 ** (np.asarray(bands_db) / 10) * K_WEIGHT).sum()))
    return Stats(100, loud, np.asarray(bands_db, float), np.asarray(bands_db, float) - loud, loud + plr, plr,
                 -12.0, -30.0, 1.0)


def power_sum(*levels):
    return 10 * np.log10(sum(10 ** (np.asarray(l) / 10) for l in levels))


def target_for(shape_bands):
    ref = stats_of(shape_bands)
    return SimpleNamespace(stats=ref, tolerance=np.full(BAND_COUNT, 0.5), offset=None, anchor=[], one_sided=[])


def source(key, role, priority, bands, **kw):
    return solver.Source(key, key.upper(), role, priority, np.asarray(bands, float), **kw)


def muddy_mix():
    lead = pink(-30)
    pad = pink(-34) + bump(400, 0.5, 9)
    return {"lead": source("lead", "lead_synth", 5, lead), "pad": source("pad", "pad", 2, pad)}


def test_the_excess_is_cut_on_the_source_that_causes_it():
    sources = muddy_mix()
    master = stats_of(power_sum(*(s.bands_db for s in sources.values())))
    plan = solver.solve(master, sources, target_for(pink(-20)))
    assert plan.cost_after < plan.cost_before * 0.6
    cuts = [m for m in plan.moves if m.kind == "bell" and m.gain < 0]
    on_pad = -sum(m.gain for m in cuts if m.key == "pad")
    on_lead = -sum(m.gain for m in cuts if m.key == "lead")
    assert on_pad >= 3 * on_lead and on_lead <= 2.0      # the background gives way, the lead keeps its core
    assert any(200 <= CENTRES[m.idx] <= 800 for m in cuts if m.key == "pad")
    assert "carries" in cuts[0].reason and cuts[0].action()["type"] == "eq"


def test_protection_vocal_range_layers_and_master_level_are_respected():
    sources = muddy_mix()
    master = stats_of(power_sum(*(s.bands_db for s in sources.values())))
    target = target_for(pink(-20))
    frozen = solver.solve(master, sources, target, solver.Constraints(frozen={"pad"}))
    assert not any(m.key == "pad" for m in frozen.moves) and frozen.unchanged["pad"] == "protected"
    # A dark reference asks for more low mids: beat mode never boosts into 200 Hz–5 kHz.
    dark = target_for(pink(-20) + bump(1000, 1.0, 6))
    beat = solver.solve(master, sources, dark, beat=True)
    vocal = set(bands_between(200, 5000))
    assert not any(m.kind == "bell" and m.gain > 0 and m.idx in vocal for m in beat.moves)
    # Little room left for the pad's level: its cuts stay small, and the master never gets louder.
    tight = {**sources, "pad": replace(sources["pad"], level_room=0.5)}
    plan = solver.solve(master, tight, target)
    gain = sum(solver.bell(m.idx, m.gain, m.q) for m in plan.moves if m.key == "pad" and m.kind == "bell")
    gain = gain + sum(m.gain for m in plan.moves if m.key == "pad" and m.kind == "fader")
    weighted = 10 ** (tight["pad"].bands_db / 10) * K_WEIGHT
    own = 10 * np.log10((weighted * 10 ** (np.asarray(gain) / 10)).sum() / weighted.sum())
    assert own > -1.5


def test_a_cap_or_ban_from_the_review_changes_the_recalculation():
    sources = muddy_mix()
    master = stats_of(power_sum(*(s.bands_db for s in sources.values())))
    target = target_for(pink(-20))
    first = solver.solve(master, sources, target)
    top = first.moves[0]
    key = (top.key, top.kind, top.idx if top.kind != "fader" else None)
    banned = solver.solve(master, sources, target, solver.Constraints(banned={key}))
    assert not any((m.key, m.kind, m.idx) == (top.key, top.kind, top.idx) for m in banned.moves)
    capped = solver.solve(master, sources, target, solver.Constraints(caps={key: 0.5}))
    same = [m for m in capped.moves if (m.key, m.kind, m.idx) == (top.key, top.kind, top.idx)]
    assert all(abs(m.gain) <= 0.5 + 1e-9 for m in same)


def _frames(levels, frequency, offset=0.0):
    band = int(np.argmin(abs(F - frequency)))
    out = []
    for i, level in enumerate(levels):
        rms = 10 ** (level / 20)
        bands = [0.0] * BAND_COUNT
        bands[band] = rms
        out.append(Frame(10 + i / 10 + offset, rms, 0, 0, rms * 2, bands))
    return out


def collision(kick_levels, bass_levels):
    fg, bg = masking_frames(_frames(kick_levels, 63)), masking_frames(_frames(bass_levels, 63, 0.02))
    hit = masking(fg, bg)
    pair = {**hit, "track_a": "kick", "track_b": "bass", "role_a": "kick", "role_b": "bass", "foreground": "kick",
            "background": "bass", "priority_a": 5, "priority_b": 4, "source_level_dbfs": -20.0}
    m = {"masking": [pair], "mask_frames": {"kick": fg, "bass": bg},
         "pair_severity": {"bass:kick": hit["severity"]}, "pair_noise": {"bass:kick": 0.01}}
    bass = np.full(BAND_COUNT, -60.0)
    bass[int(np.argmin(abs(F - 63)))] = -20
    return m, {"bass": source("bass", "bass", 4, bass, duck_free=True), "kick": source("kick", "kick", 5, bass)}


def test_collision_fixes_are_simulated_on_the_recorded_frames():
    m, sources = collision([-20] * 60, [-20] * 60)
    fixes, notes = solver.collision_fixes(m, sources)
    assert len(fixes) == 1 and fixes[0].background == "bass"
    fix = fixes[0]
    assert fix.predicted < fix.before and fix.moves[0].kind == "bell" and fix.moves[0].gain < 0
    assert abs(CENTRES[fix.moves[0].idx] - 63) < 10
    # Intermittent kick: ducking only while it plays relieves more than a static cut.
    pattern = [i % 10 < 3 for i in range(100)]
    m, sources = collision([-20 if on else -100 for on in pattern], [-20] * 100)
    fixes, _ = solver.collision_fixes(m, sources, ducking=True)
    assert fixes and fixes[0].moves[0].kind == "duck" and fixes[0].moves[0].source_track == "kick"


def test_a_layer_of_the_same_role_is_left_alone():
    m, sources = collision([-20] * 60, [-20] * 60)
    m["masking"][0].update(role_a="lead_synth", role_b="lead_synth")
    fixes, notes = solver.collision_fixes(m, sources)
    assert not fixes and "intentional layer" in notes[0]


def test_limiter_gain_is_no_lost_layer_and_loudness_stops_at_the_references_density():
    from mixmatch.analysis import Comparison
    from mixmatch.mix_quality import anchor, evaluate

    st = Stats(80, -12.0, np.full(31, -30.0), np.full(31, -18.0), -2.0, 10.0, -10.0, -30.0, 1.0)
    ref = replace(st, plr=8.0)

    def measured(master, loudness_cost):
        parts = {"masking": 1.0, "tonal": 1.0, "clipping": 0.0, "low_side": 0.0, "width": 0.0,
                 "dynamics": 0.0, "loudness": loudness_cost}
        return {"parts": parts, "objective": sum(parts.values()), "master": master, "ref": ref,
                "cmp": Comparison(-3.0, np.zeros(31), np.zeros(31), 0.0, 0.0, 0.0, -2.0), "masking": [],
                "tracks": {"t0": replace(st, loudness=-18), "t1": replace(st, loudness=-20),
                           "t2": replace(st, loudness=-22)}}

    before = measured(st, 2.5)
    louder = measured(replace(st, loudness=-10.0, plr=8.0), 0.0)     # +2 LU, exactly as dense as the refs
    assert evaluate(before, louder, anchor(before), "loudness", .05)[0]
    denser = measured(replace(st, loudness=-9.5, plr=7.5), 0.0)      # 0.5 dB denser than the references
    ok, why = evaluate(before, denser, anchor(before), "loudness", .05)
    assert not ok and "denser" in why


def test_pair_severity_is_averaged_over_the_repeated_windows():
    from mixmatch.mix_quality import combine

    base = {"parts": {"masking": 1.0}, "objective": 1.0, "master": None, "tracks": {}, "cmp": None}
    pair = {"track_a": "t1", "track_b": "t0", "severity": 0.6}
    samples = [{**base, "masking": [pair]}, {**base, "masking": [{**pair, "severity": 0.4}]}, {**base, "masking": []}]
    m = combine(samples)
    assert m["pair_severity"]["t0:t1"] == pytest.approx(1.0 / 3)
    assert m["pair_noise"]["t0:t1"] == pytest.approx(0.6)


# ── a whole run in the simulator ───────────────────────────────────────────
def complete_run(tmp_path, planner=None, **kw):
    from mixmatch import workflow

    song, hub, env, live = demo_project()
    env.wait(5)
    logs = []
    agent = Agent(live, hub, planner or RulePlanner(),
                  Settings(minutes=None, complete=True, settle=1.0, roles_file=str(tmp_path / "r.json"),
                           log_dir=str(tmp_path), **kw), env=env, log=logs.append)
    start = env.now()
    agent.run()
    return agent, logs, (env.now() - start) / 60, song


def test_a_complete_calculated_run_improves_the_mix_quickly_and_explains_every_channel(tmp_path, monkeypatch):
    from mixmatch import workflow

    updates = []
    monkeypatch.setattr(workflow, "update", lambda **values: updates.append(values))
    agent, logs, minutes, song = complete_run(tmp_path)
    text = "\n".join(logs)
    for phase in ("━━ Master against the reference", "━━ Channel pass", "━━ Final check", "Channel decisions:"):
        assert phase in text
    assert all(f"= {r.name}:" in text for r in agent.roles.values())             # every channel accounted for
    kept = [h for h in agent.history if not h.undone and h.after is not None]
    assert kept and kept[-1].after < agent.history[0].before * 0.5
    assert any(h.section == "loudness" and not h.undone for h in agent.history)   # the limiter was raised
    report = next(u["quality_check"] for u in updates if "quality_check" in u)
    assert report["benefit"] in ("improved", "mixed") and report["samples"] >= 2
    assert minutes < 10 and agent._mix_complete
    assert not (tmp_path / "checkpoint.json").exists()


class Reviewer:
    """Protects the pad and drops the strongest move; free rounds add nothing."""

    def __init__(self):
        self.reviews, self.plans = [], 0

    def review(self, ctx):
        self.reviews.append(ctx)
        pad = next(c["track"] for c in ctx["channels"] if c["name"] == "Pad")
        top = ctx["moves"][0]
        return {"assessment": "keep the pad as it is", "protect": [{"track": pad, "reason": "user wants it warm"}],
                "edits": [{"id": top["id"], "decision": "drop", "factor": None, "reason": "too much"}]}

    def plan(self, ctx):
        self.plans += 1
        return {"assessment": "fine", "done": True, "actions": []}


def test_the_review_protects_channels_and_the_free_rounds_follow(tmp_path, monkeypatch):
    reviewer = Reviewer()
    agent, logs, minutes, song = complete_run(tmp_path, planner=reviewer)
    assert len(reviewer.reviews) == 1 and reviewer.plans >= 1
    ctx = reviewer.reviews[0]
    assert {"moves", "channels", "spectrum", "user_description"} <= set(ctx)
    pad = next(k for k, r in agent.roles.items() if r.name == "Pad")
    assert not any(a.get("track") == pad for h in agent.history if not h.undone for a in h.applied
                   if h.section != "collision")
    assert any("protected: Pad" in l for l in logs) and any("dropped:" in l for l in logs)


def test_measurements_jump_to_the_loop_start(tmp_path):
    song, hub, env, live = demo_project()
    agent = Agent(live, hub, RulePlanner(), Settings(settle=1.0), env=env, log=lambda *_: None)
    agent.ex.refresh()
    agent.play_loop(16.0, 32.0)
    env.wait(5.0)
    before = env.now()
    agent.cue_loop_start()
    assert env.now() - before < 1.0 and abs(song.current_song_time - 16.0) < 1.5


def test_a_stopped_calculated_run_continues_without_the_master_phase(tmp_path, monkeypatch):
    calls = {"n": 0}
    original = Agent.channel_collisions

    def dies_once(self, *args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PlannerFailed("connection lost for good")
        return original(self, *args)

    monkeypatch.setattr(Agent, "channel_collisions", dies_once)
    song, hub, env, live = demo_project()
    env.wait(5)
    settings = lambda **kw: Settings(minutes=None, complete=True, settle=1.0, roles_file=str(tmp_path / "r.json"),
                                     log_dir=str(tmp_path), **kw)
    first = []
    Agent(live, hub, RulePlanner(), settings(), env=env, log=first.append).run()
    ck = json.load(open(tmp_path / "checkpoint.json"))
    assert ck["phase"] == "channels" and ck["engine"] == "solver"
    second = []
    Agent(live, hub, RulePlanner(), settings(resume="auto"), env=env, log=second.append).run()
    text = "\n".join(second)
    assert "Master against the reference" not in text and "━━ Channel pass" in text and "━━ Final check" in text
    assert not (tmp_path / "checkpoint.json").exists()


# ── level base, calibration, few references ───────────────────────────────
def test_calibration_learns_how_much_of_a_predicted_change_reaches_the_master():
    plan = solver.Plan([], 1.0, 0.5, diff_before=np.zeros(BAND_COUNT), diff_after=np.zeros(BAND_COUNT))
    plan.diff_after[14:17] = -2.0                         # predicted: 2 dB less around 500 Hz
    measured = np.zeros(BAND_COUNT)
    measured[14:17] = -1.0                                # measured: half of it
    k, share = solver.calibrate(None, plan, np.zeros(BAND_COUNT), measured)
    assert share == pytest.approx(0.5)
    assert np.all(k[14:17] < 0.8) and np.all(k[:14] == 1.0)
    # The model with that sensitivity expects less from the same cut.
    mix = muddy_mix()
    master = stats_of(power_sum(*(s.bands_db for s in mix.values())))
    flat = target_for(pink(-30))
    full = solver.solve(master, mix, flat)
    damped = solver.solve(master, mix, flat, sensitivity=np.where(np.arange(BAND_COUNT) >= 0, 0.5, 1.0))
    assert damped.benefit < full.benefit


def test_a_smaller_trust_region_makes_smaller_moves():
    mix = muddy_mix()
    master = stats_of(power_sum(*(s.bands_db for s in mix.values())))
    plan = solver.solve(master, mix, target_for(pink(-30)), trust=0.5)
    assert plan.moves and all(abs(m.gain) <= 1.5 + 1e-6 for m in plan.moves if m.kind == "bell")
    assert all(-1.5 - 1e-6 <= m.gain <= 1.0 + 1e-6 for m in plan.moves if m.kind == "fader")


def test_the_low_end_counts_as_one_sum_when_it_is_lumped():
    from mixmatch.analysis import LOW_LUMP, shape_difference

    ref = np.full(BAND_COUNT, -20.0)
    mix = ref.copy()
    mix[LOW_LUMP[3]] = -60.0                               # the bass note sits in another band than the ref's
    mix[LOW_LUMP[1]] = 10 * np.log10(2 * 10 ** (-2.0) - 10 ** (-6.0))   # same power sum overall
    assert np.abs(shape_difference(mix, ref, lump=True)[LOW_LUMP]).max() < 0.01
    assert np.abs(shape_difference(mix, ref)[LOW_LUMP]).max() > 2.9


def test_few_references_get_a_tolerance_floor_but_keep_the_vocal_window():
    from mixmatch.analysis import Stats
    from mixmatch.reference import VOCAL_RANGE, Reference, ReferenceSet, tolerance_floor

    def ref(seed):
        bands = np.full(31, -22.0) + np.random.default_rng(seed).normal(0, 0.2, 31)
        return Reference(f"{seed}.wav", Stats(10, -7.5, bands, bands + 7.5, 0.0, 7.5, -10.0, -25.0, 1.0))

    def target(n, beat):
        rs = ReferenceSet.__new__(ReferenceSet)
        rs.log, rs.refs = (lambda *_: None), [ref(i) for i in range(n)]
        return rs.target(beat=beat)

    one = target(1, beat=False)
    assert one.lump_low and np.all(one.tolerance >= tolerance_floor() - 1e-9)
    beat = target(1, beat=True)
    assert np.allclose(beat.tolerance[VOCAL_RANGE], 0.5 + 2.0)      # 0–4 dB under the song, as before
    many = target(3, beat=False)
    assert not many.lump_low and many.tolerance.max() < 1.0


def test_blind_votes_are_counted_against_guessing(tmp_path):
    from mixmatch import ab

    for hidden, verdict in [("after", "better"), ("before", "worse"), ("after", "better"), ("before", "better")]:
        ab.record_vote(str(tmp_path), hidden, verdict, "p1")
    summary = ab.vote_summary(str(tmp_path))
    assert summary["after_preferred"] == 3 and summary["total"] == 4
    assert summary["p_value"] == pytest.approx(5 / 16)
    with pytest.raises(ValueError):
        ab.record_vote(str(tmp_path), "after", "maybe")


def main_with_tap(limiter=False):
    from mixmatch import sim

    song, hub, env, live = demo_project()
    song.master_track.devices.append(sim.tap())
    if limiter:
        song.master_track.devices.insert(0, sim.limiter())
    return song, hub, env, live


def test_the_input_stage_sits_in_front_of_the_loudness_chain_and_new_devices_find_their_place():
    song, hub, env, live = main_with_tap(limiter=True)
    agent = Agent(live, hub, RulePlanner(), Settings(settle=1.0), env=env, log=lambda *_: None)
    assert agent.prepare_input_stage()
    names = lambda: [d.name for d in song.master_track.devices]
    assert names() == ["MARA Trim", "MixMatch Tap", "Limiter", "MixMatch Tap"]
    assert hub.latest("master_in") is not None and hub.latest("master") is not None
    agent.ex.apply([{"type": "eq", "track": "m", "freq_hz": 300, "gain_db": -1.0, "q": 1.0, "shape": "bell"}])
    assert names() == ["EQ Eight", "MARA Trim", "MixMatch Tap", "Limiter", "MixMatch Tap"]
    assert not agent.ex.ensure_input_stage()                     # nothing added twice


def test_without_input_measurement_the_input_stage_comes_off_again(monkeypatch):
    song, hub, env, live = main_with_tap()
    monkeypatch.setattr(hub, "latest", lambda kind: None)        # a tap script that cannot tell its taps apart
    logs = []
    agent = Agent(live, hub, RulePlanner(), Settings(settle=1.0), env=env, log=logs.append)
    assert not agent.prepare_input_stage()
    assert [d.name for d in song.master_track.devices] == ["MixMatch Tap"]
    assert any("Set up the connection" in l for l in logs)


def test_a_complete_run_sets_the_level_base_and_reports_the_loudness_chain(tmp_path, monkeypatch):
    from mixmatch import workflow

    updates = []
    monkeypatch.setattr(workflow, "update", lambda **values: updates.append(values))
    song, hub, env, live = main_with_tap()
    env.wait(5)
    logs = []
    agent = Agent(live, hub, RulePlanner(),
                  Settings(minutes=None, complete=True, settle=1.0, roles_file=str(tmp_path / "r.json"),
                           log_dir=str(tmp_path)), env=env, log=logs.append)
    start = env.now()
    agent.run()
    text = "\n".join(logs)
    assert "Level base: trim" in text and "model check:" in text
    assert any(h.section == "level base" for h in agent.history)
    report = next(u["quality_check"] for u in updates if "quality_check" in u)
    chain = report["loudness_chain"]
    assert abs(chain["input_peak_dbfs"] + 1.0) < 1.0 and chain["peak_reduction_db"] <= 9.5
    assert any(c["name"] == "Loudness chain" for c in report["checks"])
    assert (env.now() - start) / 60 < 10 and agent._mix_complete


def test_meters_on_main_move_behind_the_loudness_chain():
    from mixmatch import sim

    song, hub, env, live = demo_project()
    meter = lambda n: sim.Device(n, "PluginDevice", [sim.Param("Device On", 0, 1, 1, str, True, ["Off", "On"])])
    song.master_track.devices[:] = [meter("Youlean Loudness Meter 2"), meter("SPAN"), sim.tap()]
    agent = Agent(live, hub, RulePlanner(), Settings(settle=1.0), env=env, log=lambda *_: None)
    assert agent.prepare_input_stage()
    agent.ex.apply([{"type": "limiter", "delta_db": 1.0, "track": None}])
    assert [d.name for d in song.master_track.devices] == [
        "MARA Trim", "MixMatch Tap", "Limiter", "Youlean Loudness Meter 2", "SPAN", "MixMatch Tap"]


def test_time_displays_in_seconds_are_read_as_milliseconds():
    import sys
    sys.path.insert(0, "remote_script")
    from MixMatch.commands import _number

    assert _number("1.00 s") == 1000.0 and _number("2.00 ms") == 2.0 and _number("-1.5 dB") == -1.5


def test_undo_reaches_the_limiter_after_the_clipper_moved_in_front_of_it(monkeypatch):
    """A clipper inserted before the limiter in the same package shifted the limiter's index; the undo then wrote
    the limiter's old gain into the clipper."""
    from mixmatch import sim
    from mixmatch.actions import Executor

    song, hub, env, live = main_with_tap(limiter=True)
    clip = sim.Device("Classic Clipper", "PluginDevice", [sim.Param("Device On", 0, 1, 1, str, True, ["Off", "On"]),
                                                           sim.Param("Gain", -15, 15, 0, lambda v: f"{v:.1f} dB")])
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    monkeypatch.setattr(Executor, "clipper", lambda self: ("Classic Clipper", "Gain"))

    def load(track, name, source="audio_effects"):
        song.master_track.devices.append(clip)
        return len(song.master_track.devices) - 1
    monkeypatch.setattr(live, "load_device", load)
    rr = ex.apply([{"type": "limiter", "delta_db": 2.0, "track": None}, {"type": "clipper", "delta_db": 1.0, "track": None}])
    names = [d.name for d in song.master_track.devices]
    assert names.index("Classic Clipper") < names.index("Limiter")
    ex.undo(rr)
    limiter = next(d for d in song.master_track.devices if d.name == "Limiter")
    assert limiter.p("Gain").value == 0 and clip.p("Gain").value == 0   # each back where it was
