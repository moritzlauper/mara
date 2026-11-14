"""The calculated mix flow (Settings.engine == "solver"), see docs/konzept-solver.md.

Instead of one planner proposal per round, every phase measures once, calculates a package of moves from
the channels' spectra (mixmatch.solver), applies it together and verifies it with fresh measurements:

0. Level base: a trim in front of clipper and limiter on Main puts the 99th percentile of the input peaks on
   the limiter ceiling, so the limiter and clipper limits (+5/+4 dB) are gain reduction in every project
   (docs/konzept-pegelbasis.md). It is held before the loudness step.
1. Master: the tonal difference to the references is spread over the channels that cause it. The planner
   reviews the draft once (keep, scale, drop, protect), the solver recalculates, the package is measured;
   then once or twice more with what is left. Every measured package calibrates the model (per-band share of
   the predicted change); a package that falls short halves the step size for the next one. Stereo low end is
   switched to mono on the channels causing it.
2. Channels: the strongest collisions in the loudest part and in up to two other passages, each fix
   simulated on the recorded frames first and kept only when its own pair measurably improves.
3. Final: up to two free planner rounds for what the calculation does not cover (compressors, sends,
   resonances), then limiter and clipper towards the references' loudness, then the final assessment.

Thorough mode (Settings.depth == "thorough") adds a character pass after the collisions: compression on channels
whose measured level range is well beyond what their role usually has, width per channel when the master's width
differs from the references, and one planner round on at most four important channels with an open problem.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from . import mix_quality, solver, workflow
from .actions import CLIPPER_MAX_GAIN_DB, LIMITER_CEILING_DB, LIMITER_MAX_GAIN_DB, RoundResult
from .activity import focus as report_focus, report as activity
from .analysis import TONAL_BANDS, stats
from .bands import BAND_COUNT, bands_between


# Level range (P95 − P20 of the 100 ms frames while a channel plays) a role usually has in a finished mix, in dB.
# Drums are left out: their range is their transients, compressing them by this rule would take the punch.
COMPRESS_NORM = {"bass": 6, "sub": 5, "808": 6, "synth_bass": 6, "bass_guitar": 7, "pad": 6, "chords": 7,
                 "strings": 8, "keys": 8, "piano": 9, "organ": 7, "lead_synth": 8, "supersaw": 7, "pluck": 9,
                 "arp": 9, "bell": 9, "guitar": 9, "acoustic_guitar": 9, "lead_vocal": 9, "backing_vocal": 9}
COMPRESS_STYLE = {   # ratio, attack ms, release ms
    "low end": (3.0, 30.0, 150.0), "sustained": (2.0, 30.0, 200.0), "keys": (2.5, 15.0, 120.0),
    "lead": (2.5, 10.0, 100.0), "vocal": (3.0, 8.0, 80.0)}
STYLE_OF = {**{r: "low end" for r in ("bass", "sub", "808", "synth_bass", "bass_guitar")},
            **{r: "sustained" for r in ("pad", "chords", "strings", "organ", "supersaw")},
            **{r: "keys" for r in ("keys", "piano", "guitar", "acoustic_guitar")},
            **{r: "lead" for r in ("lead_synth", "pluck", "arp", "bell")},
            **{r: "vocal" for r in ("lead_vocal", "backing_vocal")}}
WIDE_ROLES = {"pad", "ambience", "fx", "texture", "strings", "chords", "keys", "supersaw", "arp", "riser"}
CORE_ROLES = {"kick", "snare", "clap", "lead_vocal", "bass", "sub", "808", "synth_bass", "lead_synth"}


def level_range(frames) -> float | None:
    """P95 − P20 of a channel's 100 ms levels while it plays; None with too little playing."""
    if frames is None or not len(frames.times):
        return None
    levels = frames.level_db[frames.active]
    if len(levels) < 20:
        return None
    return float(np.percentile(levels, 95) - np.percentile(levels, 20))


