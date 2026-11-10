import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mixmatch import songs
from mixmatch.ui import Runner, make_handler

@pytest.fixture(autouse=True)
def isolate_live_processes(monkeypatch):
    # Real mixing on the developer’s machine must not change these isolated project tests.
    monkeypatch.setattr("mixmatch.ui.run_process_alive", lambda: False)


SET_A = ["1-Kick", "2-Bass", "3-Pad", "4-Lead"]
SET_B = ["1-Drums", "2-Rhodes", "3-Strings", "4-Vox"]


def test_old_layout_becomes_the_first_track(tmp_path):
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "ref.wav").write_bytes(b"x")
    (tmp_path / "runs").mkdir()
    (tmp_path / "roles.json").write_text(json.dumps({n: {"role": "pad"} for n in SET_A}))
    (tmp_path / "mixmatch.json").write_text(json.dumps({"description": "Hyperpop, happy and bright"}))

    assert songs.migrate(str(tmp_path)) == "Hyperpop"
    folder = tmp_path / "songs" / "Hyperpop"
    assert (tmp_path / "references" / "ref.wav").exists() and (folder / "roles.json").exists()
    assert not list((folder / "references").iterdir()) and not (tmp_path / "roles.json").exists()
    assert songs.reference_folders(str(folder)) == [str(folder / "references"), str(tmp_path / "references")]
    assert songs.current_dir(str(tmp_path)) == str(folder)
    assert songs.current(str(tmp_path))["channels"] == SET_A
    assert songs.migrate(str(tmp_path)) is None                # once only


def test_a_new_track_starts_empty_and_another_set_is_noticed(tmp_path):
    root = str(tmp_path)
    old = songs.create(root, "Hyperpop", channels=SET_A)
    (tmp_path / "songs" / old / "references" / "claws.wav").write_bytes(b"x")
    assert songs.check_set(root, SET_A) == {"same": True, "other": None}
    assert songs.check_set(root, SET_B) == {"same": False, "other": None}

    new = songs.create(root, "Cleopatra beat", "dark, slow")
    assert songs.current_dir(root).endswith(new) and not (tmp_path / "songs" / new / "references" / "claws.wav").exists()
    assert json.loads((tmp_path / "songs" / new / "mixmatch.json").read_text())["description"] == "dark, slow"
    assert songs.check_set(root, SET_B)["same"]                 # a fresh track takes the open set's channels
    assert songs.check_set(root, SET_A) == {"same": False, "other": {"slug": old, "name": "Hyperpop"}}
    assert songs.check_set(root, SET_B + ["5-FX"])["same"]      # a channel added while mixing: still this track


def test_references_of_earlier_tracks_can_be_picked_again(tmp_path):
    root = str(tmp_path)
    old = songs.create(root, "Hyperpop")
    (tmp_path / "songs" / old / "references" / "claws.wav").write_bytes(b"audio")
    songs.create(root, "Next")
    assert songs.used_references(root) == [{"slug": old, "track": "Hyperpop", "file": "claws.wav"}]
    assert songs.reuse_reference(root, old, "claws.wav") == "claws.wav"
    assert (tmp_path / "songs" / "Next" / "references" / "claws.wav").read_bytes() == b"audio"
    with pytest.raises(ValueError):
        songs.reuse_reference(root, old, "../roles.json")


def test_live_name_updates_the_label_without_moving_song_data(tmp_path):
    root = str(tmp_path)
    slug = songs.create(root, "First track", channels=SET_A)
    folder = songs.current_dir(root)
    reference = tmp_path / "songs" / slug / "references" / "ref.wav"
    reference.write_bytes(b"audio")
    songs.remember_live_name(root, "Cleopatra", SET_A)
    assert songs.current(root)["name"] == "Cleopatra"
    assert songs.current_dir(root) == folder and reference.read_bytes() == b"audio"
    songs.remember_live_name(root, "Another project", SET_B)
    songs.remember_live_name(root, "", SET_A)  # unsaved sets / older remote scripts
    assert songs.current(root)["name"] == "Cleopatra"
    songs.remember_live_name(root, "Cleopatra v2", SET_A)
    assert songs.listing(root)[0]["name"] == "Cleopatra v2"


def test_connected_live_set_switches_or_creates_a_project(tmp_path):
    root = str(tmp_path)
    first = songs.create(root, "Hyperpop", channels=SET_A)
    songs.create(root, "Other", channels=SET_B)

    assert songs.activate_live_set(root, "Hyperpop", SET_A)["slug"] == first
    assert songs.activate_live_set(root, "Hyperpop v2", SET_A)["slug"] != first
    assert songs.load(root)["songs"][first]["name"] == "Hyperpop"
    assert songs.activate_live_set(root, "New Ableton project", SET_A, "/tmp/new.als")["name"] == "New Ableton project"
    assert songs.current(root)["slug"] != first


def test_interface_uses_the_live_name_on_the_first_completed_refresh(tmp_path):
    root = str(tmp_path)
    songs.create(root, "First track", channels=SET_A)
    ready = threading.Event()

    def live_set():
        ready.set()
        return {"name": "Cleopatra", "channels": SET_A, "taps": 4, "total": 4, "missing": [], "playing": False}

    server, call = _serve(root, live_set)
    try:
        call("/api/state")
        assert ready.wait(2)
        state = call("/api/state")
        assert state["track"] == "Cleopatra"
        assert next(t for t in state["tracks"] if t["current"])["name"] == "Cleopatra"
    finally:
        server.shutdown()


def _serve(root, live_set_fn):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Runner(root), live_set_fn=live_set_fn))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            return json.load(e)
    return server, call


def test_interface_adopts_the_connected_live_project_and_its_taps(tmp_path):
    root = str(tmp_path)
    songs.create(root, "Hyperpop", channels=SET_A)
    open_set = {"name": "Cleopatra beat", "path": "/tmp/cleopatra.als", "channels": SET_B,
                "taps": 0, "total": 4, "missing": SET_B, "playing": False}
    server, call = _serve(root, lambda: open_set)
    try:
        call("/api/state")
        time.sleep(0.3)                                         # Live is asked in the background
        s = call("/api/state")
        assert s["track"] == "Cleopatra beat" and s["set"] == {"same": True, "other": None}
        assert s["open_set"]["taps"] == 0
        assert "Set taps" in call("/api/run", {"auto_ref": True})["error"]      # no taps in the set yet
        assert [t["name"] for t in s["tracks"]] == ["Hyperpop", "Cleopatra beat"]
    finally:
        server.shutdown()
