"""Plans mix moves from one measurement by prediction, instead of trying them one round at a time.

Every tap reports its channel's third-octave spectrum after the fader. Adding the channels' powers tells how
much each one contributes to every band of the master, so the effect of an EQ cut or a fader move on the
master's tonal balance can be calculated before anything changes in Live. `solve` picks the moves with the
best predicted benefit, prefers cutting background sources and stays inside per-channel limits;
`collision_fixes` simulates EQ cuts, fader moves and ducking on the recorded 100 ms frames of a colliding
pair. Measurements afterwards decide what is kept.

Assumptions: channels add up in power (uncorrelated), bus processing and the master limiter do not change
the tonal shape. Both are approximations, which is why every package is measured before it stays.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

from .actions import band_db, eq_curve
from .analysis import K_WEIGHT, TONAL_BANDS, MaskingFrames, band_label, masking, masking_alignment, smooth
from .bands import BAND_COUNT, CENTRES, bands_between
from .roles import LOW_END_ROLES

F = np.array(CENTRES)
SMOOTH = np.stack([smooth(np.eye(BAND_COUNT)[j]) for j in range(BAND_COUNT)], axis=1)   # smooth(x) == SMOOTH @ x
BELL_CENTRES = bands_between(31, 12500)
VOCAL_RANGE = set(bands_between(200, 5000))
TONE_Q = 1.0              # broad bells for tonal balance, about an octave and a half wide
COLLISION_Q = 1.4
CUT_STEP, BOOST_STEP = -1.5, 1.0
MAX_CUT_PER_SOLVE = 3.0   # one solve may cut one band at most this much (the executor's step limit)
MAX_BOOST = 2.0
FADER_RANGE = (-3.0, 2.0)
MAX_NEW_BANDS = 3         # new EQ bands per channel and solve: a few broad moves, not a curve copy
MIN_SCORE = 0.02          # weighted predicted benefit a move must bring
MAX_MOVES = 24
MAX_MASTER_RISE_DB = 0.3  # predicted master loudness may rise at most this much in one solve
PRIORITY_WEIGHT = {1: 1.0, 2: 1.0, 3: 0.8, 4: 0.5, 5: 0.25}
GROUP_WEIGHT, BOOST_WEIGHT, FADER_WEIGHT = 0.7, 0.5, 0.6
NO_LOW_CUT = LOW_END_ROLES | {"kick", "toms", "drums_bus", "drum_loop", "bus"}
BACKGROUND = {"pad", "ambience", "texture", "fx", "riser", "impact", "strings"}


@dataclass
class Source:
    """A channel the solver may change: a track or return (leaf) or a group that sums leaves."""

    key: str
    name: str
    role: str
    priority: int
    bands_db: np.ndarray                 # level per band after the fader, in this passage
    members: tuple = ()                  # group: the leaf keys inside it
    is_group: bool = False
    eq: list = field(default_factory=list)   # enabled EQ Eight bands: {"freq", "gain", "q", "kind", "on"}
    eq_free: int = 8                     # free EQ Eight bands; 0 when its EQ Eight is switched off
    fader_free: bool = True              # volume neither automated nor locked
    low_side_db: float = -99.0
    bass_mono_free: bool = False         # a Utility could switch bass mono on (not on already, none disabled)
    duck_free: bool = False              # a MARA duck may be added (no sidechain compressor there already)
    level_room: float = 3.0              # dB it may still lose against the master before it counts as a lost layer


@dataclass
class Move:
    key: str
    kind: str                # bell | low_cut | fader | bass_mono | duck
    idx: int | None = None   # band index (bell, low_cut)
    gain: float = 0.0        # dB (bell, fader); reduction for duck
    q: float = TONE_Q
    benefit: float = 0.0     # predicted reduction of the tonal distance (or of the pair's severity)
    reason: str = ""
    source_track: str | None = None   # duck: the foreground that triggers it
    release_ms: float = 120.0
    id: int = 0

    def describe(self) -> str:
        if self.kind == "bell":
            return f"{self.gain:+.1f} dB at {band_label(self.idx)} Hz"
        if self.kind == "low_cut":
            return f"low cut at {band_label(self.idx)} Hz"
        if self.kind == "fader":
            return f"fader {self.gain:+.1f} dB"
        if self.kind == "duck":
            return f"duck {self.gain:.1f} dB when {self.source_track} plays"
        return "bass mono"

    def action(self) -> dict:
        base = {"track": self.key, "reason": self.reason or self.describe()}
        if self.kind == "bell":
            return {**base, "type": "eq", "freq_hz": round(CENTRES[self.idx]), "gain_db": round(self.gain, 1),
                    "q": self.q, "shape": "bell"}
        if self.kind == "low_cut":
            return {**base, "type": "eq", "freq_hz": round(CENTRES[self.idx]), "gain_db": None, "q": None,
                    "shape": "low_cut"}
        if self.kind == "fader":
            return {**base, "type": "volume", "delta_db": round(self.gain, 1)}
        if self.kind == "duck":
            return {**base, "type": "duck", "source_track": self.source_track, "reduction_db": self.gain,
                    "release_ms": self.release_ms}
        return {**base, "type": "width", "bass_mono": True, "width_pct": None}

    def scaled(self, factor: float) -> "Move":
        return replace(self, gain=self.gain * factor) if self.kind in ("bell", "fader", "duck") else self


@dataclass
class Constraints:
    """What the user, the planner's review or earlier failures ruled out for the rest of the run."""

    frozen: set = field(default_factory=set)       # channel keys that stay untouched
    banned: set = field(default_factory=set)       # (key, kind, idx)
    caps: dict = field(default_factory=dict)       # (key, kind, idx) → largest allowed |gain|


