"""One folder per track: songs/<slug>/ holds its references/, roles.json, mixmatch.json and runs/.

songs/songs.json names the tracks and which one is current. A track remembers the channel names of its
Live set, so the interface notices when another set is open and offers a new track or the matching one.
Without songs/songs.json the working directory itself is the one track (the layout before tracks existed).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time

DIR = "songs"
INDEX = "songs.json"
PER_SONG = ("runs", "roles.json", "mixmatch.json")
SAME_SET = 0.5          # share of channel names two sets have in common to count as the same track


def _index_path(root: str) -> str:
    return os.path.join(root, DIR, INDEX)


def load(root: str) -> dict | None:
    try:
        with open(_index_path(root)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    data.setdefault("songs", {})
    return data


def _save(root: str, data: dict) -> None:
    os.makedirs(os.path.join(root, DIR), exist_ok=True)
    tmp = _index_path(root) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _index_path(root))


def current(root: str) -> dict | None:
    """{"slug", "name", "channels", …} of the current track, None in the old layout."""
    idx = load(root)
    if not idx or idx.get("current") not in idx["songs"]:
        return None
    return {"slug": idx["current"], **idx["songs"][idx["current"]]}


def current_dir(root: str) -> str:
    song = current(root)
    return os.path.join(root, DIR, song["slug"]) if song else root


def project_root(workdir: str) -> str:
    """Find the shared project from a song's working directory."""
    folder = os.path.abspath(workdir)
    parent = os.path.dirname(folder)
    root = os.path.dirname(parent)
    return root if os.path.basename(parent) == DIR and load(root) else folder


def reference_folders(workdir: str) -> list[str]:
    """Chosen references for this song plus the shared reference library."""
    return list(dict.fromkeys([os.path.join(os.path.abspath(workdir), "references"),
                               os.path.join(project_root(workdir), "references")]))


def listing(root: str) -> list[dict]:
    idx = load(root) or {"songs": {}}
    return [{"slug": s, "name": v.get("name", s), "created": v.get("created"), "current": s == idx.get("current")}
            for s, v in sorted(idx["songs"].items(), key=lambda kv: kv[1].get("created") or "")]


def _slug(name: str, taken) -> str:
    base = re.sub(r"[^\w\- ]+", "", name, flags=re.UNICODE).strip().replace(" ", "-")[:60] or "track"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def create(root: str, name: str, description: str = "", channels: list[str] | None = None) -> str:
    """A new, empty track (own references, roles and runs) that becomes the current one."""
    from . import project

    name = name.strip()
    if not name:
        raise ValueError("give the track a name")
    idx = load(root) or {"current": None, "songs": {}}
    slug = _slug(name, idx["songs"])
    folder = os.path.join(root, DIR, slug)
    for sub in ("references", "runs"):
        os.makedirs(os.path.join(folder, sub), exist_ok=True)
    if description.strip():
        project.save({"description": description.strip()}, os.path.join(folder, project.PATH))
    idx["songs"][slug] = {"name": name, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "channels": channels or []}
    idx["current"] = slug
    _save(root, idx)
    return slug


def switch(root: str, slug: str) -> None:
    idx = load(root)
    if not idx or slug not in idx["songs"]:
        raise ValueError(f"no track {slug!r}")
    idx["current"] = slug
    _save(root, idx)


def activate_live_set(root: str, name: str, channels: list[str], live_path: str = "") -> dict | None:
    """Make the connected Live set the current project.

    A saved Live-set path is its primary identity.  This matters for copies and
    templates with the same channel layout: they are still new projects.  For
    older control surfaces, an explicit set name can be used as a fallback.
    Channel similarity alone cannot distinguish a copy or a Save As.
    """
    name = name.strip()
    idx = load(root)
    live_path = os.path.realpath(live_path) if live_path else ""
    if not name and not live_path:
        return None
    if idx:
        if live_path:
            by_path = next((slug for slug, song in idx["songs"].items()
                            if song.get("live_path") == live_path), None)
            if not by_path:
                # Bind a pre-path project once; never merge an already-bound copy.
                by_path = next((slug for slug, song in idx["songs"].items()
                                if not song.get("live_path") and song.get("name") == name
                                and similarity(song.get("channels"), channels) >= .9), None)
                if by_path:
                    idx["songs"][by_path]["live_path"] = live_path
                    _save(root, idx)
            if by_path:
                if idx.get("current") != by_path:
                    idx["current"] = by_path
                    _save(root, idx)
                remember_channels(root, channels)
                remember_live_name(root, name, channels)
                return current(root)
        if name:
            by_name = next((slug for slug, song in idx["songs"].items()
                            if song.get("name") == name and not song.get("live_path")), None)
            if by_name:
                if idx.get("current") != by_name or live_path:
                    idx["current"] = by_name
                    if live_path:
                        idx["songs"][by_name]["live_path"] = live_path
                    _save(root, idx)
                remember_channels(root, channels)
                return current(root)
    # An unsaved or otherwise unnamed set is still a usable project.  Give it a
    # stable, clear label until Live provides a filename.
    slug = create(root, name or "Untitled Live set", channels=channels)
    if live_path:
        idx = load(root)
        idx["songs"][slug]["live_path"] = live_path
        _save(root, idx)
    return current(root)


