import numpy as np

from mixmatch.fine import resonances


def test_finds_a_ringing_frequency_in_noise():
    sr = 44100
    rng = np.random.default_rng(1)
    t = np.arange(sr * 8) / sr
    noise = rng.standard_normal(len(t)) * 0.1
    tone = 0.05 * np.sin(2 * np.pi * 3150 * t)          # a resonance at 3150 Hz
    found = resonances((noise + tone).astype(np.float32), sr)
    assert found and abs(found[0]["hz"] - 3150) < 10 and found[0]["prominence_db"] > 6 and found[0]["q"] > 5


def test_plain_noise_has_no_resonances():
    sr = 44100
    noise = np.random.default_rng(2).standard_normal(sr * 8).astype(np.float32) * 0.1
    assert resonances(noise, sr) == []


def test_notes_of_the_key_are_not_resonances():
    from mixmatch.fine import key_pitch_classes

    sr = 44100
    t = np.arange(sr * 8) / sr
    noise = np.random.default_rng(3).standard_normal(len(t)) * 0.1
    music = noise + 0.05 * np.sin(2 * np.pi * 415.3 * t) + 0.05 * np.sin(2 * np.pi * 3080 * t)   # Ab4 and 3.08 kHz (near G7)
    pcs = key_pitch_classes("Eb", "minor")
    assert pcs == {3, 5, 6, 8, 10, 11, 1}
    found = resonances(music.astype(np.float32), sr, key_pcs=pcs)
    assert len(found) == 1 and abs(found[0]["hz"] - 3080) < 40     # Ab is in Eb minor, G is not
