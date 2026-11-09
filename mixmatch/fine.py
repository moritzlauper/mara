"""Finer than third-octave bands: narrow resonances in a recording.

The taps measure 31 bands, enough for tonal balance but blind to a single
ringing frequency. Here a recording of a channel (or the master) is analysed
with a high-resolution spectrum: a resonance is a peak that stands out from
the spectrum around it (smoothed over a third of an octave) by several dB.
"""
from __future__ import annotations

import numpy as np


def _smooth_log(freqs: np.ndarray, power: np.ndarray, fraction: float) -> np.ndarray:
    """Mean power over ±fraction/2 octave around every frequency, in dB (averaging dB would hide narrow peaks)."""
    logf = np.log2(np.maximum(freqs, 1e-9))
    cum = np.concatenate([[0.0], np.cumsum(power)])
    lo = np.searchsorted(logf, logf - fraction / 2)
    hi = np.searchsorted(logf, logf + fraction / 2, side="right")
    return 10 * np.log10((cum[hi] - cum[lo]) / np.maximum(hi - lo, 1) + 1e-20)


NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_of(hz: float) -> tuple[str, int, int]:
    """Nearest note name, its pitch class and the offset in cents («G#4», 8, +12)."""
    midi = 69 + 12 * np.log2(hz / 440.0)
    n = int(round(midi))
    return f"{NOTES[n % 12]}{n // 12 - 1}", n % 12, int(round((midi - n) * 100))


def resonances(mono: np.ndarray, sr: int, fmin: float = 80.0, fmax: float = 12000.0, threshold_db: float = 4.0,
               top: int = 6, key_pcs: set[int] | None = None) -> list[dict]:
    """Narrow peaks that stick out of their surroundings: [{"hz", "prominence_db", "q", "note"}], strongest first.

    With `key_pcs` (pitch classes of the song's key), peaks on those notes are left out: in tonal music the
    sharpest peaks are the notes being played, and cutting them would cut the music.
    """
    from scipy.signal import find_peaks, welch

    if len(mono) < sr:  # under a second says nothing
        return []
    nper = 1 << int(np.log2(min(len(mono) // 4, 32768)))
    freqs, power = welch(mono, sr, nperseg=nper)
    keep = (freqs >= fmin) & (freqs <= fmax)
    f, pw = freqs[keep], power[keep]
    fine = _smooth_log(f, pw, 1 / 48)          # a little smoothing against noise
    broad = _smooth_log(f, pw, 1 / 3)           # the surroundings
    excess = fine - broad
    min_gap = max(1, int(len(f) / (np.log2(fmax / fmin) * 12)))   # roughly a semitone apart
    peaks, props = find_peaks(excess, height=threshold_db, distance=min_gap)
    out = []
    for i in peaks:
        half = excess[i] - 3.0                  # width where the peak is 3 dB down
        lo, hi = i, i
        while lo > 0 and excess[lo] > half:
            lo -= 1
        while hi < len(excess) - 1 and excess[hi] > half:
            hi += 1
        width = max(f[hi] - f[lo], f[1] - f[0])
        name, pc, cents = note_of(float(f[i]))
        if key_pcs is not None and pc in key_pcs and abs(cents) <= 35:
            continue
        out.append({"hz": round(float(f[i]), 1), "prominence_db": round(float(excess[i]), 1),
                    "q": round(float(min(18.0, f[i] / width)), 1), "note": f"{name} {cents:+d} ct"})
    return sorted(out, key=lambda r: -r["prominence_db"])[:top]


MINOR, MAJOR = (0, 2, 3, 5, 7, 8, 10), (0, 2, 4, 5, 7, 9, 11)


def key_pitch_classes(tonic: str | None, mode: str | None) -> set[int] | None:
    if not tonic:
        return None
    flats = {"Db": 1, "Eb": 3, "Gb": 6, "Ab": 8, "Bb": 11 - 1, "Cb": 11}
    root = flats.get(tonic, NOTES.index(tonic) if tonic in NOTES else None)
    if root is None:
        return None
    return {(root + i) % 12 for i in (MINOR if mode == "minor" else MAJOR)}


def analyse(path: str, harmony: dict | None = None) -> dict:
    """Resonances of a recording file (any rate; mono mix), leaving out the notes of the song's key."""
    import soundfile as sf

    data, sr = sf.read(path, dtype="float32", always_2d=True)
    pcs = key_pitch_classes(harmony.get("tonic"), harmony.get("mode")) if harmony else None
    return {"resonances": resonances(data.mean(axis=1), sr, key_pcs=pcs)}