class SolverFlow:
    """Mixed into Agent; uses its measurement, executor, journal and checkpoint helpers."""

    W_SOLVE, W_COLLIDE, W_CHARACTER = 6.0, 4.0, 4.0

    # ── when ────────────────────────────────────────────────────────────────
    def use_solver(self, ck: dict | None) -> bool:
        if self.s.engine != "solver" or self.s.resume not in (None, "auto"):
            return False
        # A run stopped in the middle of the older channel pass continues that way.
        return not (ck and ck.get("engine") != "solver" and ck.get("phase") == "channels"
                    and ck.get("pending_channels"))

    def run_solver(self, main: tuple, phase: str, deadline: float) -> None:
        name, start, length = main
        self._constraints = solver.Constraints(frozen=set(getattr(self, "_ck", {}).get("protected", [])))
        self._decisions = {}
        self._checked_passages = set()
        self._passages = {}
        self._sensitivity = None
        self._trust = 1.0
        if phase not in ("master", "channels", "character", "effects", "final"):
            phase = "master"
        self.sync_references()
        # A run continued in thorough mode after a fast start: its progress total did not count the character pass.
        if (self.s.depth == "thorough" and phase in ("master", "channels", "character")
                and getattr(self, "_ck", {}).get("depth") != "thorough"):
            self.progress(total_add=self.W_CHARACTER)
        if phase == "master":
            self.log("\n━━ Master against the reference")
            self.log(f"\n▶ {name}")
            self.play_loop(start, length)
            self.begin_step(self.W_SOLVE)
            self.solve_master(name, deadline)
            self.finish_step("master")
            phase = "channels"
            self._save_solver_checkpoint(phase)
        if self.env.now() >= deadline:
            return
        if phase == "channels":
            self.sync_references()
            if self.s.complete:
                self.channel_collisions(main, deadline)
            phase = "character"
            self._save_solver_checkpoint(phase)
        if self.env.now() >= deadline:
            return
        if phase == "character":
            if self.s.complete and getattr(self.s, "depth", "fast") == "thorough":
                try:
                    self.character_pass(main, deadline)
                except Exception as e:   # a pause (KeyboardInterrupt) still goes through
                    # The thorough extras never stop a mix: what they changed is undone step by step inside, the
                    # rest of the run (effects, loudness, final check) still happens.
                    self.log(f"  ⚠ character pass stopped early ({str(e)[:120]}); the mix continues without it.")
                    self.finish_step("character")
            phase = "effects"
            self._save_solver_checkpoint(phase)
        if self.env.now() >= deadline:
            return
        if phase == "effects":
            if self.s.audit:
                self.audit_effects(main, deadline)
            phase = "final"
            self._save_solver_checkpoint(phase)
        if self.env.now() < deadline:
            self.sync_references()
            self.final_solver(main, deadline)

    def _save_solver_checkpoint(self, phase: str) -> None:
        self.save_checkpoint(phase=phase, next_channel=None, engine="solver", depth=self.s.depth,
                             protected=sorted(self._constraints.frozen))

    def _advance(self, share: float, step: str) -> None:
        """Books a share of the current phase's progress units after a measured package."""
        piece = getattr(self, "_budget", 0.0) * share
        self._budget = getattr(self, "_budget", 0.0) - piece
        self.progress(piece, step)

    # ── what the solver sees ────────────────────────────────────────────────
    def solver_sources(self, m: dict) -> dict:
        from .planner import device_summary

        members = self._group_members()
        audible = {k for k in self.roles if m["tracks"].get(k) is not None and not self.track_muted(k)}
        out = {}
        for key in audible:
            r, st, t = self.roles[key], m["tracks"][key], self.ex.track(key)
            summary = {d["index"]: d for d in device_summary(t)}
            eq = next((d for d in t["devices"] if d.get("class_name") == "Eq8" or d["name"].lower() == "eq eight"), None)
            bands, free = [], 8
            if eq is not None:
                on = next((p["value"] for p in eq["params"] if p["name"] == "Device On"), 1.0)
                bands = [dict(b, on=True) for b in summary.get(eq["index"], {}).get("eq_bands", [])]
                neutral = [b for b in bands if str(b["kind"]).lower() in ("bell", "low shelf", "high shelf")
                           and abs(b["gain"] or 0) < 0.05]
                bands = [b for b in bands if b not in neutral and b["freq"]]
                free = 0 if on < 0.5 else 8 - len(bands)
            utility = next((d for d in t["devices"] if d.get("class_name") == "StereoGain"), None)
            mono_free = True
            if utility is not None:
                params = {p["name"]: p for p in utility["params"]}
                mono = params.get("Bass Mono")
                mono_free = (params.get("Device On", {"value": 1})["value"] >= 0.5 and mono is not None
                             and mono["value"] < 0.5 and not mono.get("automated"))
            volume = next((p for p in t["mixer"] if p["name"] == "Volume"), None)
            fader_free = (volume is not None and not volume.get("automated")
                          and (key, "mixer", "Volume") not in self.ex.locks)
            devices = list(summary.values())
            duck_free = not any(d["name"].startswith("MARA Duck") or (
                "compressor" in d["name"].lower() and (not d.get("enabled", True)
                                                       or any(p.startswith("S/C On=On") for p in d["params"])))
                for d in devices)
            group = bool(t.get("is_group"))
            room = 3.0
            base = (getattr(self, "_level_baseline", None) or {})
            if base.get("master") and key in base.get("tracks", {}) and m.get("master") is not None:
                initial = mix_quality.layer_level(mix_quality._levels_of_anchor(base["tracks"]), key,
                                                  base["master"]["loudness"])
                now = mix_quality.layer_level(mix_quality._levels_of(m["tracks"]), key, m["master"].loudness)
                room = max(0.0, now - initial + 3.0)
            out[key] = solver.Source(
                key, r.name, r.role, r.priority, np.asarray(st.bands_db, dtype=float),
                members=tuple(k for k in members.get(key, ()) if k in audible and not self.ex.track(k).get("is_group")),
                is_group=group, eq=bands, eq_free=free, fader_free=fader_free,
                low_side_db=float(st.low_side_db), bass_mono_free=mono_free and not group, duck_free=duck_free,
                level_room=room)
        return out

    def solver_target(self, m: dict):
        """The file references' target, or reference taps in the project as a plain target."""
        if self.target is not None:
            return self.target
        if m.get("ref") is not None and m.get("cmp") is not None:
            return SimpleNamespace(stats=m["ref"], tolerance=np.full(BAND_COUNT, 0.5), offset=None,
                                   anchor=[], one_sided=[])
        return None

    # ── 1. master ───────────────────────────────────────────────────────────
    def solve_master(self, name: str, deadline: float) -> dict:
        workflow.check_pause()
        workflow.update(section=name)
        self._inspection_channel = "Main"
        report_focus(self.log, channel="Main", region=self.loop)
        trim = self.level_base()
        # Three windows for the baseline: its spread decides what counts as a measured change all run long.
        m = self.measure_verified("full", first=self.measure_fine("full", "m"), passes=3)
        if trim is not None:
            from .agent import Round
            self._record(Round("level base", 1, m["objective"], m["objective"], {
                "assessment": trim["text"], "verification": "level base for the loudness chain, not a mix change",
                "mix_intent": {"priority": "none", "evidence": trim["text"], "preserve": "balance of all channels",
                               "reference_relevance": "limiter and clipper limits count as gain reduction"}},
                trim["rr"].applied, trim["rr"].skipped, parameters=self.journal_parameters(trim["rr"])))
        baseline = self.quality_anchors.setdefault(self.quality_key(m, "full"), {
            **mix_quality.anchor(m), "before_mix": getattr(self, "_ck", {}).get("phase") == "master"})
        self._level_baseline = baseline
        if getattr(self, "_ck", None):
            self.save_checkpoint()
        self._log_distance(m)
        cmp, master = m.get("cmp"), m.get("master")
        mono_needed = bool(master is not None and (
            (cmp is not None and cmp.low_side_diff > 2.0) or (cmp is None and master.low_side_db > -20)))
        last_plan = None
        for n in range(1, self.s.solver_iterations + 1):
            workflow.check_pause()
            if self.env.now() >= deadline:
                self.log("  time is up")
                break
            self.ex.refresh()
            sources = self.solver_sources(m)
            target = self.solver_target(m)
            activity(self.log, "Calculating the next changes from the measurements")
            plan = (solver.solve(m["master"], sources, target, self._constraints, beat=self.beat,
                                 sensitivity=self._sensitivity, trust=self._trust)
                    if target is not None and m.get("master") is not None else solver.Plan([], 0.0, 0.0))
            extra = solver.bass_mono_moves(sources, mono_needed) if n == 1 else []
            if n == 1 and (plan.moves or extra):
                plan = self.review_plan(plan, m, sources, target)
                extra = [mv for mv in extra if mv.key not in self._constraints.frozen]
            last_plan = plan
            if not extra and (not plan.moves or plan.benefit < 0.08):
                self.log("  Nothing left with a clear predicted benefit; remaining differences stay documented."
                         if n > 1 else "  The calculation finds no change with a clear benefit here.")
                break
            moves = plan.moves + extra
            changed = len({mv.key for mv in moves})
            self.log(f"  round {n}: {len(moves)} changes on {changed} channels, calculated from the channel spectra"
                     + (f"; predicted tonal distance {plan.cost_before:.2f} → {plan.cost_after:.2f}"
                        if plan.moves else ""))
            intent = {"priority": "tonal" if plan.moves else "low_side",
                      "evidence": self._evidence(plan),
                      "preserve": "core range of high-priority channels, user notes and protected channels",
                      "reference_relevance": "differences beyond the references' tolerance, spread over their sources"}
            before = m
            accepted, m = self.try_package(name, n, m, baseline, moves, intent, "full", plan)
            self._advance(0.3, name)
            share = self.learn(plan, before)
            if accepted:
                mono_needed = False
                if share is not None and share >= 0.75:
                    self._trust = min(1.0, self._trust * 1.5)
                continue
            missed = self._probe_why.startswith(("no measured improvement", "reference move brought"))
            if not (missed and plan.moves) or self._trust <= 0.5:
                break
            # The prediction fell short: calculate again with the learned sensitivity and half the step size.
            self._trust *= 0.5
            self.log(f"  next calculation with the measured sensitivity and {self._trust:.0%} of the step size")
        if last_plan is not None:
            for key, why in last_plan.unchanged.items():
                if key not in self._decisions:
                    self._decisions[key] = [f"master: unchanged, {why}"]
        return m

    # ── level base ──────────────────────────────────────────────────────────
    def level_base(self, threshold: float = 0.5) -> dict | None:
        """Trim in front of clipper and limiter so the input peaks (99th percentile) sit on the ceiling.

        Uses the scan of the original song when there is one (no extra measurement), else one measurement.
        Returns {"rr", "text"} when the trim changed, for the journal."""
        self.sanitize_chain()
        if self.hub.latest("master_in") is None and not self.prepare_input_stage():
            return None
        peak, self._scan_input_peak = getattr(self, "_scan_input_peak", None), None
        if peak is None:
            peak = self.measure("full").get("input_peak99")
        if peak is None:
            self.log("  Level base: no input measurement on Main; limiter and clipper limits stay settings.")
            return None
        return self._trim_to(peak, threshold)

    NO_LIMITER_ROOM_DB = 3.0   # without a limiter yet, the calculated packages need room under the ceiling

    def _trim_to(self, peak: float, threshold: float, hold: bool = False) -> dict | None:
        """`hold`: right before the loudness step, which adds the limiter when it is missing: on the ceiling.
        (Peaks on the ceiling at a PLR above the references' cannot end louder than the references.)"""
        limited = hold or self.ex.active_limiter() is not None
        goal = LIMITER_CEILING_DB - (0.0 if limited else self.NO_LIMITER_ROOM_DB)
        delta = goal - peak
        now = self.ex.trim_gain() or 0.0
        where = (f"input peaks {peak:.1f} dBFS (99th percentile), ceiling {LIMITER_CEILING_DB:.0f} dBFS"
                 + ("" if limited else f"; no limiter yet, so {self.NO_LIMITER_ROOM_DB:.0f} dB under it"))
        actions = [{"type": "ceiling", "track": None, "reason": f"limiter ceiling {LIMITER_CEILING_DB:.0f} dBFS"}] \
            if self.ex.active_limiter() is not None else []
        if abs(delta) >= threshold:
            actions.append({"type": "trim", "track": None, "delta_db": round(delta, 1), "reason": f"level base: {where}"})
        rr = self.ex.apply(actions)
        if not any(c.param != "Ceiling" for c in rr.changes):
            self.log(f"  Level base: {where}; trim stays at {now:+.1f} dB.")
            if not rr.changes:
                return None
            self.env.wait(self.s.settle)
            return {"rr": rr, "text": f"limiter ceiling to {LIMITER_CEILING_DB:.0f} dBFS; {where}"}
        if not rr.changes:
            return None
        text = f"trim {now:+.1f} → {self.ex.trim_gain() or 0.0:+.1f} dB: {where}"
        self.log(f"  Level base: {text}")
        self.env.wait(self.s.settle)
        return {"rr": rr, "text": text}

    def sanitize_chain(self) -> bool:
        """Values on Main that MARA never sets go back into its range before anything is measured or pushed:
        a clipper below 0 dB (it once received a limiter's old value) or a limiter beyond its cap."""
        actions, notes = [], []
        clip, lim = self.ex.clipper_gain(), self.ex.limiter_gain()
        if clip is not None and clip < -0.5:
            actions.append({"type": "clipper", "delta_db": 0.0, "track": None, "reason": "clipper back to 0 dB"})
            notes.append(f"clipper {clip:+.1f} dB → 0 dB")
        if lim is not None and lim > LIMITER_MAX_GAIN_DB + 0.05:
            actions.append({"type": "limiter", "delta_db": 0.0, "track": None, "reason": "limiter to its cap"})
            notes.append(f"limiter {lim:+.1f} dB → {LIMITER_MAX_GAIN_DB:+.1f} dB")
        if not actions:
            return False
        rr = self.ex.apply(actions)
        self.log("  ⚠ Main had values MARA never sets: " + ", ".join(notes))
        self.env.wait(self.s.settle)
        return bool(rr.changes)

    def chain_report(self, m: dict) -> dict | None:
        """What the loudness chain does, from the two taps on Main."""
        peak, inp, out = m.get("input_peak99"), m.get("master_in"), m.get("master")
        if peak is None or inp is None or out is None:
            return None
        limiter, clipper = self.ex.limiter_gain() or 0.0, self.ex.clipper_gain() or 0.0
        return {"input_peak_dbfs": round(peak, 1), "trim_db": round(self.ex.trim_gain() or 0.0, 1),
                "limiter_db": round(limiter, 1), "clipper_db": round(clipper, 1),
                "peak_reduction_db": round(max(0.0, peak + limiter + clipper - LIMITER_CEILING_DB), 1),
                "chain_gain_lu": round(float(out.loudness - inp.loudness), 1)}

    # ── calibration ─────────────────────────────────────────────────────────
    def learn(self, plan, before: dict) -> float | None:
        """Per-band sensitivity from the package just measured, also when it was undone."""
        probe = getattr(self, "_probe", None)
        self._probe = None
        if probe is None or not plan.moves or before.get("cmp") is None or probe.get("cmp") is None:
            return None
        self._sensitivity, share = solver.calibrate(self._sensitivity, plan, before["cmp"].tonal_diff,
                                                    probe["cmp"].tonal_diff)
        if share is not None:
            low = [i for i in TONAL_BANDS if self._sensitivity[i] < 0.8]
            from .analysis import band_label
            self.log(f"    model check: {share:.0%} of the predicted change measured"
                     + (f"; expecting less at {', '.join(band_label(i) for i in low[:5])} Hz from now on" if low else ""))
        return share

    def _evidence(self, plan) -> str:
        if plan.excess_before is None:
            return "stereo energy below 150 Hz"
        from .analysis import TONAL_BANDS, band_label
        worst = sorted(TONAL_BANDS, key=lambda i: -abs(plan.excess_before[i]))[:3]
        return ", ".join(f"{plan.excess_before[i]:+.1f} dB at {band_label(i)} Hz" for i in worst
                         if abs(plan.excess_before[i]) > 0.2) + " beyond the references' tolerance"

    def review_plan(self, plan, m: dict, sources: dict, target):
        """One planner look at the calculated draft; the solver recalculates with its decisions."""
        from .planner import RulePlanner

        reviewer = getattr(self.planner, "review", None)
        if reviewer is None or isinstance(self.planner, RulePlanner) or not plan.moves:
            return plan
        from . import project
        names = {k: s.name for k, s in sources.items()}
        channels = [{"track": k, "name": r.name, "role": r.role, "priority": r.priority, "kind":
                     "group" if self.ex.track(k).get("is_group") else self.ex.track(k)["kind"], **({"note": r.note} if r.note else {})}
                    for k, r in self.roles.items() if k in sources]
        ctx = {
            "review": "calculated mixing plan for the loudest part",
            "user_description": project.description() or None,
            "mode": ("beat: vocals may come later, never fill 200 Hz–5 kHz with boosts" if self.beat else
                     "finished instrumental" if self.s.mode == "instrumental" else "song with vocals"),
            "target": getattr(target, "description", "reference tracks in the project"),
            "master_vs_reference": m["cmp"].summary() if m.get("cmp") else None,
            "spectrum": mix_quality.diagnosis(m, channels)["spectrum"],
            "channels": channels,
            "predicted_tonal_distance": {"before": round(plan.cost_before, 2), "after": round(plan.cost_after, 2)},
            "moves": [{"id": mv.id, "track": mv.key, "name": names.get(mv.key, mv.key), "role": sources[mv.key].role,
                       "priority": sources[mv.key].priority, "move": mv.describe(),
                       "predicted_benefit": round(mv.benefit, 3), "reason": mv.reason} for mv in plan.moves],
        }
        activity(self.log, "Reviewing the calculated plan · waiting for AI")
        try:
            out = self._review_with_retry(reviewer, ctx)
        except Exception as e:
            self.log(f"  ⚠ review unavailable ({str(e)[:100]}); applying the calculated plan as it is")
            return plan
        self.log(f"  review: {out.get('assessment', '')}")
        by_id = {mv.id: mv for mv in plan.moves}
        changed = False
        for edit in out.get("edits", []):
            mv = by_id.get(edit.get("id"))
            if mv is None or edit.get("decision") == "keep":
                continue
            key = (mv.key, mv.kind, mv.idx if mv.kind != "fader" else None)
            if edit["decision"] == "drop" or mv.kind == "low_cut":
                self._constraints.banned.add(key)
                self.log(f"    – dropped: {names.get(mv.key)} {mv.describe()}: {edit.get('reason', '')}")
            else:
                factor = float(np.clip(edit.get("factor") or 0.5, 0.25, 0.75))
                self._constraints.caps[key] = abs(mv.gain) * factor
                self.log(f"    – scaled to {factor:.0%}: {names.get(mv.key)} {mv.describe()}: {edit.get('reason', '')}")
            changed = True
        for item in out.get("protect", []):
            key = item.get("track")
            if key in self.roles and key not in self._constraints.frozen:
                self._constraints.frozen.add(key)
                self.log(f"    – protected: {self.roles[key].name}: {item.get('reason', '')}")
                self._decisions.setdefault(key, []).append("protected by the review")
                changed = True
        if not changed:
            return plan
        return solver.solve(m["master"], sources, target, self._constraints, beat=self.beat)

    def _review_with_retry(self, reviewer, ctx):
        for wait in (15, None):
            try:
                return reviewer(ctx)
            except Exception as e:
                if wait is None or not any(w in str(e).lower() for w in self.TRANSIENT):
                    raise
                self.env.wait(wait)

    # ── applying and verifying a package ────────────────────────────────────
    def apply_moves(self, moves: list, m: dict) -> list:
        """Each move separately, so a single one can be undone; returns (move, action, round result)."""
        out = []
        for mv in moves:
            a = mv.action()
            if mv.kind == "duck":
                hit = next((x for x in m.get("masking", []) if x["background"] == mv.key
                            and x["foreground"] == mv.source_track and x.get("intermittent")), None)
                if hit is None or not self.ex.set_state.get("capabilities", {}).get("sidechain_ducking"):
                    self.log("    – duck skipped: no measured repeated collision or routing support")
                    continue
                a["source_level_dbfs"] = hit["source_level_dbfs"]
            out.append((mv, a, self.ex.apply([a])))
        return out

    def undo_moves(self, applied: list) -> None:
        for _, _, rr in reversed(applied):
            self.ex.undo(rr)

    def _merged(self, applied: list) -> RoundResult:
        return RoundResult(changes=[c for _, _, rr in applied for c in rr.changes],
                           applied=[a for _, _, rr in applied for a in rr.applied],
                           skipped=[a for _, _, rr in applied for a in rr.skipped])

    def try_package(self, section: str, n: int, m: dict, baseline: dict, moves: list, intent: dict,
                    scoring: str, plan=None) -> tuple[bool, dict]:
        """Applies the moves together, measures, keeps or undoes them; one retry at half strength when
        a protection (not the missing benefit) failed."""
        from .agent import Round, _parts

        names = {k: r.name for k, r in self.roles.items()}
        self._probe, self._probe_why = None, ""
        for attempt, factor in enumerate((1.0, 0.5)):
            todo = moves if factor == 1.0 else [mv.scaled(factor) for mv in moves]
            if not todo:
                break
            if factor != 1.0:
                self.log("  retry at half strength")
            for mv in todo:
                self.log(f"    · {mv.action()['type']} {mv.key}: {names.get(mv.key, mv.key)} {mv.describe()}"
                         + (f" · {mv.reason}" if factor == 1.0 and mv.reason else ""))
            workflow.check_pause()
            applied = self.apply_moves(todo, m)
            merged = self._merged(applied)
            record = {"assessment": f"{len(todo)} calculated changes" + (" at half strength" if factor != 1.0 else ""),
                      "mix_intent": intent, "measurement_samples": m.get("samples", 1),
                      "measurement_spread": m.get("noise", {})}
            if plan is not None and plan.moves:
                record["predicted"] = {"tonal_before": round(plan.cost_before, 3), "tonal_after": round(plan.cost_after, 3)}
            if not merged.changes:
                self._record(Round(section, n, m["objective"], m["objective"], record, merged.applied, merged.skipped))
                return False, m
            try:
                self.env.wait(self.s.settle)
                after = self.measure_verified(scoring)
            except BaseException:
                self.undo_moves(applied)
                raise
            # The final step sets loudness and density with trim and limiter: packages before it are judged on
            # tone, width, low end and the protections, not on the level they happen to leave.
            ok, why = mix_quality.evaluate(m, after, baseline, intent["priority"], self.s.min_gain, level_free=True)
            record["verification"] = why
            if factor == 1.0:
                self._probe, self._probe_why = after, why   # the full package's effect, for the calibration
            if plan is not None and plan.moves and factor == 1.0:
                self.log(f"    predicted tonal {plan.cost_before:.2f} → {plan.cost_after:.2f}, measured "
                         f"{m['parts'].get('tonal', 0):.2f} → {after['parts'].get('tonal', 0):.2f}")
            rnd = Round(section, n, m["objective"], after["objective"], record, merged.applied, merged.skipped,
                        parameters=self.journal_parameters(merged))
            if ok:
                self.log(f"    ✓ {m['objective']:.2f} → {after['objective']:.2f}   ({_parts(after['parts'])})")
                self.log(f"    verified: {why}")
                self.explain(after)
                self._record(rnd)
                for mv in todo:
                    self._decisions.setdefault(mv.key, []).append(f"master: {mv.describe()}")
                return True, after
            self.undo_moves(applied)
            rnd.undone = True
            self._record(rnd)
            self.log(f"    ↺ undone: {why} ({m['objective']:.2f} → {after['objective']:.2f})")
            self.env.wait(self.s.settle)
            if why.startswith("no measured improvement") or why.startswith("reference move brought"):
                break   # the prediction missed: half of it will not do better
        return False, m

    # ── 2. collisions ───────────────────────────────────────────────────────
    def channel_collisions(self, main: tuple, deadline: float) -> None:
        name, start, length = main
        self.log("\n━━ Channel pass: collisions between channels")
        self.begin_step(self.W_COLLIDE)
        self.log(f"\n▶ {name}: collisions")
        self.play_loop(start, length)
        self.solve_collisions("collision", deadline, "full")
        self._advance(0.4, "collisions")
        others = {}
        for key, region in self.regions.items():
            if region is None or key not in self.roles or self.track_muted(key):
                continue
            if abs(region[0] - start) < 1e-6:
                continue
            others.setdefault(tuple(region), []).append(key)
        chosen = sorted(others.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:self.s.other_passages]
        for region, keys in chosen:
            workflow.check_pause()
            if self.env.now() >= deadline:
                self.log("  time is up")
                break
            a, b = region[0] / 4 + 1, (region[0] + region[1]) / 4 + 1
            self.log(f"\n▶ Other passage, bars {a:g}–{b:g} (outside the loudest part: overlap and mono bass only)")
            self.play_loop(*region)
            after = self.solve_collisions(f"collision in bars {a:g}–{b:g}", deadline, "local", keys=set(keys))
            self._passages[region] = after.get("master")
            self._checked_passages.add(region)
            self._advance(0.5, "collisions")
        for region, keys in others.items():
            if region not in self._checked_passages:
                for key in keys:
                    self._decisions.setdefault(key, []).append(
                        "plays only outside the checked passages; covered by the protections")
        self.finish_step("channels")
        self.log_decisions()

    def solve_collisions(self, label: str, deadline: float, scoring: str, keys: set | None = None) -> dict:
        from .agent import Round, _parts

        workflow.check_pause()
        workflow.update(section=label)
        report_focus(self.log, channel="Mix", region=self.loop)
        m = self.measure_verified(scoring)
        baseline = self.quality_anchors.setdefault(self.quality_key(m, scoring), mix_quality.anchor(m))
        self._level_baseline = baseline
        self._log_distance(m)
        self.ex.refresh()
        sources = self.solver_sources(m)
        ducking = bool(self.ex.set_state.get("capabilities", {}).get("sidechain_ducking"))
        fixes, notes = solver.collision_fixes(m, sources, self.s.collision_pairs, keys, ducking, self._constraints)
        for note in notes:
            self.log(f"    = {note}")
        if not fixes:
            self.log("  No collision with a simulated benefit beyond measurement spread.")
            return m
        self.log(f"  round 1: {len(fixes)} collision fixes, simulated on the recorded frames first")
        moves = [mv for f in fixes for mv in f.moves]
        names = {k: r.name for k, r in self.roles.items()}
        for mv in moves:
            self.log(f"    · {mv.action()['type']} {mv.key}: {names.get(mv.key, mv.key)} {mv.describe()} · {mv.reason}")
        applied = self.apply_moves(moves, m)
        if not any(rr.changes for _, _, rr in applied):
            return m
        try:
            self.env.wait(self.s.settle)
            after = self.measure_verified(scoring)
        except BaseException:
            self.undo_moves(applied)
            raise
        safe, why = mix_quality.evaluate(m, after, baseline, "protect", self.s.min_gain)
        intent = {"priority": "masking", "evidence": "simulated relief on the recorded frames",
                  "preserve": "foreground of each pair", "reference_relevance": "not a reference move"}
        if not safe:
            self.undo_moves(applied)
            merged = self._merged(applied)
            self._record(Round(label, 1, m["objective"], after["objective"],
                               {"assessment": f"{len(fixes)} collision fixes", "mix_intent": intent, "verification": why},
                               merged.applied, merged.skipped, undone=True, parameters=self.journal_parameters(merged)))
            self.log(f"    ↺ undone: {why} ({m['objective']:.2f} → {after['objective']:.2f})")
            self.env.wait(self.s.settle)
            return m
        results = []
        for fix in fixes:
            entries = [x for x in applied if x[0] in fix.moves]
            if not entries:
                continue
            now = after.get("pair_severity", {}).get(fix.pair, 0.0)
            spread = m.get("pair_noise", {}).get(fix.pair, 0.0) + after.get("pair_noise", {}).get(fix.pair, 0.0)
            # Kept when the measured relief clearly exceeds the spread, or when it confirms the simulation:
            # at least half of the simulated relief, in the simulated direction. A single pair's severity is
            # noisy; a prediction that the measurement backs up is evidence too.
            relief, simulated = fix.before - now, fix.before - fix.predicted
            kept = relief > max(0.03, spread) or (simulated > 0 and relief >= max(0.02, 0.5 * simulated))
            if not kept:
                self.undo_moves(entries)
            results.append((fix, entries, now, kept))
        if any(kept for *_, kept in results):
            self.log(f"    ✓ {m['objective']:.2f} → {after['objective']:.2f}   ({_parts(after['parts'])})")
        for fix, entries, now, kept in results:
            pair = f"{names.get(fix.background, fix.background)} ↔ {names.get(fix.foreground, fix.foreground)} at {fix.freq_hz} Hz"
            verdict = (f"{pair}: severity {fix.before:.2f} → {now:.2f} (simulated {fix.predicted:.2f})" if kept else
                       f"{pair}: no measurable relief ({fix.before:.2f} → {now:.2f}, simulated {fix.predicted:.2f})")
            self.log(f"    verified: {verdict}" if kept else f"    ↺ undone: {verdict}")
            merged = self._merged(entries)
            self._record(Round(label, 1, m["objective"], after["objective"],
                               {"assessment": f"collision {pair}", "mix_intent": intent, "verification": verdict},
                               merged.applied, merged.skipped, undone=not kept, parameters=self.journal_parameters(merged)))
            if kept:
                for mv, _, _ in entries:
                    self._decisions.setdefault(mv.key, []).append(f"{label}: {mv.describe()}")
        if any(kept for *_, kept in results):
            self.explain(after)
        return after

    # ── 2b. character (thorough mode) ───────────────────────────────────────
    def character_pass(self, main: tuple, deadline: float) -> None:
        name, start, length = main
        workflow.update(phase="character")
        self.log("\n━━ Character: dynamics, width and the important channels")
        self.begin_step(self.W_CHARACTER)
        self.play_loop(start, length)
        m = self.measure_verified("full")
        baseline = self.quality_anchors.get(self.quality_key(m, "full")) or mix_quality.anchor(m)
        self._log_distance(m)
        m = self.compress_channels(m, baseline)
        self._advance(0.35, "dynamics")
        if self.env.now() < deadline:
            m = self.width_channels(m, baseline)
            self._advance(0.25, "width")
        if self.env.now() < deadline:
            self.review_channels(m, deadline)
        self.finish_step("character")

    def _user_compressor(self, key: str) -> bool:
        for d in self.ex.track(key)["devices"]:
            name, cls = d["name"].lower(), str(d.get("class_name", "")).lower()
            on = next((p["value"] for p in d["params"] if p["name"] == "Device On"), 1.0) >= 0.5
            if on and (cls in ("compressor2", "gluecompressor") or "pro-c" in name or "comp" in name):
                return True
        return False

    def compress_plan(self, m: dict) -> list[dict]:
        """Compressor settings for channels whose level range is well beyond their role's usual one."""
        gains, frames, plan = self.fader_gains(), m.get("mask_frames") or {}, []
        for key, r in self.roles.items():
            norm = COMPRESS_NORM.get(r.role)
            st = m.get("tracks", {}).get(key)
            if (norm is None or st is None or st.active < 0.5 or self.track_muted(key)
                    or key in self._constraints.frozen or self.ex.track(key).get("is_group")
                    or self._user_compressor(key)):
                continue
            rng = level_range(frames.get(key))
            if rng is None or rng - norm < 3.0:
                continue
            excess = rng - norm
            gr = min(4.0, 0.5 * excess)
            ratio, attack, release = COMPRESS_STYLE[STYLE_OF.get(r.role, "keys")]
            f = frames[key]
            pre = f.level_db[f.active] - gains.get(key, 0.0)   # the compressor sits before the fader
            threshold = float(np.percentile(pre, 95) - gr / (1 - 1 / ratio))
            makeup = float(np.clip(np.mean(np.maximum(0.0, pre - threshold)) * (1 - 1 / ratio), 0.0, 6.0))
            plan.append({"key": key, "range": rng, "norm": norm, "gr": gr, "score": excess * r.priority,
                         "action": {"type": "compress", "track": key, "ratio": ratio, "attack_ms": attack,
                                    "release_ms": release, "threshold_db": round(threshold, 1),
                                    "makeup_db": round(makeup, 1),
                                    "reason": f"level range {rng:.1f} dB, usual for {r.role} about {norm} dB"}})
        return sorted(plan, key=lambda p: -p["score"])[:6]

    def compress_channels(self, m: dict, baseline: dict) -> dict:
        from .agent import Round

        plan = self.compress_plan(m)
        if not plan:
            self.log("  Dynamics: every channel's level range fits its role (or you compress it yourself).")
            return m
        names = {k: r.name for k, r in self.roles.items()}
        self.log(f"  Dynamics: {len(plan)} channels vary well beyond their role, compressed as one package")
        applied = []
        for p in plan:
            a = p["action"]
            self.log(f"    · compress {names[p['key']]}: {a['ratio']:g}:1, threshold {a['threshold_db']:.1f} dB, "
                     f"attack {a['attack_ms']:g} ms, makeup {a['makeup_db']:+.1f} dB · {a['reason']}")
            rr = self.ex.apply([a])
            if rr.skipped:   # a half-set compressor never stays: take it out again right away
                self.ex.undo(rr)
                continue
            if rr.changes or rr.created_devices:
                applied.append((p, rr))
        if not applied:
            return m
        try:
            self.env.wait(self.s.settle)
            after = self.measure_verified("full")
        except BaseException:
            for _, rr in reversed(applied):
                self.ex.undo(rr)
            raise
        safe, why = mix_quality.evaluate(m, after, baseline, "protect", self.s.min_gain)
        if not safe:
            for _, rr in reversed(applied):
                self.ex.undo(rr)
            self.log(f"    ↺ undone: {why}")
            return m
        frames, kept = after.get("mask_frames") or {}, 0
        for p, rr in applied:
            now = level_range(frames.get(p["key"]))
            before_lvl, after_lvl = m["tracks"][p["key"]], after["tracks"].get(p["key"])
            shift = (after_lvl.loudness - before_lvl.loudness) if after_lvl is not None else 99.0
            ok = now is not None and p["range"] - now >= max(1.0, 0.4 * p["gr"]) and abs(shift) <= 1.5
            verdict = (f"{names[p['key']]}: level range {p['range']:.1f} → {now:.1f} dB, level {shift:+.1f} dB"
                       if now is not None else f"{names[p['key']]}: no measurement")
            if not ok:
                self.ex.undo(rr)
            self.log(f"    {'verified' if ok else '↺ undone'}: {verdict}")
            self._record(Round("dynamics", 1, m["objective"], after["objective"],
                               {"assessment": f"compression on {names[p['key']]}", "verification": verdict,
                                "mix_intent": {"priority": "dynamics", "evidence": p["action"]["reason"],
                                               "preserve": "channel level and transients", "reference_relevance": "role norm"}},
                               rr.applied, rr.skipped, undone=not ok, parameters=self.journal_parameters(rr)))
            if ok:
                kept += 1
                self._decisions.setdefault(p["key"], []).append(f"compressed {p['action']['ratio']:g}:1")
        return self.measure_verified("full") if kept != len(applied) else after

    def _width_now(self, key: str) -> float:
        for d in self.ex.track(key)["devices"]:
            if d.get("class_name") == "StereoGain":
                p = next((p for p in d["params"] if p["name"] in ("Stereo Width", "Width")), None)
                if p is not None:
                    return self.ex._db_of(p)
        return 100.0

    def width_channels(self, m: dict, baseline: dict) -> dict:
        """When the master is clearly wider or narrower than the references, the channels that carry the side
        signal move 25% towards them, as one measured package."""
        from .agent import Round

        cmp = m.get("cmp")
        if cmp is None or abs(cmp.width_diff) < 1.5:
            self.log("  Width: the master's width is within 1.5 dB of the references; channels stay as they are.")
            return m
        wider = cmp.width_diff < 0
        cands = []
        for key, r in self.roles.items():
            st = m.get("tracks", {}).get(key)
            if (st is None or st.active < 0.5 or self.track_muted(key) or key in self._constraints.frozen
                    or self.ex.track(key).get("is_group")):
                continue
            if wider and r.role in WIDE_ROLES and -20 < st.width_db < -4:
                cands.append((st.loudness, key))
            elif not wider and r.role not in CORE_ROLES and st.width_db > -12:
                cands.append((st.loudness + st.width_db, key))
        if not cands:
            self.log("  Width: no channel that could move the master's width without touching the core.")
            return m
        moves = []
        for _, key in sorted(cands, reverse=True)[:3]:
            now = self._width_now(key)
            moves.append({"type": "width", "track": key, "width_pct": round(now + (25 if wider else -25)),
                          "bass_mono": None, "reason": f"master {abs(cmp.width_diff):.1f} dB "
                                                       f"{'narrower' if wider else 'wider'} than the references"})
        names = {k: r.name for k, r in self.roles.items()}
        self.log(f"  Width: master {abs(cmp.width_diff):.1f} dB {'narrower' if wider else 'wider'} than the "
                 f"references; {'widening' if wider else 'narrowing'} " + ", ".join(names[a["track"]] for a in moves))
        rr = self.ex.apply(moves)
        if not rr.changes:
            return m
        try:
            self.env.wait(self.s.settle)
            after = self.measure_verified("full")
        except BaseException:
            self.ex.undo(rr)
            raise
        ok, why = mix_quality.evaluate(m, after, baseline, "width", self.s.min_gain, level_free=True)
        self._record(Round("width", 1, m["objective"], after["objective"],
                           {"assessment": "width towards the references", "verification": why,
                            "mix_intent": {"priority": "width", "evidence": moves[0]["reason"],
                                           "preserve": "core channels centred", "reference_relevance": "stereo width"}},
                           rr.applied, rr.skipped, undone=not ok, parameters=self.journal_parameters(rr)))
        if not ok:
            self.ex.undo(rr)
            self.log(f"    ↺ undone: {why}")
            return m
        self.log(f"    ✓ verified: {why}")
        for a in moves:
            self._decisions.setdefault(a["track"], []).append(f"width {a['width_pct']}%")
        return after

    def review_channels(self, m: dict, deadline: float) -> None:
        """One planner round on at most four important channels where a problem is still open after the calculation:
        an unresolved collision or a tonal region the master still misses. Not with built-in rules."""
        from .planner import RulePlanner

        if isinstance(self.planner, RulePlanner):
            return
        channels = [{"track": k, "name": r.name, "role": r.role, "priority": r.priority,
                     "kind": "group" if self.ex.track(k).get("is_group") else self.ex.track(k)["kind"]}
                    for k, r in self.roles.items() if not self.track_muted(k)]
        keys = [k for k in mix_quality.revisit_candidates(m, channels, limit=6)
                if self.roles[k].priority >= 3 and k not in self._constraints.frozen][:4]
        if not keys:
            self.log("  Important channels: nothing left open that the calculation could not reach.")
            return
        self.log("  Important channels with an open problem: " + ", ".join(self.roles[k].name for k in keys))
        for key in keys:
            if self.env.now() >= deadline:
                break
            self.section(f"channel {self.roles[key].name}", deadline, focus=key, max_rounds=1, scoring="balance")

    def log_decisions(self) -> None:
        """One line per channel: what this run decided there."""
        self.log("\n  Channel decisions:")
        for r in self._channel_order():
            if self.track_muted(r.key):
                what = "muted, left alone"
            elif r.key in self._decisions:
                what = "; ".join(dict.fromkeys(self._decisions[r.key]))
            elif self.regions and self.regions.get(r.key) is None:
                what = "silent in the whole song"
            else:
                what = "unchanged"
            self.log(f"    = {r.name}: {what}")

    # ── 3. final ────────────────────────────────────────────────────────────
    def final_solver(self, main: tuple, deadline: float) -> None:
        from .planner import RulePlanner

        name, start, length = main
        self.log(f"\n━━ Final check: master, {name}")
        self.play_loop(start, length)
        self.begin_step(self.W_FINAL)
        m = None
        if self.s.complete and self.s.free_rounds and not isinstance(self.planner, RulePlanner):
            self._loudness_auto = True
            try:
                m = self.section("final check", deadline, max_rounds=self.s.free_rounds)
            finally:
                self._loudness_auto = False
        if self.env.now() >= deadline:
            return
        if m is None:
            m = self.measure_verified("full")
            self._log_distance(m)
        baseline = self.quality_anchors.get(self.quality_key(m, "full")) or mix_quality.anchor(m)
        self.log("\n▶ Loudness")
        if self.sanitize_chain():
            m = self.measure_verified("full")
        if m.get("input_peak99") is not None:
            # The calculated cuts lowered the level into the chain: back onto the ceiling, so the limits stay
            # gain reduction. Only when it moved by a dB or more, to spare a measurement.
            trim = self._trim_to(m["input_peak99"], 1.0, hold=True)
            if trim is not None:
                from .agent import Round
                after = self.measure_verified("full")
                self._record(Round("level base", 2, m["objective"], after["objective"], {
                    "assessment": trim["text"], "verification": "level base held before the loudness step",
                    "mix_intent": {"priority": "none", "evidence": trim["text"], "preserve": "balance of all channels",
                                   "reference_relevance": "limiter and clipper limits count as gain reduction"}},
                    trim["rr"].applied, trim["rr"].skipped, parameters=self.journal_parameters(trim["rr"])))
                m = after
        measured = self.set_loudness(m, baseline)
        self.report_final(measured)

    def set_loudness(self, m: dict, baseline: dict) -> dict:
        """Limiter, then clipper, towards the references' loudness, no denser than they allow."""
        cmp, master, ref_plr = m.get("cmp"), m.get("master"), mix_quality.reference_plr(m)
        if cmp is None or master is None or ref_plr is None:
            self.log("  Loudness: no reference comparison here, left as it is.")
            return m
        gap = -float(cmp.loudness_diff)
        if gap <= 0.5:
            self.log(f"  Loudness: {abs(gap):.1f} LU from the references, nothing to do.")
            return m
        # A small margin under the references' density, so measurement spread does not undo the first step.
        room = master.plr - (ref_plr - mix_quality.DENSER_THAN_REFERENCE_DB) - 0.2
        if room <= 0.3:
            self.log(f"  Loudness: {gap:.1f} LU quieter, but already as dense as the references allow "
                     f"(PLR {master.plr:.1f}, references {ref_plr:.1f} dB); left as it is.")
            return m
        push = min(gap, room)
        # Input peaks above the ceiling are gain reduction the limiter already does.
        over = max(0.0, (m.get("input_peak99") or LIMITER_CEILING_DB) - LIMITER_CEILING_DB)
        amount = push
        for attempt in range(2):
            limiter_now = self.ex.limiter_gain() or 0.0
            lim = max(0.0, min(amount, LIMITER_MAX_GAIN_DB - limiter_now - over))
            clip = 0.0
            if amount - lim > 0.3 and self.ex.clipper():
                clip = max(0.0, min(amount - lim, CLIPPER_MAX_GAIN_DB - (self.ex.clipper_gain() or 0.0)))
            actions = _chunks("limiter", lim, 2.0) + _chunks("clipper", clip, 1.5)
            if not actions:
                self.log("  Loudness: limiter and clipper are at their maximum; the remaining gap is accepted.")
                return m
            self.log(f"  round {attempt + 1}: {gap:.1f} LU quieter than the references (PLR {master.plr:.1f}, "
                     f"references {ref_plr:.1f} dB): limiter +{lim:.1f} dB" + (f", clipper +{clip:.1f} dB" if clip else ""))
            for a in actions:
                self.log(f"    · {a['type']} master: {a['reason']}")
            workflow.check_pause()
            rr = self.ex.apply(actions)
            if not rr.changes:
                return m
            try:
                self.env.wait(self.s.settle)
                after = self.measure_verified("full")
            except BaseException:
                self.ex.undo(rr)
                raise
            ok, why = mix_quality.evaluate(m, after, baseline, "loudness", self.s.min_gain)
            from .agent import Round, _parts
            record = {"assessment": "loudness towards the references",
                      "mix_intent": {"priority": "loudness", "evidence": f"{gap:.1f} LU under the references",
                                     "preserve": f"PLR at least {ref_plr - mix_quality.DENSER_THAN_REFERENCE_DB:.1f} dB",
                                     "reference_relevance": "loudness of the full reference songs"},
                      "verification": why}
            rnd = Round("loudness", attempt + 1, m["objective"], after["objective"], record, rr.applied, rr.skipped,
                        parameters=self.journal_parameters(rr))
            if ok:
                self.log(f"    ✓ {m['objective']:.2f} → {after['objective']:.2f}   ({_parts(after['parts'])})")
                self.log(f"    verified: {why}")
                self._record(rnd)
                return after
            self.ex.undo(rr)
            rnd.undone = True
            self._record(rnd)
            self.log(f"    ↺ undone: {why} ({m['objective']:.2f} → {after['objective']:.2f})")
            self.env.wait(self.s.settle)
            # The second step from what the first one measured: PLR lost per dB of gain, so it lands under the
            # references' density instead of guessing half.
            lost = master.plr - after["master"].plr if after.get("master") is not None else 0.0
            amount = (min(push, room / (lost / (lim + clip)) * 0.9) if lost > 0.1 and lim + clip > 0 else push * 0.5)
            if amount < 0.3:
                break
        return m

    def report_final(self, measured: dict) -> None:
        from .agent import _parts

        self.log(f"  distance {measured['objective']:.2f}   ({_parts(measured['parts'])})")
        self.explain(measured)
        report = {**mix_quality.final_assessment(measured, self.quality_anchors.get(self.quality_key(measured, "full"))),
                  "passage": list(self.loop), "parts": measured["parts"], "spread": measured["noise"],
                  "revisited_channels": [], "other_passages_checked": len(self._checked_passages),
                  "listening_judgment": False}
        chain = self.chain_report(measured)
        if chain is not None:
            report["loudness_chain"] = chain
            text = (f"Input peaks {chain['input_peak_dbfs']:.1f} dBFS after a trim of {chain['trim_db']:+.1f} dB; "
                    f"limiter +{chain['limiter_db']:.1f}, clipper +{chain['clipper_db']:.1f} dB: about "
                    f"{chain['peak_reduction_db']:.1f} dB peak reduction, {chain['chain_gain_lu']:+.1f} LU louder.")
            report["checks"].append({"name": "Loudness chain", "status": "attention" if chain["peak_reduction_db"]
                                     > LIMITER_MAX_GAIN_DB + CLIPPER_MAX_GAIN_DB + 0.5 else "clear", "text": text})
            self.log("  " + text)
        drift = self.passage_drift(measured)
        if drift is not None:
            report["passages"] = drift
            worst = [d for d in drift if d["status"] == "attention"]
            report["checks"].append({"name": "Other passages", "status": "attention" if worst else "clear", "text":
                                     ("; ".join(d["text"] for d in worst) if worst else
                                      f"{len(drift)} other passage(s) changed like the loudest part (within 2 dB per region).")})
            for d in drift:
                self.log(f"  {'⚠' if d['status'] == 'attention' else '='} {d['text']}")
            if worst and report.get("status") == "checked":
                report.update(status="attention", summary="Some measured risks need attention.")
        workflow.update(quality_check=report)
        self.log("  " + report["summary"] + " " + report["benefit_text"])
        self.log("  Final measurements verified; this is an automated check, not a listening judgment.")
        self.finish_step("done")
        self.clear_checkpoint()
        self._mix_complete = True

    def passage_drift(self, measured: dict) -> list | None:
        """The other checked passages against the original song, region by region, relative to the change in
        the loudest part: the calculated EQ is static and also acts where it was not measured."""
        scan, master = getattr(self, "_scan", None), measured.get("master")
        if scan is None or not scan.master or not getattr(self, "_passages", None) or master is None:
            return None

        def original(region):
            s0, length = region
            return stats([f for b, f in scan.master if s0 - scan.start <= b < s0 - scan.start + length])

        def by_region(now, then):
            return {name: float(np.mean(now.shape_db[bands_between(lo, hi)] - then.shape_db[bands_between(lo, hi)]))
                    for name, lo, hi in mix_quality.REGIONS}

        main = original(self.loop)
        if main is None:
            return None
        loud = by_region(master, main)
        out = []
        for region, now in sorted(self._passages.items()):
            then = original(region)
            if now is None or then is None:
                continue
            rel = {k: v - loud[k] for k, v in by_region(now, then).items()}
            name, worst = max(rel.items(), key=lambda kv: abs(kv[1]))
            a, b = region[0] / 4 + 1, (region[0] + region[1]) / 4 + 1
            attention = abs(worst) > 2.0
            out.append({"bars": [a, b], "region": name, "relative_db": round(worst, 1),
                        "status": "attention" if attention else "clear",
                        "text": (f"bars {a:g}–{b:g}: {name} {worst:+.1f} dB against the original, beyond what changed "
                                 "in the loudest part" if attention else
                                 f"bars {a:g}–{b:g}: changed like the loudest part (largest {name} {worst:+.1f} dB)")})
        return out

    def _log_distance(self, m: dict) -> None:
        from .agent import _parts

        self.log(f"  distance {m['objective']:.2f}   ({_parts(m['parts'])})")
        self.explain(m)


def _chunks(kind: str, amount: float, step: float) -> list[dict]:
    """The executor moves limiter and clipper at most `step` dB per action: several actions for more."""
    out = []
    while amount > 0.05:
        part = min(step, amount)
        out.append({"type": kind, "delta_db": round(part, 2), "track": None,
                    "reason": f"+{part:.1f} dB towards the references' loudness"})
        amount -= part
    return out
