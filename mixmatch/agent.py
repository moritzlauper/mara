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
import math
import os
import time
from dataclasses import dataclass, field, replace

import numpy as np

from . import roles as roles_mod
from .actions import CLIPPER_MAX_GAIN_DB, LIMITER_MAX_GAIN_DB, Executor
from .analysis import Comparison, compare, masking, objective, objective_parts, stats
from .bands import CENTRES
from .display import parse_display
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
    minutes: float | None = 10.0  # upper limit; None = until the run is complete
    bars: int = 8                 # measurement window per round, from the loop start (shorter loops: all of it)
    settle: float = 1.5           # seconds after a change before measuring (reverb tails, compressor release)
    max_rounds_per_section: int = 12
    stall_rounds: int = 4         # stop a section after this many rounds without improvement
    min_gain: float = 0.05        # a round must improve the objective by at least this much
    assist: bool = False          # ask before applying
    mode: str = "auto"            # beat (no vocals) | song | auto (beat if no vocal channel is found)
    loudness_from: str = "full"   # beat mode: loudness target from the full references or their instrumentals
    roles_file: str = "roles.json"
    log_dir: str = "runs"
    focus: str = "sections"       # sections: work through cue points / loop | loud: only the loudest part
    focus_bars: int = 16          # length of the loudest part
    complete: bool = False        # after the master: one pass over every channel, then a final master check
    channel_bars: int = 8         # loop length when a channel is worked on outside the loudest part
    channel_rounds: int = 2       # rounds per channel in the channel pass
    final_rounds: int = 4         # rounds of the final master check
    resume: str | None = None     # track name: skip the master phase and start the channel pass there


@dataclass
class Scan:
    """The range played once: every frame of the master and of each channel, with its song position."""

    start: float                  # beat where the range starts
    total: float                  # length in beats
    master: list                  # (beat offset, frame)
    channels: dict                # track key → [(beat offset, frame)]


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


class PlannerFailed(Exception):
    pass


