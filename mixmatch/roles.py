"""What each channel is: kick, bass, lead vocal, pad …

Three sources, combined: the track name, the devices on it, and how it sounds
(where its energy sits, how percussive it is). Claude can refine the result,
and the user has the last word through `roles.json`.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass

import numpy as np

from .analysis import Stats
from .bands import CENTRES

ROLE_GROUPS = {
    "drums": ["kick", "snare", "clap", "hats", "cymbals", "shaker", "toms", "percussion", "drum_loop", "drums_bus"],
    "low end": ["sub", "808", "bass", "synth_bass", "bass_guitar"],
    "vocals": ["lead_vocal", "backing_vocal", "vocal_chop", "vocal_fx", "choir"],
    "synths": ["lead_synth", "supersaw", "arp", "pluck", "chords", "pad", "bell"],
    "instruments": ["keys", "piano", "organ", "guitar", "acoustic_guitar", "strings", "brass", "woodwinds",
                    "mallets", "harp", "sample"],
    "fx and space": ["riser", "impact", "fx", "texture", "ambience"],
    "other": ["bus", "other"],
}
ROLES = [r for group in ROLE_GROUPS.values() for r in group]
LOW_END_ROLES = {"sub", "808", "bass", "synth_bass", "bass_guitar"}
BACKGROUND_ROLES = {"fx", "riser", "impact", "texture", "ambience", "bus", "drums_bus"}   # not checked for masking

# Priority 1 (background) … 5 (must be heard clearly). A starting point, not a law.
DEFAULT_PRIORITY = {
    "lead_vocal": 5, "kick": 5, "snare": 4, "clap": 4, "bass": 4, "sub": 4, "lead_synth": 4,
    "backing_vocal": 3, "hats": 3, "percussion": 2, "pluck": 3, "keys": 3, "piano": 3, "guitar": 3,
    "strings": 2, "brass": 3, "pad": 2, "fx": 1, "ambience": 1, "vocal_fx": 2, "drums_bus": 4, "bus": 3, "other": 2,
    "cymbals": 2, "shaker": 2, "toms": 3, "drum_loop": 3, "808": 5, "synth_bass": 4, "bass_guitar": 4,
    "vocal_chop": 3, "choir": 3, "supersaw": 4, "arp": 3, "chords": 3, "bell": 3, "organ": 3,
    "acoustic_guitar": 3, "woodwinds": 3, "mallets": 3, "harp": 2, "sample": 3, "riser": 1, "impact": 1, "texture": 1,
}

NAME_HINTS = [
    (r"\bkick|\bbd\b|bassdrum|kik", "kick"),
    (r"snare|\bsd\b|\brim", "snare"),
    (r"clap", "clap"),
    (r"ride|cymbal|crash|splash|china", "cymbals"),
    (r"shaker|tamb", "shaker"),
    (r"hat|\bhh[cop]?\b|\boh\b", "hats"),
    (r"\btoms?\b|floor ?tom", "toms"),
    (r"perc|conga|bongo|cowbell|clave|\bds (fm|clang)\b", "percussion"),
    (r"top ?loop|drum ?loop|break", "drum_loop"),
    (r"drum|beat|loop", "drums_bus"),
    (r"808", "808"),
    (r"\bsub\b", "sub"),
    (r"bass ?gtr|bass ?guitar|e-?bass|electric ?bass", "bass_guitar"),
    (r"reese|wobble|synth ?bass|growl", "synth_bass"),
    (r"bass|\bbs\b", "bass"),
    (r"choir|chorus ?vox", "choir"),
    (r"chop", "vocal_chop"),
    (r"lead ?vox|lead ?voc|main ?vox|vocal(?!.*(bv|back|choir))|\bvox\b|\bvoc\b|rap|sing", "lead_vocal"),
    (r"\bbv|backing|choir|harmon|adlib|dbl", "backing_vocal"),
    (r"vocal ?fx|vox ?fx", "vocal_fx"),
    (r"supersaw|super ?saw|hoover", "supersaw"),
    (r"\barp", "arp"),
    (r"lead|melody|hook|saw", "lead_synth"),
    (r"pluck", "pluck"),
    (r"chords?\b|stab", "chords"),
    (r"bell|glock|celesta|music ?box", "bell"),
    (r"pad|atmo|drone", "pad"),
    (r"piano|rhodes|wurli|\bep\b", "piano"),
    (r"organ", "organ"),
    (r"keys|synth", "keys"),
    (r"acoustic|nylon|ukulele|uke", "acoustic_guitar"),
    (r"guitar|gtr", "guitar"),
    (r"string|violin|viola|cello|orch", "strings"),
    (r"brass|horn|trumpet|trombone|sax|tuba", "brass"),
    (r"flute|clarinet|oboe|bassoon|woodwind|pan ?pipe", "woodwinds"),
    (r"marimba|vibra|xylo|kalimba|mallet|steel ?drum", "mallets"),
    (r"harp", "harp"),
    (r"sample|\bsmp\b", "sample"),
    (r"riser|uplifter|sweep ?up|build", "riser"),
    (r"impact|hit\b|boom|downlifter|whoosh", "impact"),
    (r"texture|foley|vinyl|crackle|noise", "texture"),
    (r"\bfx\b|sweep", "fx"),
    (r"reverb|delay|verb|room|hall|space", "ambience"),
    (r"bus|group|sum", "bus"),
]


@dataclass
class Role:
    key: str                 # remote script track key, e.g. "t3"
    name: str
    role: str
    priority: int
    source: str              # name / drum pads / devices / midi / sound / user
    note: str = ""


def from_name(name: str) -> str | None:
    n = name.lower().replace("_", " ")
    for pattern, role in NAME_HINTS:
        if re.search(pattern, n):
            return role
    return None


# Devices whose names say nothing about the sound: Live's own containers and MixMatch itself.
NEUTRAL_DEVICES = {"drum rack", "ds drum rack", "instrument rack", "audio effect rack", "mixmatch tap", "drum buss"}
NEUTRAL_CLASSES = {"drumbuss", "eq8", "redux2", "compressor2", "gluecompressor", "stereogain"}


def from_devices(devices: list[dict]) -> str | None:
    """Name hints in device and preset names, e.g. «Kick Tight», «Basic Sub Sine», «vocalchain»."""
    for d in devices:
        if not d.get("enabled", True) or d.get("class_name", "").lower() in NEUTRAL_CLASSES:
            continue
        if is_drum_rack([d]):
            continue
        name = d.get("name", "")
        if name.lower() not in NEUTRAL_DEVICES:
            role = from_name(name)
            if role:
                return role
    return None


def is_drum_rack(devices: list[dict]) -> bool:
    for d in devices:
        if not d.get("enabled", True):
            continue
        label = (d.get("class_name", "") + " " + d.get("name", "")).lower()
        if "drumgroup" in label or "drum rack" in label or "drum_pads" in d:
            return True
        if any(is_drum_rack(c.get("devices", [])) for c in d.get("chains", []) if not c.get("muted")):
            return True
    return False


def from_rack_content(devices: list[dict], played_notes: dict | None = None) -> tuple[str | None, str]:
    """Infer a rack's instrument from the pads actually played, never from a GM note table.

    A full kit with one used kick pad is a kick. Different played drum families
    form a kit. If note data is unavailable, many loaded pads are inconclusive;
    one loaded sound can still identify the instrument.
    """
    played = None if played_notes is None else {int(n) for n, count in played_notes.items() if count > 0}
    found_pads = []

    def active(items):
        soloed = any(x.get("solo") for x in items)
        return [x for x in items if not x.get("muted") and (not soloed or x.get("solo"))]

    def names(ds):
        found = []
        for d in ds:
            if not d.get("enabled", True) or d.get("type") == 2:
                continue  # an effect preset does not identify the instrument inside a pad
            if d.get("sample_name"):
                found.append(d["sample_name"])
            found += names([child for c in active(d.get("chains", [])) for child in c.get("devices", [])])
            found.append(d.get("name", ""))
        return found

    def walk(ds):
        for d in ds:
            if not d.get("enabled", True):
                continue
            if "drum_pads" in d:
                pads = active(d["drum_pads"])
                if played is not None:
                    pads = [p for p in pads if p["note"] in played]
                elif len(pads) > 1:
                    continue  # loaded sounds alone do not say which of them the part plays
                for p in pads:
                    chains = active(p.get("chains", []))
                    sounding = [child for c in chains for child in c.get("devices", [])
                                if child.get("enabled", True) and child.get("type") != 2]
                    if not sounding:
                        continue
                    labels = names(sounding) + [c.get("name", "") for c in chains] + [p.get("name", "")]
                    roles = [from_name(n) for n in labels]
                    role = next((r for r in roles if r and r not in {"drums_bus", "bus"}), None)
                    found_pads.append((role, f"{p['note']}: {p['name']}"))
            else:
                walk([child for c in active(d.get("chains", [])) for child in c.get("devices", [])])
    walk(devices)
    if found_pads:
        roles = {r for r, _ in found_pads}
        note = "Played pads: " if played is not None else "Only populated pad: "
        note += "; ".join(n for _, n in found_pads)
        if len(roles) == 1 and None not in roles:
            return next(iter(roles)), note
        if len(roles - {None}) >= 2:
            return "drums_bus", note
        return None, note  # an unknown sound alongside a kick must not be called just a kick
    if not is_drum_rack(devices):
        role = from_devices([{"name": n} for n in names(devices)])
        return role, ""
    return None, ""


def says_nothing(track: dict) -> bool:
    """Live's automatic names («3-Audio», «2-Drum Rack», «14-Serum») only repeat the track type or a device."""
    m = re.match(r"^\d+[-\s]+(.*)$", track["name"].strip())
    rest = (m.group(1) if m else track["name"]).strip().lower()
    return rest in ("audio", "midi", "") or rest in {d.get("name", "").lower() for d in track.get("devices", [])}