@dataclass
class Plan:
    moves: list
    cost_before: float
    cost_after: float
    excess_before: np.ndarray | None = None
    excess_after: np.ndarray | None = None
    unchanged: dict = field(default_factory=dict)  # key → why nothing changes there

    @property
    def benefit(self) -> float:
        return self.cost_before - self.cost_after


# ── responses ──────────────────────────────────────────────────────────────
_BELL_CACHE: dict = {}


def bell(idx: int, gain: float, q: float) -> np.ndarray:
    key = (idx, round(gain, 3), q)
    if key not in _BELL_CACHE:
        _BELL_CACHE[key] = band_db("bell", CENTRES[idx], gain, q, F) if gain else np.zeros(BAND_COUNT)
    return _BELL_CACHE[key]


def low_cut(idx: int) -> np.ndarray:
    """EQ Eight «Low Cut 12» (second-order Butterworth high-pass) at the band centres."""
    return -10 * np.log10(1 + (CENTRES[idx] / F) ** 4)


def floor_curve(src: Source) -> np.ndarray:
    """Deepest cut this solve run may add per band: little in the core of important sources, more on beds."""
    if src.is_group:
        return np.full(BAND_COUNT, -3.0)
    if src.priority >= 4:
        core = src.bands_db >= src.bands_db.max() - 6
        return np.where(core, -2.0, -4.0)
    return np.full(BAND_COUNT, -6.0 if src.priority <= 2 or src.role in BACKGROUND else -4.0)


# ── the tonal model ────────────────────────────────────────────────────────
class Model:
    """Predicts the master's tonal distance to the target from changes to the channels."""

    def __init__(self, master, leaves: dict[str, np.ndarray], target):
        self.power = {k: 10 ** (np.asarray(b, dtype=float) / 10) for k, b in leaves.items()}
        self.total = np.maximum(sum(self.power.values()), 1e-30) if self.power else np.ones(BAND_COUNT)
        self.mb, self.loud = np.asarray(master.bands_db, dtype=float), float(master.loudness)
        self.kw = K_WEIGHT * 10 ** (self.mb / 10)
        self.kw_sum = float(self.kw.sum())
        self.ref = np.asarray(target.stats.shape_db, dtype=float)
        self.tol = np.asarray(target.tolerance, dtype=float)
        self.offset = None if target.offset is None else np.asarray(target.offset, dtype=float)
        self.anchor = list(target.anchor or [])
        self.one_sided = list(target.one_sided or [])

    def excess(self, ratio: np.ndarray) -> np.ndarray:
        """`ratio`: new over old power per band of the summed channels, shape (..., bands)."""
        ratio = np.maximum(ratio, 1e-6)
        mb = self.mb + 10 * np.log10(ratio)
        loud = self.loud + 10 * np.log10((ratio * self.kw).sum(axis=-1) / self.kw_sum)
        diff = (mb - loud[..., None] - self.ref) @ SMOOTH.T
        if self.anchor:
            diff = diff - diff[..., self.anchor].mean(axis=-1, keepdims=True)
        if self.offset is not None:
            diff = diff - self.offset
        out = np.sign(diff) * np.maximum(0.0, np.abs(diff) - self.tol)
        if self.one_sided:
            out[..., self.one_sided] = np.maximum(0.0, out[..., self.one_sided])
        return out

    def cost(self, ratio: np.ndarray) -> np.ndarray:
        return np.sqrt(np.mean(self.excess(ratio)[..., TONAL_BANDS] ** 2, axis=-1))


