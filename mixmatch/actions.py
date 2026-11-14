"""Turns the agent's decisions into parameter changes in Live, in small, reversible steps.

Actions (what the planner may ask for):

  {"type": "volume",  "track": "t3", "delta_db": -1.5}
  {"type": "pan",     "track": "t3", "value": -20}                 # -50 … 50
  {"type": "eq",      "track": "t3", "freq_hz": 80, "gain_db": -2, "q": 1.0, "shape": "bell"}
  {"type": "width",   "track": "t3", "width_pct": 80, "bass_mono": true}
  {"type": "limiter", "delta_db": 1.0}                              # master loudness
  {"type": "param",   "track": "t3", "device_index": 2, "param": "Threshold", "target": -18}
  {"type": "load_device", "track": "t3", "name": "Compressor"}
  {"type": "duck", "track": "t3", "source_track": "t0", "reduction_db": 2, "release_ms": 120}

Every step is clamped (no jumps of 6 dB), every changed parameter's old raw
value is remembered so the whole round can be undone, and parameters the user
touched by hand are locked.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .bands import RATIO
from .display import parse_display

def _plain(name: str) -> str:
    n = name.lower().strip()
    for ext in (".vst3", ".component", ".vst"):
        if n.endswith(ext):
            n = n[: -len(ext)]
    return n.removeprefix("fabfilter ").strip()


def _number_ms(display: str) -> float | None:
    """A display as a number, times in milliseconds («1.00 s» → 1000)."""
    v = parse_display(display)
    if v is None:
        return None
    t = str(display).strip().lower()
    return float(v) * 1000.0 if t.endswith(" s") and not t.endswith("ms") else float(v)


def _db(display: str) -> float | None:
    v = parse_display(display)
    return None if v is None else float(v)


def neutral_device(d: dict) -> bool:
    """True for an EQ Eight or Utility whose settings change nothing: safe to remove without listening."""
    P = {p["name"]: p["display"] for p in d.get("params", [])}
    if d.get("class_name") == "Eq8":
        if abs(_db(P.get("Output", "0 dB")) or 0.0) > 0.05:
            return False
        for n in range(1, 9):
            if P.get(f"{n} Filter On A", "Off") != "On":
                continue
            kind = P.get(f"{n} Filter Type A", "").lower()
            gain = _db(P.get(f"{n} Gain A", "0 dB")) or 0.0
            if kind not in ("bell", "low shelf", "high shelf") or abs(gain) > 0.05:
                return False
        return True
    if d.get("class_name") == "StereoGain":
        off = ("Left Inv", "Right Inv", "Mono", "Bass Mono", "Mute", "DC Filter")
        return (all(P.get(k, "Off") == "Off" for k in off) and P.get("Channel Mode", "Stereo") == "Stereo"
                and P.get("Balance", "C") in ("C", "0", "0.00") and abs((_db(P.get("Stereo Width", "100")) or 100) - 100) < 0.5
                and abs(_db(P.get("Output", P.get("Gain", "0 dB"))) or 0.0) < 0.05)
    return False


def _aliases(name):
    """Parameter names differ between Live versions: EQ Eight «1 Resonance A» is «1 Q A» in Live 12,
    the Limiter's «Gain» is «Input Gain»."""
    if not isinstance(name, str):
        return [name]
    out = [name]
    if name.endswith(" Resonance A"):
        out.append(name[: -len("Resonance A")] + "Q A")
    if name == "Gain":
        out.append("Input Gain")
    if name == "Ceiling":
        out.append("Output Level")
    return out


def _neutral(band: dict) -> bool:
    """A bell or shelf at 0 dB changes nothing; cuts and notches act even at 0 dB gain."""
    kind = str(band["type"].get("display", "")).lower()
    return kind in ("bell", "low shelf", "high shelf") and abs(band["gain"]) < 0.05


MAX_STEP = {"volume": 2.0, "eq": 3.0, "limiter": 2.0, "clipper": 1.5, "send": 3.0, "pan": 15.0, "width": 25.0}
EQ_GAIN_LIMIT = 12.0
MASTER_EQ_CUT_LIMIT_DB = 3.0   # all master EQ bands together cut at most this much anywhere; the rest on the channels