def from_sound(st: Stats | None) -> str | None:
    """Very rough guess from the spectrum: where is the energy, and how spiky is the signal."""
    if st is None:
        return None
    power = 10 ** (st.bands_db / 10)
    total = power.sum()
    if total <= 0:
        return None
    centroid = float((power * np.array(CENTRES)).sum() / total)
    low = power[[i for i, f in enumerate(CENTRES) if f < 120]].sum() / total
    percussive = st.plr > 14
    if low > 0.6 and percussive:
        return "kick"
    if low > 0.5:
        return "bass"
    if centroid > 6000 and percussive:
        return "hats"
    if percussive and 150 < centroid < 4000:
        return "snare"
    if st.width_db > -4 and not percussive:
        return "pad"
    return None


def from_midi(notes: list | None) -> str | None:
    """What a part plays, from its MIDI notes ([track, pitch, start, duration, velocity]).

    Low single notes: bass. Chords held for a bar or so: pad. Short chords: pluck (stabs). Single notes higher
    up: a lead or hook. Anything with too few notes says nothing.
    """
    if not notes or len(notes) < 8:
        return None
    pitches = np.array([n[1] for n in notes], dtype=float)
    durations = np.array([n[3] for n in notes], dtype=float)
    poly = len(notes) / max(1, len({round(float(n[2]), 2) for n in notes}))   # notes per onset
    pitch, length = float(np.median(pitches)), float(np.median(durations))
    if pitch < 40 and poly < 1.3:
        return "bass"
    if poly >= 2.0:
        return "pad" if length >= 1.5 else "pluck"
    if poly < 1.5:
        return "pad" if length >= 2.0 else "lead_synth"
    return "keys"