def _weight(src: Source) -> float:
    return GROUP_WEIGHT if src.is_group else PRIORITY_WEIGHT.get(int(src.priority), 0.8)


def solve(master, sources: dict[str, Source], target, constraints: Constraints | None = None,
          beat: bool = False, max_moves: int = MAX_MOVES) -> Plan:
    """Greedy: each step takes the move with the best weighted predicted benefit until none is worth it."""
    constraints = constraints or Constraints()
    leaves = {k: s.bands_db for k, s in sources.items() if not s.is_group}
    model = Model(master, leaves, target)
    gain = {k: np.zeros(BAND_COUNT) for k in leaves}     # dB added to each leaf so far
    planned = {}                                          # (key, kind, idx) → Move
    eq_added = {k: np.zeros(BAND_COUNT) for k in sources}
    fader_added = {k: 0.0 for k in sources}
    new_bands = {k: 0 for k in sources}

    def numerator():
        return sum(model.power[k] * 10 ** (gain[k] / 10) for k in leaves)

    def affected(src):
        return [m for m in src.members if m in leaves] if src.is_group else [src.key]

    num = numerator()
    cost = float(model.cost(num / model.total))
    start, excess_before = cost, model.excess(num / model.total)

    for _ in range(max_moves):
        best = None
        for src in sources.values():
            if src.key in constraints.frozen:
                continue
            members = affected(src)
            if not members:
                continue
            amount = sum(model.power[k] * 10 ** (gain[k] / 10) for k in members)
            weight = _weight(src)
            floor = floor_curve(src)
            existing = eq_curve(src.eq, F) if src.eq else np.zeros(BAND_COUNT)
            curves, meta = [], []
            # Bells: a new band, or one more step on a band this solve already planned nearby.
            if src.eq_free > 0:
                for idx in BELL_CENTRES:
                    near = next((j for (k, kind, j) in planned if k == src.key and kind == "bell"
                                 and abs(j - idx) <= 1), None)
                    if near is not None and near != idx:
                        continue
                    old = planned[(src.key, "bell", idx)].gain if near is not None else 0.0
                    reuses = near is not None or any(b.get("on", True) and "bell" in str(b.get("kind", "")).lower()
                                                     and abs(math.log2(b["freq"] / CENTRES[idx])) <= 1 / 3
                                                     for b in src.eq)
                    if not reuses and new_bands[src.key] >= min(src.eq_free, MAX_NEW_BANDS):
                        continue
                    for step, boost in ((CUT_STEP, False), (BOOST_STEP, True)):
                        new = old + step
                        if (src.key, "bell", idx) in constraints.banned:
                            continue
                        cap = constraints.caps.get((src.key, "bell", idx))
                        if cap is not None and abs(new) > cap + 1e-6:
                            continue
                        if boost:
                            if new > MAX_BOOST or old < 0 or (beat and idx in VOCAL_RANGE):
                                continue
                        else:
                            if old > 0 or -new > MAX_CUT_PER_SOLVE:
                                continue
                        delta = bell(idx, new, TONE_Q) - bell(idx, old, TONE_Q)
                        after = eq_added[src.key] + delta
                        cuts = delta < -0.05
                        if cuts.any() and (np.any((after < floor)[cuts]) or np.any((existing + after < floor - 6)[cuts])):
                            continue
                        curves.append(delta)
                        meta.append(("bell", idx, new, weight * (BOOST_WEIGHT if boost else 1.0), not reuses))
                # A low cut on sources whose range starts well above it.
                if (not src.is_group and src.role not in NO_LOW_CUT and new_bands[src.key] < min(src.eq_free, MAX_NEW_BANDS)
                        and not any(kind == "low_cut" for (k, kind, _) in planned if k == src.key)):
                    top = src.bands_db.max()
                    for idx in bands_between(40, 250):
                        if (src.key, "low_cut", idx) in constraints.banned:
                            continue
                        below = [i for i in range(BAND_COUNT) if CENTRES[i] <= CENTRES[idx] * 1.6]
                        if src.bands_db[below].max() > top - 15:
                            break
                        curves.append(low_cut(idx))
                        meta.append(("low_cut", idx, 0.0, weight, True))
            if src.fader_free:
                for step in (-1.0, 1.0):
                    new = fader_added[src.key] + step
                    cap = constraints.caps.get((src.key, "fader", None))
                    if not FADER_RANGE[0] <= new <= FADER_RANGE[1] or (src.key, "fader", None) in constraints.banned:
                        continue
                    if cap is not None and abs(new) > cap + 1e-6:
                        continue
                    curves.append(np.full(BAND_COUNT, step))
                    meta.append(("fader", None, new, weight * FADER_WEIGHT * (BOOST_WEIGHT if step > 0 else 1.0), False))
            if not curves:
                continue
            curves = np.array(curves)
            ratio = (num + amount * (10 ** (curves / 10) - 1)) / model.total
            costs = model.cost(ratio)
            # No move may make a channel lose more against the master than the layer protection allows.
            master_delta = 10 * np.log10(np.maximum((np.maximum(ratio, 1e-6) * model.kw).sum(axis=-1), 1e-30) / model.kw_sum)
            allowed = np.ones(len(curves), dtype=bool)
            for k in members:
                weighted = model.power[k] * K_WEIGHT
                own = 10 * np.log10(np.maximum((weighted * 10 ** ((gain[k] + curves) / 10)).sum(axis=-1), 1e-30)
                                    / max(float(weighted.sum()), 1e-30))
                room = sources[k].level_room if k in sources else 3.0
                allowed &= own - master_delta >= -room
            # Loudness comes last, from the limiter: the calculated moves may not make the master louder
            # (louder means higher peaks before the limiter, and a fader race instead of a balance).
            allowed &= master_delta <= MAX_MASTER_RISE_DB
            costs = np.where(allowed, costs, np.inf)
            for i, (c, (kind, idx, new, w, adds_band)) in enumerate(zip(costs, meta)):
                score = (cost - float(c)) * w
                if best is None or score > best[0]:
                    best = (score, src, kind, idx, new, float(c), adds_band, curves[i])
        if best is None or best[0] < MIN_SCORE:
            break
        score, src, kind, idx, new, new_cost, adds_band, curve = best
        for k in affected(src):
            gain[k] = gain[k] + curve
        if kind == "fader":
            fader_added[src.key] = new
        else:
            eq_added[src.key] = eq_added[src.key] + curve
        if adds_band:
            new_bands[src.key] += 1
        key = (src.key, kind, idx)
        move = planned.get(key) or Move(src.key, kind, idx, q=TONE_Q)
        move.gain = new if kind != "low_cut" else 0.0
        move.benefit += cost - new_cost
        planned[key] = move
        num = numerator()
        cost = new_cost

    moves = [m for m in planned.values() if m.kind == "low_cut" or abs(m.gain) >= 0.05]
    excess_after = model.excess(num / model.total)
    shares = {k: _share(model, sources[k], leaves) for k in sources}
    for m in moves:
        m.reason = _reason(m, sources[m.key], shares[m.key], excess_before)
    plan = Plan(sorted(moves, key=lambda m: -m.benefit), start, cost, excess_before, excess_after)
    for i, m in enumerate(plan.moves, 1):
        m.id = i
    changed = {m.key for m in moves}
    for k, src in sources.items():
        if k in changed:
            continue
        if k in constraints.frozen:
            plan.unchanged[k] = "protected"
        elif not np.any((shares[k] > 0.05) & (np.abs(excess_before) > 0.5)):
            plan.unchanged[k] = "little share where the mix differs from the references"
        else:
            plan.unchanged[k] = "no move with enough predicted benefit within its limits"
    return plan


