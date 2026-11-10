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
            # Only the bands the objective scores (30 Hz–16 kHz): above that is air and taste, not measured progress.
            "tonal_balance_db": {band_label(i): round(float(self.shape_db[i]), 1) for i in TONAL_BANDS[::2]},
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
            "tonal_diff_db": {band_label(i): round(float(self.tonal_diff[i]), 1) for i in TONAL_BANDS[::2]},
        }


def compare(mix: Stats, ref: Stats, loudness: float | None = None, tolerance: np.ndarray | None = None,
            one_sided: list[int] = (), offset: np.ndarray | None = None, anchor: list[int] = ()) -> Comparison:
    """`anchor`: bands whose mean difference is taken as level 0 (instead of equal loudness);
    `offset`: per band, how much more (+) or less (−) the mix is expected to have than the reference."""
    diff = tonal_difference(mix, ref)
    if len(anchor):
        diff = diff - float(np.mean(diff[list(anchor)]))
    if offset is not None:
        diff = diff - offset
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


@dataclass
class MaskingFrames:
    times: np.ndarray
    bands_db: np.ndarray
    level_db: np.ndarray
    active: np.ndarray


def masking_frames(frames: list[Frame], gain_db: float = 0.0) -> MaskingFrames:
    """Prepare each channel once, retaining silence and time instead of averaging it away.

    Taps measure before faders. Use the same band calibration as stats(), then apply
    the channel/group gains. Quiet tails (>24 dB below the channel's peak RMS) do not
    establish a collision. This remains a 10 Hz heuristic, not a perceptual meter.
    """
    valid = {f.t: f for f in frames if len(f.bands) == BAND_COUNT
             and np.isfinite([f.t, f.m_rms, *f.bands]).all() and f.m_rms >= 0
             and min(f.bands) >= 0}
    ordered = [valid[t] for t in sorted(valid)]
    if not ordered or not math.isfinite(gain_db):
        return MaskingFrames(np.empty(0), np.empty((0, BAND_COUNT)), np.empty(0), np.empty(0, dtype=bool))
    rms = np.array([f.m_rms for f in ordered])
    powers = np.array([f.bands for f in ordered]) ** 2
    powers *= (rms ** 2 / np.maximum(powers.sum(axis=1), 1e-30))[:, None]
    levels = db(rms) + gain_db
    active = levels > max(-70.0, float(levels.max()) - 24.0)
    return MaskingFrames(np.array([f.t for f in ordered]), 10 * np.log10(np.maximum(powers, 1e-18)) + gain_db,
                         levels, active)


def masking_alignment(foreground: MaskingFrames, background: MaskingFrames) -> tuple[np.ndarray, np.ndarray, float]:
    """Shared observation coverage, also used to reject improvements caused by lost taps."""
    if min(len(foreground.times), len(background.times)) < 8:
        return np.empty(0, dtype=int), np.empty(0, dtype=int), 0.0
    # Mutual nearest neighbours avoid reusing a frame from a slower/gappy tap.
    def nearest(a, b):
        right = np.searchsorted(b, a).clip(0, len(b) - 1)
        left = (right - 1).clip(0)
        return np.where(abs(b[left] - a) <= abs(b[right] - a), left, right)

    fi = np.arange(len(foreground.times))
    bi = nearest(foreground.times, background.times)
    reverse = nearest(background.times, foreground.times)
    matched = (reverse[bi] == fi) & (abs(foreground.times - background.times[bi]) <= 0.060001)
    fi, bi = fi[matched], bi[matched]
    # A common dropout must not look like complete coverage merely because both
    # channels lost the same frames. The taps nominally send ten frames/second.
    duration = max(foreground.times[-1], background.times[-1]) - min(foreground.times[0], background.times[0])
    expected = max(len(foreground.times), len(background.times), duration * 10 + 1)
    coverage = min(1.0, len(fi) / expected)
    return fi, bi, coverage


