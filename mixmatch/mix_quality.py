"""Mix-first planning evidence and bounded, measured tradeoffs.

These are engineering heuristics, not a perceptual listening score. Reference
distance remains useful, but cannot buy clipping, lost layers or lost punch.
"""
from __future__ import annotations

import math

import numpy as np

from .bands import bands_between

VERSION = "mix-first-v2"
# Loudness may make the master denser than the original mix, but not more than this much denser (lower PLR)
# than the references: loud like them, while still holding together.
DENSER_THAN_REFERENCE_DB = 1.0
METRICS = ("masking", "tonal", "dynamics", "width", "loudness", "low_side", "clipping")
REGIONS = (("sub", 30, 80), ("bass", 80, 200), ("low mids", 200, 500),
           ("mids", 500, 2000), ("presence", 2000, 6000), ("air", 6000, 16000))


def combine(samples: list[dict]) -> dict:
    """Mean repeated windows, keeping conservative safety readings and observed spread."""
    from dataclasses import replace

    result = dict(samples[-1])
    keys = set().union(*(m["parts"] for m in samples))
    result["parts"] = {k: float(np.mean([m["parts"].get(k, 0) for m in samples])) for k in keys}
    result["noise"] = {k: float(np.ptp([m["parts"].get(k, 0) for m in samples])) for k in keys}
    result["objective"] = sum(result["parts"].values())
    result["samples"] = len(samples)
    assessment = [m.get("assessment_parts", m["parts"]) for m in samples]
    assessment_keys = set.intersection(*(set(p) for p in assessment))
    result["assessment_parts"] = {k: float(np.mean([p[k] for p in assessment])) for k in assessment_keys}
    result["assessment_noise"] = {k: float(np.ptp([p[k] for p in assessment])) for k in assessment_keys}
    def average_stats(values):
        if any(v is None for v in values):
            return None
        means = {k: np.mean([getattr(v, k) for v in values], axis=0)
                 for k in ("loudness", "bands_db", "shape_db", "plr", "width_db", "low_side_db")}
        means.update(plr=min(v.plr for v in values), low_side_db=max(v.low_side_db for v in values))
        return replace(values[-1], **means, peak_db=max(v.peak_db for v in values),
                       active=min(v.active for v in values))
    result["master"] = average_stats([m.get("master") for m in samples])
    tracks = set().union(*(m.get("tracks", {}) for m in samples))
    result["tracks"] = {k: average_stats([m.get("tracks", {}).get(k) for m in samples]) for k in tracks}
    pairs = set().union(*(m.get("masking_coverage", {}) for m in samples))
    collisions = {}
    for sample in samples:
        for pair in sample.get("masking", []):
            key = tuple(sorted((pair["track_a"], pair["track_b"])))
            if key not in collisions or pair.get("severity", 0) > collisions[key].get("severity", 0):
                collisions[key] = pair
    result["masking"] = list(collisions.values())
    # Per pair: mean severity and its spread over the repeated windows (a pair missing in a window counts 0),
    # so a collision fix can be judged on its own pair instead of on the sum over every pair.
    per_sample = [{":".join(sorted((p["track_a"], p["track_b"]))): p.get("severity", 0.0)
                   for p in sample.get("masking", [])} for sample in samples]
    severities = {k: [here.get(k, 0.0) for here in per_sample] for k in set().union(*per_sample)}
    result["pair_severity"] = {k: float(np.mean(v)) for k, v in severities.items()}
    result["pair_noise"] = {k: float(np.ptp(v)) for k, v in severities.items()}
    result["masking_coverage"] = {k: min(m.get("masking_coverage", {}).get(k, 0) for m in samples) for k in pairs}
    comparisons = [m.get("cmp") for m in samples]
    result["reference_consistent"] = len({c is None for c in comparisons}) == 1
    if all(c is not None for c in comparisons):
        result["cmp"] = replace(comparisons[-1], **{
            k: np.mean([getattr(c, k) for c in comparisons], axis=0)
            for k in comparisons[-1].__dataclass_fields__})
    else:
        result["cmp"] = None
    return result


