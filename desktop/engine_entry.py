"""PyInstaller entry point. Run children keep their project cwd; only the service chooses the data root."""
import multiprocessing
import os
import sys

def main():
    multiprocessing.freeze_support()
    from mixmatch.cli import main as cli
    if sys.argv[1:2] == ['desktop-service']:
        from mixmatch.platforms import workspace, engine_build
        folder = workspace(); folder.mkdir(parents=True, exist_ok=True)
        os.chdir(folder)
        import json
        import urllib.request
        try:
            data = json.loads((folder / 'desktop-service.json').read_text())
            url = data['url']
            if not url.startswith('http://127.0.0.1:'):
                raise ValueError('Not a local engine')
            with urllib.request.urlopen(url + '/api/health', timeout=2) as response:
                health = json.load(response)
            if health.get('app') == 'MARA' and health.get('workspace') == str(folder):
                with urllib.request.urlopen(url + '/api/state', timeout=2) as response:
                    state = json.load(response)
                if health.get('build') == engine_build() or state.get('active') or state.get('running') or state.get('engine', {}).get('active'):
                    print('MARA is already running: ' + url, flush=True)
                    return
        except (OSError, ValueError, KeyError):
            pass
        os.environ['MARA_DESKTOP_SERVICE'] = '1'
        cli(['ui', '--no-browser', '--port', '0'])
    else:
        cli()

if __name__ == '__main__':
    main()