def masking(foreground: MaskingFrames, background: MaskingFrames, alignment=None) -> dict | None:
    """Time-aligned, directional masking: how much the background covers the foreground.

    Match nearest frames one-to-one within 60 ms; never interpolate through packet
    gaps. Require 60% coverage and at least three collisions, so one stray frame is
    not an EQ instruction. Severity is exposure during foreground activity, making
    a short kick as important as a sustained vocal. A louder masker is worse, not
    magically harmless as in the old ±6 dB level-similarity test.
    """
    fi, bi, coverage = masking_alignment(foreground, background) if alignment is None else alignment
    if len(fi) < 8 or coverage < 0.6:
        return None
    fa, ba = foreground.active[fi], background.active[bi]
    joint = fa & ba
    if joint.sum() < 3:
        return None
    a, b = foreground.bands_db[fi], background.bands_db[bi]
    # Gradual relevance avoids a hard threshold where a tiny EQ cut makes a band disappear.
    aw = np.clip((a - a.max(axis=1)[:, None] + 9) / 9, 0, 1)
    bw = np.clip((b - b.max(axis=1)[:, None] + 9) / 9, 0, 1)
    pressure = np.clip((b - a + 12) / 18, 0, 1)
    evidence = pressure * np.sqrt(aw * bw)
    evidence[~joint] = 0
    evidence[(a < -70) | (b < -70)] = 0
    outside = sorted(set(range(BAND_COUNT)) - set(bands_between(30, 8000)))
    evidence[:, outside] = 0
    evidence[:, (evidence > 0.05).sum(axis=0) < 3] = 0
    scores = evidence.sum(axis=0) / max(1, int(fa.sum()))
    ranked = sorted(range(BAND_COUNT), key=lambda i: -scores[i])
    if scores[ranked[0]] < 0.05:
        return None
    # Show up to three separated regions, but score every moment and every pair.
    regions = []
    for i in ranked:
        if scores[i] >= 0.05 and all(abs(i - j) > 1 for j in regions):
            regions.append(i)
        if len(regions) == 3:
            break
    ducking = None
    if fa.sum() >= 3 and (~fa).sum() >= 3:
        ducking = round(float(np.median(background.level_db[bi][~fa])
                              - np.median(background.level_db[bi][fa])), 1)
    # Only contiguous samples establish repeated attacks, not missing packets.
    attacks = int(np.sum(fa[1:] & ~fa[:-1] & (np.diff(foreground.times[fi]) < 0.16)))
    return {
        "freq_hz": round(CENTRES[ranked[0]]),
        "bands": [{"freq_hz": round(CENTRES[i]), "severity": round(float(scores[i]), 3)} for i in regions],
        "severity": float(evidence.max(axis=1)[fa].mean()),
        "overlap_fraction": round(float(joint.mean()), 3),
        "foreground_active_fraction": round(float(fa.mean()), 3),
        "collision_frames": int((evidence.max(axis=1) > 0.05).sum()),
        "coverage": round(coverage, 3),
        "intermittent": bool(fa.mean() < 0.8 and attacks >= 2),
        "observed_ducking_db": ducking,
        "foreground_level_dbfs": round(float(np.percentile(foreground.level_db[fi][fa], 90)), 2),
    }


def objective_parts(cmp: Comparison | None, maskings: float = 0.0, target_lufs_tolerance: float = 0.5) -> dict:
    """The parts the objective adds up, to show where the remaining distance comes from."""
    if cmp is None:
        return {"masking": maskings}
    return {
        "tonal": float(np.sqrt(np.mean(cmp.tonal_excess[TONAL_BANDS] ** 2))),
        "loudness": max(0.0, abs(cmp.loudness_diff) - target_lufs_tolerance),
        "clipping": max(0.0, cmp.peak_db + 1.0) * 4,  # anything above −1 dBFS true-peak-ish is punished hard
        "low_side": max(0.0, cmp.low_side_diff) * 0.5,
        "width": abs(cmp.width_diff) * 0.2,
        "dynamics": abs(cmp.plr_diff) * 0.3,
        "masking": maskings,
    }


def objective(cmp: Comparison | None, maskings: float = 0.0, target_lufs_tolerance: float = 0.5) -> float:
    """How far the mix is from the goal, as one number (lower is better).

    Used to check every step: a change that raises this number is undone.
    """
    if cmp is None:
        return math.inf
    return sum(objective_parts(cmp, maskings, target_lufs_tolerance).values())