def reference_plr(m: dict) -> float | None:
    """PLR of the references when this measurement is compared with them, else None."""
    ref = m.get("ref")
    if m.get("cmp") is None or ref is None:
        return None
    try:
        value = float(ref.plr)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def layer_level(levels: dict, key: str, fallback: float) -> float:
    """A channel's level against the median of the other active channels (`levels`: key → (loudness, active)).

    Not against the master: a limiter or master EQ changes the master but no channel, and must not look like
    every layer getting quieter. Falls back to `fallback` (the master) when there are no other channels."""
    others = [loud for k, (loud, active) in levels.items() if k != key and active >= 0.25 and math.isfinite(loud)]
    return levels[key][0] - (float(np.median(others)) if others else fallback)


def _levels_of(tracks: dict) -> dict:
    return {k: (float(st.loudness), float(st.active)) for k, st in tracks.items() if st is not None}


def _levels_of_anchor(tracks: dict) -> dict:
    return {k: (float(v["loudness"]), float(v.get("active", 1.0))) for k, v in tracks.items()}


def uncertainty(before: dict, after: dict, metric: str) -> float:
    return before.get("noise", {}).get(metric, 0) + after.get("noise", {}).get(metric, 0)


def reference_cost(parts: dict) -> float:
    return sum(parts.get(k, 0) for k in ("tonal", "width", "low_side", "loudness", "dynamics"))


def revisit_candidates(m: dict, channels: list[dict], limit=3) -> list[str]:
    """A bounded second pass on active sources of unresolved problems, never the whole set again."""
    eligible = {c["track"] for c in channels if c.get("kind") not in ("group", "return")
                and m.get("tracks", {}).get(c["track"]) is not None
                and m["tracks"][c["track"]].active >= .5}
    scores = {}
    for region in diagnosis(m, channels)["spectrum"]:
        excess = region["outside_reference_range_db"]
        if excess is not None and excess > 1.5:
            for source in region["strongest_sources"][:2]:
                k = source["track"]
                if k in eligible:
                    scores[k] = scores.get(k, 0) + excess
    for pair in m.get("masking", []):
        k = pair.get("background")
        if k in eligible and pair.get("severity", 0) > .15:
            scores[k] = scores.get(k, 0) + pair["severity"]
    return sorted(scores, key=lambda k: (-scores[k], k))[:limit]


def diagnosis(m: dict, channels: list[dict]) -> dict:
    """Show the spectrum in musical regions and the sources that can explain it.

    Sources are relative levels, not additive shares: routing and correlation
    make summing solo channel powers misleading. Groups/returns are excluded.
    """
    master, cmp = m.get("master"), m.get("cmp")
    regions = []
    for name, low, high in REGIONS:
        bands = bands_between(low, high)
        sources = []
        for c in channels:
            st = m.get("tracks", {}).get(c["track"])
            if st is None or c.get("kind") in ("group", "return"):
                continue
            level = 10 * math.log10(max(1e-18, float(np.mean(10 ** (st.bands_db[bands] / 10)))))
            sources.append({"track": c["track"], "name": c["name"], "role": c["role"],
                            "priority": c["priority"], "band_level_db": round(level, 1)})
        regions.append({"region": name, "hz": [low, high],
                        "mix_shape_db": round(float(np.mean(master.shape_db[bands])), 1) if master else None,
                        "reference_difference_db": round(float(np.mean(cmp.tonal_diff[bands])), 1) if cmp else None,
                        "outside_reference_range_db": round(float(np.mean(abs(cmp.tonal_excess[bands]))), 1) if cmp else None,
                        "strongest_sources": sorted(sources, key=lambda c: -c["band_level_db"])[:3]})
    return {"policy": VERSION, "spectrum": regions, "measured_costs": m.get("parts", {}),
            "reference_applies": cmp is not None, "measurement_spread": m.get("noise", {}),
            "reference_cost": reference_cost(m.get("parts", {})),
            "interpretation": "A difference is a question, not a defect. Relate it to arrangement, source roles, "
                              "user intent and simultaneous masking. Source levels are not additive contributions. "
                              "A dark Rhodes or a bright hat is not meant to have the reference's full spectrum.",
            "verification": "Name one measurable priority in mix_intent. It must improve after the change. "
                            "Small reference tradeoffs are allowed for mix health, bounded against the first "
                            "measurement of this passage across channels and resumes. Unchanged metrics do not "
                            "prove audible improvement. Preserve useful contrast, layers and punch."}


