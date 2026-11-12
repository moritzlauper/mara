"""The calculated mix flow (Settings.engine == "solver"), see docs/konzept-solver.md.

Instead of one planner proposal per round, every phase measures once, calculates a package of moves from
the channels' spectra (mixmatch.solver), applies it together and verifies it with fresh measurements:

1. Master: the tonal difference to the references is spread over the channels that cause it. The planner
   reviews the draft once (keep, scale, drop, protect), the solver recalculates, the package is measured;
   then once or twice more with what is left. Stereo low end is switched to mono on the channels causing it.
2. Channels: the strongest collisions in the loudest part and in up to two other passages, each fix
   simulated on the recorded frames first and kept only when its own pair measurably improves.
3. Final: up to two free planner rounds for what the calculation does not cover (compressors, sends,
   resonances), then limiter and clipper towards the references' loudness, then the final assessment.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from . import mix_quality, solver, workflow
from .actions import CLIPPER_MAX_GAIN_DB, LIMITER_MAX_GAIN_DB, RoundResult
from .activity import focus as report_focus, report as activity
from .bands import BAND_COUNT


class SolverFlow:
    """Mixed into Agent; uses its measurement, executor, journal and checkpoint helpers."""

    W_SOLVE, W_COLLIDE = 6.0, 4.0

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
        if phase not in ("master", "channels", "effects", "final"):
            phase = "master"
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
            if self.s.complete:
                self.channel_collisions(main, deadline)
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
            self.final_solver(main, deadline)

    def _save_solver_checkpoint(self, phase: str) -> None:
        self.save_checkpoint(phase=phase, next_channel=None, engine="solver",
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
        m = self.measure_verified("full", first=self.measure_fine("full", "m"))
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
            plan = (solver.solve(m["master"], sources, target, self._constraints, beat=self.beat)
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
            accepted, m = self.try_package(name, n, m, baseline, moves, intent, "full", plan)
            self._advance(0.3, name)
            if not accepted:
                break
            mono_needed = False
        if last_plan is not None:
            for key, why in last_plan.unchanged.items():
                if key not in self._decisions:
                    self._decisions[key] = [f"master: unchanged, {why}"]
        return m

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
            ok, why = mix_quality.evaluate(m, after, baseline, intent["priority"], self.s.min_gain)
            record["verification"] = why
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
            self.solve_collisions(f"collision in bars {a:g}–{b:g}", deadline, "local", keys=set(keys))
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
            kept = fix.before - now > max(0.03, spread)
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
        room = master.plr - (ref_plr - mix_quality.DENSER_THAN_REFERENCE_DB)
        if room <= 0.3:
            self.log(f"  Loudness: {gap:.1f} LU quieter, but already as dense as the references allow "
                     f"(PLR {master.plr:.1f}, references {ref_plr:.1f} dB); left as it is.")
            return m
        push = min(gap, room)
        for attempt, factor in enumerate((1.0, 0.5)):
            amount = push * factor
            limiter_now = self.ex.limiter_gain() or 0.0
            lim = max(0.0, min(amount, LIMITER_MAX_GAIN_DB - limiter_now))
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
        return m

    def report_final(self, measured: dict) -> None:
        from .agent import _parts

        self.log(f"  distance {measured['objective']:.2f}   ({_parts(measured['parts'])})")
        self.explain(measured)
        report = {**mix_quality.final_assessment(measured, self.quality_anchors.get(self.quality_key(measured, "full"))),
                  "passage": list(self.loop), "parts": measured["parts"], "spread": measured["noise"],
                  "revisited_channels": [], "other_passages_checked": len(self._checked_passages),
                  "listening_judgment": False}
        workflow.update(quality_check=report)
        self.log("  " + report["summary"] + " " + report["benefit_text"])
        self.log("  Final measurements verified; this is an automated check, not a listening judgment.")
        self.finish_step("done")
        self.clear_checkpoint()
        self._mix_complete = True

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
