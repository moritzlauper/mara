"""Persist non-secret planner choices; keep credentials in the OS credential store."""
from __future__ import annotations
import hashlib
import json
import os
import uuid
from urllib.parse import urlsplit
from .platforms import workspace

HAIKU = 'claude-haiku-5-5'
ANTHROPIC_MODELS = [HAIKU, 'claude-sonnet-5-5', 'claude-opus-5-5', 'claude-fable-5-2']
PROVIDERS = {'auto': 'Automatic', 'anthropic': 'Anthropic', 'claude-code': 'Claude Code',
             'gemini': 'Google Gemini', 'compatible': 'OpenAI-compatible API', 'rules': 'Built-in rules'}
ENDPOINTS = {'gemini': 'https://generativelanguage.googleapis.com/v1beta/openai'}
ENV_KEYS = {'anthropic': 'ANTHROPIC_API_KEY', 'gemini': 'GEMINI_API_KEY'}


def read():
    try:
        saved = json.loads((workspace() / 'planner-settings.json').read_text())
    except (OSError, ValueError):
        saved = {}
    return {'provider': 'auto', 'profiles': {'anthropic': {'model': HAIKU},
            'claude-code': {'model': HAIKU}, **saved.get('profiles', {})}, **{k:v for k,v in saved.items() if k != 'profiles'}}


def validated(provider, model='', base_url=''):
    if provider not in PROVIDERS:
        raise ValueError('Choose a supported planner provider.')
    if not isinstance(model, str) or len(model) > 200 or any(c.isspace() for c in model.strip()):
        raise ValueError('Enter a valid model ID.')
    model = model.strip()
    if provider in ('auto', 'anthropic', 'claude-code'):
        model = model or HAIKU
    if provider not in ('auto', 'rules') and not model:
        raise ValueError('Choose a model or enter its model ID.')
    url = ENDPOINTS.get(provider, '')
    if provider == 'compatible':
        url = str(base_url).strip().rstrip('/')
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Enter an API base URL without credentials, query or fragment.')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
            raise ValueError('Use HTTPS, or HTTP for a local server.')
    return {'model': model, 'base_url': url}


def account(provider, base_url=''):
    if provider == 'compatible':
        return 'compatible-' + hashlib.sha256(base_url.encode()).hexdigest()[:24]
    return provider + '-api-key'


def key(provider, base_url=''):
    try:
        import keyring
        saved = keyring.get_password('MARA', account(provider, base_url))
        if saved:
            return saved
    except Exception:
        pass
    return os.environ.get(ENV_KEYS.get(provider, ''), '')


def save(provider, model='', base_url='', api_key=''):
    profile = validated(provider, model, base_url)
    if not isinstance(api_key, str) or '\n' in api_key or '\r' in api_key:
        raise ValueError('Enter a valid API key.')
    credential_provider = 'anthropic' if provider == 'auto' else provider
    if api_key.strip():
        if credential_provider not in ('anthropic', 'gemini', 'compatible'):
            raise ValueError('This provider does not use an API key.')
        import keyring
        keyring.set_password('MARA', account(credential_provider, profile['base_url']), api_key.strip())
        if credential_provider in ENV_KEYS:
            os.environ[ENV_KEYS[credential_provider]] = api_key.strip()
    data = read()
    data['provider'] = provider
    if provider != 'rules':
        data['profiles'][credential_provider] = profile
    path = workspace() / 'planner-settings.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)
    return public()


def public():
    data = read()
    data['providers'] = PROVIDERS
    data['suggested_models'] = {'anthropic': ANTHROPIC_MODELS, 'claude-code': ANTHROPIC_MODELS}
    data['pricing'] = {
        'checked': '2026-11-10',
        'anthropic': {'source': 'https://platform.claude.com/docs/en/about-claude/pricing', 'models': {
            'claude-haiku-5-5': [.10, .50], 'claude-sonnet-5-5': [2, 10],
            'claude-opus-5-5': [4, 20], 'claude-fable-5-2': [8, 40]}},
        'gemini': {'source': 'https://ai.google.dev/gemini-api/docs/pricing', 'models': {
            'gemini-2.5-flash': [.30, 2.50]}}}
    data['configured'] = {p: bool(key(p, data['profiles'].get(p, {}).get('base_url', ''))) for p in ('anthropic', 'gemini', 'compatible')}
    return data


def models(provider, base_url='', api_key=''):
    if provider == 'auto':
        provider = 'anthropic'
    if provider == 'rules':
        return []
    if provider == 'claude-code':
        return ANTHROPIC_MODELS
    profile = validated(provider, 'model-list', base_url)
    token = api_key.strip() or key(provider, profile['base_url'])
    if provider == 'anthropic':
        if not token:
            return ANTHROPIC_MODELS
        import anthropic
        try:
            return [m.id for m in anthropic.Anthropic(api_key=token, timeout=20, max_retries=0).models.list(limit=100)]
        except Exception:
            raise ValueError('Could not load Anthropic models. Check your key and connection.') from None
    import httpx
    try:
        response = httpx.get(profile['base_url'] + '/models', headers={'Authorization': 'Bearer ' + token} if token else {}, timeout=20)
        response.raise_for_status()
        return sorted({m['id'] for m in response.json()['data'] if isinstance(m.get('id'), str)})
    except Exception:
        raise ValueError('Could not load models. Check the API URL, key and connection; you can also enter a model ID.') from None
