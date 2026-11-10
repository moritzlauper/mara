import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('build,active,reuse', [('old', False, False), ('old', True, True), ('new', False, True)])
def test_desktop_update_reuses_only_current_or_active_engine(tmp_path, monkeypatch, build, active, reuse):
    from mixmatch import cli, platforms
    spec = importlib.util.spec_from_file_location('desktop_entry', Path(__file__).parents[1] / 'desktop/engine_entry.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    (tmp_path / 'desktop-service.json').write_text(json.dumps({'url': 'http://127.0.0.1:43210'}))
    monkeypatch.setattr(platforms, 'workspace', lambda: tmp_path)
    monkeypatch.setattr(platforms, 'engine_build', lambda: 'new')
    monkeypatch.setattr(sys, 'argv', ['mara-engine', 'desktop-service'])
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(cli, 'main', calls.append)
    def response(url, **kwargs):
        data = {'app': 'MARA', 'workspace': str(tmp_path), 'build': build} if url.endswith('/health') else {'active': active}
        return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr('urllib.request.urlopen', response)
    entry.main()
    assert calls == ([] if reuse else [['ui', '--no-browser', '--port', '0']])
