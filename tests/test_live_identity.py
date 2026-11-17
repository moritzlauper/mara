from pathlib import Path
from types import SimpleNamespace

from mixmatch import live_identity, songs


def test_document_path_decodes_the_live_document_and_rejects_ambiguity(monkeypatch):
    monkeypatch.setattr(live_identity.sys, "platform", "darwin")
    monkeypatch.setattr(live_identity, "_live_running", lambda: True)
    result = SimpleNamespace(returncode=0, stdout="file:///Users/music/unt%20Project/new.als\n")
    monkeypatch.setattr(live_identity.subprocess, "run", lambda *a, **kw: result)
    assert live_identity.document_path() == str(Path("/Users/music/unt Project/new.als").resolve())
    result.stdout += "file:///Users/music/another.als\n"
    assert live_identity.document_path() == ""
    result.stdout = "missing value\n"
    assert live_identity.document_path() == ""


def test_save_as_gets_fresh_state_even_with_identical_channels(tmp_path):
    root = str(tmp_path)
    channels = ["Kick", "Bass"]
    old = songs.activate_live_set(root, "mix", channels, "/tmp/mix.als")
    folder = tmp_path / "songs" / old["slug"]
    (folder / "runs" / "checkpoint.json").write_text('{"done": 100}')
    (folder / "references" / "ref.wav").write_bytes(b"audio")
    new = songs.activate_live_set(root, "mix2", channels, "/tmp/mix2.als")
    assert new["slug"] != old["slug"]
    assert not list((tmp_path / "songs" / new["slug"] / "runs").iterdir())
    assert not list((tmp_path / "songs" / new["slug"] / "references").iterdir())
    assert songs.activate_live_set(root, "", channels) is None
    assert songs.activate_live_set(root, "mix", channels, "/tmp/mix.als")["slug"] == old["slug"]
    assert (folder / "references" / "ref.wav").exists()


def _osascript(monkeypatch, returncode=0, stdout="", stderr=""):
    monkeypatch.setattr(live_identity.sys, "platform", "darwin")
    result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(live_identity.subprocess, "run", lambda *a, **kw: result)
    monkeypatch.setattr(live_identity, "_live_running", lambda: True)


def test_macos_reports_why_the_set_is_unknown(monkeypatch):
    _osascript(monkeypatch, 1, stderr="execution error: Not authorized to send Apple events to System Events. (-1743)")
    assert live_identity.identify()["problem"] == live_identity.PERMISSION
    _osascript(monkeypatch, 1, stderr="osascript is not allowed assistive access. (-25211)")
    assert live_identity.identify()["problem"] == live_identity.PERMISSION
    _osascript(monkeypatch, stdout="MARA:NOLIVE\n")
    assert live_identity.identify()["problem"] == live_identity.NOT_RUNNING
    _osascript(monkeypatch, stdout="missing value\n")
    assert live_identity.identify()["problem"] == live_identity.UNSAVED
    monkeypatch.setattr(live_identity, "_live_running", lambda: False)
    assert live_identity.identify()["problem"] == live_identity.NOT_RUNNING

    def slow(*a, **kw):
        raise live_identity.subprocess.TimeoutExpired("osascript", 15)
    _osascript(monkeypatch)
    monkeypatch.setattr(live_identity.subprocess, "run", slow)
    assert live_identity.identify()["problem"] == live_identity.SLOW
    _osascript(monkeypatch, stdout="file:///Users/music/Song%20Project/Song.als\n")
    assert live_identity.identify() == {"path": str(Path("/Users/music/Song Project/Song.als").resolve()),
                                        "name": "Song", "problem": ""}


def test_windows_reads_the_set_name_from_the_window_title(monkeypatch):
    assert live_identity.set_name_from_title("Song [Song Project] - Ableton Live 12 Suite") == "Song"
    assert live_identity.set_name_from_title("My - Mix* - Ableton Live 11 Standard") == "My - Mix"
    assert live_identity.set_name_from_title("Untitled - Ableton Live 12 Lite") == ""
    assert live_identity.set_name_from_title("Serum/1-Bass") == ""
    monkeypatch.setattr(live_identity.sys, "platform", "win32")
    monkeypatch.setattr(live_identity, "_windows_titles", lambda: ["Serum/1-Bass", "Song [Song Project] - Ableton Live 12 Suite"])
    assert live_identity.identify() == {"path": "", "name": "Song", "problem": ""}
    monkeypatch.setattr(live_identity, "_windows_titles", lambda: None)
    assert live_identity.identify()["problem"] == live_identity.NOT_RUNNING
    monkeypatch.setattr(live_identity, "_windows_titles", lambda: ["Untitled* - Ableton Live 12 Suite"])
    assert live_identity.identify()["problem"] == live_identity.UNSAVED
