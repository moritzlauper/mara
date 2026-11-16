"""The character of a finished mix from its audio file: loudness, density, tonal balance, stereo, kick and bass.

Made for requests like «make it loud like this», «mix it like this» or «make the kick like this track»: the
reference and a recording of the user's mix go through the same analysis, so the numbers compare like for like.

There is no stem separation. Kick and bass come from a full mix by timing: the kick is what rises sharply in
35–150 Hz at an onset, the bass is what stays there between the hits. That is robust for four-on-the-floor,
trap, DnB and most pop; with a sustained 808 that *is* the kick it describes the 808, and under a bass louder
than the kick the hits get lost. Every number is a measurement, not a verdict: two tracks can differ on purpose.
"""
from __future__ import annotations

import math

import numpy as np

HOP_S = 0.01           # envelope resolution for transients
LOW = (35.0, 150.0)    # kick and bass fundamentals
MIN_GAP_S = 0.2        # two kicks are at least this far apart (300 BPM sixteenths would not be a kick)
SPECTRUM_BANDS = {     # what an onset adds, per region
    "sub_35_70": (35, 70), "low_70_150": (70, 150), "body_150_400": (150, 400),
    "mid_400_2k": (400, 2000), "click_2k_6k": (2000, 6000), "air_6k_12k": (6000, 12000),
}


def _r(x, n=1):
    return None if x is None or not np.isfinite(x) else round(float(x), n)


def _db(x):
    return 20 * np.log10(np.maximum(x, 1e-9))


def _envelope(x: np.ndarray, sr: int, hop_s: float = HOP_S, window: int = 3) -> np.ndarray:
    """RMS per hop over `window` hops, centred: one 10 ms frame alone ripples with a 50 Hz waveform."""
    hop = max(1, int(sr * hop_s))
    n = len(x) // hop
    if n == 0:
        return np.zeros(0)
    power = (x[: n * hop].reshape(n, hop) ** 2).mean(axis=1)
    if window > 1:
        power = np.convolve(np.pad(power, window // 2, mode="edge"), np.ones(window) / window, mode="valid")
    return np.sqrt(power)


# ── loudness ───────────────────────────────────────────────────────────────
def loudness(data: np.ndarray, sr: int) -> dict:
    import pyloudnorm as pyln
    from scipy.signal import resample_poly

    meter = pyln.Meter(sr)
    integrated = meter.integrated_loudness(data)
    # Short-term loudness (3 s windows, 1 s hop) for the loudest part and the loudness range.
    win, hop = int(3 * sr), int(sr)
    blocks = []
    for i in range(0, max(1, len(data) - win + 1), hop):
        seg = data[i:i + win]
        if len(seg) >= int(0.4 * sr):
            v = meter.integrated_loudness(seg) if len(seg) >= win else -math.inf
            if np.isfinite(v):
                blocks.append(v)
    blocks = np.array(blocks)
    gated = blocks[(blocks > -70) & (blocks > integrated - 20)] if len(blocks) and np.isfinite(integrated) else blocks
    loud = None
    if len(blocks):
        top = np.sort(blocks)[::-1][: max(1, int(len(blocks) * 0.3))]
        top = top[top >= top[0] - 3.0]
        loud = 10 * np.log10(np.mean(10 ** (top / 10)))
    sample_peak = float(np.max(np.abs(data))) if data.size else 0.0
    over = resample_poly(data, 4, 1, axis=0) if len(data) < sr * 900 else data
    true_peak = float(np.max(np.abs(over))) if data.size else 0.0
    return {
        "integrated_lufs": _r(integrated),
        "loud_part_lufs": _r(loud),
        "short_term_max_lufs": _r(blocks.max()) if len(blocks) else None,
        "true_peak_dbtp": _r(_db(true_peak)),
        "sample_peak_dbfs": _r(_db(sample_peak)),
        "plr_db": _r(_db(true_peak) - loud) if loud is not None else None,
        "loudness_range_lu": _r(np.percentile(gated, 95) - np.percentile(gated, 10)) if len(gated) > 2 else None,
    }


def density(mono: np.ndarray, sr: int) -> dict:
    """How much the short peaks stick out of the body: small = dense, clipped or heavily limited."""
    env = _db(_envelope(mono, sr, window=1))
    per = int(0.4 / HOP_S)
    crest = [seg.max() - 10 * np.log10(np.mean(10 ** (seg / 10)))
             for seg in (env[i:i + per] for i in range(0, len(env) - per + 1, per)) if seg.max() > -60]
    return {"short_crest_db": _r(np.median(crest)) if crest else None}


# ── kick and bass ──────────────────────────────────────────────────────────
def _onsets(low_env_db: np.ndarray) -> np.ndarray:
    from scipy.signal import find_peaks

    if len(low_env_db) < 10:
        return np.zeros(0, dtype=int)
    rise = low_env_db - np.minimum.reduce([np.roll(low_env_db, k) for k in (1, 2, 3)])
    rise[:3] = 0
    floor = low_env_db.max() - 30
    peaks, _ = find_peaks(rise, height=2.0, distance=int(MIN_GAP_S / HOP_S))
    peaks = peaks[low_env_db[np.minimum(peaks + 3, len(low_env_db) - 1)] > floor]
    if len(peaks) < 4:
        return peaks
    # A hit peaks and falls again; a sidechained bass coming back rises and stays.
    n = len(low_env_db)
    top = [o + int(np.argmax(low_env_db[o:o + 6])) for o in peaks]
    falls = np.array([low_env_db[p:min(n, p + 30)].min() <= low_env_db[p] - 3.0 for p in top], dtype=bool)
    peaks, top = peaks[falls], np.array(top)[falls]
    if len(peaks) < 4:
        return peaks
    # Relative to the clear hits: a loud bass makes every rise smaller, a fixed threshold would miss the kicks.
    # Beating between a kick tail and the bass also rises and falls, but well below the level of a hit.
    strong = np.percentile(rise[peaks], 90)
    loud = np.percentile(low_env_db[top], 90)
    return peaks[(rise[peaks] >= max(2.0, 0.4 * strong)) & (low_env_db[top] >= loud - 4.0)]


def _band_power(seg: np.ndarray, sr: int, n_fft: int) -> tuple[np.ndarray, np.ndarray]:
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n=n_fft)) ** 2
    return np.fft.rfftfreq(n_fft, 1 / sr), spec


