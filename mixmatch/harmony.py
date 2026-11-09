"""Key, major or minor, and chords: from Live's MIDI clips (exact) or from audio (chroma, an estimate).

The key comes from the Krumhansl–Kessler profiles, matched against how long
and how loud each pitch class sounds. Chords are matched bar by bar against
templates (triads, sus, sevenths); the lowest strong note makes the bass of
a slash chord.
"""
from __future__ import annotations

import numpy as np

SHARPS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLATS = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
FLAT_KEYS = {"F", "Bb", "Eb", "Ab", "Db", "Gb", "Dm", "Gm", "Cm", "Fm", "Bbm", "Ebm"}

MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

CHORDS = {  # suffix → intervals from the root
    "": (0, 4, 7), "m": (0, 3, 7), "dim": (0, 3, 6), "sus2": (0, 2, 7), "sus4": (0, 5, 7),
    "maj7": (0, 4, 7, 11), "m7": (0, 3, 7, 10), "7": (0, 4, 7, 10), "m7b5": (0, 3, 6, 10), "add9": (0, 2, 4, 7),
}


def _names(key_name: str | None) -> list[str]:
    return FLATS if key_name in FLAT_KEYS else SHARPS


def key(chroma: np.ndarray) -> dict:
    """{"key": "F#m", "tonic": "F#", "mode": "minor", "confidence": r} from a 12-bin pitch-class weight."""
    c = np.asarray(chroma, dtype=float)
    if c.sum() <= 0:
        return {"key": None, "tonic": None, "mode": None, "confidence": 0.0}
    best = (-2.0, 0, "major")
    for tonic in range(12):
        for mode, profile in (("major", MAJOR), ("minor", MINOR)):
            r = float(np.corrcoef(c, np.roll(profile, tonic))[0, 1])
            if r > best[0]:
                best = (r, tonic, mode)
    r, tonic, mode = best
    name = SHARPS[tonic] + ("m" if mode == "minor" else "")
    if name not in FLAT_KEYS and FLATS[tonic] + ("m" if mode == "minor" else "") in FLAT_KEYS:
        name = FLATS[tonic] + ("m" if mode == "minor" else "")
    return {"key": name, "tonic": name.rstrip("m"), "mode": mode, "confidence": round(r, 2)}


def chord(chroma: np.ndarray, bass: int | None = None, key_name: str | None = None) -> str | None:
    """Best matching chord for one bar's pitch-class weights, e.g. «D», «F#m7», «A/C#»."""
    c = np.asarray(chroma, dtype=float)
    if c.sum() <= 0:
        return None
    c = c / c.sum()
    best, best_score = None, -1.0
    for root in range(12):
        for suffix, intervals in CHORDS.items():
            tones = [(root + i) % 12 for i in intervals]
            inside = c[tones].sum()
            # covered energy, minus energy outside the chord, slightly preferring simple triads
            score = inside - 0.6 * (1 - inside) - 0.03 * (len(tones) - 3)
            if score > best_score:
                best, best_score = (root, suffix), score
    if best_score < 0.25:
        return None
    names = _names(key_name)
    root, suffix = best
    name = names[root] + suffix
    if bass is not None and bass % 12 != root:
        name += "/" + names[bass % 12]
    return name


