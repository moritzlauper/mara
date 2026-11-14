import json
import pytest
import httpx
from mixmatch import planner_settings as settings
from mixmatch import planner

@pytest.fixture
def storage(tmp_path, monkeypatch):
    import keyring
    secrets = {}
    monkeypatch.setattr(settings, 'workspace', lambda: tmp_path)
    monkeypatch.setattr(keyring, 'set_password', lambda service, account, value: secrets.update({account:value}))
    monkeypatch.setattr(keyring, 'get_password', lambda service, account: secrets.get(account))
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    return tmp_path, secrets


def test_defaults_and_key_not_in_settings(storage):
    path, secrets = storage
    assert settings.read()['profiles']['anthropic']['model'] == 'claude-haiku-5-5'
    result = settings.save('auto', 'claude-sonnet-5-5', api_key='test-secret')
    assert result['configured']['anthropic']
    assert 'test-secret' not in json.dumps(result)
    assert 'test-secret' not in (path/'planner-settings.json').read_text()
    assert secrets['anthropic-api-key'] == 'test-secret'
    settings.save('anthropic', 'claude-haiku-5-5')
    assert settings.key('anthropic') == 'test-secret'


def test_custom_credentials_bound_to_endpoint(storage):
    settings.save('compatible', 'model-a', 'http://localhost:1234/v1', 'local-secret')
    assert settings.key('compatible', 'http://localhost:1234/v1') == 'local-secret'
    assert not settings.key('compatible', 'https://another.example/v1')
    with pytest.raises(ValueError): settings.save('compatible', 'a', 'https://secret@example.com/v1')
    with pytest.raises(ValueError): settings.save('compatible', 'a', 'http://remote.example/v1')


def test_factory_honors_provider_and_model(storage, monkeypatch):
    captured = {}
    monkeypatch.setattr(planner, 'ClaudePlanner', lambda **kw: captured.update(kw) or kw)
    monkeypatch.setattr(planner.shutil, 'which', lambda _: '/bin/claude')
    settings.save('anthropic', 'claude-haiku-5-5', api_key='test-secret')
    assert planner.make_planner()['model'] == 'claude-haiku-5-5'
    settings.save('compatible', 'local-model', 'http://localhost:1234/v1')
    result = planner.make_planner()
    assert isinstance(result, planner.CompatiblePlanner) and result.model == 'local-model'
    assert isinstance(planner.make_planner('rules'), planner.RulePlanner)


def test_codex_provider_uses_the_local_chatgpt_cli(storage, monkeypatch):
    monkeypatch.setattr(planner.shutil, 'which', lambda name: '/bin/codex' if name == 'codex' else None)
    settings.save('codex', 'default')
    result = planner.make_planner()
    assert isinstance(result, planner.CodexPlanner) and result.selected_model == 'default'


def test_compatible_plan_contract_and_incomplete_response():
    plan = {'assessment':'Balanced', 'done':True, 'actions':[], 'mix_intent':{
        'priority':'none','evidence':'Measured','preserve':'Groove','reference_relevance':'Direction'}}
    response = {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(plan)}}]}
    def handle(request):
        body=json.loads(request.content)
        assert body['model']=='selected-model'
        assert planner.SYSTEM in body['messages'][0]['content']
        return httpx.Response(200,json=response)
    p=planner.CompatiblePlanner('selected-model','https://example.com/v1','secret')
    p.client=httpx.Client(transport=httpx.MockTransport(handle))
    assert p.plan({'test':1})==plan
    response['choices'][0]['finish_reason']='length'
    with pytest.raises(RuntimeError,match='invalid mixing plan'):p.plan({})
    response['choices'][0].update(finish_reason='stop',message={'content':'{"actions":[{"type":"delete_track"}]}'})
    with pytest.raises(RuntimeError,match='invalid mixing plan'):p.plan({})