def band_db(kind: str, f0: float, gain: float, q: float, f, fs: float = 48000.0):
    """Gain in dB of one bell or shelf band at frequency `f` (RBJ cookbook biquads, as EQ Eight draws them).
    Cuts and notches return 0: they are meant to remove, the cut limit is about tonal balance."""
    import numpy as np

    kind = kind.lower()
    f = np.asarray(f, dtype=float)
    if not any(k in kind for k in ("bell", "shelf")) or gain == 0:
        return np.zeros_like(f)
    A, w0 = 10 ** (gain / 40), 2 * math.pi * f0 / fs
    alpha, cw = math.sin(w0) / (2 * max(q, 0.1)), math.cos(w0)
    if "bell" in kind:
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]
        a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    else:
        sa = 2 * math.sqrt(A) * alpha
        if "low" in kind:
            b = [A * ((A + 1) - (A - 1) * cw + sa), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - sa)]
            a = [(A + 1) + (A - 1) * cw + sa, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sa]
        else:
            b = [A * ((A + 1) + (A - 1) * cw + sa), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - sa)]
            a = [(A + 1) - (A - 1) * cw + sa, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sa]
    z = np.exp(-1j * 2 * math.pi * f / fs)
    h = (b[0] + b[1] * z + b[2] * z ** 2) / (a[0] + a[1] * z + a[2] * z ** 2)
    return 20 * np.log10(np.abs(h))


def eq_curve(bands: list[dict], f):
    """Summed response in dB of the bands that are on."""
    import numpy as np

    out = np.zeros(len(f))
    for b in bands:
        if b["on"]:
            out += band_db(b["kind"], b["freq"], b["gain"], b.get("q", 0.71), f)
    return out
VOLUME_CEILING_DB = 6.0
LIMITER_CEILING_DB = -1.0
LIMITERS = ("limiter", "pro-l 2")   # Live's Limiter or FabFilter Pro-L 2 on the master
LIMITER_MAX_GAIN_DB = 5.0   # about 4–5 dB of peak gain reduction: loud but still holding together
# Clippers before the master limiter, as (plugin, drive parameter); the first installed one is used.
CLIPPERS = (("Classic Clipper", "Gain"),)
CLIPPER_MAX_GAIN_DB = 4.0   # the clipper takes the transients, it copes with them better than the limiter
TAP = "MixMatch Tap"
TRIM = "MARA Trim"          # Utility in front of clipper and limiter on Main: the level base of the loudness chain
TRIM_RANGE_DB = 24.0
MAX_TRIM_STEP_DB = 12.0
SHAPES = {"bell": ["bell"], "low_shelf": ["low shelf", "lowshelf"], "high_shelf": ["high shelf", "highshelf"],
          "low_cut": ["low cut 12", "lowcut 12", "low cut", "lowcut", "12 db"], "notch": ["notch"]}


@dataclass
class Change:
    track: str
    device: object
    param: object
    old_value: float
    new: dict = field(default_factory=dict)
    old_display: str = ""


@dataclass
class RoundResult:
    changes: list[Change] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    created_devices: list[tuple[str, str]] = field(default_factory=list)


def _clamp(x: float, limit: float) -> float:
    return max(-limit, min(limit, x))