def _share(model: Model, src: Source, leaves) -> np.ndarray:
    keys = [m for m in src.members if m in leaves] if src.is_group else [src.key]
    return sum(model.power[k] for k in keys) / model.total if keys else np.zeros(BAND_COUNT)


def _reason(m: Move, src: Source, share: np.ndarray, excess: np.ndarray) -> str:
    who = f"{src.name} ({src.role})"
    if m.kind == "fader":
        i = max(TONAL_BANDS, key=lambda j: share[j] * abs(excess[j]))
    elif m.kind == "low_cut":
        low = [i for i in range(BAND_COUNT) if CENTRES[i] <= CENTRES[m.idx]]
        i = max(low, key=lambda j: share[j]) if low else m.idx
        return f"{who}: nothing it needs below {band_label(m.idx)} Hz; {share[i]:.0%} of the master at {band_label(i)} Hz"
    else:
        i = m.idx
    return (f"{who} carries {share[i]:.0%} of the master at {band_label(i)} Hz, "
            f"where the mix is {excess[i]:+.1f} dB off the references")


def bass_mono_moves(sources: dict[str, Source], needed: bool) -> list[Move]:
    """Stereo low end: bass mono on the leaves that put noticeable stereo energy below 150 Hz."""
    if not needed:
        return []
    low = bands_between(20, 150)
    level = {k: 10 * math.log10(max(1e-18, float(np.sum(10 ** (s.bands_db[low] / 10)))))
             for k, s in sources.items() if not s.is_group}
    if not level:
        return []
    loudest = max(level.values())
    return [Move(k, "bass_mono", reason=f"{s.name}: stereo energy below 150 Hz (side {s.low_side_db:.0f} dB under mid)")
            for k, s in sources.items()
            if not s.is_group and s.bass_mono_free and s.low_side_db > -20 and level[k] > loudest - 12]


