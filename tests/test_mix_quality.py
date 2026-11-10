import copy
from dataclasses import replace

import numpy as np
import pytest

from mixmatch.analysis import Comparison, Stats
from mixmatch.mix_quality import anchor, diagnosis, evaluate


def measurement(masking=2.0, tonal=1.0, **costs):
    st = Stats(80, -12.0, np.full(31, -30.0), np.full(31, -18.0), -2.0, 10.0, -10.0, -30.0, 1.0)
    parts = {"masking": masking, "tonal": tonal, "clipping": 0.0, "low_side": 0.0,
             "width": 0.0, "dynamics": 0.0, "loudness": 0.0, **costs}
    cmp = Comparison(-1.0, np.zeros(31), np.zeros(31), 0.0, 0.0, 0.0, -2.0)
    return {"parts": parts, "objective": sum(parts.values()), "master": st, "ref": st, "cmp": cmp,
            "tracks": {"t0": replace(st, loudness=-18), "t1": replace(st, loudness=-20)}, "masking": []}


def test_less_masking_can_outweigh_a_small_reference_setback():
    before, after = measurement(), measurement(masking=1.8, tonal=1.4)
    assert after["objective"] > before["objective"]
    assert evaluate(before, after, anchor(before), "masking", .05)[0]


def test_claimed_benefit_must_actually_happen():
    before, after = measurement(), measurement(tonal=0.1)
    assert not evaluate(before, after, anchor(before), "masking", .05)[0]
    assert not evaluate(before, before, anchor(before), "tonal", .05)[0]


def test_preserving_a_deliberate_difference_does_not_require_zero_distance():
    before, after = measurement(tonal=4), measurement(masking=1.5, tonal=4)
    assert evaluate(before, after, anchor(before), "masking", .05)[0]


def test_reference_matching_cannot_buy_more_masking():
    before, after = measurement(), measurement(masking=2.5, tonal=0)
    assert after["objective"] < before["objective"]
    assert not evaluate(before, after, anchor(before), "tonal", .05)[0]


@pytest.mark.parametrize("change", [
    {"peak_db": 0.2}, {"low_side_db": -15}, {"plr": 8.0, "loudness": -10.0}, {"plr": 6.0},
])
def test_better_distance_cannot_buy_clipping_stereo_bass_or_flattening(change):
    before, after = measurement(), measurement(tonal=0)
    after["master"] = replace(after["master"], **change)
    assert not evaluate(before, after, anchor(before), "tonal", .05)[0]


def test_peak_reduction_without_loudness_push_is_not_automatically_lost_punch():
    before, after = measurement(), measurement(masking=1)
    after["master"] = replace(after["master"], plr=8, peak_db=-4, loudness=-12)
    assert evaluate(before, after, anchor(before), "masking", .05)[0]


def test_small_tradeoffs_cannot_accumulate_without_bound():
    start = measurement()
    first, second = measurement(masking=1.8, tonal=1.4), measurement(masking=1.6, tonal=1.8)
    assert evaluate(start, first, anchor(start), "masking", .05)[0]
    assert not evaluate(first, second, anchor(start), "masking", .05)[0]


def test_single_hollow_region_cannot_hide_in_average_distance():
    before, after = measurement(), measurement(masking=1)
    after["cmp"].tonal_excess[15] = -3.0
    assert not evaluate(before, after, anchor(before), "masking", .05)[0]


def test_masking_cannot_be_solved_by_erasing_a_layer():
    before, after = measurement(), measurement(masking=0)
    after["tracks"]["t1"] = None
    assert not evaluate(before, after, anchor(before), "masking", .05)[0]
    after["tracks"]["t1"] = replace(before["tracks"]["t1"], loudness=-25)
    assert not evaluate(before, after, anchor(before), "masking", .05)[0]


def test_spectrum_diagnosis_names_sources_without_double_counting_groups():
    m = measurement()
    m["tracks"]["t0"] = replace(m["tracks"]["t0"], bands_db=np.full(31, -16.0))
    channels = [{"track": "t0", "name": "Bus", "role": "bus", "priority": 3, "kind": "group"},
                {"track": "t1", "name": "Rhodes", "role": "piano", "priority": 3, "kind": "track"}]
    d = diagnosis(m, channels)
    assert len(d["spectrum"]) == 6
    assert all([s["name"] for s in r["strongest_sources"]] == ["Rhodes"] for r in d["spectrum"])
    m["cmp"] = None
    assert all(r["reference_difference_db"] is None for r in diagnosis(m, channels)["spectrum"])


def test_mix_loop_keeps_the_verified_tradeoff_and_logs_why(monkeypatch):
    from mixmatch.agent import Agent, Settings
    from mixmatch.sim import demo_project

    class Planner:
        def plan(self, ctx):
            assert ctx["mix_diagnosis"]["policy"] == "mix-first-v1"
            return {"assessment": "give the kick room", "done": True,
                    "mix_intent": {"priority": "masking", "evidence": "kick/bass collision",
                                   "preserve": "warmth", "reference_relevance": "keep useful low mids"},
                    "actions": [{"type": "volume", "track": "t1", "delta_db": -1, "reason": "make room"}]}

    song, hub, env, live = demo_project()
    env.wait(2)
    agent = Agent(live, hub, Planner(), Settings(fine=False), env=env, log=lambda *_: None)
    agent.understand()
    before, after = measurement(), measurement(masking=1.8, tonal=1.4)
    monkeypatch.setattr(agent, "measure_fine", lambda *args: copy.deepcopy(before))
    monkeypatch.setattr(agent, "measure", lambda *args: copy.deepcopy(after))
    agent.section("test", env.now() + 600, max_rounds=1)
    assert not agent.history[-1].undone
    assert agent.history[-1].after > agent.history[-1].before
    assert "verified masking" in agent.history[-1].plan["verification"]


def test_resume_retains_cumulative_protections_and_old_checkpoints_start_fresh(tmp_path):
    import json
    from mixmatch.agent import Agent, Settings
    from mixmatch.mix_quality import VERSION
    from mixmatch.planner import RulePlanner
    from mixmatch.sim import demo_project

    song, hub, env, live = demo_project()
    env.wait(2)
    agent = Agent(live, hub, RulePlanner(), Settings(log_dir=str(tmp_path)), env=env, log=lambda *_: None)
    agent.understand()
    agent.quality_anchors = {"passage": anchor(measurement())}
    agent.save_checkpoint(phase="channels", next_channel=None, regions={}, main=["loudest part", 0, 32])
    ck = json.loads((tmp_path / "checkpoint.json").read_text())
    assert ck["mix_policy"] == VERSION
    resumed = Agent(live, hub, RulePlanner(), Settings(resume="auto"), env=env, log=lambda *_: None)
    resumed.understand()
    resumed._resume_from(ck)
    assert resumed.quality_anchors == agent.quality_anchors
    ck.pop("mix_policy")
    resumed._resume_from(ck)
    assert resumed.quality_anchors == {}