def anchor(m: dict) -> dict:
    """JSON-safe baseline, kept across rounds/channels/resumes for the same loop."""
    master = m.get("master")
    cmp = m.get("cmp")
    return {"parts": dict(m.get("parts", {})),
            "samples": m.get("samples", 1), "noise": dict(m.get("noise", {})),
            "assessment_parts": dict(m.get("assessment_parts", m.get("parts", {}))),
            "assessment_noise": dict(m.get("assessment_noise", m.get("noise", {}))),
            "reference_available": m.get("cmp") is not None and m.get("reference_consistent", True),
            "master": ({k: float(getattr(master, k)) for k in ("peak_db", "plr", "low_side_db", "loudness")}
                       if master else None),
            "tonal": [float(x) for x in cmp.tonal_excess] if cmp else None,
            "tracks": {k: {"loudness": float(s.loudness), "active": float(s.active)}
                       for k, s in m.get("tracks", {}).items() if s is not None}}


def final_assessment(m: dict, baseline: dict | None) -> dict:
    """Report measured benefit separately from mix health and reference distance.

    Baselines must be the original full-mix measurement of this exact passage,
    scoring and reference. A final-pass or legacy baseline cannot stand in for Before.
    These checks describe evidence, not a subjective verdict or a target to force.
    """
    # Planner allowances (e.g. exhausted loudness headroom) must not make the
    # reported reference distance disappear. Assess the unmodified comparison.
    parts, noise, master = m.get("assessment_parts", m.get("parts", {})), m.get("assessment_noise", m.get("noise", {})), m.get("master")
    if baseline:
        baseline = {**baseline, "parts": baseline.get("assessment_parts", baseline.get("parts", {})),
                    "noise": baseline.get("assessment_noise", baseline.get("noise", {}))}
    report = {"schema": 1, "basis": "Repeated measurements of the main passage",
              "samples": m.get("samples", 0), "listening_judgment": False,
              "benefit": "unknown", "checks": [], "improved": [], "worsened": []}
    valid = (master is not None and m.get("samples", 0) >= 2
             and all(k in parts and k in noise and math.isfinite(float(parts[k]))
                     and math.isfinite(float(noise[k])) for k in METRICS)
             and all(math.isfinite(float(getattr(master, k))) for k in
                     ("peak_db", "loudness", "plr", "low_side_db", "active")))
    if not valid or master.active < .5:
        return {**report, "status": "incomplete", "summary": "Not enough reliable audio data for a final assessment.",
                "benefit_text": "Benefit compared with Before could not be verified."}

    def check(name, status, text):
        report["checks"].append({"name": name, "status": status, "text": text})

    peak = master.peak_db
    check("Peak level", "attention" if peak > -1 else "clear",
          f"Sample peak {peak:.1f} dBFS. " + ("Limited headroom." if peak > -1 else "Headroom retained.")
          + " True peaks are not measured.")
    check("Low-end stereo", "attention" if master.low_side_db > -20 else "clear",
          f"Low-frequency side level {master.low_side_db:.1f} dB relative to mid. "
          + ("Check bass translation in mono." if master.low_side_db > -20 else "Low-frequency side energy is limited."))
    comparable = (baseline and baseline.get("before_mix") and baseline.get("samples", 0) >= 2
                  and baseline.get("master")
                  and all(k in baseline.get("parts", {}) and k in baseline.get("noise", {})
                          and math.isfinite(float(baseline["parts"][k]))
                          and math.isfinite(float(baseline["noise"][k])) for k in METRICS)
                  and all(math.isfinite(float(baseline["master"].get(k, float("nan"))))
                          for k in ("plr", "loudness")))
    if comparable:
        loss = baseline["master"]["plr"] - master.plr
        ref_plr = reference_plr(m)
        if ref_plr is not None:
            dynamics_risk = master.plr < ref_plr - DENSER_THAN_REFERENCE_DB - .3
        else:
            dynamics_risk = loss > 3 or (loss > 1.5 and master.loudness > baseline["master"]["loudness"] + .5)
        check("Dynamics", "attention" if dynamics_risk else "clear",
              f"PLR {baseline['master']['plr']:.1f} → {master.plr:.1f} dB"
              + (f" (references {ref_plr:.1f} dB). " if ref_plr is not None else ". ")
              + ("Substantial dynamics loss compared with Before." if dynamics_risk else "No substantial dynamics loss detected.")
              + " PLR is a proxy for punch.")
        now_levels, base_levels = _levels_of(m.get("tracks", {})), _levels_of_anchor(baseline.get("tracks", {}))
        lost = [k for k, st in baseline.get("tracks", {}).items() if st.get("active", 0) >= .5
                and (m.get("tracks", {}).get(k) is None or m["tracks"][k].active < st["active"] * .7
                     or layer_level(now_levels, k, master.loudness)
                     < layer_level(base_levels, k, baseline["master"]["loudness"]) - 4)]
        check("Layers", "attention" if lost else "clear",
              "Reduced or missing layers: " + ", ".join(lost) if lost else "No lost active layers detected against Before.")
    else:
        check("Dynamics", "unknown", f"PLR {master.plr:.1f} dB. No matching original baseline to assess preservation.")

    collisions = len([p for p in m.get("masking", []) if p.get("severity", 0) > .15])
    check("Instrument overlap", "context" if collisions else "unknown",
          f"{collisions} potential collisions in simultaneous playback. Overlap alone does not prove audible masking."
          if collisions else "No strong collisions recorded; this does not establish full masking coverage.")
    ref_available = m.get("cmp") is not None and m.get("reference_consistent", True)
    reference_keys = ("tonal", "width", "low_side", "loudness", "dynamics")
    comparable_ref = comparable and baseline.get("reference_available") and ref_available
    report["reference"] = {"status": "unknown"}
    if comparable_ref:
        start, end = reference_cost(baseline["parts"]), reference_cost(parts)
        spread = sum(noise[k] + baseline["noise"][k] for k in reference_keys)
        change = "closer" if start - end > max(.05, spread) else "farther" if end - start > max(.05, spread) else "similar"
        report["reference"] = {"status": change, "before": start, "after": end, "spread": spread}
        check("Reference direction", "context", f"{start:.2f} → {end:.2f} measured distance. " + {
            "closer": "Closer beyond measured variation.", "farther": "Farther beyond measured variation.",
            "similar": "No clear overall convergence beyond measured variation."}[change])
    else:
        check("Reference direction", "unknown", "No matching repeated Before/After reference comparison available.")

    labels = {"tonal": "Tone balance", "width": "Stereo width", "low_side": "Low-end stereo",
              "loudness": "Loudness", "dynamics": "Dynamics", "masking": "Potential masking", "clipping": "Peak safety"}
    if comparable:
        for key in METRICS:
            if key in reference_keys and not comparable_ref:
                continue
            delta = baseline["parts"][key] - parts[key]
            if abs(delta) > max(.05, noise[key] + baseline["noise"][key]):
                report["improved" if delta > 0 else "worsened"].append(labels[key])
        report["benefit"] = ("mixed" if report["improved"] and report["worsened"] else
                             "improved" if report["improved"] else "regressed" if report["worsened"] else "small")
    report["benefit_text"] = {
        "unknown": "Benefit compared with Before could not be verified.",
        "small": "Little measurable change beyond normal variation. An audible benefit is not established.",
        "improved": "Measured improvements: " + ", ".join(report["improved"]) + ".",
        "mixed": "Mixed result. Improved: " + ", ".join(report["improved"]) + ". Increased differences: " + ", ".join(report["worsened"]) + ".",
        "regressed": "No measured improvement. Increased differences: " + ", ".join(report["worsened"]) + ".",
    }[report["benefit"]]
    attention = any(c["status"] == "attention" for c in report["checks"])
    report.update(status="attention" if attention else "checked",
                  summary="Some measured risks need attention." if attention else "No issues flagged by the available technical checks.")
    return report


