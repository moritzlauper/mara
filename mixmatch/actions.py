"""Turns the agent's decisions into parameter changes in Live, in small, reversible steps.

Actions (what the planner may ask for):

  {"type": "volume",  "track": "t3", "delta_db": -1.5}
  {"type": "pan",     "track": "t3", "value": -20}                 # -50 … 50
  {"type": "eq",      "track": "t3", "freq_hz": 80, "gain_db": -2, "q": 1.0, "shape": "bell"}
  {"type": "width",   "track": "t3", "width_pct": 80, "bass_mono": true}
  {"type": "limiter", "delta_db": 1.0}                              # master loudness
  {"type": "param",   "track": "t3", "device_index": 2, "param": "Threshold", "target": -18}
  {"type": "load_device", "track": "t3", "name": "Compressor"}

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
VOLUME_CEILING_DB = 6.0
LIMITER_CEILING_DB = -1.0
LIMITERS = ("limiter", "pro-l 2")   # Live's Limiter or FabFilter Pro-L 2 on the master
LIMITER_MAX_GAIN_DB = 6.0
# Clippers before the master limiter, as (plugin, drive parameter); the first installed one is used.
CLIPPERS = (("Classic Clipper", "Gain"),)
CLIPPER_MAX_GAIN_DB = 4.0   # beyond this, limiting starts to flatten and distort the mix
SHAPES = {"bell": ["bell"], "low_shelf": ["low shelf", "lowshelf"], "high_shelf": ["high shelf", "highshelf"],
          "low_cut": ["low cut 12", "lowcut 12", "low cut", "lowcut", "12 db"], "notch": ["notch"]}


@dataclass
class Change:
    track: str
    device: object
    param: object
    old_value: float
    new: dict = field(default_factory=dict)


@dataclass
class RoundResult:
    changes: list[Change] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _clamp(x: float, limit: float) -> float:
    return max(-limit, min(limit, x))


class Executor:
    def __init__(self, live, log=print):
        self.live = live
        self.log = log
        self.set_state: dict = {}
        self.locks: set[tuple] = set()
        self.last_set: dict[tuple, float] = {}   # what we set last time, to notice the user's hand
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
        rr.changes.append(Change(key, device, name, p["value"], res))
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

    def _inserted(self, key: str, at: int, count_before: int) -> None:
        self._reindex(key, list(range(at)) + [None] + list(range(at, count_before)))

    def _deleted(self, key: str, idx: int, count_before: int) -> None:
        self._reindex(key, [i for i in range(count_before) if i != idx])

    def _moved(self, key: str, frm: int, to: int, count: int) -> None:
        order = [i for i in range(count) if i != frm]
        order.insert(to, frm)
        self._reindex(key, order)

    def _ensure_device(self, key: str, name: str, *classes: str) -> int:
        idx = self._device(key, *(c.lower() for c in classes))
        if idx is None:
            plugin = self.plugin_named(name)
            count = len(self.track(key)["devices"])
            if plugin:
                at = self.live.load_device(key, plugin, source="plugins")
            else:
                at = self.live.load_device(key, name)
            self._inserted(key, at, count)
            self.refresh()
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
                   for c in reversed(rr.changes)]
        if changes:
            self.live.batch(changes)
            for c in reversed(rr.changes):  # a parameter changed twice ends at its first old value
                self.last_set[(c.track, c.device, c.param)] = c.old_value
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
            out.append({"n": n, "on": on["value"] >= 0.5, "freq": self._db_of(freq), "gain": self._db_of(gain),
                        "type": typ})
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
            self._set(rr, key, dev, f"{b['n']} Gain A", target=new_gain)
            return
        # Off, or on but doing nothing (Live 12 starts EQ Eight with bands 1–4 on at 0 dB): free to use.
        free = [b for b in bands if not b["on"] or _neutral(b)]
        if not free:
            raise RuntimeError("all eight EQ bands are in use")
        n = free[0]["n"]
        self._set(rr, key, dev, f"{n} Filter Type A", value=self._shape_index(free[0]["type"], shape))
        self._set(rr, key, dev, f"{n} Frequency A", target=freq)
        if shape != "low_cut":
            self._set(rr, key, dev, f"{n} Gain A", target=_clamp(float(a.get("gain_db") or 0), MAX_STEP["eq"]))
        if a.get("q"):
            self._set(rr, key, dev, f"{n} Resonance A", target=float(a["q"]))
        self._set(rr, key, dev, f"{n} Filter On A", value=1)

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
                dev = self._device(key, plugin)
        p = self._param(key, dev, param)
        if p is None:
            raise KeyError(f"{plugin} has no parameter {param!r}")
        target = max(0.0, min(CLIPPER_MAX_GAIN_DB, self._db_of(p) + _clamp(float(a["delta_db"]), MAX_STEP["clipper"])))
        self._set(rr, key, dev, param, target=target)

    def _do_param(self, rr, a):
        self._set(rr, a["track"], int(a["device_index"]), a["param"], target=float(a["target"]))

    def _do_load_device(self, rr, a):
        self._ensure_device(a["track"], a["name"], a["name"])
