import json
from pathlib import Path
import pytest
from mixmatch import ab, review, workflow
from mixmatch.sim import demo_project


def test_completion_requires_previews_and_dead_worker_is_not_running(tmp_path):
    workflow.begin(tmp_path)
    workflow.update(status='mixing')
    shown=workflow.presentation(tmp_path,{},active=False,previews={'before':None,'after':None},checkpoint={})
    assert shown['status']=='interrupted'
    workflow.update(status='finished',mix_complete=True,output_restored=True)
    shown=workflow.presentation(tmp_path,{},active=False,previews={'before':{},'after':None},checkpoint=None)
    assert shown['status']=='preview_failed' and shown['stage']=='result'
    shown=workflow.presentation(tmp_path,{},active=False,previews={'before':{'id':'a'},'after':{'id':'b'}},checkpoint=None)
    assert shown['status']=='finished'
    workflow.end()


def test_pause_is_cooperative_and_resuming_clears_request(tmp_path):
    workflow.begin(tmp_path)
    (tmp_path/'pause.request').touch()
    with pytest.raises(KeyboardInterrupt): workflow.check_pause()
    workflow.update(status='paused',before='original.json')
    workflow.begin(tmp_path,resume=True)
    workflow.check_pause()
    assert workflow.read(tmp_path)['before']=='original.json'
    workflow.end()


def test_legacy_checkpoint_prevents_false_finished_state(tmp_path):
    shown=workflow.presentation(tmp_path,{'done':'Done. Partial run'},active=False,
                                previews={'before':{'id':'a'},'after':{'id':'b'}},checkpoint={'phase':'channels'})
    assert shown['status']=='paused'


def test_review_reports_net_changes_not_rejected_attempts(tmp_path):
    _,_,_,live=demo_project()
    initial=live.get_set()
    before=ab.save(initial,str(tmp_path),'2026','before')
    track=initial['tracks'][0]
    volume=next(p for p in track['mixer'] if p['name']=='Volume')['value']
    live.set(track['key'],'mixer','Volume',value=volume-.03)
    ab.save(live.get_set(),str(tmp_path),'2026','after')
    workflow.event(tmp_path,{'before_time':json.loads(Path(before).read_text())['time'],'status':'rejected',
                            'parameters':[{'track':track['key'],'device':'Mixer','param':'Volume'}],
                            'verification':'Punch worsened'})
    changes=review.changes(str(tmp_path))
    assert len(changes['kept'])==1 and changes['kept'][0]['param']=='Volume'
    assert changes['kept'][0]['raw_after']==pytest.approx(volume-.03)
    assert len(changes['rejected'])==1
    plan=review.restore_plan(str(tmp_path),live.get_set())
    assert plan['count']==1
    live.set(track['key'],'mixer','Volume',value=volume-.05)
    with pytest.raises(ValueError,match='manual edits'): review.restore_plan(str(tmp_path),live.get_set())


def test_partial_review_uses_only_verified_changes_from_current_run(tmp_path):
    _,_,_,live=demo_project()
    path=ab.save(live.get_set(),str(tmp_path),'2026','before')
    before=json.loads(Path(path).read_text())
    key=next(iter(before['tracks']))
    value=before['tracks'][key]['mixer']['Volume']
    workflow.event(tmp_path,{'before_time':before['time'],'status':'kept','reason':'Verified gain correction',
                            'parameters':[{'track':key,'device':'Mixer','param':'Volume','new_value':value-.02,'new_display':'-1 dB'}]})
    result=review.changes(str(tmp_path))
    assert result['partial'] and result['kept'][0]['after']=='-1 dB'
    assert result['kept'][0]['reason']=='Verified gain correction'
    with pytest.raises(ValueError,match='No complete'):review.restore_plan(str(tmp_path),live.get_set())


def test_resumed_mix_preparation_stays_in_mix_stage(tmp_path):
    workflow.begin(str(tmp_path),kind='mix',resume=True)
    for status in ('preparing','analysing','mixing'):
        workflow.update(status=status)
        view=workflow.presentation(str(tmp_path),{},active=True,previews={},checkpoint={})
        assert view['stage']=='mix'
    workflow.begin(str(tmp_path),kind='analysis')
    assert workflow.presentation(str(tmp_path),{},active=True,previews={},checkpoint={})['stage']=='analysis'
    workflow.end()


def test_added_device_keeps_verified_settings_in_review(tmp_path):
    _,_,_,live=demo_project()
    path=ab.save(live.get_set(),str(tmp_path),'2026','before')
    before=json.loads(Path(path).read_text())
    key=next(iter(before['tracks']))
    workflow.event(tmp_path,{'before_time':before['time'],'status':'kept','reason':'Reduce masking',
        'verification':'Verified masking improvement','parameters':[
        {'track':key,'device':'New EQ#1','param':'Frequency','new_value':.5,'new_display':'403 Hz'},
        {'track':key,'device':'New EQ#1','param':'Gain','new_value':-.1,'new_display':'-1.5 dB'}]})
    item=review.changes(str(tmp_path))['kept'][0]
    assert item['kind']=='added'
    assert item['settings']==[{'param':'Frequency','value':'403 Hz'},{'param':'Gain','value':'-1.5 dB'}]
    assert item['verification']=='Verified masking improvement'