def kick_and_bass(mono: np.ndarray, sr: int, mix_lufs: float | None) -> dict:
    from scipy.signal import butter, sosfilt

    low = sosfilt(butter(4, LOW, btype="bandpass", fs=sr, output="sos"), mono)
    env = _envelope(low, sr, window=3)
    env_db = _db(env)
    onsets = _onsets(env_db)
    if len(onsets) < 4:
        return {"kick": None, "bass": None,
                "note": "No regular kick found in 35–150 Hz (no kick, or a sustained 808 without clear hits)."}
    hop = int(sr * HOP_S)
    peaks, between, decays, fundamentals = [], [], [], []
    added = {k: [] for k in SPECTRUM_BANDS}
    n_fft = 1 << int(math.ceil(math.log2(sr)))     # ~1 Hz resolution for the fundamental
    for j, o in enumerate(onsets):
        nxt = onsets[j + 1] if j + 1 < len(onsets) else min(len(env_db), o + int(0.6 / HOP_S))
        if nxt - o < 6:
            continue
        p = o + int(np.argmax(env_db[o:o + 6]))
        peaks.append(env_db[p])
        gap = env_db[o + int((nxt - o) * 0.5): o + int((nxt - o) * 0.9)]
        if len(gap):
            between.append(np.median(gap))
        below = np.nonzero(env_db[p:nxt] <= env_db[p] - 10)[0]
        if len(below):
            decays.append(below[0] * HOP_S * 1000)
        start = o * hop
        body = mono[start + int(0.04 * sr): start + int(0.16 * sr)]
        if len(body) == int(0.16 * sr) - int(0.04 * sr):
            f, s = _band_power(body, sr, n_fft)
            sel = (f >= LOW[0]) & (f <= LOW[1])
            fundamentals.append(f[sel][np.argmax(s[sel])])
        hit, pre = mono[start: start + int(0.05 * sr)], mono[max(0, start - int(0.06 * sr)): start - int(0.01 * sr)]
        if len(hit) == int(0.05 * sr) and len(pre) == int(0.05 * sr):
            f, s_hit = _band_power(hit, sr, 4096)
            _, s_pre = _band_power(pre, sr, 4096)
            gain = np.maximum(s_hit - s_pre, 0)
            for name, (lo, hi) in SPECTRUM_BANDS.items():
                added[name].append(gain[(f >= lo) & (f < hi)].sum())
    peaks, between = np.array(peaks), np.array(between)
    added_db = {k: 10 * np.log10(max(np.median(v), 1e-18)) for k, v in added.items() if v}
    ref = 10 * np.log10(max(10 ** (added_db.get("sub_35_70", -180) / 10) + 10 ** (added_db.get("low_70_150", -180) / 10),
                            1e-18))
    duration = len(mono) / sr
    kick = {
        "hits": int(len(onsets)),
        "hits_per_minute": _r(len(onsets) / duration * 60, 0),
        "fundamental_hz": _r(np.median(fundamentals), 0) if fundamentals else None,
        "decay_ms_to_-10db": _r(np.median(decays), 0) if len(decays) >= len(peaks) / 2 else None,
        "rings_into_next_hit": len(decays) < len(peaks) / 2,
        "level_vs_mix_db": _r(np.median(peaks) - mix_lufs) if mix_lufs is not None else None,
        "over_bass_db": _r(np.median(peaks[: len(between)] - between)) if len(between) else None,
        # What one hit adds above the music before it, relative to its own low end (35–150 Hz):
        "spectrum_vs_low_db": {k: _r(v - ref) for k, v in added_db.items()
                               if k not in ("sub_35_70", "low_70_150")},
        "sub_vs_low_db": _r(added_db.get("sub_35_70", -180) - added_db.get("low_70_150", -180)),
    }
    gaps = []
    for j in range(len(onsets) - 1):
        o, nxt = onsets[j], onsets[j + 1]
        gaps.append(mono[(o + int((nxt - o) * 0.5)) * hop: (o + int((nxt - o) * 0.9)) * hop])
    tail = np.concatenate(gaps) if gaps else np.zeros(0)
    bass = None
    if len(tail) > sr * 0.5:
        f, s = _band_power(tail[: sr * 20], sr, 1 << int(math.ceil(math.log2(min(len(tail), sr * 20)))))
        sub = s[(f >= 35) & (f < 70)].sum()
        upper = s[(f >= 70) & (f < 150)].sum()
        bass = {"level_vs_mix_db": _r(np.median(between) - mix_lufs) if mix_lufs is not None and len(between) else None,
                "sub_vs_low_db": _r(10 * np.log10(max(sub, 1e-18) / max(upper, 1e-18)))}
    return {"kick": kick, "bass": bass}