# ── collisions ─────────────────────────────────────────────────────────────
@dataclass
class Fix:
    pair: str
    foreground: str
    background: str
    freq_hz: float
    before: float            # measured mean severity
    predicted: float         # predicted severity after the fix
    moves: list


def _with_eq(frames: MaskingFrames, curve: np.ndarray) -> MaskingFrames:
    if not len(frames.times):
        return frames
    bands = frames.bands_db + curve
    old = np.log10(np.maximum((10 ** (frames.bands_db / 10)).sum(axis=1), 1e-30))
    new = np.log10(np.maximum((10 ** (bands / 10)).sum(axis=1), 1e-30))
    return MaskingFrames(frames.times, bands, frames.level_db + 10 * (new - old), frames.active)


def _with_duck(fg: MaskingFrames, bg: MaskingFrames, reduction: float) -> MaskingFrames:
    fi, bi, _ = masking_alignment(fg, bg)
    hit = np.zeros(len(bg.times), dtype=bool)
    hit[bi[fg.active[fi]]] = True
    bands, levels = bg.bands_db.copy(), bg.level_db.copy()
    bands[hit] -= reduction
    levels[hit] -= reduction
    return MaskingFrames(bg.times, bands, levels, bg.active)


def _severity(fg, bg) -> float:
    hit = masking(fg, bg)
    return float(hit["severity"]) if hit else 0.0