class Executor:
    def __init__(self, live, log=print):
        self.live = live
        self.log = log
        self.set_state: dict = {}
        self.locks: set[tuple] = set()
        self.last_set: dict[tuple, float] = {}   # what we set last time, to notice the user's hand
        # Every change made this session: when devices move, their indices follow, so an undo always reaches the
        # device it changed (a clipper moved in front of the limiter once received the limiter's old values).
        self._changes: list = []
        self._plugins: list[str] | None = None

    @property
    def plugins(self) -> list[str]:
        """Installed plugins (VST3/AU) as Live's browser lists them; read once."""
        if self._plugins is None:
            try:
                names, seen = [], set()
                for n in self.live.request("list_plugins")["plugins"]:
                    if "(mono)" in n.lower() or _plain(n) in seen:
                        continue
                    seen.add(_plain(n))
                    names.append(n)
                self._plugins = names
            except Exception:  # older remote script or no browser: no plugins
                self._plugins = []
        return self._plugins

    def plugin_named(self, name: str) -> str | None:
        """The installed plugin whose name matches (case and «FabFilter » prefix ignored)."""
        want = _plain(name)
        return next((p for p in self.plugins if _plain(p) == want), None)

    # ── reading the set ─────────────────────────────────────────────────────
    def refresh(self) -> dict:
        self.set_state = self.live.get_set()
        self._detect_manual_changes()
        return self.set_state

    def track(self, key: str) -> dict:
        for t in self.set_state["tracks"]:
            if t["key"] == key:
                return t
        raise KeyError(key)

    def master_key(self) -> str:
        return "m"

    def _param(self, key: str, device, name) -> dict | None:
        t = self.track(key)
        params = t["mixer"] if device == "mixer" else t["devices"][device]["params"]
        for candidate in _aliases(name):
            for p in params:
                if p["name"].lower() == str(candidate).lower() or p["index"] == candidate:
                    return p
        return None

    def _device(self, key: str, *names: str) -> int | None:
        plain = {_plain(n) for n in names}
        for d in self.track(key)["devices"]:
            if d["class_name"].lower() in names or d["name"].lower() in names or _plain(d["name"]) in plain:
                return d["index"]
        return None

    def _detect_manual_changes(self) -> None:
        for (key, device, name), value in list(self.last_set.items()):
            try:
                p = self._param(key, device, name)
            except (KeyError, IndexError):
                continue
            if p and abs(p["value"] - value) > 1e-4 * max(1.0, abs(p["max"] - p["min"])):
                if (key, device, name) not in self.locks:
                    self.log(f"  🔒 {key} {device} {name}: changed by hand, locked")
                self.locks.add((key, device, name))
                del self.last_set[(key, device, name)]

    # ── applying ────────────────────────────────────────────────────────────
    def _set(self, rr: RoundResult, key: str, device, name, target: float | None = None, value: float | None = None):
        p = self._param(key, device, name)
        if p is None:
            raise KeyError(f"{key}: no parameter {name!r} on {device}")
        name = p["name"]  # the real name, also when an alias («Resonance A» → «Q A») was asked for
        if (key, device, name) in self.locks:
            raise PermissionError(f"{key} {device} {name} is locked (changed by hand)")
        if p.get("automated"):
            raise PermissionError(f"{key} {device} {name} is automated; MixMatch leaves automation alone")
        res = self.live.set(key, device, name, target=target, value=value)
        change = Change(key, device, name, p["value"], res, p.get("display", str(p["value"])))
        rr.changes.append(change)
        self._changes.append(change)
        self.last_set[(key, device, name)] = res["value"]
        p["value"], p["display"] = res["value"], res["display"]
        return res

    def _reindex(self, key: str, order: list[int | None]) -> None:
        """Devices of a track moved: order[new index] = old index (None for a new device). Keeps locks and
        remembered values on the same device instead of the same position."""
        old_to_new = {old: new for new, old in enumerate(order) if old is not None}

        def remap(entries):
            out = type(entries)()
            for item in entries:
                k, dev, name = item
                if k == key and isinstance(dev, int):
                    if dev not in old_to_new:
                        continue  # the device is gone
                    item = (k, old_to_new[dev], name)
                if isinstance(entries, dict):
                    out[item] = entries[(k, dev, name)]
                else:
                    out.add(item)
            return out

        self.last_set = remap(self.last_set)
        self.locks = remap(self.locks)
        for c in self._changes:
            if c.track == key and isinstance(c.device, int):
                c.device = old_to_new.get(c.device)   # None: the device is gone, nothing to restore there

    def _inserted(self, key: str, at: int, count_before: int) -> None:
        self._reindex(key, list(range(at)) + [None] + list(range(at, count_before)))

    def _deleted(self, key: str, idx: int, count_before: int) -> None:
        self._reindex(key, [i for i in range(count_before) if i != idx])

    def _moved(self, key: str, frm: int, to: int, count: int) -> None:
        order = [i for i in range(count) if i != frm]
        order.insert(to, frm)
        self._reindex(key, order)

    # ── Main: [user devices …] [MARA Trim] [input tap] [clipper] [limiter] [… end tap] ──────────────
    def _is_chain(self, d: dict) -> bool:
        name = _plain(d["name"])
        return (d.get("class_name") == "Limiter" or name in {_plain(n) for n in LIMITERS}
                or any(name == _plain(c) for c, _ in CLIPPERS))

    def master_layout(self, exclude: int | None = None) -> dict:
        """Indices on Main: the trim, the input tap (a tap with another tap after it), the loudness chain,
        and the end tap. None where there is none. `exclude`: a device just loaded, left out when telling where
        the others sit (it may have landed between the trim and the input tap)."""
        devs = [d for d in self.track(self.master_key())["devices"] if d["index"] != exclude]
        taps = [i for i, d in enumerate(devs) if d["name"] == TAP]
        pre = taps[0] if len(taps) >= 2 else None
        trim = next((i for i, d in enumerate(devs) if d["name"] == TRIM), None)
        if trim is None and pre is not None and pre > 0 and devs[pre - 1].get("class_name") == "StereoGain":
            trim = pre - 1   # an older remote script cannot rename the device: found by its place
        at = lambda i: None if i is None else devs[i]["index"]
        return {"trim": at(trim), "pre": at(pre), "end": at(taps[-1]) if taps else None,
                "chain": [d["index"] for d in devs if self._is_chain(d)]}

    METER_WORDS = ("meter", "analyzer", "analyser", "spectrum", "tuner", "insight", "youlean")
    METER_NAMES = ("span", "span plus", "spectrumanalyzer", "tuner")

    def _is_meter(self, d: dict) -> bool:
        name, cls = _plain(d["name"]), str(d.get("class_name", "")).lower()
        return (name in self.METER_NAMES or cls in self.METER_NAMES
                or any(w in name for w in self.METER_WORDS)) and d["name"] != TAP

    def meters_last(self) -> int:
        """Meters on Main (SPAN, Youlean, Spectrum …) go right in front of the end tap, behind clipper and
        limiter, so they show what leaves the master. They pass the sound unchanged. Returns how many moved.

        Only leftward moves: the device that belongs in front of a meter moves in front of it. Live places a
        rightward move one slot differently than a leftward one, so a rightward move can silently stay put."""
        key, moved = self.master_key(), 0
        for _ in range(16):
            devs = self.track(key)["devices"]
            end = self.master_layout()["end"]
            if end is None:
                return moved
            meter = next((d["index"] for d in devs[:end] if self._is_meter(d)), None)
            later = None if meter is None else next(
                (o["index"] for o in devs[meter + 1:end] if not self._is_meter(o)), None)
            if later is None:
                return moved
            self._move_on_master(later, meter)
            moved += 1
        return moved

    def _move_on_master(self, at: int, to: int) -> int:
        """Moves a device on Main to the left (`to` < `at`) and checks where it really landed."""
        key = self.master_key()
        if at == to:
            return at
        if to > at:
            raise RuntimeError("devices on Main are only moved to the left")
        devs = self.track(key)["devices"]
        count, name = len(devs), devs[at]["name"]
        self.live.move_device(key, at, to)
        self._moved(key, at, to, count)
        self.refresh()
        if self.track(key)["devices"][to]["name"] != name:
            raise RuntimeError(f"Live did not put {name} where it belongs on Main")
        return to

    def _place_on_master(self, at: int) -> int:
        """A device loaded onto Main goes in front of the level base, so the loudness chain stays last;
        clipper and limiter go behind the input tap. Older remote scripts load in front of the first tap."""
        key = self.master_key()
        dev = self.track(key)["devices"][at]
        layout = self.master_layout(exclude=at)
        if self._is_chain(dev):
            name = dev["name"]
            if layout["pre"] is not None and at < layout["pre"]:
                self._move_on_master(layout["pre"], at)          # the input tap moves in front of it
            self.meters_last()
            return next((d["index"] for d in self.track(key)["devices"] if d["name"] == name), at)
        first = min(i for i in (layout["trim"], layout["pre"], *layout["chain"]) if i is not None) \
            if any(i is not None for i in (layout["trim"], layout["pre"], *layout["chain"])) else None
        if first is not None and at > first:
            return self._move_on_master(at, first)
        return at

    def ensure_input_stage(self) -> bool | None:
        """Puts a trim Utility and a second tap in front of clipper and limiter on Main. Both pass the sound
        unchanged (the trim starts at 0 dB). Returns True when it added something, None without a tap on Main."""
        key = self.master_key()
        self.refresh()
        layout = self.master_layout()
        if layout["end"] is None:
            return None
        added = False
        # Repair: clipper or limiter in front of the input tap (an earlier move that Live did not carry out).
        if layout["pre"] is not None and layout["chain"] and min(layout["chain"]) < layout["pre"]:
            self._move_on_master(layout["pre"], min(layout["chain"]))
            self.log("  Main: input measurement moved back in front of clipper and limiter")
            added = True
            layout = self.master_layout()
        if layout["pre"] is None:
            count = len(self.track(key)["devices"])
            at = self.live.load_device(key, TAP, source="user_library")
            self._inserted(key, at, count)
            self.refresh()
            layout = self.master_layout()
            # The new tap is the last one now; the tap that measured Main so far stays the end tap.
            end_before = layout["end"] if at != layout["end"] else layout["pre"]
            target = min(layout["chain"]) if layout["chain"] else end_before
            self._move_on_master(at, target)
            added = True
            layout = self.master_layout()
        if layout["trim"] is None:
            count = len(self.track(key)["devices"])
            at = self.live.load_device(key, "Utility")
            self._inserted(key, at, count)
            self.refresh()
            pre = self.master_layout()["pre"]
            if pre is not None:
                self._move_on_master(at, pre - 1 if at < pre else pre)
            try:
                idx = self.master_layout()["pre"] - 1
                self.live.request("rename_device", track=key, device=idx, name=TRIM)
                self.refresh()
            except Exception:
                pass   # older remote script: the trim is found by its place in front of the input tap
            added = True
        if self.meters_last():
            added = True
        return added

    def remove_input_stage(self) -> None:
        """Takes the input tap and an untouched trim off Main again (old tap script: no separate identity)."""
        key = self.master_key()
        self.refresh()
        layout = self.master_layout()
        for idx in sorted((i for i in (layout["pre"], layout["trim"]) if i is not None), reverse=True):
            if idx == layout["trim"]:
                p = self._trim_param(idx)
                if p is not None and abs(self._db_of(p)) > 0.05:
                    continue
            count = len(self.track(key)["devices"])
            self.live.delete_device(key, idx)
            self._deleted(key, idx, count)
            self.refresh()

    def active_limiter(self) -> int | None:
        """Index of a switched-on limiter on Main: only then are the output peaks held at its ceiling."""
        try:
            devs = self.track(self.master_key())["devices"]
        except KeyError:
            return None
        for d in devs:
            name = _plain(d["name"])
            if d.get("class_name") == "Limiter" or name in {_plain(n) for n in LIMITERS}:
                on = next((p["value"] for p in d["params"] if p["name"] == "Device On"), 1.0)
                if on >= 0.5:
                    return d["index"]
        return None

    def _do_ceiling(self, rr, a):
        """The limiter's ceiling to MARA's -1 dBFS (the loudness step would set it anyway)."""
        dev = self.active_limiter()
        if dev is None:
            raise RuntimeError("no switched-on limiter on Main")
        ceiling = self._param(self.master_key(), dev, "Ceiling")
        if ceiling is None:
            raise KeyError("the limiter has no ceiling parameter")
        if abs(self._db_of(ceiling) - LIMITER_CEILING_DB) > 0.05:
            self._set(rr, self.master_key(), dev, "Ceiling", target=LIMITER_CEILING_DB)

    def _trim_param(self, idx: int) -> dict | None:
        """Utility's level: «Output» in Live 12, «Gain» in older versions."""
        key = self.master_key()
        return self._param(key, idx, "Output") or self._param(key, idx, "Gain")

    def trim_gain(self) -> float | None:
        try:
            idx = self.master_layout()["trim"]
        except KeyError:
            return None
        if idx is None:
            return None
        p = self._trim_param(idx)
        return self._db_of(p) if p else None

    def _do_trim(self, rr, a):
        idx = self.master_layout()["trim"]
        if idx is None:
            raise RuntimeError("no trim in front of the loudness chain on Main")
        p = self._trim_param(idx)
        if p is None:
            raise KeyError("the trim Utility has no Output or Gain parameter")
        target = max(-TRIM_RANGE_DB, min(TRIM_RANGE_DB, self._db_of(p) + _clamp(float(a["delta_db"]), MAX_TRIM_STEP_DB)))
        self._set(rr, self.master_key(), idx, p["name"], target=target)

    def _ensure_device(self, key: str, name: str, *classes: str) -> int:
        idx = self._device(key, *(c.lower() for c in classes))
        if idx is not None:
            dev = self.track(key)["devices"][idx]
            on = next((p["value"] for p in dev["params"] if p["name"] == "Device On"), 1.0)
            if on < 0.5:   # switched off on purpose: neither use it nor add a second one beside it
                raise RuntimeError(f"{dev['name']} on {self.track(key)['name']} is switched off, so it stays alone")
        if idx is None:
            plugin = self.plugin_named(name)
            count = len(self.track(key)["devices"])
            if plugin:
                at = self.live.load_device(key, plugin, source="plugins")
            else:
                at = self.live.load_device(key, name)
            self._inserted(key, at, count)
            self.refresh()
            if key == self.master_key():
                at = self._place_on_master(at)
            idx = self._device(key, *(c.lower() for c in classes))
            if idx is None and plugin and 0 <= at < len(self.track(key)["devices"]):
                idx = at  # plugins show up under their own name, e.g. «FabFilter Pro-C 2»
            if idx is None:
                raise RuntimeError(f"could not load {name} on {key}")
            if plugin and len(self.track(key)["devices"][idx]["params"]) <= 1:
                count = len(self.track(key)["devices"])
                self.live.delete_device(key, idx)
                self._deleted(key, idx, count)
                self.refresh()
                raise RuntimeError(f"{plugin} shows Live no parameters (it would need «Configure» in Live); removed")
            self.log(f"  + {name} on {self.track(key)['name']}")
        return idx

    def apply(self, actions: list[dict]) -> RoundResult:
        rr = RoundResult()
        for a in actions:
            try:
                getattr(self, "_do_" + a["type"])(rr, a)
                rr.applied.append(a)
            except Exception as e:
                rr.skipped.append({**a, "error": str(e)})
                self.log(f"  ✗ skipped {a.get('type')} on {a.get('track', 'master')}: {e}")
        return rr

    def undo(self, rr: RoundResult) -> None:
        changes = [{"track": c.track, "device": c.device, "param": c.param, "value": c.old_value}
                   for c in reversed(rr.changes) if c.device is not None]
        if changes:
            result = self.live.batch(changes)
            if result.get("errors"):
                raise RuntimeError(f"could not restore parameters: {result['errors']}")
            for c in reversed(rr.changes):  # a parameter changed twice ends at its first old value
                if c.device is not None:
                    self.last_set[(c.track, c.device, c.param)] = c.old_value
            self.refresh()
        for key, name in reversed(rr.created_devices):
            matches = [d for d in self.track(key)["devices"] if d["name"] == name]
            if len(matches) != 1:
                raise RuntimeError(f"cannot safely remove new ducker {name!r} on {key}")
            idx, count = matches[0]["index"], len(self.track(key)["devices"])
            self.live.delete_device(key, idx)
            self._deleted(key, idx, count)
            self.refresh()

    # ── actions ─────────────────────────────────────────────────────────────
    def limiter_gain(self) -> float | None:
        """Gain of the Limiter on the master in dB, None if there is none."""
        try:
            dev = self._device(self.master_key(), *LIMITERS)
        except KeyError:
            return None
        if dev is None:
            return None
        p = self._param(self.master_key(), dev, "Gain")
        return self._db_of(p) if p else None

    def _db_of(self, p: dict) -> float:
        v = parse_display(p["display"])
        return -70.0 if v is None or v == -math.inf else v

    def _do_volume(self, rr, a):
        p = self._param(a["track"], "mixer", "Volume")
        now = self._db_of(p)
        target = min(VOLUME_CEILING_DB, now + _clamp(float(a["delta_db"]), MAX_STEP["volume"]))
        self._set(rr, a["track"], "mixer", "Volume", target=target)

    def _do_send(self, rr, a):
        """Send level of a track to a return («A», «B», …), in dB; a send that is off starts at -30 dB."""
        name = f"Send {str(a['name']).strip().upper()[:1]}"
        p = self._param(a["track"], "mixer", name)
        if p is None:
            raise KeyError(f"{a['track']} has no {name}")
        now = self._db_of(p)
        start = now if now > -60 else -30.0
        target = min(0.0, start + _clamp(float(a["delta_db"]), MAX_STEP["send"]))
        self._set(rr, a["track"], "mixer", name, target=target)

    def _do_pan(self, rr, a):
        p = self._param(a["track"], "mixer", "Pan")
        now = self._db_of(p)
        target = now + _clamp(float(a["value"]) - now, MAX_STEP["pan"])
        self._set(rr, a["track"], "mixer", "Pan", target=max(-50, min(50, target)))

    def _eq_bands(self, key: str, dev: int) -> list[dict]:
        out = []
        for n in range(1, 9):
            on, freq, gain = (self._param(key, dev, f"{n} {x} A") for x in ("Filter On", "Frequency", "Gain"))
            typ = self._param(key, dev, f"{n} Filter Type A")
            if not (on and freq and gain and typ):
                continue
            q = self._param(key, dev, f"{n} Q A") or self._param(key, dev, f"{n} Resonance A")
            qv = parse_display(q["display"]) if q else None
            out.append({"n": n, "on": on["value"] >= 0.5, "freq": self._db_of(freq), "gain": self._db_of(gain),
                        "type": typ, "kind": str(typ.get("display", "")), "q": qv if qv and qv > 0 else 0.71})
        return out

    def _shape_index(self, typ: dict, shape: str) -> int:
        items = [i.lower() for i in typ.get("items", [])]
        for want in SHAPES.get(shape, [shape]):
            for i, item in enumerate(items):
                if want in item:
                    return i
        # Fallback: EQ Eight's usual order (48/12 dB low cut, low shelf, bell, notch, high shelf, …).
        return {"low_cut": 1, "low_shelf": 2, "bell": 3, "notch": 4, "high_shelf": 5}.get(shape, 3)

    def _do_eq(self, rr, a):
        key, shape = a["track"], a.get("shape") or "bell"
        dev = self._ensure_device(key, "EQ Eight", "Eq8", "EQ Eight")
        bands = self._eq_bands(key, dev)
        freq = float(a["freq_hz"])
        same = [b for b in bands if b["on"] and self._shape_index(b["type"], shape) == int(b["type"]["value"])
                and abs(math.log(b["freq"] / freq, RATIO)) <= 1.0]
        if same:  # adjust the band that already sits there
            b = same[0]
            new_gain = _clamp(b["gain"] + _clamp(float(a.get("gain_db") or 0), MAX_STEP["eq"]), EQ_GAIN_LIMIT)
            self._check_master_cut(key, bands, {**b, "gain": new_gain})
            self._set(rr, key, dev, f"{b['n']} Gain A", target=new_gain)
            return
        # Off, or on but doing nothing (Live 12 starts EQ Eight with bands 1–4 on at 0 dB): free to use.
        free = [b for b in bands if not b["on"] or _neutral(b)]
        if not free:
            raise RuntimeError("all eight EQ bands are in use")
        n = free[0]["n"]
        if shape != "low_cut":
            self._check_master_cut(key, bands, {"n": n, "on": True, "freq": freq, "kind": shape.replace("_", " "),
                                                "gain": _clamp(float(a.get("gain_db") or 0), MAX_STEP["eq"]),
                                                "q": float(a.get("q") or 0.71)})
        self._set(rr, key, dev, f"{n} Filter Type A", value=self._shape_index(free[0]["type"], shape))
        self._set(rr, key, dev, f"{n} Frequency A", target=freq)
        if shape != "low_cut":
            self._set(rr, key, dev, f"{n} Gain A", target=_clamp(float(a.get("gain_db") or 0), MAX_STEP["eq"]))
        if a.get("q"):
            self._set(rr, key, dev, f"{n} Resonance A", target=float(a["q"]))
        self._set(rr, key, dev, f"{n} Filter On A", value=1)

    def _check_master_cut(self, key: str, bands: list[dict], changed: dict) -> None:
        """Refuses a master EQ change that would make the bands together cut more than the limit somewhere
        (and deeper than now): a broad excess comes from channels and is fixed there."""
        if key != self.master_key():
            return
        import numpy as np

        f = np.geomspace(30, 16000, 200)
        before = eq_curve(bands, f)
        after = eq_curve([changed if b["n"] == changed["n"] else b for b in bands]
                         + ([changed] if all(b["n"] != changed["n"] for b in bands) else []), f)
        if after.min() < -MASTER_EQ_CUT_LIMIT_DB and after.min() < before.min() - 0.01:
            at = f[int(np.argmin(after))]
            raise RuntimeError(f"the master EQ would cut {-after.min():.1f} dB in sum around {at:.0f} Hz "
                               f"(limit {MASTER_EQ_CUT_LIMIT_DB:.0f} dB): cut the channels that cause it instead")

    def _do_width(self, rr, a):
        key = a["track"]
        dev = self._ensure_device(key, "Utility", "StereoGain", "Utility")
        if a.get("width_pct") is not None:
            for name in ("Stereo Width", "Width"):
                p = self._param(key, dev, name)
                if p:
                    now = self._db_of(p)
                    self._set(rr, key, dev, name, target=now + _clamp(float(a["width_pct"]) - now, MAX_STEP["width"]))
                    break
        if a.get("bass_mono") is not None:
            if self._param(key, dev, "Bass Mono"):
                self._set(rr, key, dev, "Bass Mono", value=1 if a["bass_mono"] else 0)

    def _do_limiter(self, rr, a):
        key = self.master_key()
        dev = self._device(key, *LIMITERS)
        if dev is None:
            name = "Pro-L 2" if self.plugin_named("Pro-L 2") else "Limiter"
            dev = self._ensure_device(key, name, name, "Limiter")
        ceiling = self._param(key, dev, "Ceiling")
        if ceiling and abs(self._db_of(ceiling) - LIMITER_CEILING_DB) > 0.05:
            self._set(rr, key, dev, "Ceiling", target=LIMITER_CEILING_DB)
        gain = self._param(key, dev, "Gain")
        if gain is None:
            raise KeyError("Limiter has no Gain parameter")
        target = max(0.0, min(LIMITER_MAX_GAIN_DB, self._db_of(gain) + _clamp(float(a["delta_db"]), MAX_STEP["limiter"])))
        self._set(rr, key, dev, "Gain", target=target)

    def clipper(self) -> tuple[str, str] | None:
        """(installed plugin name, drive parameter) of the clipper to use, None if none is installed."""
        for name, param in CLIPPERS:
            plugin = self.plugin_named(name)
            if plugin:
                return plugin, param
        return None

    def clipper_gain(self) -> float | None:
        c = self.clipper()
        if c is None:
            return None
        dev = self._device(self.master_key(), c[0])
        if dev is None:
            return 0.0
        p = self._param(self.master_key(), dev, c[1])
        return self._db_of(p) if p else None

    def _do_clipper(self, rr, a):
        """Drive into a clipper right before the master limiter: shaves peaks so the limiter works less."""
        c = self.clipper()
        if c is None:
            raise RuntimeError("no clipper plugin installed")
        plugin, param = c
        key = self.master_key()
        dev = self._device(key, plugin)
        if dev is None:
            dev = self._ensure_device(key, plugin, plugin)
            lim = self._device(key, *LIMITERS)
            if lim is not None and lim < dev:   # place it in front of the limiter
                count = len(self.track(key)["devices"])
                self.live.move_device(key, dev, lim)
                self._moved(key, dev, lim, count)
                self.refresh()
                self.meters_last()
                dev = self._device(key, plugin)
        p = self._param(key, dev, param)
        if p is None:
            raise KeyError(f"{plugin} has no parameter {param!r}")
        target = max(0.0, min(CLIPPER_MAX_GAIN_DB, self._db_of(p) + _clamp(float(a["delta_db"]), MAX_STEP["clipper"])))
        self._set(rr, key, dev, param, target=target)

    COMP = "MARA Comp"

    def _do_compress(self, rr, a):
        """A new Compressor at the end of the channel (before the tap), set from measured levels, its own device so
        undo removes it again. Each value is read back: an older remote script reads «1.00 s» as 1 ms and would set
        a one-second attack without noticing."""
        key = a["track"]
        if any(d["name"] == self.COMP for d in self.track(key)["devices"]):
            raise RuntimeError("MARA already compresses this channel")
        count = len(self.track(key)["devices"])
        at = self.live.load_device(key, "Compressor")
        self._inserted(key, at, count)
        self.refresh()
        dev = self.track(key)["devices"][at]
        if dev.get("class_name") != "Compressor2":
            raise RuntimeError("Live did not load its Compressor")
        name = dev["name"]
        try:
            self.live.request("rename_device", track=key, device=at, name=self.COMP)
            self.refresh()
            name = self.track(key)["devices"][at]["name"]
        except Exception:
            pass
        if sum(d["name"] == name for d in self.track(key)["devices"]) != 1:
            count = len(self.track(key)["devices"])
            self.live.delete_device(key, at)
            self._deleted(key, at, count)
            self.refresh()
            raise RuntimeError("the new compressor cannot be told apart from one already there (update the Live connection)")
        rr.created_devices.append((key, name))
        model = self._param(key, at, "Model")
        if model is not None and "RMS" in model.get("items", []):
            self._set(rr, key, at, "Model", value=model["min"] + model["items"].index("RMS"))
        for param, target in (("Ratio", a["ratio"]), ("Attack", a["attack_ms"]), ("Release", a["release_ms"]),
                              ("Threshold", a["threshold_db"]), ("Output", a.get("makeup_db", 0.0))):
            res = self._set(rr, key, at, param, target=float(target))
            got = _number_ms(res.get("display", ""))
            if got is None or abs(got - float(target)) > max(0.5, abs(float(target)) * 0.15):
                raise RuntimeError(f"Compressor {param} reads {res.get('display')} instead of {target:g}; "
                                   "restart Live so it loads the current MixMatch connection")

    def _do_param(self, rr, a):
        dev = self.track(a["track"])["devices"][int(a["device_index"])]
        if dev["name"].startswith("MARA Duck") and not (a["param"] == "Device On" and float(a["target"]) == 0):
            raise PermissionError("MARA ducking is bounded; generic parameter changes cannot deepen or reconfigure it")
        self._set(rr, a["track"], int(a["device_index"]), a["param"], target=float(a["target"]))

    def _do_load_device(self, rr, a):
        self._ensure_device(a["track"], a["name"], a["name"])

    def _do_duck(self, rr, a):
        from .taps import is_reference_name

        key, source = a["track"], a["source_track"]
        if not self.set_state.get("capabilities", {}).get("sidechain_ducking"):
            raise RuntimeError("update the MixMatch remote script to enable sidechain routing")
        if any(is_reference_name(self.track(k)["name"]) for k in (key, source)):
            raise ValueError("reference tracks cannot participate in ducking")
        # This action only creates a dedicated device. It never modifies or routes
        # a user's existing compressor; the remote command is atomic on failure.
        count = len(self.track(key)["devices"])
        result = self.live.request("create_ducker", track=key, source_track=source,
                                   source_level_dbfs=float(a["source_level_dbfs"]),
                                   reduction_db=float(a.get("reduction_db") or 2.0),
                                   release_ms=float(a.get("release_ms") or 120.0))
        idx = result["index"]
        self._inserted(key, idx, count)
        rr.created_devices.append((key, result["name"]))
        # Bypass first when undoing, then remove the added device. Other parameters
        # in the round are restored before removal changes the device indices.
        rr.changes.append(Change(key, idx, "Device On", 0.0, {"value": 1.0, "display": "On"}))
        self.last_set[(key, idx, "Device On")] = 1.0
        self.refresh()
