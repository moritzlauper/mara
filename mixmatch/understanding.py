"""How MARA understands the song after the analysis, in plain words for the References step.

Written once per analysis to runs/understanding.json: key, tempo, style, the loudest part, the channels by
family and, when references are known, how the loudest part compares with them. The interface shows it before
the user chooses references, so nothing has to be measured again when the mix starts.
"""
from __future__ import annotations

import json
import os
import re

import numpy as np

from .roles import ROLE_GROUPS

FAMILY = {role: family for family, roles in ROLE_GROUPS.items() for role in roles}
REGION_WORDS = {"sub": "sub", "bass": "bass", "low mids": "low mids", "mids": "mids", "presence": "presence",
                "air": "air"}


def short_name(name: str) -> str:
    """«12-KMRBI_IMK_vocal_loop_glitch_burn_130BPM_C#m» → «KMRBI IMK vocal loop glitch burn»."""
    name = re.sub(r"^\d+-", "", name).replace("_", " ")
    name = re.sub(r"\b\d+\s?BPM\b.*$", "", name, flags=re.I).strip()
    return name[:40] or name


def families(roles: dict, muted) -> list[dict]:
    out: dict[str, list[str]] = {}
    for key, r in roles.items():
        if muted(key):
            continue
        family = "groups" if r.role in ("bus", "drums_bus") else FAMILY.get(r.role, "other")
        out.setdefault(family, []).append(short_name(r.name))
    order = [*ROLE_GROUPS, "groups"]
    return [{"family": f, "count": len(out[f]), "names": out[f]} for f in order if f in out]


def comparison(master, target) -> dict | None:
    """Loudest part of the original against the target, in a few readable numbers."""
    if master is None or target is None:
        return None
    from .analysis import compare
    from .mix_quality import REGIONS
    from .bands import bands_between

    cmp = compare(master, target.stats, target.loudness, target.tolerance, target.one_sided,
                  offset=target.offset, anchor=target.anchor, lump=getattr(target, "lump_low", False))
    regions = []
    for name, lo, hi in REGIONS:
        value = float(np.mean(cmp.tonal_excess[bands_between(lo, hi)]))
        if abs(value) >= 1.0:
            regions.append({"region": name, "db": round(value, 1)})
    return {"loudness_lu": round(float(cmp.loudness_diff), 1), "plr_db": round(float(master.plr), 1),
            "reference_plr_db": round(float(target.stats.plr), 1), "width_db": round(float(cmp.width_diff), 1),
            "regions": regions}


def describe(data: dict) -> str:
    """Two or three sentences a producer would say about the track."""
    parts = []
    kind = {"beat": "A beat", "song": "A song with vocals", "instrumental": "An instrumental"}.get(data.get("mode"), "A track")
    key = data.get("key")
    first = kind + (f" in {key}" if key else "") + (f" at {data['tempo']:g} BPM" if data.get("tempo") else "")
    style = data.get("sounds_like") or []
    parts.append(first + (f" that sounds closest to {style[0]}" + (f" and {style[1]}" if len(style) > 1 else "")
                          if style else "") + ".")
    fam = [f for f in data.get("families", []) if f["family"] != "groups"]
    if fam:
        channels = sum(f["count"] for f in fam)
        parts.append(f"{channels} playing channels: " + ", ".join(f"{f['family']} ({f['count']})" for f in fam) + ".")
    loud = data.get("loudest")
    if loud:
        parts.append(f"The loudest part, bars {loud['from']:g}–{loud['to']:g}, sits at {loud['lufs']:.1f} LUFS.")
    cmp = data.get("comparison")
    if cmp:
        words = []
        if abs(cmp["loudness_lu"]) >= 1:
            words.append(f"{abs(cmp['loudness_lu']):.1f} LU {'quieter' if cmp['loudness_lu'] < 0 else 'louder'}")
        if cmp["plr_db"] - cmp["reference_plr_db"] >= 1.5:
            words.append(f"more dynamic (PLR {cmp['plr_db']:.1f} against {cmp['reference_plr_db']:.1f} dB)")
        elif cmp["reference_plr_db"] - cmp["plr_db"] >= 1.5:
            words.append(f"denser (PLR {cmp['plr_db']:.1f} against {cmp['reference_plr_db']:.1f} dB)")
        for r in cmp["regions"][:3]:
            words.append(f"{'more' if r['db'] > 0 else 'less'} {REGION_WORDS.get(r['region'], r['region'])} "
                         f"({r['db']:+.1f} dB)")
        if words:
            parts.append("Against the references it is " + ", ".join(words) + ".")
    return " ".join(parts)


def write(log_dir: str, data: dict) -> dict:
    data = {**data, "text": describe(data)}
    os.makedirs(log_dir, exist_ok=True)
    tmp = os.path.join(log_dir, "understanding.json.part")
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(log_dir, "understanding.json"))
    return data


def read(log_dir: str) -> dict | None:
    try:
        with open(os.path.join(log_dir, "understanding.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