def remember_channels(root: str, channels: list[str]) -> None:
    """The open set belongs to the current track: keep its channel names (tracks get added while mixing)."""
    idx = load(root)
    if idx and idx.get("current") in idx["songs"] and channels:
        if idx["songs"][idx["current"]].get("channels") != channels:
            idx["songs"][idx["current"]]["channels"] = list(channels)
            _save(root, idx)


def channel_names(set_state: dict) -> list[str]:
    return [t["name"] for t in set_state.get("tracks", []) if t.get("kind") != "master"]


def remember_live_name(root: str, name: str, channels: list[str]) -> None:
    """Use the matching Live set's name without moving its references or running jobs."""
    name = name.strip()
    idx = load(root)
    if not name or not channels or not idx or idx.get("current") not in idx["songs"]:
        return
    song = idx["songs"][idx["current"]]
    if song.get("channels") and similarity(song["channels"], channels) < SAME_SET:
        return
    if song.get("name") != name:
        song["name"] = name
        _save(root, idx)


def similarity(a, b) -> float:
    a, b = set(a or ()), set(b or ())
    return len(a & b) / len(a | b) if a and b else 0.0


def check_set(root: str, channels: list[str] | None) -> dict:
    """Whether the open Live set is the current track. A track without channel names yet (just created) takes
    the open set's. Returns {"same": bool, "other": {"slug", "name"} | None}; same=True when nothing is known."""
    song = current(root)
    if song is None or not channels:
        return {"same": True, "other": None}
    if not song.get("channels"):
        remember_channels(root, channels)
        return {"same": True, "other": None}
    if similarity(song["channels"], channels) >= SAME_SET:
        remember_channels(root, channels)
        return {"same": True, "other": None}
    idx = load(root)
    best = max(((similarity(v.get("channels"), channels), s) for s, v in idx["songs"].items() if s != song["slug"]),
               default=(0.0, None))
    other = {"slug": best[1], "name": idx["songs"][best[1]].get("name", best[1])} if best[0] >= SAME_SET else None
    return {"same": False, "other": other}


def migrate(root: str) -> str | None:
    """Move song data into the first track; references/ stays as the shared library."""
    from . import project

    if load(root) is not None:
        return None
    present = [p for p in PER_SONG if os.path.exists(os.path.join(root, p))]
    if not present and not os.path.isdir(os.path.join(root, "references")):
        return None
    desc = project.description(os.path.join(root, project.PATH)) if "mixmatch.json" in present else ""
    name = (re.split(r"[,;.]", desc)[0].strip()[:40] if desc else "") or "First track"
    channels = []
    if "roles.json" in present:
        try:
            with open(os.path.join(root, "roles.json")) as f:
                channels = list(json.load(f))
        except (OSError, ValueError):
            pass
    slug = _slug(name, ())
    folder = os.path.join(root, DIR, slug)
    os.makedirs(folder, exist_ok=True)
    for p in present:
        shutil.move(os.path.join(root, p), os.path.join(folder, p))
    os.makedirs(os.path.join(folder, "references"), exist_ok=True)
    _save(root, {"current": slug, "songs": {slug: {"name": name, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                  "channels": channels}}})
    return name


AUDIO = (".wav", ".aif", ".aiff", ".flac", ".mp3")


def used_references(root: str) -> list[dict]:
    """Reference files of the other tracks, to pick again: [{"slug", "track", "file"}]."""
    idx = load(root)
    if not idx:
        return []
    out = []
    for slug, v in sorted(idx["songs"].items(), key=lambda kv: kv[1].get("created") or "", reverse=True):
        if slug == idx.get("current"):
            continue
        folder = os.path.join(root, DIR, slug, "references")
        if os.path.isdir(folder):
            out += [{"slug": slug, "track": v.get("name", slug), "file": f}
                    for f in sorted(os.listdir(folder)) if f.lower().endswith(AUDIO) and not f.startswith(".")]
    return out


def reuse_reference(root: str, slug: str, file: str) -> str:
    """Copies a reference of another track into the current one; returns the file name."""
    idx = load(root)
    name = os.path.basename(file)
    if not idx or slug not in idx["songs"] or name != file or not name.lower().endswith(AUDIO):
        raise ValueError("no such reference")
    src = os.path.join(root, DIR, slug, "references", name)
    if not os.path.isfile(src):
        raise ValueError(f"{name} is not in «{idx['songs'][slug].get('name', slug)}» any more")
    target = os.path.join(current_dir(root), "references")
    os.makedirs(target, exist_ok=True)
    shutil.copy2(src, os.path.join(target, name))
    return name