def evaluate(before: dict, after: dict, baseline: dict, priority: str | None, min_gain: float) -> tuple[bool, str]:
    """The planner chooses what needs help; measurements decide whether it helped.

    Never rebase allowances on each accepted round. Otherwise small compromises
    accumulate into a hollow mix or a flattened master. No model-selected weights.
    """
    if not before.get("reference_consistent", True) or not after.get("reference_consistent", True):
        return False, "reference unavailable during a verification pass"
    bp, ap, base = before.get("parts", {}), after.get("parts", {}), baseline["parts"]
    if not all(math.isfinite(float(v)) for v in (*ap.values(), after["objective"])):
        return False, "invalid measurements"
    a, b, original = after.get("master"), before.get("master"), baseline.get("master")
    if a is None or b is None or original is None:
        return False, "missing master measurement"
    if not all(math.isfinite(getattr(a, k)) for k in ("peak_db", "plr", "loudness", "low_side_db")):
        return False, "invalid master measurement"
    for pair in before.get("masking", []):
        key = ":".join(sorted((pair["track_a"], pair["track_b"])))
        if after.get("masking_coverage", {}).get(key, 0) < max(0.6, before.get("masking_coverage", {}).get(key, 0) * 0.8):
            return False, "insufficient simultaneous tap data"
    if a.peak_db > max(-1.0, min(b.peak_db, original["peak_db"])) + 0.15:
        return False, "peak/headroom regressed"
    if a.low_side_db > max(-20.0, min(b.low_side_db, original["low_side_db"])) + 1.0:
        return False, "low-end mono compatibility regressed"
    # PLR is only a proxy: removing a bass collision can reduce peaks without
    # flattening transients. Be stricter when loudness is being pushed upwards.
    plr_loss = original["plr"] - a.plr
    pushed = a.loudness > original["loudness"] + 0.5
    ref_plr = reference_plr(after)
    if ref_plr is not None and pushed:
        # Louder towards the references: allowed down to their density, a little beyond, never further.
        if a.plr < ref_plr - DENSER_THAN_REFERENCE_DB and a.plr < b.plr - 0.2:
            return False, f"denser than the references allow (PLR {a.plr:.1f}, references {ref_plr:.1f} dB)"
    elif plr_loss > 3.0 or (plr_loss > 1.5 and pushed):
        return False, "cumulative dynamics loss against the passage baseline"
    now_levels = _levels_of(after.get("tracks", {}))
    base_levels = _levels_of_anchor(baseline.get("tracks", {}))
    for k, st in before.get("tracks", {}).items():
        if st is None or st.active < 0.25:
            continue
        now = after.get("tracks", {}).get(k)
        if now is None or now.active < st.active * 0.7:
            return False, f"active layer {k} disappeared"
        # Relative balance, so an overall gain change does not look like lost layers.
        if k in base_levels:
            initial_relative = layer_level(base_levels, k, original["loudness"])
        else:
            initial_relative = layer_level({**now_levels, k: (st.loudness, st.active)}, k, b.loudness)
        if layer_level(now_levels, k, a.loudness) < initial_relative - 4.0:
            return False, f"cumulative level loss on {k}"
    # An already-drifted baseline must not veto an otherwise improving move.
    # Keep the cumulative allowance, but allow recovery without requiring a single
    # action to undo the entire drift. Small repeated-window variation is tolerated.
    mask_limit = max(bp.get("masking", 0), base.get("masking", 0) + max(0.15, base.get("masking", 0) * 0.1))
    if ap.get("masking", 0) > mask_limit + min(.25, uncertainty(before, after, "masking")):
        return False, "masking regressed"
    # Tolerated reference compromises, in the existing weighted score's units.
    for metric, allowance in (("tonal", 0.6), ("width", 0.3), ("dynamics", 0.45), ("loudness", 1.0)):
        if ap.get(metric, 0) > max(bp.get(metric, 0), base.get(metric, 0) + allowance) + min(.1, uncertainty(before, after, metric)):
            return False, f"{metric} moved too far from the reference"
    cmp = after.get("cmp")
    if cmp is not None and baseline.get("tonal") is not None:
        # RMS alone would hide a newly hollow or harsh individual region.
        if np.any(abs(cmp.tonal_excess) > np.abs(baseline["tonal"]) + 2.0):
            return False, "a spectral region deteriorated beyond the passage baseline"
    if priority == "protect":
        return True, "other passage retains headroom, dynamics and active layers"
    if priority in METRICS:
        improvement = bp.get(priority, 0) - ap.get(priority, 0)
        if improvement <= max(min_gain, uncertainty(before, after, priority)):
            return False, f"no measured improvement in the intended {priority}"
        # Reference dimensions may not trade against each other just to chase numbers.
        if priority in ("tonal", "width", "dynamics", "loudness"):
            spread = sum(uncertainty(before, after, k) for k in ("tonal", "width", "dynamics", "loudness", "low_side"))
            if reference_cost(bp) - reference_cost(ap) <= max(min_gain, spread):
                return False, "reference move brought no reliable reference improvement"
        return True, f"verified {priority} improvement; mix character protected"
    if priority is not None:
        return False, "no measurable priority selected"
    # Old/custom/rule planners have no structured intent. Still apply the protections.
    if after["objective"] < before["objective"] - max(min_gain, sum(uncertainty(before, after, k) for k in METRICS)):
        return True, "measured improvement within mix protections"
    return False, "no measurable improvement"