def from_notes(notes: list, start: float, end: float, beats_per_bar: int = 4) -> dict:
    """Harmony of Live's MIDI notes ([track, pitch, start, duration, velocity]) between two song positions."""
    total = np.zeros(12)
    bars: list[dict] = []
    for b0 in np.arange(start, end, beats_per_bar):
        b1 = b0 + beats_per_bar
        pcs, low = np.zeros(12), {}
        for _, pitch, t, dur, vel in notes:
            overlap = min(t + dur, b1) - max(t, b0)
            if overlap > 0:
                w = overlap * (vel / 127.0)
                pcs[pitch % 12] += w
                low[pitch] = low.get(pitch, 0.0) + w
        total += pcs
        strong = [p for p, w in low.items() if w >= 0.25 * max(low.values())] if low else []
        bars.append({"bar": int(b0 // beats_per_bar) + 1, "pcs": pcs, "bass": min(strong) if strong else None})
    k = key(total)
    for bar in bars:
        bar["chord"] = chord(bar.pop("pcs"), bar.pop("bass"), k["key"])
    k = settle_relative(k, [b["chord"] for b in bars if b["chord"]])
    return {**k, "chords": progression(bars), "source": "MIDI"}


def _root(name: str) -> tuple[int, bool]:
    """Pitch class of a chord's root and whether it is a minor-type chord («Ebm7/Gb» → (3, True))."""
    base = name.split("/")[0]
    letter = base[:2] if len(base) > 1 and base[1] in "#b" else base[:1]
    pc = (SHARPS.index(letter) if letter in SHARPS else FLATS.index(letter))
    rest = base[len(letter):]
    return pc, rest.startswith("m") and not rest.startswith("maj")


def settle_relative(k: dict, chords: list[str]) -> dict:
    """Major key or its relative minor (same notes): the one whose tonic chord the song uses more; ties: the first chord."""
    if not k["key"] or not chords:
        return k
    tonic = (SHARPS.index(k["tonic"]) if k["tonic"] in SHARPS else FLATS.index(k["tonic"]))
    if k["mode"] == "major":
        other_pc, other_mode = (tonic + 9) % 12, "minor"
    else:
        other_pc, other_mode = (tonic + 3) % 12, "major"
    roots = [_root(c) for c in chords]
    count = lambda pc, minor: sum(1 for r in roots if r == (pc, minor))
    mine, theirs = count(tonic, k["mode"] == "minor"), count(other_pc, other_mode == "minor")
    if theirs > mine or (theirs == mine and roots[0] == (other_pc, other_mode == "minor")):
        names = FLATS if (FLATS[other_pc] + ("m" if other_mode == "minor" else "")) in FLAT_KEYS else SHARPS
        name = names[other_pc] + ("m" if other_mode == "minor" else "")
        return {**k, "key": name, "tonic": names[other_pc], "mode": other_mode}
    return k


def progression(bars: list[dict]) -> list[str]:
    """Chords as a compact sequence: repeats of the same chord merged («Am ×2»)."""
    out: list[list] = []
    for bar in bars:
        if bar["chord"] is None:
            continue
        if out and out[-1][0] == bar["chord"]:
            out[-1][1] += 1
        else:
            out.append([bar["chord"], 1])
    return [c if n == 1 else f"{c} ×{n}" for c, n in out]


def chroma(mono: np.ndarray, sr: int, hop: float = 0.1) -> np.ndarray:
    """Pitch-class energy per hop (frames × 12) from an STFT, 55 Hz–4 kHz."""
    from scipy.signal import stft

    f, _, z = stft(mono, fs=sr, nperseg=8192, noverlap=8192 - int(sr * hop))
    mag = np.abs(z) ** 2
    keep = (f >= 55) & (f <= 4000)
    pcs = (np.round(12 * np.log2(f[keep] / 440.0)) + 9) % 12   # A = 9
    out = np.zeros((mag.shape[1], 12))
    for pc in range(12):
        out[:, pc] = mag[keep][pcs == pc].sum(axis=0)
    return out


def from_audio(mono: np.ndarray, sr: int, tempo: float | None = None, beats_per_bar: int = 4) -> dict:
    """Harmony estimated from audio: key from the whole chroma, chords per bar when the tempo is known."""
    hop = 0.1
    ch = chroma(mono, sr, hop)
    ch = ch / np.maximum(ch.sum(axis=1, keepdims=True), 1e-12)      # each frame counts the same
    k = key(ch.sum(axis=0))
    chords: list[str] = []
    if tempo:
        frames_per_bar = max(1, int(round(beats_per_bar * 60 / tempo / hop)))
        bars = [{"chord": chord(ch[i:i + frames_per_bar].sum(axis=0), None, k["key"])}
                for i in range(0, len(ch) - frames_per_bar + 1, frames_per_bar)]
        k = settle_relative(k, [b["chord"] for b in bars if b["chord"]])
        chords = progression(bars)
    return {**k, "chords": chords, "source": "audio (estimate)"}


def bass_frequencies(tonic: str | None) -> dict:
    """Tonic and fifth below 200 Hz: where kick, sub and bass put their energy in this key."""
    if not tonic:
        return {}
    names = FLATS if tonic in FLATS else SHARPS
    pc = names.index(tonic)
    out = {}
    for label, semis in (("tonic", 0), ("fifth", 7)):
        midi = 24 + (pc + semis) % 12                 # octave 1
        freqs = [440.0 * 2 ** ((m - 69) / 12) for m in (midi, midi + 12, midi + 24)]
        out[label] = [round(f, 1) for f in freqs if f < 200]
    return out