class MasterSilent(Exception):
    """The master tap reports nothing: Live is not playing, exporting, or its audio engine is off."""


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
        self.target_no_vocals = None        # used in parts where the vocal channels are silent
        self.loop: tuple[float, float] | None = None   # (start, length) of the loop the agent plays
        self.beat = False
        self.logfile = None

    # ── 1. understand the set ───────────────────────────────────────────────
    def window_seconds(self) -> float:
        tempo = self.ex.set_state.get("tempo") or 120
        beats = self.s.bars * 4
        if self.loop is not None:  # aligned to the loop: the whole loop, at most `bars`
            beats = min(beats, self.loop[1])
        return max(4.0, beats * 60.0 / tempo)

    def play_loop(self, start: float, length: float) -> None:
        self.live.transport(loop_start=start, loop_length=length, loop=True, time=start, play=True)
        self.loop = (start, length)

    def wait_for_loop_start(self) -> None:
        """Waits until playback is back at the loop start, so every measurement hears the same bars."""
        if self.loop is None:
            return
        start, length = self.loop
        tempo = self.ex.set_state.get("tempo") or 120
        deadline = self.env.now() + length * 60 / tempo + 2
        last = None
        while self.env.now() < deadline:
            pos = self.live.transport()["song_time"]
            if last is not None and pos < last - 0.5:     # wrapped from the loop end to its start
                return
            if last is None and abs(pos - start) < 0.1:   # just started there
                return
            last = pos
            self.env.wait(0.05)

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

    def scan(self, sections: list[tuple[str, float, float]]) -> Scan | None:
        """Plays the whole range once and records where the master and every channel play, and how loud."""
        start = min(s[1] for s in sections)
        total = max(s[1] + s[2] for s in sections) - start
        tempo = self.ex.set_state.get("tempo") or 120
        self.log(f"Listening to the whole song once: {total / 4:.0f} bars ({total * 60 / tempo:.0f} s) …")
        self.live.transport(loop_start=start, loop_length=total, loop=True, time=start, play=True)
        # Live does not jump to the start at once, so the position is read back instead of assumed.
        t0, marks, last = self.env.now(), [], -1.0
        deadline = t0 + total * 60 / tempo + 10
        while self.env.now() < deadline:
            pos = self.live.transport()["song_time"]
            if marks and pos < last - 1:      # wrapped around: one pass is done
                break
            marks.append((self.env.now(), pos))
            last = pos
            self.env.wait(0.25)
        if len(marks) < 2:
            return None
        times, beats = [m[0] for m in marks], [m[1] - start for m in marks]

        def positioned(tap):
            out = []
            for f in tap.window(t0, self.env.now()):
                if times[0] <= f.t <= times[-1]:
                    b = float(np.interp(f.t, times, beats))
                    if b >= beats[0] - 0.5:
                        out.append((b, f))
            return out

        master_tap = next((t for t in self.hub.snapshot() if t.kind == "master"), None)
        return Scan(start, total, positioned(master_tap) if master_tap else [],
                    {k: positioned(tap) for k, tap in self.tap_of.items()})

    def loudest_part(self, sections: list[tuple[str, float, float]], scan: Scan | None = None) -> tuple[str, float, float]:
        """The loudest `focus_bars` bars, measured at the master.

        The references are judged by their loudest part (drop, chorus), so the mix should be too.
        """
        start = min(s[1] for s in sections)
        total = max(s[1] + s[2] for s in sections) - start
        length = min(total, self.s.focus_bars * 4.0)
        if length >= total:
            return sections[0]
        scan = scan or self.scan(sections)
        found = loudest_window(scan.master, scan.total, length) if scan else None
        if found is None:
            self.log("  the master stayed silent; working on the whole range")
            return sections[0]
        offset, loudness = found
        bar = (scan.start + offset) / 4 + 1
        self.log(f"  loudest part: bars {bar:.0f}–{bar + length / 4:.0f}, {loudness:.1f} LUFS")
        return ("loudest part", scan.start + offset, length)

    def channel_region(self, key: str, scan: Scan, main: tuple[str, float, float] | None) -> tuple[float, float] | None:
        """Where to work on a channel: the loudest part if it plays there, else its own loudest bars."""
        frames = scan.channels.get(key, [])
        if main is not None:
            _, s, length = main
            st = stats([f for b, f in frames if s - scan.start <= b < s - scan.start + length])
            if st is not None and st.active >= 0.5:
                return s, length
        length = min(self.s.channel_bars * 4.0, scan.total)
        found = loudest_window(frames, scan.total, length)
        if found is None:
            return None
        return scan.start + found[0], length

    def channel_pass(self, scan: Scan, main: tuple[str, float, float] | None, deadline: float) -> None:
        """Every channel once, most important first: level, EQ, masking, width, dynamics, each where it plays."""
        order = sorted(self.roles.values(), key=lambda r: (-r.priority, r.name))
        if self.s.resume:
            names = [r.name for r in order]
            if self.s.resume not in names:
                raise ValueError(f"--resume: no channel named {self.s.resume!r}")
            order = order[names.index(self.s.resume):]
            self.log(f"\n━━ Channel pass, resumed at {self.s.resume}: {len(order)} channels left")
        else:
            self.log(f"\n━━ Channel pass: {len(order)} channels")
        for r in order:
            if self.env.now() >= deadline:
                self.log("  time is up")
                return
            region = self.channel_region(r.key, scan, main)
            if region is None:
                self.log(f"\n▶ {r.name}: silent in the whole song, skipped")
                continue
            start, length = region
            bar = start / 4 + 1
            self.log(f"\n▶ {r.name} ({r.role}, priority {r.priority}), bars {bar:.0f}–{bar + length / 4:.0f}")
            self.play_loop(start, length)
            self.section(f"channel {r.name}", deadline, focus=r.key, max_rounds=self.s.channel_rounds,
                         scoring="balance")

    # ── 2. measure ──────────────────────────────────────────────────────────
    def measure(self, scoring: str = "full", retries: int = 6) -> dict:
        """`scoring` «balance» leaves out loudness and dynamics: a quiet part need not sound like the drop.

        A silent master is never a result: MixMatch waits and measures again, and gives up with MasterSilent.
        """
        for attempt in range(retries + 1):
            self.wait_for_loop_start()
            self.env.wait(self.window_seconds())
            end = self.env.now()
            start = end - self.window_seconds()
            master_tap = next((t for t in self.hub.snapshot() if t.kind == "master"), None)  # «Main» in Live 12
            master = stats(master_tap.window(start, end)) if master_tap else None
            if master_tap is None or (master is not None and master.active >= 0.5):
                break  # a master that plays in less than half the window (audio dropped out) does not count
            if attempt == retries:
                raise MasterSilent("the master stayed silent")
            self.log("  ⚠ the master is silent (Live stopped, exporting, or audio engine off?): waiting …")
            self.env.wait(10)
            try:
                self.live.transport(play=True)
            except Exception:
                pass  # Live busy (e.g. exporting): try again after the next wait
        gains = self.fader_gains()
        tracks = {k: post_fader(stats(tap.window(start, end)), gains.get(k, 0.0)) for k, tap in self.tap_of.items()}
        if self.target:
            target = self.target
            vocals = [k for k, r in self.roles.items() if r.role in VOCAL_ROLES]
            if not self.beat and vocals and all(tracks.get(k) is None for k in vocals) and self.refset:
                # No vocals in this part: like a beat, the vocal range only counts where the mix is louder.
                if self.target_no_vocals is None:
                    self.target_no_vocals = self.refset.target(True, self.s.loudness_from)
                target = self.target_no_vocals
            ref = target.stats
            cmp = compare(master, ref, target.loudness, target.tolerance, target.one_sided) if master else None
        else:
            taps = self.references
            if self.beat and any("inst" in t.name.lower() for t in taps):
                taps = [t for t in taps if "inst" in t.name.lower()]
            refs = [s for s in (stats(r.window(start, end)) for r in taps) if s]
            ref = refs[0] if len(refs) == 1 else _average(refs) if refs else None
            cmp = compare(master, ref) if master and ref else None
        masks = self._maskings(tracks)
        penalty = sum(m["severity"] * min(m["priority_a"], m["priority_b"]) / 5 for m in masks[:4])
        scored = cmp
        if cmp and scoring == "balance":
            scored = replace(cmp, loudness_diff=0.0, plr_diff=0.0)
        elif cmp and cmp.loudness_diff < 0 and self.loudness_exhausted():
            # Limiter (and clipper) as far as they may go: the rest of the gap is accepted, not chased with faders.
            scored = replace(cmp, loudness_diff=0.0)
        return {"master": master, "ref": ref, "tracks": tracks, "cmp": cmp, "masking": masks,
                "objective": objective(scored, penalty) if scored else penalty,
                "parts": objective_parts(scored, penalty) if scored else {"masking": penalty}}

    def limiter_at_cap(self) -> bool:
        gain = self.ex.limiter_gain() if self.ex.set_state else None
        return gain is not None and gain >= LIMITER_MAX_GAIN_DB - 0.05

    def loudness_exhausted(self) -> bool:
        """Limiter at its cap, and the clipper too (or none installed)."""
        if not self.limiter_at_cap():
            return False
        clip = self.ex.clipper_gain()
        return clip is None or clip >= CLIPPER_MAX_GAIN_DB - 0.05

    def fader_gains(self) -> dict[str, float]:
        """dB the mixer adds after each channel's tap: its own fader plus those of the groups it sits in.

        The tap is the last device, so it measures before the fader; this turns its numbers into what reaches the mix.
        """
        tracks = self.live.get_set()["tracks"]
        groups = {t["name"]: t for t in tracks if t.get("is_group")}

        def volume(t):
            v = next((parse_display(p["display"]) for p in t["mixer"] if p["name"] == "Volume"), None)
            return 0.0 if v is None else v

        out = {}
        for t in tracks:
            if t["kind"] == "master":
                continue
            gain, seen, parent = volume(t), set(), groups.get(t.get("group"))
            while parent is not None and parent["name"] not in seen:
                seen.add(parent["name"])
                gain += volume(parent)
                parent = groups.get(parent.get("group"))
            out[t["key"]] = gain
        return out

    def _group_members(self) -> dict[str, set[str]]:
        """Group track key → keys of every track inside it (also inside nested groups)."""
        tracks = self.ex.set_state.get("tracks", [])
        by_name = {t["name"]: t for t in tracks if t.get("is_group")}
        out: dict[str, set[str]] = {}
        for t in tracks:
            parent, seen = by_name.get(t.get("group")), set()
            while parent is not None and parent["name"] not in seen:
                seen.add(parent["name"])
                out.setdefault(parent["key"], set()).add(t["key"])
                parent = by_name.get(parent.get("group"))
        return out

    def _maskings(self, tracks: dict) -> list[dict]:
        out = []
        keys = [k for k, s in tracks.items() if s is not None and k in self.roles]
        inside = self._group_members()
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if b in inside.get(a, ()) or a in inside.get(b, ()):
                    continue  # a group contains its own tracks: that is not masking
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
    def context(self, section: str, m: dict, focus: str | None = None) -> dict:
        cmp: Comparison | None = m["cmp"]
        channels = []
        for key, r in self.roles.items():
            st = m["tracks"].get(key)
            t = self.ex.track(key)
            channels.append({
                "track": key, "name": r.name, "role": r.role, "priority": r.priority, "note": r.note,
                "volume": next((p["display"] + (" (automated)" if p.get("automated") else "")
                                for p in t["mixer"] if p["name"] == "Volume"), None),
                "pan": next((p["display"] for p in t["mixer"] if p["name"] == "Pan"), None),
                "sends": {p["name"]: p["display"] for p in t["mixer"] if p["name"].startswith("Send ")} or None,
                "kind": "group" if t.get("is_group") else t["kind"],
                "group": t.get("group"),
                "measured": st.summary() if st else "silent in this section",
                "loudness_vs_master_lu": round(st.loudness - m["master"].loudness, 1) if st and m["master"] else None,
                "devices": device_summary(t),
            })
        ctx = {
            "section": section,
            "mode": ("beat without vocals, vocals may be recorded later: the target comes from the references with "
                     "their vocals removed. The beat must sound complete on its own: remove harshness and resonances in "
                     "1–5 kHz where they stick out, but keep presence and air; do not hollow out that range in advance, "
                     "and do not boost instruments into it to imitate a voice. Do not add low end to match the target")
                    if self.beat else "song with vocals",
            "target": self.target.description if self.target else "reference tracks playing in the project",
            "objective": round(m["objective"], 2),
            "master": m["master"].summary() if m["master"] else None,
            "reference": m["ref"].summary() if m["ref"] else "no reference (pass --ref files or name tracks «REF …»)",
            "master_vs_reference": {**cmp.summary(), "_tonal_excess": [float(x) for x in cmp.tonal_excess]} if cmp else None,
            "master_devices": device_summary(self.ex.track("m")),
            "limiter": {"gain_db": self.ex.limiter_gain(), "max_gain_db": LIMITER_MAX_GAIN_DB,
                        "at_max": self.limiter_at_cap()},
            "clipper": ({"plugin": self.ex.clipper()[0], "gain_db": self.ex.clipper_gain(),
                         "max_gain_db": CLIPPER_MAX_GAIN_DB} if self.ex.clipper() else "none installed"),
            "loudness_note": ("limiter and clipper are at their maximum: the remaining loudness gap is accepted and "
                              "does not count; do not raise faders to chase loudness") if self.loudness_exhausted() else "",
            "installed_plugins": self.ex.plugins[:80],
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
        if focus is None and section == "final check":
            ctx["final_check"] = (
                "Final check of the master after the channel pass. If the master is quieter than the target and the "
                "limiter or clipper still has room (see «limiter», «clipper»), raise them first: limiter up to its "
                "maximum, then the clipper. After that only small master corrections (a broad master EQ move of at most "
                "1.5 dB for an overall tilt the channels did not fix). Channels are done; do not change them here.")
        if focus is not None and self.ex.track(focus).get("is_group"):
            r = self.roles[focus]
            ctx["task"] = (
                f"Channel pass on the group (bus) {focus} «{r.name}»: it sums the tracks inside it (see «group» of the "
                "channels). Check its level in the mix, its bus devices (compressor amount: gentle glue, 1–3 dB of gain "
                "reduction; load one only if the group clearly needs it), and EQ that belongs to the whole group. Do not "
                "fix single tracks here. 0 to 3 actions, all on this track; none if it sits well (done=true).")
        elif focus is not None and self.ex.track(focus)["kind"] == "return":
            r = self.roles[focus]
            ctx["task"] = (
                f"Channel pass on the return {focus} «{r.name}» (reverb or delay fed by the channels' sends). Check its "
                "level, EQ (cut lows below ~200–300 Hz and harsh highs so it does not muddy the mix), width, and which "
                "channels send how much (send actions on those channels are allowed in this pass). 0 to 3 actions; "
                "none if it sits well (done=true).")
        elif focus is not None:
            r = self.roles[focus]
            ctx["task"] = (
                f"Channel pass: work only on track {focus} «{r.name}» (role {r.role}, priority {r.priority}"
                + (f", note: {r.note}" if r.note else "") + "). Check, in this order: its level against its role and "
                "priority; EQ (cut what it does not need: rumble below its range, mud, harshness, resonances; make room "
                "where it masks or is masked by a more important channel); width and pan (low end mono); dynamics "
                "(load a Compressor only if it clearly needs control). 0 to 3 actions, all on this track. If it already "
                "sits well, return no actions and done=true. Master loudness is not a goal in this pass.")
            ctx["channels"].sort(key=lambda c: c["track"] != focus)
        return ctx

    # ── 4. the loop ─────────────────────────────────────────────────────────
    def run(self) -> list[Round]:
        os.makedirs(self.s.log_dir, exist_ok=True)
        self.logfile = open(os.path.join(self.s.log_dir, time.strftime("%Y%m%d-%H%M%S") + ".jsonl"), "w")
        deadline = self.env.now() + self.s.minutes * 60 if self.s.minutes else math.inf
        self.understand()
        if not self.references and not self.target:
            self.log("No reference found. Name the reference tracks «REF …» and put a tap on them; "
                     "until then only the balance between channels is worked on.")
        try:
            self._run(deadline)
        except PlannerFailed as e:
            self.log(f"\nStopped: the planner failed ({e}). Everything kept so far stays in the set.")
        except MasterSilent:
            self.log("\nStopped: the master stayed silent for over a minute, so nothing could be measured. "
                     "Everything kept so far stays in the set; continue with --resume once Live plays again.")
        self.log("\nDone. " + self.report())
        self.logfile.close()
        return self.history

    def _run(self, deadline: float) -> None:
        sections = self.sections()
        scan = self.scan(sections) if self.s.complete else None
        if scan is not None and not self.beat and self.s.mode == "auto":
            vocals = [k for k, r in self.roles.items() if r.role in VOCAL_ROLES]
            if vocals and all(stats([f for _, f in scan.channels.get(k, [])]) is None for k in vocals):
                self.beat = True
                self.log("The vocal channels are silent in the whole song: treating it as a beat (no vocals).")
                if self.refset:
                    self.target = self.refset.target(True, self.s.loudness_from)
                    self.log(f"Target: {self.target.description}, {self.target.loudness:.1f} LUFS")
        main = [self.loudest_part(sections, scan)] if self.s.focus == "loud" else sections
        if self.s.complete and not self.s.resume:
            self.log(f"\n━━ Master against the reference")
        for name, start, length in ([] if self.s.resume else main):
            if self.env.now() >= deadline:
                break
            self.log(f"\n▶ {name}")
            self.play_loop(start, length)
            self.section(name, deadline)
        if self.s.complete and scan is not None:
            self.channel_pass(scan, main[0] if self.s.focus == "loud" else None, deadline)
            name, start, length = main[0]
            if self.env.now() < deadline:
                self.log(f"\n━━ Final check: master, {name}")
                self.play_loop(start, length)
                self.section("final check", deadline, max_rounds=self.s.final_rounds)

    def section(self, name: str, deadline: float, focus: str | None = None, max_rounds: int | None = None,
                scoring: str = "full") -> None:
        m = self.measure(scoring)
        best, stall = m["objective"], 0
        self.log(f"  distance {best:.2f}   ({_parts(m['parts'])})")
        for n in range(1, (max_rounds or self.s.max_rounds_per_section) + 1):
            if self.env.now() >= deadline:
                self.log("  time is up")
                return
            self.ex.refresh()
            try:
                plan = self.planner.plan(self.context(name, m, focus))
            except Exception as e:  # e.g. no API credit left, network down: stop cleanly, keep what was done
                raise PlannerFailed(str(e)) from e
            self.log(f"  round {n}: {plan.get('assessment', '')}")
            actions = plan.get("actions", [])
            if focus is not None:
                is_return = self.ex.track(focus)["kind"] == "return"
                ok = lambda a: a.get("track") == focus or (is_return and a.get("type") == "send")
                other = [a for a in actions if not ok(a)]
                actions = [a for a in actions if ok(a)]
                for a in other:
                    self.log(f"    – not now: {a['type']} {a.get('track') or 'master'} (channel pass is on {focus})")
            if not actions:
                if focus is not None:
                    self.log("  sits well, nothing to change")
                elif plan.get("done"):
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
            after = self.measure(scoring)
            rnd = Round(name, n, best, after["objective"], plan, rr.applied, rr.skipped)
            if after["objective"] < best - self.s.min_gain:
                self.log(f"    ✓ {best:.2f} → {after['objective']:.2f}   ({_parts(after['parts'])})")
                best, m, stall = after["objective"], after, 0
            elif after["objective"] > best + self.s.min_gain:
                self.ex.undo(rr)
                rnd.undone = True
                self.log(f"    ↺ undone: {best:.2f} → {after['objective']:.2f}")
                self.env.wait(self.s.settle)
                m = self.measure(scoring)
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


def _parts(parts: dict) -> str:
    """«tonal 0.9 · loudness 0.5 · …», largest first, small ones left out."""
    big = sorted(((v, k) for k, v in parts.items() if v >= 0.05), reverse=True)
    return " · ".join(f"{k} {v:.2f}" for v, k in big) or "nothing left"


def post_fader(st, gain_db: float):
    """Stats shifted by the mixer gain after the tap; None when the fader is all the way down."""
    if st is None or gain_db == -math.inf:
        return None
    if gain_db == 0.0:
        return st
    return replace(st, loudness=st.loudness + gain_db, bands_db=st.bands_db + gain_db, peak_db=st.peak_db + gain_db)


def loudest_window(positioned, total_beats: float, length_beats: float,
                   step_beats: float = 4.0) -> tuple[float, float] | None:
    """Offset in beats (from the start of the range) and loudness of the loudest window, on a one-bar grid.

    `positioned` are (beat offset, frame) pairs recorded while the range played once.
    """
    best = None
    offset = 0.0
    while offset + length_beats <= total_beats + 1e-9:
        st = stats([f for b, f in positioned if offset <= b < offset + length_beats])
        if st is not None and (best is None or st.loudness > best[1]):
            best = (offset, st.loudness)
        offset += step_beats
    return best


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
