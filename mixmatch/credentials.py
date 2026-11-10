"""Planner credentials in the operating system's credential store."""
import os
import shutil
SERVICE='MARA'
ACCOUNT='anthropic-api-key'

def load():
    if os.environ.get('ANTHROPIC_API_KEY'):
        return
    try:
        import keyring
        value=keyring.get_password(SERVICE,ACCOUNT)
        if value:
            os.environ['ANTHROPIC_API_KEY']=value
    except Exception:
        pass  # Optional: rules and an existing Claude Code login also work.

def status():
    from . import planner_settings
    settings = planner_settings.public()
    provider = settings['provider']
    label = settings['providers'][provider]
    if provider == 'auto':
        label = 'Automatic · ' + ('Claude Code' if shutil.which('claude') else 'Anthropic' if settings['configured']['anthropic'] else 'Built-in rules')
    return {'api_configured': settings['configured']['anthropic'],
            'claude_code': bool(shutil.which('claude')), 'planner': label, 'planner_settings': settings}

def save(value):
    if not isinstance(value,str) or not value.strip().startswith('sk-ant-') or len(value.strip())<25:
        raise ValueError('Enter an Anthropic API key.')
    import keyring
    keyring.set_password(SERVICE,ACCOUNT,value.strip())
    os.environ['ANTHROPIC_API_KEY']=value.strip()
    return status()
