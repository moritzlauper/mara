"""Turns tap frames into numbers the agent can compare.

All levels are in dB. Loudness is an approximation of short-term LUFS from the
band levels (K-weighting applied per band, bands scaled to the measured mid
power), within about a dB of a real meter; it is not a certified meter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .bands import BAND_COUNT, CENTRES, bands_between
from .taps import Frame

SILENCE = 1e-5  # mid RMS below this counts as silence and is ignored


def db(x: float | np.ndarray) -> float | np.ndarray:
    return 20 * np.log10(np.maximum(x, 1e-9))


def _k_weights() -> np.ndarray:
    """K-weighting (ITU-R BS.1770) as power weights at the band centres, from the standard 48 kHz filters."""
    from scipy.signal import freqz

    shelf = ([1.53512485958697, -2.69169618940638, 1.19839281085285], [1.0, -1.69065929318241, 0.73248077421585])
    highpass = ([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621])
    h = np.ones(len(CENTRES), dtype=complex)
    for b, a in (shelf, highpass):
        h *= freqz(b, a, worN=CENTRES, fs=48000)[1]
    return np.abs(h) ** 2


K_WEIGHT = _k_weights()


@dataclass
class Stats:
    """One track (or master, or reference) over a measurement window."""

    frames: int
    loudness: float          # ≈ short-term LUFS
    bands_db: np.ndarray     # level per band
    shape_db: np.ndarray     # bands relative to loudness: the tonal balance
    peak_db: float
    plr: float               # peak to loudness ratio, smaller = more compressed
    width_db: float          # side relative to mid
    low_side_db: float       # side below 150 Hz relative to mid level, high = bass not mono
    active: float            # share of the window in which the track played

    def summary(self) -> dict:
        return {
            "loudness_lufs": round(self.loudness, 1),
            "peak_dbfs": round(self.peak_db, 1),
            "plr_db": round(self.plr, 1),
            "width_db": round(self.width_db, 1),
            "low_side_db": round(self.low_side_db, 1),
            "active": round(self.active, 2),
            "tonal_balance_db": {band_label(i): round(float(self.shape_db[i]), 1) for i in range(0, BAND_COUNT, 2)},
        }


def band_label(i: int) -> str:
    f = CENTRES[i]
    return f"{f/1000:.1f}k" if f >= 1000 else f"{f:.0f}"


def stats(frames: list[Frame]) -> Stats | None:
    if not frames:
        return None
    live = [f for f in frames if f.m_rms > SILENCE]
    if not live:
        return None
    bands = np.array([f.bands for f in live])            # rms per band
    # The bands overlap and the tap's filter bank has gain, so their sum is louder than the signal.
    # Scale each frame so the band powers add up to the (exactly measured) mid power.
    band_sum = (bands**2).sum(axis=1)
    mid_power = np.array([f.m_rms**2 for f in live])
    scale = np.where(band_sum > 0, mid_power / np.maximum(band_sum, 1e-30), 1.0)
    power = (bands**2 * scale[:, None]).mean(axis=0)     # mean power per band
    m = np.sqrt(np.mean([f.m_rms**2 for f in live]))
    s = np.sqrt(np.mean([f.s_rms**2 for f in live]))
    s_low = np.sqrt(np.mean([f.s_low_rms**2 for f in live]))
    peak = max(f.peak for f in live)
    # Stereo loudness = power of L plus R = 2·(M² + S²); side is not band-split, so unweighted.
    k_power = float((power * K_WEIGHT).sum())
    loud = -0.691 + 10 * math.log10(max(2 * (k_power + s**2), 1e-18))
    bands_db = db(np.sqrt(power))
    return Stats(
        frames=len(live),
        loudness=loud,
        bands_db=bands_db,
        shape_db=bands_db - loud,
        peak_db=float(db(peak)),
        plr=float(db(peak)) - loud,
        width_db=float(db(s) - db(m)),
        low_side_db=float(db(s_low) - db(m)),
        active=len(live) / len(frames),
    )


def smooth(x: np.ndarray, width: int = 3) -> np.ndarray:
    """Moving average over neighbouring bands: copying a spectrum 1:1 gives harsh, resonant EQ."""
    kernel = np.ones(width) / width
    padded = np.pad(x, width // 2, mode="edge")
    return np.convolve(padded, kernel, mode="valid")


# Bands that matter for tonal balance; the extremes are mostly noise and taste.
TONAL_BANDS = bands_between(30, 16000)


def tonal_difference(mix: Stats, ref: Stats) -> np.ndarray:
    """Positive = the mix has more in that band than the reference (at equal loudness)."""
    return smooth(mix.shape_db - ref.shape_db)


@dataclass
class Comparison:
    loudness_diff: float
    tonal_diff: np.ndarray       # raw: positive = mix has more in that band than the target
    tonal_excess: np.ndarray     # what counts: beyond the tolerance, one-sided bands only when the mix is louder
    plr_diff: float
    width_diff: float
    low_side_diff: float
    peak_db: float

    def summary(self) -> dict:
        worst = sorted(TONAL_BANDS, key=lambda i: -abs(self.tonal_excess[i]))[:6]
        return {
            "loudness_diff_lu": round(self.loudness_diff, 1),
            "plr_diff_db": round(self.plr_diff, 1),
            "width_diff_db": round(self.width_diff, 1),
            "low_side_diff_db": round(self.low_side_diff, 1),
            "mix_peak_dbfs": round(self.peak_db, 1),
            "largest_tonal_differences_db": {band_label(i): round(float(self.tonal_excess[i]), 1) for i in worst
                                             if abs(self.tonal_excess[i]) > 0.05},
            "tonal_diff_db": {band_label(i): round(float(self.tonal_diff[i]), 1) for i in range(0, BAND_COUNT, 2)},
        }


def compare(mix: Stats, ref: Stats, loudness: float | None = None, tolerance: np.ndarray | None = None,
            one_sided: list[int] = ()) -> Comparison:
    diff = tonal_difference(mix, ref)
    tol = np.zeros(BAND_COUNT) if tolerance is None else tolerance
    excess = np.sign(diff) * np.maximum(0.0, np.abs(diff) - tol)
    for i in one_sided:
        excess[i] = max(0.0, excess[i])
    return Comparison(
        loudness_diff=mix.loudness - (ref.loudness if loudness is None else loudness),
        tonal_diff=diff,
        tonal_excess=excess,
        plr_diff=mix.plr - ref.plr,
        width_diff=mix.width_db - ref.width_db,
        low_side_diff=mix.low_side_db - ref.low_side_db,
        peak_db=mix.peak_db,
    )


def masking(a: Stats, b: Stats, lo: float = 30, hi: float = 8000) -> list[tuple[int, float]]:
    """Bands where both tracks are strong (within 6 dB of their own peak band) and close in level.

    Returns (band, severity 0…1) sorted by severity. Kick and bass fighting at 60 Hz is the classic case.
    """
    out = []
    a_top, b_top = float(a.bands_db.max()), float(b.bands_db.max())
    for i in bands_between(lo, hi):
        ai, bi = float(a.bands_db[i]), float(b.bands_db[i])
        if ai > a_top - 6 and bi > b_top - 6:
            closeness = max(0.0, 1 - abs(ai - bi) / 6)
            if closeness > 0:
                out.append((i, closeness))
    return sorted(out, key=lambda x: -x[1])


def objective(cmp: Comparison | None, maskings: float = 0.0, target_lufs_tolerance: float = 0.5) -> float:
    """How far the mix is from the goal, as one number (lower is better).

    Used to check every step: a change that raises this number is undone.
    """
    if cmp is None:
        return math.inf
    tonal = float(np.sqrt(np.mean(cmp.tonal_excess[TONAL_BANDS] ** 2)))
    loud = max(0.0, abs(cmp.loudness_diff) - target_lufs_tolerance)
    clip = max(0.0, cmp.peak_db + 1.0) * 4  # anything above −1 dBFS true-peak-ish is punished hard
    low_side = max(0.0, cmp.low_side_diff) * 0.5
    width = abs(cmp.width_diff) * 0.2
    dyn = abs(cmp.plr_diff) * 0.3
    return tonal + loud + clip + low_side + width + dyn + maskings
