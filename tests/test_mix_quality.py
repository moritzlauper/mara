import copy
from dataclasses import replace

import numpy as np
import pytest

from mixmatch.analysis import Comparison, Stats
from mixmatch.mix_quality import VERSION, anchor, diagnosis, evaluate, combine, revisit_candidates


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
            assert ctx["mix_diagnosis"]["policy"] == VERSION
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
    samples = iter([before, after, after])
    monkeypatch.setattr(agent, "measure", lambda *args: copy.deepcopy(next(samples)))
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
    agent.quality_anchors = {"passage": {**anchor(combine([measurement(tonal=2), measurement(tonal=2.1)])),
                                          "before_mix": True}}
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


def test_repeat_measurements_reject_noise_but_accept_a_clear_reference_gain():
    before = combine([measurement(tonal=2), measurement(tonal=2.2)])
    noise_only = combine([measurement(tonal=1.95), measurement(tonal=2.15)])
    assert not evaluate(before, noise_only, anchor(before), 'tonal', .05)[0]
    improved = combine([measurement(tonal=1.2), measurement(tonal=1.25)])
    assert evaluate(before, improved, anchor(before), 'tonal', .05)[0]
    assert before['samples'] == 2
    assert before['noise']['tonal'] == pytest.approx(.2)


def test_reference_gain_does_not_have_to_outweigh_unrelated_masking_noise():
    before = measurement(masking=7, tonal=2)
    after = measurement(masking=7.15, tonal=1.9)
    assert after['objective'] > before['objective']
    assert evaluate(before, after, anchor(before), 'tonal', .05)[0]
    assert not evaluate(before, measurement(masking=8, tonal=1), anchor(before), 'tonal', .05)[0]


def test_drifted_baseline_allows_recovery_but_not_more_drift():
    start, before = measurement(masking=7, tonal=2), measurement(masking=8.1, tonal=2)
    assert evaluate(before, measurement(masking=8.0, tonal=1.7), anchor(start), 'tonal', .05)[0]
    assert not evaluate(before, measurement(masking=8.3, tonal=1.7), anchor(start), 'tonal', .05)[0]


def test_repeated_windows_cannot_hide_missing_audio_or_a_peak():
    before = measurement()
    bad = measurement(tonal=0)
    bad['master'] = replace(bad['master'], peak_db=.5)
    after = combine([measurement(tonal=0), bad])
    assert not evaluate(before, after, anchor(before), 'tonal', .05)[0]
    bad = measurement(tonal=0)
    bad['cmp'] = None
    after = combine([measurement(tonal=0), bad])
    assert not evaluate(before, after, anchor(before), 'tonal', .05)[0]


def test_other_passage_can_stay_unchanged_but_cannot_lose_a_layer():
    before = measurement()
    assert evaluate(before, copy.deepcopy(before), anchor(before), 'protect', .05)[0]
    after = copy.deepcopy(before)
    after['tracks']['t1'] = None
    assert not evaluate(before, after, anchor(before), 'protect', .05)[0]


def test_revisit_selects_active_sources_of_unresolved_spectrum():
    m = measurement(tonal=3)
    m['cmp'].tonal_excess[:] = 3
    channels = [{'track': key, 'name': key, 'role': 'keys', 'priority': 3, 'kind': 'track'}
                for key in ['t0', 't1', 'missing']]
    assert revisit_candidates(m, channels, 1) == ['t0']
    m['cmp'].tonal_excess[:] = 0
    assert revisit_candidates(m, channels) == []


def test_final_repair_resumes_remaining_sources_without_repeating_completed_ones(tmp_path, monkeypatch):
    from mixmatch.agent import Agent, Settings
    from mixmatch.sim import demo_project
    from mixmatch.planner import RulePlanner
    song, hub, env, live = demo_project()
    agent = Agent(live, hub, RulePlanner(), Settings(log_dir=str(tmp_path)), env=env, log=lambda *_: None)
    agent.understand()
    agent._ck = {'phase': 'final', 'revisit_pending': ['t0', 't1']}
    seen = []
    def section(name, deadline, focus, **kwargs):
        if focus == 't1':
            raise KeyboardInterrupt()
        seen.append(focus)
    monkeypatch.setattr(agent, 'section', section)
    with pytest.raises(KeyboardInterrupt):
        agent.final_revisit(('Main', 0, 32), env.now()+100)
    assert seen == ['t0']
    assert agent._ck['revisit_pending'] == ['t1']
    monkeypatch.setattr(agent, 'section', lambda name, deadline, focus, **kw: seen.append(focus))
    assert agent.final_revisit(('Main', 0, 32), env.now()+100)
    assert seen == ['t0', 't1']
    assert agent._ck['revisit_done']
    assert agent.final_revisit(('Main', 0, 32), env.now()+100)
    assert seen == ['t0', 't1']


