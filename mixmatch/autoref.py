"""Finds reference tracks by itself: listens to the project, then picks the closest mastered songs.

1. Records the loudest part of the song from Live's master (a temporary audio
   track with input «Resampling», deleted again afterwards).
2. Names its style with CLAP.
3. Collects candidates: what is in references/, mastered songs from the
   user's library, and with --jamendo Creative Commons tracks from Jamendo in
   that style (only where the artist allows the download; JAMENDO_CLIENT_ID).
4. Keeps the few that sound closest to the project.
5. Asks Claude for real tracks to get, with Bandcamp links (many releases there
   are «name your price»). Getting them stays the user's click: MixMatch never
   downloads commercial music.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

from .style import BASE, Listener, library_files, styles

JAMENDO_API = "https://api.jamendo.com/v3.0/tracks/"
JAMENDO_CACHE = os.path.join(BASE, "jamendo")
MASTERED_LUFS = -12.0     # loudest part at least this loud: a finished master, not a rough bounce

# CLAP style → Jamendo tag
JAMENDO_TAGS = {
    "drum and bass": "drumnbass", "liquid drum and bass": "drumnbass", "atmospheric drum and bass": "drumnbass",
    "neurofunk": "drumnbass", "PS2-era video game drum and bass": "drumnbass", "jungle": "jungle",
    "video game soundtrack": "videogame", "breakbeat": "breakbeat", "breakcore": "breakcore",
    "hyperpop": "futurebass", "digicore": "futurebass", "future bass": "futurebass", "melodic bass": "futurebass",
    "emo rap": "hiphop", "cloud rap": "hiphop", "trap": "trap", "rage beat": "trap", "plugg": "trap",
    "drill": "hiphop", "phonk": "hiphop", "boom bap hip hop": "hiphop", "lo-fi hip hop": "lofi",
    "r&b": "rnb", "pop": "pop", "indie pop": "indiepop", "bedroom pop": "indiepop",
    "house": "house", "deep house": "deephouse", "tech house": "techhouse", "techno": "techno", "trance": "trance",
    "uk garage": "2step", "2-step": "2step", "future garage": "2step",   # Jamendo's «garage» is garage rock "dubstep": "dubstep", "riddim": "dubstep",
    "electro house": "electrohouse", "synthwave": "synthwave", "vaporwave": "vaporwave", "ambient": "ambient",
    "idm": "idm", "glitch": "glitch", "chiptune": "chiptune", "jersey club": "club", "footwork": "footwork",
}


# Words in the user's description → Jamendo tags (they win over the audio model)
DESCRIPTION_TAGS = {
    "hyperpop": ["futurebass", "edm", "electronic"], "pc music": ["futurebass", "edm"], "bubblegum": ["edm", "pop"],
    "future bass": ["futurebass", "edm"], "drum and bass": ["drumnbass"], "dnb": ["drumnbass"], "jungle": ["jungle"],
    "garage": ["2step"], "2-step": ["2step"], "chill": ["chillstep"], "lofi": ["lofi"], "lo-fi": ["lofi"],
    "house": ["house"], "techno": ["techno"], "trap": ["trap"], "hip hop": ["hiphop"], "pop": ["pop"],
    "ambient": ["ambient"], "dubstep": ["dubstep"], "synthwave": ["synthwave"],
}


def jamendo_tags(description: str, clap_styles: list[str], limit: int = 4) -> list[str]:
    import re

    words = description.lower()
    tags = [t for key, ts in DESCRIPTION_TAGS.items() if re.search(r"\b" + re.escape(key) + r"\b", words) for t in ts]
    tags += [JAMENDO_TAGS.get(s, "electronic") for s in clap_styles]
    return list(dict.fromkeys(tags))[:limit] or ["electronic"]


def jamendo_client_id() -> str | None:
    return os.environ.get("JAMENDO_CLIENT_ID") or os.environ.get("JAMEDIA_CLIENT_ID")


def jamendo_search(tags: list[str], client_id: str, per_tag: int = 8, log=print) -> list[dict]:
    """Tracks for these tags whose download the artist allows, most popular first."""
    found, seen = [], set()
    for tag in tags:
        query = urllib.parse.urlencode({
            "client_id": client_id, "format": "json", "limit": per_tag * 2, "tags": tag, "order": "popularity_total",
            "audiodlformat": "mp32", "durationbetween": "90_600", "include": "licenses",
        })
        try:
            with urllib.request.urlopen(f"{JAMENDO_API}?{query}", timeout=30) as r:
                data = json.load(r)
        except OSError as e:
            log(f"  Jamendo search for «{tag}» failed: {e}")
            continue
        kept = 0
        for t in data.get("results", []):
            if t["id"] in seen or not t.get("audiodownload_allowed") or not t.get("audiodownload"):
                continue
            seen.add(t["id"])
            found.append({"id": t["id"], "artist": t["artist_name"], "title": t["name"], "tag": tag,
                          "download": t["audiodownload"], "url": t.get("shareurl"), "license": t.get("license_ccurl")})
            kept += 1
            if kept >= per_tag:
                break
    return found


def jamendo_file(track: dict, log=print) -> str | None:
    """Downloads a Jamendo track once (cached)."""
    os.makedirs(JAMENDO_CACHE, exist_ok=True)
    path = os.path.join(JAMENDO_CACHE, f"{track['id']}.mp3")
    if not os.path.exists(path):
        try:
            urllib.request.urlretrieve(track["download"], path + ".part")
            os.replace(path + ".part", path)
        except OSError as e:
            log(f"  download of {track['artist']} – {track['title']} failed: {e}")
            return None
    return path


def capture(live, start: float, length: float, tempo: float, log=print) -> str | None:
    """Records `length` beats of Live's master from `start`; returns the WAV path (in the project's Samples)."""
    live.transport(loop_start=start, loop_length=length, loop=True, time=start, play=True)
    cap = live.request("capture_start", beats=length)
    deadline = time.time() + length * 60 / tempo + 15
    path = None
    try:
        while time.time() < deadline:
            st = live.request("capture_status", track=cap["track"])
            if st["file"] and not st["recording"]:
                path = st["file"]
                break
            time.sleep(0.5)
    finally:
        cleanup(live, cap["track"])
    if path is None:
        log("  recording the master did not finish")
    return path


def capture_begin(live, beats: float, source: str | None = None) -> int:
    """Starts recording `beats` of the master (or of track `source`) at the next bar; returns the capture track."""
    req = {"beats": beats}
    if source:
        req["source"] = source
    return live.request("capture_start", **req)["track"]


def capture_end(live, track: int, timeout: float = 20.0) -> str | None:
    """Waits for the recording to finish, deletes the capture track, returns the WAV path."""
    deadline, path = time.time() + timeout, None
    try:
        while time.time() < deadline:
            st = live.request("capture_status", track=track)
            if st["file"] and not st["recording"]:
                path = st["file"]
                break
            time.sleep(0.3)
    finally:
        cleanup(live, track)
    return path


def cleanup(live, track: int, tries: int = 20) -> None:
    """Deletes the capture track once its recording has really stopped."""
    for _ in range(tries):
        if not live.request("capture_cleanup", track=track).get("pending"):
            return
        time.sleep(0.25)


def mastered(path: str) -> bool:
    from .reference import profile

    try:
        return profile(path).loudness >= MASTERED_LUFS
    except Exception:
        return False


def buy_list(recording: str, listener: Listener, harmony: dict | None = None, note: str = "",
             log=print) -> dict:
    """Claude's suggestions: tracks to get (Bandcamp often has «name your price») and free, legal ones (NCS,
    Creative Commons, free downloads), plus real Jamendo tracks in the style when JAMENDO_CLIENT_ID is set."""
    from .style import describe, styles, suggest

    try:
        desc = describe(recording, listener)
        if harmony:  # Live's MIDI beats the audio estimate
            desc["harmony"] = {k: harmony[k] for k in ("key", "mode", "chords", "source")}
        result = suggest(desc, note=note)
    except Exception as e:
        log(f"  no suggestions from Claude ({str(e)[:100]})")
        return {"style": "", "buy": [], "free": []}
    free = list(result.get("free_suggestions", []))
    client_id = jamendo_client_id()
    if client_id:
        project = listener.file(recording)
        tags = jamendo_tags(note, [s for s, _ in styles(listener, project, 3)])
        scored = []
        for t in jamendo_search(tags, client_id, per_tag=5, log=log):
            path = jamendo_file(t, log)
            if path:
                try:
                    scored.append((float(listener.file(path) @ project), t))   # listened to, not just tagged
                except Exception:
                    continue
        for score, t in sorted(scored, key=lambda x: -x[0])[:3]:
            free.append({"artist": t["artist"], "title": t["title"], "source": "Jamendo",
                         "why": f"Sounds closest to your track among {len(scored)} Jamendo tracks tagged "
                                f"{', '.join(tags)} (similarity {score:.2f}). Creative Commons "
                                f"({t['license'] or 'see page'}), download allowed.",
                         "links": {"jamendo": t["url"], "download": t["download"]}})
    log(f"  to get (style: {result['style']}):")
    for s in result["suggestions"]:
        log(f"    {s['artist']} – {s['title']}: {s['links']['spotify']}  {s['links']['bandcamp']}")
    if free:
        log("  free and legal:")
        for s in free:
            log(f"    {s['artist']} – {s['title']} [{s['source']}]: " + "  ".join(v for k, v in s["links"].items() if k != "spotify"))
    return {"style": result["style"], "buy": result["suggestions"], "free": free}


def find(live, start: float, length: float, tempo: float, library: str | None = "~/Music", count: int = 3,
         references: str = "references", jamendo: bool = False, suggest: bool = True,
         harmony: dict | None = None, note: str = "", log=print) -> tuple[list[dict], dict]:
    """The `count` references that sound closest to the project, as {path, source, artist, title, …}."""
    log("\n━━ Finding references")
    recording = capture(live, start, length, tempo, log)
    if not recording:
        return [], []
    listener = Listener(log=log)
    project = listener.file(recording)
    top = styles(listener, project, 5)
    log("  sounds like: " + ", ".join(f"{s} ({v:.2f})" for s, v in top))

    candidates = []
    if references and os.path.isdir(references):   # what the user put there themselves always competes
        for path in library_files(references, min_seconds=30):
            try:
                candidates.append({"path": path, "source": "references", "score": float(listener.file(path) @ project),
                                   "artist": "", "title": os.path.splitext(os.path.basename(path))[0]})
            except Exception:
                continue
    if library:
        songs = [p for p in library_files(library) if "/Unknown Artist/" not in p]  # loose files: mostly own bounces
        log(f"  listening to {len(songs)} songs in {library} (cached after the first time) …")
        for path in songs:
            try:
                candidates.append({"path": path, "source": "library", "score": float(listener.file(path) @ project),
                                   "artist": os.path.basename(os.path.dirname(os.path.dirname(path))),
                                   "title": os.path.splitext(os.path.basename(path))[0]})
            except Exception:
                continue
    client_id = jamendo_client_id() if jamendo else None
    if jamendo and not client_id:
        log("  --jamendo needs JAMENDO_CLIENT_ID (in env.local)")
    if client_id:
        tags = list(dict.fromkeys(JAMENDO_TAGS.get(s, "electronic") for s, _ in top[:3]))
        found = jamendo_search(tags, client_id, log=log)
        log(f"  Jamendo ({', '.join(tags)}): {len(found)} tracks to try")
        for t in found:
            path = jamendo_file(t, log)
            if path:
                try:
                    candidates.append({**t, "path": path, "source": "jamendo",
                                       "score": float(listener.file(path) @ project)})
                except Exception:
                    continue

    picked, seen = [], set()
    for c in sorted(candidates, key=lambda c: -c["score"]):
        key = os.path.realpath(c["path"])
        if key not in seen and mastered(c["path"]):
            seen.add(key)
            picked.append(c)
            if len(picked) == count:
                break
    for c in picked:
        extra = f"  {c['url']}  ({c['license']})" if c["source"] == "jamendo" else ""
        name = f"{c['artist']} – {c['title']}" if c["artist"] else c["title"]
        log(f"  ✓ {c['score']:.2f}  {name}  [{c['source']}]{extra}")
    if not picked:
        log("  no mastered song close enough was found in references/ or your library")
    to_get = buy_list(recording, listener, harmony, note, log) if suggest else {"style": "", "buy": [], "free": []}
    return picked, to_get
