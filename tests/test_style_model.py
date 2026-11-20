import builtins

import pytest

from mixmatch import autoref, style


def test_missing_torch_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(style, "ensure_laion", lambda log: None)
    real_import = builtins.__import__

    def no_torch(name, *a, **kw):
        if name == "torch":
            raise ModuleNotFoundError("No module named 'torch'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_torch)
    with pytest.raises(style.StyleModelUnavailable):
        style.Listener(log=lambda *_: None)


def test_reference_search_is_skipped_without_a_model(monkeypatch, tmp_path):
    wav = tmp_path / "song.wav"
    wav.write_bytes(b"")
    monkeypatch.setattr(autoref, "capture", lambda *a, **kw: str(wav))
    monkeypatch.setattr(autoref, "silent_file", lambda path: False)

    def unavailable(**kw):
        raise style.StyleModelUnavailable("no audio analysis model available")

    monkeypatch.setattr(autoref, "Listener", unavailable)
    lines = []
    picked, rest = autoref.find(None, 0, 16, 120, library=None, log=lines.append)
    assert picked == [] and rest["buy"] == []
    assert any("reference search skipped" in l for l in lines)


def test_laion_worker_script_ships_next_to_style():
    import os
    assert os.path.exists(os.path.join(os.path.dirname(style.__file__), "laion_worker.py"))