def collision_fixes(m: dict, sources: dict[str, Source], limit: int = 5, keys: set | None = None,
                    ducking: bool = False, constraints: Constraints | None = None) -> tuple[list[Fix], list[str]]:
    """The strongest collisions and, for each, the simulated fix with the most relief on the background.

    Returns the fixes and notes about pairs that were left alone (layers, no predicted relief)."""
    constraints = constraints or Constraints()
    frames = m.get("mask_frames") or {}
    severity, noise = m.get("pair_severity", {}), m.get("pair_noise", {})
    pairs = sorted(m.get("masking", []), key=lambda p: -severity.get(_pair(p), p["severity"])
                   * min(p["priority_a"], p["priority_b"]))
    fixes, notes, used = [], [], set()
    for p in pairs:
        if len(fixes) >= limit:
            break
        key = _pair(p)
        fg, bg = p["foreground"], p["background"]
        before = severity.get(key, p["severity"])
        if before < 0.15 or bg in used or bg in constraints.frozen or bg not in sources or fg not in frames or bg not in frames:
            continue
        if keys is not None and fg not in keys and bg not in keys:
            continue
        if p.get("role_a") == p.get("role_b") and p.get("overlap_fraction", 0) >= 0.8:
            notes.append(f"{sources[bg].name} ↔ {sources.get(fg, sources[bg]).name}: same role playing together, "
                         "treated as an intentional layer")
            continue
        src = sources[bg]
        base = _severity(frames[fg], frames[bg])
        if base <= 0:
            continue
        candidates = []
        floor = floor_curve(src) - 2.0
        weighted = 10 ** (src.bands_db / 10) * K_WEIGHT

        def keeps_layer(curve):
            own = 10 * math.log10(max(float((weighted * 10 ** (curve / 10)).sum()), 1e-30) / max(float(weighted.sum()), 1e-30))
            return own >= -src.level_room
        existing = eq_curve(src.eq, F) if src.eq else np.zeros(BAND_COUNT)
        if src.eq_free > 0 and not src.is_group:
            for band in p.get("bands") or [{"freq_hz": p["freq_hz"]}]:
                idx = int(np.argmin(np.abs(F - band["freq_hz"])))
                for g in (-2.0, -3.0):
                    curve = bell(idx, g, COLLISION_Q)
                    if ((bg, "bell", idx) in constraints.banned or np.any((existing + curve < floor)[curve < -0.05])
                            or not keeps_layer(curve)):
                        continue
                    candidates.append((Move(bg, "bell", idx, g, COLLISION_Q), _with_eq(frames[bg], curve)))
        if src.fader_free and src.priority <= 3 and src.level_room >= 1.5:
            candidates.append((Move(bg, "fader", None, -1.5), _with_eq(frames[bg], np.full(BAND_COUNT, -1.5))))
        if ducking and p.get("intermittent") and src.duck_free and (p.get("observed_ducking_db") or 0) < 3:
            for reduction in (2.0, 3.0):
                candidates.append((Move(bg, "duck", None, reduction, source_track=fg),
                                   _with_duck(frames[fg], frames[bg], reduction)))
        # A static move changes the background all the time, also when the foreground rests; with an intermittent
        # foreground that costs more than ducking only while it plays.
        static_cost = 0.1 * (1 - p.get("foreground_active_fraction", 1.0)) if p.get("intermittent") else 0.0
        scored = []
        for move, new_frames in candidates:
            ratio = _severity(frames[fg], new_frames) / base
            # Prefer the gentler move when two give about the same relief.
            gentleness = 0.02 * abs(move.gain) + (static_cost if move.kind != "duck" else 0.0)
            scored.append((ratio + gentleness, ratio, move))
        if not scored:
            continue
        _, ratio, move = min(scored, key=lambda s: s[0])
        predicted = before * ratio
        needed = max(0.05, 2 * noise.get(key, 0.0))
        name = lambda k: sources[k].name if k in sources else k
        if before - predicted < needed:
            notes.append(f"{name(bg)} ↔ {name(fg)} at {p['freq_hz']} Hz: no simulated move relieves it beyond "
                         f"measurement spread ({before:.2f} → {predicted:.2f} at best)")
            continue
        move.benefit = before - predicted
        if move.kind == "duck":
            move.reason = (f"make room for {name(fg)} only while it plays: repeated collisions at {p['freq_hz']} Hz "
                           f"(simulated {before:.2f} → {predicted:.2f})")
        else:
            move.reason = (f"make room for {name(fg)} at {p['freq_hz']} Hz, {p.get('overlap_fraction', 0):.0%} "
                           f"simultaneous (simulated {before:.2f} → {predicted:.2f})")
        if move.kind == "duck":
            move.release_ms = 120.0
        used.add(bg)
        fixes.append(Fix(key, fg, bg, p["freq_hz"], before, predicted, [move]))
    return fixes, notes


def _pair(p: dict) -> str:
    return ":".join(sorted((p["track_a"], p["track_b"])))
