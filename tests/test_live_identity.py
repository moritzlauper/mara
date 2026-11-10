from pathlib import Path
from types import SimpleNamespace

from mixmatch import live_identity, songs


def test_document_path_decodes_the_live_document_and_rejects_ambiguity(monkeypatch):
    monkeypatch.setattr(live_identity.sys, "platform", "darwin")
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