def test_final_move_is_undone_when_it_damages_a_different_passage(monkeypatch):
    from mixmatch.agent import Agent, Settings
    from mixmatch.sim import demo_project
    class Planner:
        def plan(self, ctx):
            return {'done': True, 'mix_intent': {'priority': 'tonal'},
                    'actions': [{'type': 'volume', 'track': 't1', 'delta_db': -1, 'reason': 'balance'}]}
    song, hub, env, live = demo_project()
    env.wait(2)
    agent = Agent(live, hub, Planner(), Settings(fine=False), env=env, log=lambda *_: None)
    agent.understand()
    before, after = measurement(tonal=2), measurement(tonal=1)
    monkeypatch.setattr(agent, 'measure_fine', lambda *args: copy.deepcopy(before))
    samples = iter([before, after, after, before, before])
    monkeypatch.setattr(agent, 'measure', lambda *args: copy.deepcopy(next(samples)))
    broken = copy.deepcopy(before)
    broken['tracks']['t1'] = None
    other = iter([{(64, 32): before}, {(64, 32): broken}])
    monkeypatch.setattr(agent, 'measure_validation_passages', lambda: next(other))
    original = song.tracks[1].mixer_device.volume.value
    agent.section('final repair', env.now()+100, max_rounds=1)
    assert agent.history[-1].undone
    assert 'other passage' in agent.history[-1].plan['verification']
    assert song.tracks[1].mixer_device.volume.value == original


def assessed(start, end):
    from mixmatch.mix_quality import final_assessment
    original = anchor(combine(start))
    original['before_mix'] = True
    return final_assessment(combine(end), original)


def test_final_assessment_does_not_sell_noise_as_improvement():
    report = assessed([measurement(tonal=2), measurement(tonal=2.2)],
                      [measurement(tonal=1.95), measurement(tonal=2.15)])
    assert report['benefit'] == 'small'
    assert report['reference']['status'] == 'similar'
    assert not report['listening_judgment']


def test_final_assessment_separates_health_reference_and_tradeoffs():
    report = assessed([measurement(tonal=3)] * 2, [measurement(tonal=2, masking=2.3)] * 2)
    assert report['reference']['status'] == 'closer'
    assert report['status'] == 'checked'
    assert report['benefit'] == 'mixed'
    assert report['improved'] == ['Tone balance']
    assert report['worsened'] == ['Potential masking']
    # A large reference difference alone does not mean the mix is technically bad.
    report = assessed([measurement(tonal=8)] * 2, [measurement(tonal=8)] * 2)
    assert report['status'] == 'checked'
    assert report['benefit'] == 'small'


def test_final_assessment_flags_damage_even_when_reference_is_closer():
    after = measurement(tonal=0)
    after['master'] = replace(after['master'], peak_db=.2, plr=5, low_side_db=-10)
    after['tracks']['t1'] = None
    report = assessed([measurement()] * 2, [after] * 2)
    assert report['status'] == 'attention'
    assert {c['name'] for c in report['checks'] if c['status'] == 'attention'} == {
        'Peak level', 'Dynamics', 'Low-end stereo', 'Layers'}


def test_final_assessment_never_uses_a_final_or_legacy_baseline_as_before():
    from mixmatch.mix_quality import final_assessment
    m = combine([measurement()] * 2)
    for baseline in (None, anchor(m), {'before_mix': True, 'parts': m['parts']}):
        report = final_assessment(m, baseline)
        assert report['benefit'] == 'unknown'
        assert report['reference']['status'] == 'unknown'


@pytest.mark.parametrize('fault', ['missing', 'silent', 'nan', 'single'])
def test_final_assessment_incomplete_data_cannot_pass(fault):
    from mixmatch.mix_quality import final_assessment
    m = combine([measurement()] * 2)
    if fault == 'missing':
        m['master'] = None
    elif fault == 'silent':
        m['master'] = replace(m['master'], active=.1)
    elif fault == 'nan':
        m['master'] = replace(m['master'], peak_db=float('nan'))
    else:
        m['samples'] = 1
    assert final_assessment(m, None)['status'] == 'incomplete'


def test_final_report_keeps_loudness_gap_even_if_planner_stops_chasing_it():
    before, after = measurement(loudness=2), measurement(loudness=0)
    after['assessment_parts'] = dict(before['parts'])
    report = assessed([before] * 2, [after] * 2)
    assert report['benefit'] == 'small'
    assert report['reference']['status'] == 'similar'
