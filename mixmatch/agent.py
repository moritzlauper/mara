"""The mixing loop: listen, compare, decide, adjust, check, for as long as it helps.

1. Understand the set: tracks, devices, which tap belongs to which track,
   references, roles of all channels.
2. For each section (between cue points, or the current loop): loop it, measure
   a few bars, let the planner decide 1–4 small steps, apply them, wait for the
   sound to settle, measure again. A round that makes the distance to the
   target worse is undone and remembered, so it is not tried again.
3. Stop when the targets are met, nothing helps any more, the time is up or
   the user stops it. Every round is logged to runs/<time>.jsonl.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from . import roles as roles_mod
from .actions import Executor
from .analysis import Comparison, compare, masking, objective, stats
from .bands import CENTRES
from .planner import device_summary

VOCAL_ROLES = {"lead_vocal", "backing_vocal", "vocal_fx"}


class RealEnv:
    """Wall clock; the song plays in Live."""

    def now(self) -> float:
        return time.time()

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class Settings:
    minutes: float = 10.0
    bars: int = 4                 # measurement window per round
    settle: float = 1.5           # seconds after a change before measuring (reverb tails, compressor release)
    max_rounds_per_section: int = 12
    stall_rounds: int = 4         # stop a section after this many rounds without improvement
    min_gain: float = 0.05        # a round must improve the objective by at least this much
    assist: bool = False          # ask before applying
    mode: str = "auto"            # beat (no vocals) | song | auto (beat if no vocal channel is found)
    loudness_from: str = "full"   # beat mode: loudness target from the full references or their instrumentals
    roles_file: str = "roles.json"
    log_dir: str = "runs"


@dataclass
class Round:
    section: str
    n: int
    before: float
    after: float | None
    plan: dict
    applied: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    undone: bool = False


class Agent:
    def __init__(self, live, hub, planner, settings: Settings | None = None, env=None, log=print, ask=input,
                 references=None):
        self.live, self.hub, self.planner = live, hub, planner
        self.s = settings or Settings()
        self.env = env or RealEnv()
        self.log, self.ask = log, ask
        self.ex = Executor(live, log)
        self.history: list[Round] = []
        self.roles: dict[str, roles_mod.Role] = {}
        self.tap_of: dict[str, object] = {}       # track key → tap
        self.references: list = []          # reference taps inside the project
        self.refset = references            # ReferenceSet from audio files (preferred)
        self.target = None
        self.beat = False
        self.logfile = None

    # ── 1. understand the set ───────────────────────────────────────────────
    def window_seconds(self) -> float:
        tempo = self.ex.set_state.get("tempo") or 120
        return max(4.0, self.s.bars * 4 * 60.0 / tempo)

    def match_taps(self) -> None:
        taps = self.hub.by_name()
        self.tap_of, self.references = {}, []
        for t in self.ex.set_state["tracks"]:
            tap = taps.get((t["kind"], t["name"]))
            if tap is None or t["kind"] == "master":
                continue
            if tap.is_reference:
                self.references.append(tap)
            else:
                self.tap_of[t["key"]] = tap
        missing = [t["name"] for t in self.ex.set_state["tracks"]
                   if t["key"] not in self.tap_of and not t["name"].lower().startswith("ref")
                   and not t.get("is_group") and t["kind"] != "return"
                   and not (t["kind"] == "master" and any(tap.kind == "master" for tap in taps.values()))]
        self.log(f"Taps: {len(self.tap_of)} channels, {len(self.references)} reference(s)"
                 + (f"; no tap on: {', '.join(missing)}" if missing else ""))

    def understand(self) -> None:
        self.ex.refresh()
        self.match_taps()
        since = self.env.now() - 60
        for t in self.ex.set_state["tracks"]:
            if t["key"] == "m" or t["key"] not in self.tap_of:
                continue
            self.roles[t["key"]] = roles_mod.guess(t, stats(self.tap_of[t["key"]].window(since, self.env.now())))
        roles_mod.apply_overrides(self.roles, self.s.roles_file)
        if not os.path.exists(self.s.roles_file):
            roles_mod.save_template(self.roles, self.s.roles_file)
            self.log(f"Wrote {self.s.roles_file}: correct the roles there if needed, they are read at the next start.")
        for r in self.roles.values():
            self.log(f"  {r.name:<24} {r.role:<14} priority {r.priority}  ({r.source})")
        has_vocals = any(r.role in VOCAL_ROLES for r in self.roles.values())
        self.beat = self.s.mode == "beat" or (self.s.mode == "auto" and not has_vocals)
        self.log(f"Mode: {'beat (no vocals)' if self.beat else 'song with vocals'}")
        if self.refset:
            self.target = self.refset.target(self.beat, self.s.loudness_from)
            self.log(f"Target: {self.target.description}, {self.target.loudness:.1f} LUFS")

    def sections(self) -> list[tuple[str, float, float]]:
        cues = self.ex.set_state.get("cue_points") or []
        if len(cues) >= 2:
            return [(c["name"] or f"section {i+1}", c["time"], cues[i + 1]["time"] - c["time"])
                    for i, c in enumerate(cues[:-1]) if cues[i + 1]["time"] > c["time"]]
        loop = self.ex.set_state["loop"]
        return [("loop", loop["start"], loop["length"])]

    # ── 2. measure ──────────────────────────────────────────────────────────
    def measure(self) -> dict:
        self.env.wait(self.window_seconds())
        end = self.env.now()
        start = end - self.window_seconds()
        master_tap = next((t for t in self.hub.snapshot() if t.kind == "master"), None)  # «Main» in Live 12
        master = stats(master_tap.window(start, end)) if master_tap else None
        tracks = {k: stats(tap.window(start, end)) for k, tap in self.tap_of.items()}
        if self.target:
            ref = self.target.stats
            cmp = compare(master, ref, self.target.loudness, self.target.tolerance, self.target.one_sided) if master else None
        else:
            taps = self.references
            if self.beat and any("inst" in t.name.lower() for t in taps):
                taps = [t for t in taps if "inst" in t.name.lower()]
            refs = [s for s in (stats(r.window(start, end)) for r in taps) if s]
            ref = refs[0] if len(refs) == 1 else _average(refs) if refs else None
            cmp = compare(master, ref) if master and ref else None
        masks = self._maskings(tracks)
        penalty = sum(m["severity"] * min(m["priority_a"], m["priority_b"]) / 5 for m in masks[:4])
        return {"master": master, "ref": ref, "tracks": tracks, "cmp": cmp, "masking": masks,
                "objective": objective(cmp, penalty) if cmp else penalty}

    def _maskings(self, tracks: dict) -> list[dict]:
        out = []
        keys = [k for k, s in tracks.items() if s is not None and k in self.roles]
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                ra, rb = self.roles[a], self.roles[b]
                if ra.role in ("fx", "ambience", "bus", "drums_bus") or rb.role in ("fx", "ambience", "bus", "drums_bus"):
                    continue
                hits = masking(tracks[a], tracks[b])
                if hits:
                    band, sev = hits[0]
                    out.append({"track_a": a, "track_b": b, "role_a": ra.role, "role_b": rb.role,
                                "freq_hz": round(CENTRES[band]), "severity": round(sev, 2),
                                "priority_a": ra.priority, "priority_b": rb.priority})
        return sorted(out, key=lambda m: -m["severity"] * (m["priority_a"] + m["priority_b"]))

    # ── 3. decide ───────────────────────────────────────────────────────────
    def context(self, section: str, m: dict) -> dict:
        cmp: Comparison | None = m["cmp"]
        channels = []
        for key, r in self.roles.items():
            st = m["tracks"].get(key)
            t = self.ex.track(key)
            channels.append({
                "track": key, "name": r.name, "role": r.role, "priority": r.priority, "note": r.note,
                "volume": next((p["display"] for p in t["mixer"] if p["name"] == "Volume"), None),
                "pan": next((p["display"] for p in t["mixer"] if p["name"] == "Pan"), None),
                "measured": st.summary() if st else "silent in this section",
                "loudness_vs_master_lu": round(st.loudness - m["master"].loudness, 1) if st and m["master"] else None,
                "devices": device_summary(t),
            })
        ctx = {
            "section": section,
            "mode": ("beat without vocals: the target comes from the references with their vocals removed; "
                     "do not fill the vocal range just because the reference songs have vocals")
                    if self.beat else "song with vocals",
            "target": self.target.description if self.target else "reference tracks playing in the project",
            "objective": round(m["objective"], 2),
            "master": m["master"].summary() if m["master"] else None,
            "reference": m["ref"].summary() if m["ref"] else "no reference (pass --ref files or name tracks «REF …»)",
            "master_vs_reference": {**cmp.summary(), "_tonal_excess": [float(x) for x in cmp.tonal_excess]} if cmp else None,
            "master_devices": device_summary(self.ex.track("m")),
            "channels": channels,
            "masking": m["masking"][:8],
            "locked": [list(map(str, l)) for l in sorted(self.ex.locks, key=str)],
            "history": [
                {"section": h.section, "round": h.n, "objective_before": round(h.before, 2),
                 "objective_after": None if h.after is None else round(h.after, 2), "undone": h.undone,
                 "actions": [{k: v for k, v in a.items() if v is not None} for a in h.applied],
                 "assessment": h.plan.get("assessment", "")}
                for h in self.history[-10:]
            ],
        }
        return ctx

    # ── 4. the loop ─────────────────────────────────────────────────────────
    def run(self) -> list[Round]:
        os.makedirs(self.s.log_dir, exist_ok=True)
        self.logfile = open(os.path.join(self.s.log_dir, time.strftime("%Y%m%d-%H%M%S") + ".jsonl"), "w")
        deadline = self.env.now() + self.s.minutes * 60
        self.understand()
        if not self.references and not self.target:
            self.log("No reference found. Name the reference tracks «REF …» and put a tap on them; "
                     "until then only the balance between channels is worked on.")
        for name, start, length in self.sections():
            if self.env.now() >= deadline:
                break
            self.log(f"\n▶ {name}")
            self.live.transport(loop_start=start, loop_length=length, loop=True, time=start, play=True)
            self.section(name, deadline)
        self.log("\nDone. " + self.report())
        self.logfile.close()
        return self.history

    def section(self, name: str, deadline: float) -> None:
        m = self.measure()
        best, stall = m["objective"], 0
        for n in range(1, self.s.max_rounds_per_section + 1):
            if self.env.now() >= deadline:
                self.log("  time is up")
                return
            self.ex.refresh()
            plan = self.planner.plan(self.context(name, m))
            self.log(f"  round {n}: {plan.get('assessment', '')}")
            actions = plan.get("actions", [])
            if not actions:
                if plan.get("done"):
                    self.log("  targets reached for this section")
                return
            for a in actions:
                self.log(f"    · {a['type']} {a.get('track') or 'master'}: {a.get('reason', '')}")
            if self.s.assist and self.ask("    apply? [Y/n] ").strip().lower() == "n":
                self._record(Round(name, n, best, None, plan))
                continue
            rr = self.ex.apply(actions)
            if not rr.changes:
                self._record(Round(name, n, best, best, plan, rr.applied, rr.skipped))
                stall += 1
                if stall >= self.s.stall_rounds:
                    return
                continue
            self.env.wait(self.s.settle)
            after = self.measure()
            rnd = Round(name, n, best, after["objective"], plan, rr.applied, rr.skipped)
            if after["objective"] < best - self.s.min_gain:
                self.log(f"    ✓ {best:.2f} → {after['objective']:.2f}")
                best, m, stall = after["objective"], after, 0
            elif after["objective"] > best + self.s.min_gain:
                self.ex.undo(rr)
                rnd.undone = True
                self.log(f"    ↺ undone: {best:.2f} → {after['objective']:.2f}")
                self.env.wait(self.s.settle)
                m = self.measure()
                best = min(best, m["objective"])
                stall += 1
            else:  # no measurable effect: keep it, but it does not count as progress
                self.log(f"    = no measurable change ({after['objective']:.2f})")
                m = after
                stall += 1
            self._record(rnd)
            if plan.get("done") or stall >= self.s.stall_rounds:
                self.log("  no further improvement in this section" if stall else "  targets reached")
                return

    def _record(self, rnd: Round) -> None:
        self.history.append(rnd)
        if self.logfile:
            self.logfile.write(json.dumps(rnd.__dict__, default=str, ensure_ascii=False) + "\n")
            self.logfile.flush()

    def report(self) -> str:
        kept = [h for h in self.history if not h.undone and h.after is not None]
        undone = sum(h.undone for h in self.history)
        if not self.history:
            return "Nothing changed."
        first, last = self.history[0].before, min((h.after for h in kept), default=self.history[0].before)
        return (f"{len(self.history)} rounds, {len(kept)} kept, {undone} undone. "
                f"Distance to target {first:.2f} → {last:.2f}.")


def _average(refs):
    """Several references: average their levels, so no single track dictates the target."""
    import numpy as np
    from .analysis import Stats

    mean = lambda attr: float(np.mean([getattr(r, attr) for r in refs]))
    bands = np.mean([r.bands_db for r in refs], axis=0)
    loud = mean("loudness")
    return Stats(frames=sum(r.frames for r in refs), loudness=loud, bands_db=bands, shape_db=bands - loud,
                 peak_db=mean("peak_db"), plr=mean("plr"), width_db=mean("width_db"),
                 low_side_db=mean("low_side_db"), active=1.0)
