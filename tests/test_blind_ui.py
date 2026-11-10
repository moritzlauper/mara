import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from mixmatch import ab, ui
from mixmatch.sim import demo_project


def test_blind_api_hides_identity_and_count_until_reveal(tmp_path, monkeypatch):
    from mixmatch.live_client import LiveClient

    song, hub, env, live = demo_project()
    folder = tmp_path / "runs"
    ab.save(live.get_set(), str(folder), "1", "before")
    live.set("t0", "mixer", "Volume", target=-2)
    ab.save(live.get_set(), str(folder), "1", "after")
    monkeypatch.setattr(LiveClient, "connect", lambda self: live)
    monkeypatch.setattr(live, "close", lambda: None, raising=False)
    monkeypatch.setattr(ui, "run_process_alive", lambda: False)
    monkeypatch.setattr(ui, "project_references", lambda: [])
    monkeypatch.setattr(ab.secrets, "choice", lambda options: "before")
    runner = ui.Runner(str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), ui.make_handler(runner, live_set_fn=lambda: None))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path, body=None):
        request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(request))
        except urllib.error.HTTPError as e:
            return json.load(e)
    try:
        assert "error" in call("/api/ab/reveal", {})
        assert call("/api/ab/blind", {}) == {"blind": True}
        state = call("/api/state")
        assert state["ab"] is None and state["ab_blind"] is True
        assert state["ab_blind_ready"] is True
        assert "which" not in state and "changed" not in state
        assert call("/api/ab/reveal", {}) == {"which": "before"}
        assert call("/api/state")["ab"] == "before"
        assert call("/api/ab/blind", {}) == {"blind": True}
        assert call("/api/state")["ab"] is None
        call("/api/ab", {"which": "after"})
        assert "error" in call("/api/ab/reveal", {})
        assert "error" in call("/api/ab", {"which": "typo"})
        monkeypatch.setattr(ui, "run_process_alive", lambda: True)
        assert "error" in call("/api/ab/blind", {})
        assert "error" in call("/api/ab", {"which": "before"})
    finally:
        server.shutdown()
        server.server_close()