def guess(track: dict, st: Stats | None, notes: list | None = None) -> Role:
    devices = track.get("devices", [])
    detail = track.get("role_devices", devices)
    rack = is_drum_rack(detail)
    content_role, evidence = from_rack_content(detail, track.get("played_notes"))
    name_role = None if says_nothing(track) else from_name(track["name"])
    if track.get("is_group"):
        role = "drums_bus" if name_role in set(ROLE_GROUPS["drums"]) else "bus"
        return Role(track["key"], track["name"], role, DEFAULT_PRIORITY[role], "group")
    # An automatic container name and an effect called "Drum Buss" are weak evidence.
    specific_name = name_role if name_role not in {"drums_bus", "bus"} else None
    sources = [("name", specific_name),
               ("drum pads" if rack else "devices", content_role),
               ("devices", from_devices([d for d in detail if not rack or
                   (d.get("type") != 2 and from_name(d.get("name", "")) not in {"ambience", "fx"})])),
               ("midi", from_midi(notes) if not rack else None),
               ("sound", from_sound(st)),
               ("devices", "percussion" if rack else None),
               ("name", name_role)]
    for source, role in sources:
        if role:
            return Role(track["key"], track["name"], role, DEFAULT_PRIORITY[role], source,
                        evidence if source == "drum pads" else "")
    return Role(track["key"], track["name"], "other", DEFAULT_PRIORITY["other"], "default")


def _is_users(entry: dict) -> bool:
    """Set by the user: saved in the interface (source «user»), or an older entry that is not the «other» fallback."""
    source = entry.get("source")
    return source == "user" or (source is None and entry.get("role") not in (None, "other"))


def apply_overrides(roles: dict[str, Role], path: str) -> None:
    """roles.json: {"Track name": {"role": "pad", "priority": 2, "note": "…", "source": "user"}}.

    Only the user's entries override; entries MixMatch wrote itself («auto») are guessed again each run.
    """
    if not os.path.exists(path):
        return
    with open(path) as f:
        overrides = json.load(f)
    for r in roles.values():
        o = overrides.get(r.name)
        if not o or not _is_users(o):
            continue
        if o.get("role") in ROLES:
            r.role = o["role"]
        r.priority = int(o.get("priority", DEFAULT_PRIORITY.get(r.role, 2)))
        r.note = o.get("note", r.note)
        r.source = "user"


def save_template(roles: dict[str, Role], path: str) -> None:
    """Writes the roles so the user can correct them: their own entries kept, the rest marked «auto»."""
    existing = {}
    if os.path.exists(path):
        with open(path) as f:
            existing = json.load(f)
    data = {}
    for r in roles.values():
        old = existing.get(r.name)
        data[r.name] = old if old and _is_users(old) else {"role": r.role, "priority": r.priority, "note": r.note,
                                                           "source": "auto", "evidence": r.source}
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def as_dicts(roles: dict[str, Role]) -> list[dict]:
    return [asdict(r) for r in roles.values()]
