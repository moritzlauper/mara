import json
from pathlib import Path

import pytest

from mixmatch import analysis_review, workflow


class Clock:
    def __init__(self):
        self.at = 1000
        self.tick = lambda: None

    def now(self):
        return self.at

    def wait(self, seconds):
        self.at += seconds
        self.tick()


def test_review_continues_without_an_open_browser(tmp_path):
    env = Clock()
    run = workflow.begin(str(tmp_path))
    try:
        analysis_review.wait(str(tmp_path), run['id'], 30, env)
        assert 1030 <= env.now() < 1031
        assert analysis_review.read(tmp_path)['closed']
        with pytest.raises(ValueError, match='ended'):
            analysis_review.request(tmp_path, run['id'], 'hold')
    finally:
        workflow.end()


def test_editing_holds_review_until_explicit_continuation(tmp_path):
    env = Clock()
    run = workflow.begin(str(tmp_path))
    held = False
    def tick():
        nonlocal held
        if not held:
            analysis_review.request(tmp_path, run['id'], 'hold')
            held = True
        if env.now() > 1040:
            view = workflow.presentation(str(tmp_path), {}, active=True, previews={}, checkpoint={})
            assert view['stage'] == 'analysis' and view['review']['held']
            analysis_review.request(tmp_path, run['id'], 'continue')
    env.tick = tick
    try:
        analysis_review.wait(str(tmp_path), run['id'], 30, env)
        assert env.now() > 1040
        with pytest.raises(ValueError):
            analysis_review.request(tmp_path, 'old-run', 'continue')
    finally:
        workflow.end()


def test_pause_during_review_still_stops_at_a_safe_boundary(tmp_path):
    env = Clock()
    run = workflow.begin(str(tmp_path))
    env.tick = lambda: (tmp_path / 'pause.request').touch()
    try:
        with pytest.raises(KeyboardInterrupt):
            analysis_review.wait(str(tmp_path), run['id'], 30, env)
    finally:
        workflow.end()


def test_settings_remember_the_library_without_moving_project_data(tmp_path):
    from mixmatch import preferences
    root = tmp_path / 'workspace'
    root.mkdir()
    library = tmp_path / 'music'
    library.mkdir()
    assert preferences.read(root)['reference_library'] == str(root / 'references')
    saved = preferences.save(root, str(library))
    assert saved['workspace'] == str(root)
    assert preferences.read(root)['reference_library'] == str(library)
    with pytest.raises(ValueError, match='does not exist'):
        preferences.save(root, str(tmp_path / 'missing'))
    assert preferences.read(root) == saved


def test_reviewed_roles_are_used_before_the_first_mixing_pass(tmp_path, monkeypatch):
    from mixmatch.agent import Agent, Settings
    from mixmatch.planner import RulePlanner
    from mixmatch.sim import demo_project
    song, hub, env, live = demo_project()
    env.wait(5)
    roles_path = tmp_path / 'roles.json'
    agent = Agent(live, hub, RulePlanner(), Settings(review_seconds=30, roles_file=str(roles_path),
                  log_dir=str(tmp_path)), env=env, log=lambda *_: None)
    agent.understand()
    key, role = next(iter(agent.roles.items()))
    monkeypatch.setattr(agent, '_prepare', lambda *args: [('Main', 0, 16)])
    monkeypatch.setattr(agent, 'record_preview', lambda *args: True)
    def review(*args):
        roles_path.write_text(json.dumps({role.name: {'role': 'pad', 'priority': 1, 'source': 'user'}}))
    monkeypatch.setattr(analysis_review, 'wait', review)
    agent._before_file = 'before.json'
    agent._run(env.now()-1)  # preparation/review completes, mixing deadline already elapsed
    assert agent.roles[key].role == 'pad' and agent.roles[key].priority == 1