def test_review_excludes_automated_values_from_change_count(tmp_path):
    _, _, _, live = demo_project()
    before = live.get_set()
    track = before['tracks'][0]
    track['mixer'][0]['automated'] = True
    ab.save(before, str(tmp_path), '1', 'before')
    track['mixer'][0]['value'] += .1
    ab.save(before, str(tmp_path), '1', 'after')
    result = review.changes(str(tmp_path))
    assert result['ready'] and result['kept'] == []


def test_a_failed_analysis_stays_on_the_analysis_stage(tmp_path):
    from mixmatch import workflow

    folder = str(tmp_path)
    workflow.write(folder, {"schema": 1, "id": "a", "kind": "analysis", "status": "failed", "phase": "references",
                            "error": "The original preview is missing."})
    stage = lambda: workflow.presentation(folder, {}, active=False, previews={}, checkpoint=None)["stage"]
    assert stage() == "analysis"
    workflow.write(folder, {"schema": 1, "id": "b", "kind": "mix", "status": "failed"})
    assert stage() == "mix"


def test_a_run_that_starts_with_main_muted_keeps_the_saved_listening_level(tmp_path, monkeypatch):
    import json
    from mixmatch import monitoring, workflow

    folder = str(tmp_path)
    (tmp_path / "monitor.json").write_text(json.dumps({"run_id": "old", "volume": 0.8, "managed": True}))
    workflow.write(folder, {"schema": 1, "id": "new", "kind": "analysis", "status": "preparing"})
    monitoring.begin(folder, 0.0, True)
    assert json.loads((tmp_path / "monitor.json").read_text())["volume"] == 0.8


def test_before_is_never_recorded_with_main_at_minus_inf(tmp_path):
    from mixmatch.agent import UNITY_VOLUME, Agent, Settings
    from mixmatch.planner import RulePlanner
    from mixmatch.sim import demo_project

    song, hub, env, live = demo_project()
    logs = []
    agent = Agent(live, hub, RulePlanner(), Settings(log_dir=str(tmp_path)), env=env, log=logs.append)
    live.set("m", "mixer", "Volume", value=0.0)
    agent.ex.refresh()
    state = agent.snapshot_state(0.0)
    volume = next(p["value"] for t in state["tracks"] if t["key"] == "m" for p in t["mixer"] if p["name"] == "Volume")
    assert volume == UNITY_VOLUME and any("0 dB" in l for l in logs)


def test_the_analysis_shows_on_your_mix_and_its_suggestions_on_references(tmp_path):
    from mixmatch import workflow

    folder = str(tmp_path)
    stage = lambda: workflow.presentation(folder, {}, active=True, previews={}, checkpoint=None)["stage"]
    workflow.write(folder, {"schema": 1, "id": "a", "kind": "analysis", "status": "analysing", "purpose": "references"})
    assert stage() == "analysis"
    workflow.write(folder, {"schema": 1, "id": "a", "kind": "analysis", "status": "ready", "purpose": "references"})
    assert stage() == "references"
    workflow.write(folder, {"schema": 1, "id": "b", "kind": "analysis", "status": "analysing"})
    assert stage() == "analysis"


def test_a_running_mix_takes_a_new_reference_selection_at_the_next_phase(tmp_path):
    from conftest import write_audio
    from mixmatch.agent import Agent, Settings
    from mixmatch.planner import RulePlanner
    from mixmatch.reference import ReferenceSet
    from mixmatch.sim import demo_project

    refs = tmp_path / "references"
    refs.mkdir()
    first = write_audio(str(refs / "a.wav"), tilt=0, seed=1)
    song, hub, env, live = demo_project()
    logs = []
    agent = Agent(live, hub, RulePlanner(), Settings(ref_dirs=(str(refs),), log_dir=str(tmp_path)), env=env,
                  log=logs.append, references=ReferenceSet([first], log=lambda *_: None))
    assert not agent.sync_references()                    # unchanged selection: nothing happens
    write_audio(str(refs / "b.wav"), tilt=3, seed=2)
    assert agent.sync_references() and len(agent.refset.paths) == 2 and agent.target is not None
    assert any("References changed" in l for l in logs)
    for f in refs.iterdir():
        f.unlink()
    assert not agent.sync_references() and len(agent.refset.paths) == 2   # empty: keeps the previous ones


def test_the_analysis_progress_only_moves_forward(tmp_path, monkeypatch):
    from mixmatch import workflow
    from mixmatch.agent import Agent, Settings
    from mixmatch.planner import RulePlanner
    from mixmatch.sim import demo_project

    updates = []
    monkeypatch.setattr(workflow, "update", lambda **v: updates.append(v.get("analysis_progress")))
    song, hub, env, live = demo_project()
    agent = Agent(live, hub, RulePlanner(), Settings(log_dir=str(tmp_path)), env=env, log=lambda *_: None)
    agent._analysis_est = {"scan": 60.0, "search": 90.0, "preview": 30.0}
    for key in ("scan", "search", "preview", "done"):
        agent.analysis_step(key, key)
    starts = [u["base"] for u in updates]
    assert starts == sorted(starts) and starts[0] == 0 and starts[-1] == 1.0
    assert abs(updates[1]["base"] - 60 / 180) < 1e-3 and updates[0]["by_position"]
    assert [u["step"] for u in updates[:3]] == [1, 2, 3] and updates[0]["steps"] == 3
