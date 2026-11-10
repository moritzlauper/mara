"""Mix-first planning evidence and bounded, measured tradeoffs.

These are engineering heuristics, not a perceptual listening score. Reference
distance remains useful, but cannot buy clipping, lost layers or lost punch.
"""
from __future__ import annotations

import math

import numpy as np

from .bands import bands_between

VERSION = "mix-first-v1"
METRICS = ("masking", "tonal", "dynamics", "width", "loudness", "low_side", "clipping")
REGIONS = (("sub", 30, 80), ("bass", 80, 200), ("low mids", 200, 500),
           ("mids", 500, 2000), ("presence", 2000, 6000), ("air", 6000, 16000))


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
            "reference_applies": cmp is not None,
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
            "master": ({k: float(getattr(master, k)) for k in ("peak_db", "plr", "low_side_db", "loudness")}
                       if master else None),
            "tonal": [float(x) for x in cmp.tonal_excess] if cmp else None,
            "tracks": {k: {"loudness": float(s.loudness), "active": float(s.active)}
                       for k, s in m.get("tracks", {}).items() if s is not None}}


def evaluate(before: dict, after: dict, baseline: dict, priority: str | None, min_gain: float) -> tuple[bool, str]:
    """The planner chooses what needs help; measurements decide whether it helped.

    Never rebase allowances on each accepted round. Otherwise small compromises
    accumulate into a hollow mix or a flattened master. No model-selected weights.
    """
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
    if plr_loss > 3.0 or (plr_loss > 1.5 and a.loudness > original["loudness"] + 0.5):
        return False, "cumulative dynamics loss against the passage baseline"
    for k, st in before.get("tracks", {}).items():
        if st is None or st.active < 0.25:
            continue
        now = after.get("tracks", {}).get(k)
        start = baseline.get("tracks", {}).get(k, {"loudness": st.loudness})
        if now is None or now.active < st.active * 0.7:
            return False, f"active layer {k} disappeared"
        # Relative balance, so an overall gain change does not look like lost layers.
        initial_relative = start["loudness"] - original["loudness"]
        if now.loudness - a.loudness < initial_relative - 4.0:
            return False, f"cumulative level loss on {k}"
    if ap.get("masking", 0) > min(bp.get("masking", 0), base.get("masking", 0)) + max(0.15, base.get("masking", 0) * 0.1):
        return False, "masking regressed"
    # Tolerated reference compromises, in the existing weighted score's units.
    for metric, allowance in (("tonal", 0.6), ("width", 0.3), ("dynamics", 0.45), ("loudness", 1.0)):
        if ap.get(metric, 0) > min(bp.get(metric, 0), base.get(metric, 0)) + allowance:
            return False, f"{metric} moved too far from the reference"
    cmp = after.get("cmp")
    if cmp is not None and baseline.get("tonal") is not None:
        # RMS alone would hide a newly hollow or harsh individual region.
        if np.any(abs(cmp.tonal_excess) > np.abs(baseline["tonal"]) + 2.0):
            return False, "a spectral region deteriorated beyond the passage baseline"
    if priority in METRICS:
        improvement = bp.get(priority, 0) - ap.get(priority, 0)
        if improvement <= min_gain:
            return False, f"no measured improvement in the intended {priority}"
        # Reference dimensions may not trade against each other just to chase numbers.
        if priority in ("tonal", "width", "dynamics", "loudness") and after["objective"] >= before["objective"] - min_gain:
            return False, "reference move brought no overall improvement"
        return True, f"verified {priority} improvement; mix character protected"
    if priority is not None:
        return False, "no measurable priority selected"
    # Old/custom/rule planners have no structured intent. Still apply the protections.
    if after["objective"] < before["objective"] - min_gain:
        return True, "measured improvement within mix protections"
    return False, "no measurable improvement"
