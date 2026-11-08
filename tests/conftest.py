import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "remote_script"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Cache, roles file and run logs go to a temporary folder."""
    monkeypatch.setenv("MIXMATCH_CACHE", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    import mixmatch.reference as ref
    monkeypatch.setattr(ref, "CACHE", str(tmp_path / "cache"))
    yield


def write_audio(path, seconds=12.0, sr=44100, vocals=True, tilt=0.0, seed=0, loud=0.25):
    """Pink-ish noise as a stand-in for a song; `vocals` adds a strong 300 Hz–3 kHz band."""
    import soundfile as sf
    from scipy.signal import butter, sosfilt

    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    white = rng.standard_normal((n, 2))
    spec = np.fft.rfft(white, axis=0)
    f = np.fft.rfftfreq(n, 1 / sr)
    shape = 1 / np.sqrt(np.maximum(f, 20)) * (np.maximum(f, 20) / 1000) ** (tilt / 6)
    x = np.fft.irfft(spec * shape[:, None], n=n, axis=0)
    if vocals:
        sos = butter(2, [300, 3000], btype="bandpass", fs=sr, output="sos")
        voc = sosfilt(sos, rng.standard_normal(n))
        x += np.repeat(voc[:, None], 2, axis=1) * (np.std(x) * 2.5 / np.std(voc))
    x *= loud / np.max(np.abs(x))
    sf.write(path, x.astype(np.float32), sr)
    return path
