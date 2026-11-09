"""Reference tracks as audio files: analysis, vocal removal, and the combined target.

The references are files you already have (WAV, AIFF, FLAC, MP3), so nothing
from your project has to be exported. Each one is measured the way the taps
measure your mix (third-octave bands, mid/side, 100 ms frames), using its
loudest part (drop or chorus), not the intro.

Beat mode: your beat has no vocals, the references do. The target is still the
full song, the real master: a vocal split (Demucs) changes the rest of the song
too (darker highs, narrower, heavier bass) and the mix would chase those
artefacts. Instead the comparison is anchored where the voice does not sit
(below 200 Hz, above 5 kHz: the same instruments in song and beat), and in the
vocal range 200 Hz–5 kHz the beat may sit up to 4 dB under the song. That is the
room for a vocal; louder than the song, or hollower than that, counts.

Several references: the target is their average, and where they disagree, the
tolerance gets wider (half their spread per band), so no single track dictates
the sound.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

import numpy as np

from .analysis import TONAL_BANDS, Stats, stats
from .bands import CENTRES, bands_between
from .taps import Frame

CACHE = os.environ.get("MIXMATCH_CACHE", os.path.expanduser("~/.cache/mixmatch"))
HOP = 0.1          # seconds, like the taps
BLOCK = 3.0        # seconds per block when looking for the loudest part
LOUDEST_SHARE = 0.3
LOUD_WITHIN_LU = 3.0   # a block counts only this close to the loudest one: no fades, intros or breakdowns
SHORT_CLIP_S = 60.0    # shorter files (e.g. 30 s previews) count half, and not for loudness and dynamics
VOCAL_RANGE = bands_between(200, 5000)
VOCAL_ROOM_DB = 4.0   # beat mode: how far under the song the vocal range may sit


# ── measuring a file like a tap would ──────────────────────────────────────
def load(path: str) -> tuple[np.ndarray, int]:
    import soundfile as sf

    data, sr = sf.read(path, dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    return data[:, :2], sr


def file_frames(path: str) -> list[Frame]:
    from scipy.signal import butter, iirpeak, lfilter, sosfilt

    from .bands import Q

    data, sr = load(path)
    left, right = data[:, 0].astype(np.float64), data[:, 1].astype(np.float64)
    mid, side = (left + right) / 2, (left - right) / 2
    hop = int(sr * HOP)
    n = len(mid) // hop
    if n == 0:
        raise ValueError(f"{path} is too short")

    def frame_rms(x):
        x = x[: n * hop].reshape(n, hop)
        return np.sqrt((x**2).mean(axis=1))

    bands = []
    for fc in CENTRES:
        if fc >= sr / 2 * 0.99:
            bands.append(np.zeros(n))
            continue
        # Two-pole resonators like the tap's fffb~, so mix and reference have the same band shape.
        b, a = iirpeak(fc, Q, fs=sr)
        bands.append(frame_rms(lfilter(b, a, mid)))
    bands = np.array(bands).T                                     # n × 31
    s_low = frame_rms(sosfilt(butter(2, 150, btype="lowpass", fs=sr, output="sos"), side))
    m_rms, s_rms = frame_rms(mid), frame_rms(side)
    peaks = np.abs(data[: n * hop]).reshape(n, hop, 2).max(axis=(1, 2))
    return [Frame(i * HOP, float(m_rms[i]), float(s_rms[i]), float(s_low[i]), float(peaks[i]), list(bands[i]))
            for i in range(n)]


def loudest_part(frames: list[Frame]) -> list[Frame]:
    """Frames of the loudest blocks: the drop or chorus a mix is usually judged by."""
    per = int(BLOCK / HOP)
    blocks = [frames[i:i + per] for i in range(0, len(frames) - per + 1, per)]
    scored = [(st.loudness, b) for b in blocks if (st := stats(b)) is not None]
    if not scored:
        return frames
    scored.sort(key=lambda x: -x[0])
    keep = scored[: max(1, int(len(scored) * LOUDEST_SHARE))]
    keep = [k for k in keep if k[0] >= keep[0][0] - LOUD_WITHIN_LU]
    return [f for _, b in keep for f in b]


def _seconds(path: str) -> float:
    try:
        import soundfile as sf

        info = sf.info(path)
        return info.frames / info.samplerate
    except Exception:
        return 300.0


# ── cache ──────────────────────────────────────────────────────────────────
def _key(path: str, what: str) -> str:
    st = os.stat(path)
    h = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime}|{what}|v3".encode()).hexdigest()[:16]
    return os.path.join(CACHE, f"{h}.json")


def _to_dict(s: Stats) -> dict:
    d = dict(s.__dict__)
    d["bands_db"], d["shape_db"] = s.bands_db.tolist(), s.shape_db.tolist()
    return d


def _from_dict(d: dict) -> Stats:
    d = dict(d)
    d["bands_db"], d["shape_db"] = np.array(d["bands_db"]), np.array(d["shape_db"])
    return Stats(**d)


def profile(path: str) -> Stats:
    """Measured loudest part of an audio file, cached."""
    cached = _key(path, "profile")
    if os.path.exists(cached):
        with open(cached) as f:
            return _from_dict(json.load(f))
    st = stats(loudest_part(file_frames(path)))
    if st is None:
        raise ValueError(f"{path} is silent")
    os.makedirs(CACHE, exist_ok=True)
    with open(cached, "w") as f:
        json.dump(_to_dict(st), f)
    return st


# ── vocal removal ──────────────────────────────────────────────────────────
def demucs_available() -> bool:
    return importlib.util.find_spec("demucs") is not None


def instrumental(path: str, log=print) -> str | None:
    """Path of the reference without vocals (Demucs, two stems), made once and cached."""
    if not demucs_available():
        return None
    out = os.path.join(CACHE, "stems")
    name = os.path.splitext(os.path.basename(path))[0]
    target = os.path.join(out, "htdemucs", name, "no_vocals.wav")
    if not os.path.exists(target):
        log(f"  separating vocals from {os.path.basename(path)} (once, takes a minute) …")
        cmd = [sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-o", out, path]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            # The GPU backend can crash (e.g. MPS on older macOS); the CPU is slower but works everywhere.
            log("  GPU separation failed, separating on the CPU (a minute or two) …")
            subprocess.run(cmd[:3] + ["-d", "cpu"] + cmd[3:], check=True, capture_output=True)
    return target if os.path.exists(target) else None


# ── the combined target ────────────────────────────────────────────────────
@dataclass
class Reference:
    path: str
    full: Stats
    inst: Stats | None = None
    seconds: float = 300.0   # length of the file; short clips weigh less

    @property
    def short(self) -> bool:
        return self.seconds < SHORT_CLIP_S


@dataclass
class Target:
    """What the master should look like, plus how strict each band is."""

    stats: Stats                      # tonal balance, width, dynamics to aim for
    loudness: float                   # LUFS to aim for
    tolerance: np.ndarray             # per band, dB of difference that does not count
    one_sided: list[int] = field(default_factory=list)  # bands where only "mix louder than reference" counts
    description: str = ""
    offset: np.ndarray | None = None  # per band: expected mix minus reference (beat mode: room for a vocal)
    anchor: list[int] = field(default_factory=list)     # bands that set the level of the comparison


def _mean_stats(items: list[Stats], weights: list[float] | None = None) -> Stats:
    w = np.ones(len(items)) if weights is None else np.asarray(weights, dtype=float)
    avg = lambda xs: np.average(np.asarray(xs, dtype=float), axis=0, weights=w)
    mean = lambda a: float(avg([getattr(s, a) for s in items]))
    return Stats(frames=sum(s.frames for s in items), loudness=mean("loudness"),
                 bands_db=avg([s.bands_db for s in items]), shape_db=avg([s.shape_db for s in items]),
                 peak_db=mean("peak_db"), plr=mean("plr"), width_db=mean("width_db"),
                 low_side_db=mean("low_side_db"), active=1.0)


class ReferenceSet:
    def __init__(self, paths: list[str], log=print):
        self.log = log
        self.paths = list(paths)
        self.refs: list[Reference] = []
        for p in paths:
            self.refs.append(Reference(p, profile(p), seconds=_seconds(p)))
            r = self.refs[-1]
            note = f" ({r.seconds:.0f} s clip: half weight, not used for loudness)" if r.short else ""
            self.log(f"  reference {os.path.basename(p)}: {r.full.loudness:.1f} LUFS{note}")

    def separate(self) -> bool:
        """Add the instrumental version of every reference. False if Demucs is missing."""
        if not demucs_available():
            return False
        for r in self.refs:
            if r.inst is None:
                path = instrumental(r.path, self.log)
                r.inst = profile(path) if path else None
        return all(r.inst for r in self.refs)

    def target(self, beat: bool, loudness_from: str = "full") -> Target:
        """The combined target. `loudness_from` is kept for old callers: everything comes from the full songs."""
        full = [r.full for r in self.refs]
        weights = [0.5 if r.short else 1.0 for r in self.refs]
        target = _mean_stats(full, weights)
        # Loudness and dynamics only from whole tracks when there are any: a clip has fades and maybe no drop.
        whole = [r.full for r in self.refs if not r.short] or full
        level = _mean_stats(whole)
        target = Stats(**{**target.__dict__, "loudness": level.loudness, "plr": level.plr, "peak_db": level.peak_db})
        spread = np.std([s.shape_db for s in full], axis=0) if len(full) > 1 else np.zeros(len(CENTRES))
        tolerance = 0.5 + spread / 2
        offset, anchor = None, []
        if beat:
            # Room for a vocal: the beat may sit 0 to VOCAL_ROOM_DB under the song in the vocal range.
            offset = np.zeros(len(CENTRES))
            offset[VOCAL_RANGE] = -VOCAL_ROOM_DB / 2
            tolerance = tolerance.copy()
            tolerance[VOCAL_RANGE] += VOCAL_ROOM_DB / 2
            # The comparison is smoothed over three bands, so the edge of the vocal range bleeds one band out.
            edges = [VOCAL_RANGE[0] - 1, VOCAL_RANGE[-1] + 1]
            tolerance[edges] += VOCAL_ROOM_DB / 3
            anchor = [i for i in TONAL_BANDS if i not in VOCAL_RANGE and i not in edges]
            how = (f"full references; vocal range 200 Hz–5 kHz may sit up to {VOCAL_ROOM_DB:.0f} dB under the song "
                   "(room for a vocal), level set by the bands outside it")
        else:
            how = "full references"
        clips = sum(r.short for r in self.refs)
        count = f"{len(full)} reference(s)" + (f", {clips} short clip(s) at half weight" if clips else "")
        return Target(stats=target, loudness=level.loudness, tolerance=tolerance,
                      one_sided=[], offset=offset, anchor=anchor,
                      description=f"{count}, {how}; loudness and dynamics from the "
                                  f"{'whole tracks' if len(whole) < len(full) else 'full songs'}")

