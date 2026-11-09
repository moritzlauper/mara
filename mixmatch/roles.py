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

ROLES = [
    "kick", "snare", "clap", "hats", "percussion", "drums_bus",
    "sub", "bass",
    "lead_vocal", "backing_vocal", "vocal_fx",
    "lead_synth", "pluck", "pad", "keys", "piano", "guitar", "strings", "brass",
    "fx", "ambience", "bus", "other",
]

# Priority 1 (background) … 5 (must be heard clearly). A starting point, not a law.
DEFAULT_PRIORITY = {
    "lead_vocal": 5, "kick": 5, "snare": 4, "clap": 4, "bass": 4, "sub": 4, "lead_synth": 4,
    "backing_vocal": 3, "hats": 3, "percussion": 2, "pluck": 3, "keys": 3, "piano": 3, "guitar": 3,
    "strings": 2, "brass": 3, "pad": 2, "fx": 1, "ambience": 1, "vocal_fx": 2, "drums_bus": 4, "bus": 3, "other": 2,
}

NAME_HINTS = [
    (r"\bkick|\bbd\b|bassdrum|kik", "kick"),
    (r"snare|\bsd\b|rim", "snare"),
    (r"clap", "clap"),
    (r"hat|\bhh\b|\boh\b|ride|cymbal|crash|shaker", "hats"),
    (r"perc|tom|conga|bongo|tamb", "percussion"),
    (r"drum|beat|break|loop", "drums_bus"),
    (r"\bsub\b|808", "sub"),
    (r"bass|\bbs\b|reese", "bass"),
    (r"lead ?vox|lead ?voc|main ?vox|vocal(?!.*(bv|back|choir))|\bvox\b|\bvoc\b|rap|sing", "lead_vocal"),
    (r"\bbv|backing|choir|harmon|adlib|dbl", "backing_vocal"),
    (r"chop|vocal ?fx|vox ?fx", "vocal_fx"),
    (r"lead|melody|hook|arp|saw", "lead_synth"),
    (r"pluck|stab", "pluck"),
    (r"pad|atmo|drone|chord", "pad"),
    (r"piano|rhodes|\bep\b", "piano"),
    (r"keys|organ|synth", "keys"),
    (r"guitar|gtr", "guitar"),
    (r"string|violin|cello|orch", "strings"),
    (r"brass|horn|trumpet|sax", "brass"),
    (r"\bfx\b|riser|sweep|impact|noise|uplifter|downlifter|whoosh", "fx"),
    (r"reverb|delay|verb|room|hall|space", "ambience"),
    (r"bus|group|sum", "bus"),
]


@dataclass
class Role:
    key: str                 # remote script track key, e.g. "t3"
    name: str
    role: str
    priority: int
    source: str              # name / devices / sound / claude / user
    note: str = ""


def from_name(name: str) -> str | None:
    n = name.lower()
    for pattern, role in NAME_HINTS:
        if re.search(pattern, n):
            return role
    return None


# Devices whose names say nothing about the sound: Live's own containers and MixMatch itself.
NEUTRAL_DEVICES = {"drum rack", "instrument rack", "audio effect rack", "mixmatch tap"}


def from_devices(devices: list[dict]) -> str | None:
    """Name hints in device and preset names, e.g. «Kick Tight», «Basic Sub Sine», «vocalchain»."""
    for d in devices:
        name = d.get("name", "")
        if name.lower() not in NEUTRAL_DEVICES:
            role = from_name(name)
            if role:
                return role
    return None


def is_drum_rack(devices: list[dict]) -> bool:
    classes = " ".join(d.get("class_name", "") + " " + d.get("name", "") for d in devices).lower()
    return "drumgroup" in classes or "drum rack" in classes


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
    sources = [("name", None if says_nothing(track) else from_name(track["name"])),
               ("devices", from_devices(devices)),
               ("midi", from_midi(notes)),
               ("sound", from_sound(st)),
               ("devices", "drums_bus" if is_drum_rack(devices) else None),
               ("name", from_name(track["name"]))]
    for source, role in sources:
        if role:
            return Role(track["key"], track["name"], role, DEFAULT_PRIORITY[role], source)
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
                                                           "source": "auto"}
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def as_dicts(roles: dict[str, Role]) -> list[dict]:
    return [asdict(r) for r in roles.values()]