# ── everything ─────────────────────────────────────────────────────────────
ASPECTS = ("loudness", "tonal", "stereo", "kick", "bass")


def describe(path: str) -> dict:
    """Cached per file (path, size, modification time)."""
    import json
    import os

    from .reference import CACHE, _key

    cached = _key(path, "character-v1")
    if os.path.exists(cached):
        with open(cached) as f:
            return json.load(f)
    out = _describe(path)
    os.makedirs(CACHE, exist_ok=True)
    with open(cached, "w") as f:
        json.dump(out, f)
    return out


def _describe(path: str) -> dict:
    from .analysis import TONAL_BANDS, band_label
    from .reference import load, profile

    data, sr = load(path)
    data = data.astype(np.float64)
    if not data.size or float(np.max(np.abs(data))) < 1e-5:
        raise ValueError(f"{path} is silent")
    mono = data.mean(axis=1)
    loud = loudness(data, sr)
    loud.update(density(mono, sr))
    prof = profile(path)
    kb = kick_and_bass(mono, sr, loud["loud_part_lufs"] or loud["integrated_lufs"])
    out = {
        "seconds": _r(len(mono) / sr, 1),
        "loudness": loud,
        # Band level relative to loudness in the loudest part: the tonal balance, independent of volume.
        "tonal": {band_label(i): _r(prof.shape_db[i]) for i in TONAL_BANDS[::2]},
        "stereo": {"width_db": _r(prof.width_db), "low_side_db": _r(prof.low_side_db)},
        "kick": kb.get("kick"),
        "bass": kb.get("bass"),
    }
    if kb.get("note"):
        out["note"] = kb["note"]
    return out


def difference(mix: dict, ref: dict) -> dict:
    """mix minus reference for every number both have; positive = the mix has more."""
    out = {}
    for k, v in ref.items():
        m = mix.get(k) if isinstance(mix, dict) else None
        if isinstance(v, dict) and isinstance(m, dict):
            d = difference(m, v)
            if d:
                out[k] = d
        elif isinstance(v, (int, float)) and isinstance(m, (int, float)) and not isinstance(v, bool):
            out[k] = round(m - v, 1)
    return out


def only(described: dict, aspects) -> dict:
    if not aspects:
        return described
    unknown = set(aspects) - set(ASPECTS)
    if unknown:
        raise ValueError(f"Unknown aspect(s) {sorted(unknown)}; choose from {list(ASPECTS)}")
    keep = set(aspects) | {"seconds", "note"}
    return {k: v for k, v in described.items() if k in keep}
