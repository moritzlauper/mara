"""MARA Bridge: an MCP server that lets Claude Code, Codex or any MCP client hear and change an Ableton set.

    mara-bridge              (or `mixmatch bridge`; stdio, started by the MCP client, not by hand)

The client drives the mix itself. The bridge gives it what a chat model cannot get from Live's API alone: the
measured sound of every channel and of Main (loudness, peaks, tonal balance, stereo width, mono low end), which
channels collide in which frequencies, and the distance to reference tracks. Changes go through the same remote
script as MARA's runs; a snapshot of every parameter is taken before the first change, so `restore` can undo them.

References work two ways. `load_references` makes finished songs the target of `listen` (tonal balance, loudness,
width per channel and on Main, measured live). `record_and_compare` records a passage of Main in Live and
analyses it like a reference file, for «mix it like this», «make it loud like this» or «make the kick like this»:
loudness and density, tonal balance, stereo, and the kick and bass as heard in the full mix.

The protocol is MCP over stdio (newline-delimited JSON-RPC 2.0), implemented here without extra dependencies.
Stdout carries the protocol only: everything else goes to stderr.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import traceback
from pathlib import Path

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
TAP = "MixMatch Tap"
MAX_LISTEN_S = 60.0
MASKING_LIMIT = 8

INSTRUCTIONS = """\
This server connects you to the user's Ableton Live set and lets you hear it through measurement taps.
Typical loop: `status` (connection, taps, cue points) → `get_set` (tracks, devices, parameters) → `listen`
(measure a passage) → `set_parameters` → `listen` again on the same passage and compare. Keep changes that
measurably help and sound plausible, undo the others; `restore` returns to the state before your first change.
References: the user gives a file path (in Claude Code they can drag the file into the chat) or puts the song on
a track named «REF …» in Live (`load_references` without paths finds those). For «make it loud / mix it / make the
kick like this»: `analyze_audio` on the reference, then `record_and_compare` (records the passage from Main and
returns mix minus reference for loudness, tonal, stereo, kick, bass), change, record again. `load_references` sets
the same songs as the target of `listen`, which is faster for iterating on the tonal balance.
Main loudness and the limiter: raise loudness with the limiter/clipper on Main, about 5 dB of gain reduction at
most, and check true peak and PLR against the reference.
Taps measure before the fader; `listen` reports channel levels after faders and groups, i.e. what reaches Main.
Numbers are decision support, not taste: tell the user what you changed and why, and let them listen."""


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def _r(x, n=1):
    return None if x is None or not math.isfinite(x) else round(float(x), n)


class BridgeError(Exception):
    """A problem the client can act on: returned as a tool error, not a crash."""


class Bridge:
    def __init__(self, live=None, hub=None, env=None, state_dir: str | Path | None = None):
        from .agent import RealEnv

        self._live, self._hub = live, hub
        self.env = env or RealEnv()
        if state_dir is None:
            from .platforms import workspace
            state_dir = workspace() / "bridge"
        self.state_dir = Path(state_dir)
        self.target = None
        self.target_info: dict | None = None
        self.started: str | None = None   # snapshot taken before the first change of this session

    # ── connections (lazy: the server starts even while Live is closed) ─────
    @property
    def live(self):
        if self._live is None:
            from .live_client import LiveClient
            try:
                client = LiveClient().connect()
                client.request("ping")
            except OSError:
                raise BridgeError("Cannot reach Live on port 9877. Open Live and select «MixMatch» as a control "
                                  "surface (Settings → Link/Tempo/MIDI). MARA's «Set up the connection» installs it.")
            self._live = client
        return self._live

    @property
    def hub(self):
        if self._hub is None:
            from .taps import TapHub
            hub = TapHub()
            try:
                hub.listen()
            except OSError:
                raise BridgeError("Port 9878 is in use: the MARA app is probably running. Close it, the taps can "
                                  "only report to one program.")
            self._hub = hub
        return self._hub

    def _state(self) -> dict:
        from .live_client import LiveError
        try:
            return self.live.get_set()
        except (OSError, LiveError) as e:
            self._live = None   # reconnect on the next call
            raise BridgeError(f"Lost the connection to Live: {e}")

    def _track(self, state: dict, key: str) -> dict:
        for t in state["tracks"]:
            if t["key"] == key or t["name"].lower() == str(key).lower():
                return t
        raise BridgeError(f"No track {key!r}. Use the keys from get_set (t0, t1 …, r0 …, m for Main).")

    # ── tools ───────────────────────────────────────────────────────────────
    def status(self) -> dict:
        state = self._state()
        taps = {t["key"]: any(d["name"] == TAP for d in t["devices"]) for t in state["tracks"]}
        from .taps import is_reference_name
        without = [t["name"] for t in state["tracks"] if not taps[t["key"]]
                   and not (t["kind"] == "track" and is_reference_name(t["name"]))]
        return {
            "set": state.get("name") or Path(state.get("file_path") or "unsaved").stem,
            "tempo": state.get("tempo"),
            "playing": state.get("is_playing"),
            "song_time_beats": _r(state.get("song_time"), 2),
            "loop": state.get("loop"),
            "cue_points": state.get("cue_points", []),
            "tracks": len(state["tracks"]),
            "tracks_without_tap": without,
            "hint": "Call add_taps to measure these tracks." if without else None,
            "references": self.target_info,
            "snapshot_before_changes": self.started,
        }

    def get_set(self, track: str | None = None, parameters: bool = False) -> dict:
        state = self._state()
        tracks = [self._track(state, track)] if track else state["tracks"]
        detail = parameters or track is not None

        def param(p):
            out = {"i": p["index"], "name": p["name"], "display": p["display"]}
            if detail:
                out.update(value=_r(p["value"], 4), min=p["min"], max=p["max"])
                if p.get("items"):
                    out["items"] = p["items"]
                if p.get("automated"):
                    out["automated"] = True
            return out

        out = []
        for t in tracks:
            devices = []
            for d in t["devices"]:
                if d["name"] == TAP:
                    continue
                on = next((p["value"] >= 0.5 for p in d["params"] if p["name"] == "Device On"), True)
                entry = {"index": d["index"], "name": d["name"], "class": d.get("class_name"), "on": on}
                if detail:
                    entry["params"] = [param(p) for p in d["params"] if p["name"] != "Device On"]
                devices.append(entry)
            out.append({"key": t["key"], "name": t["name"], "kind": t["kind"],
                        **({"group": t["group"]} if t.get("group") else {}),
                        **({"is_group": True} if t.get("is_group") else {}),
                        **({"muted": True} if t.get("muted") else {}),
                        "mixer": {p["name"]: p["display"] for p in t["mixer"]}, "devices": devices})
        note = None if detail else "Parameters left out: get_set(track=…) or parameters=true lists them."
        return {"tempo": state.get("tempo"), "tracks": out, **({"note": note} if note else {})}

    def set_parameters(self, changes: list[dict]) -> dict:
        if not changes:
            raise BridgeError("No changes given.")
        state = self._state()
        batch = []
        for c in changes:
            t = self._track(state, c.get("track"))
            device = c.get("device", "mixer")
            if isinstance(device, str) and device != "mixer":
                device = self._device_index(t, device)
            if device != "mixer" and any(d["index"] == device and d["name"] == TAP for d in t["devices"]):
                raise BridgeError("The MixMatch Tap is the measurement device; leave it as it is.")
            if "param" not in c or ("value" not in c and "target" not in c):
                raise BridgeError(f"Each change needs param and target (display units) or value (raw): {c}")
            item = {"track": t["key"], "device": device, "param": c["param"]}
            item["target" if "target" in c else "value"] = float(c["target"] if "target" in c else c["value"])
            batch.append(item)
        self._snapshot_once(state)
        result = self.live.batch(batch)
        applied = [{"track": b["track"], "device": b["device"], "param": b["param"], "now": r["display"]}
                   for b, r in zip(batch, result["results"]) if r]
        return {"applied": applied, "errors": result.get("errors", []),
                "undo": f"restore(name={self.started!r}) returns to the state before your first change."}

    def _device_index(self, track: dict, name: str) -> int:
        hits = [d["index"] for d in track["devices"] if d["name"].lower() == name.lower()]
        if len(hits) != 1:
            raise BridgeError(f"Device {name!r} on {track['name']}: {'not found' if not hits else 'ambiguous'}; "
                              "use its index from get_set.")
        return hits[0]

    def add_device(self, track: str, name: str, source: str = "audio_effects") -> dict:
        state = self._state()
        t = self._track(state, track)
        self._snapshot_once(state)
        from .live_client import LiveError
        try:
            index = self.live.load_device(t["key"], name, source=source)
        except LiveError as e:
            raise BridgeError(str(e))
        after = self._track(self._state(), t["key"])
        d = next(d for d in after["devices"] if d["index"] == index)
        return {"track": t["key"], "device": {"index": index, "name": d["name"],
                                               "params": [{"i": p["index"], "name": p["name"], "display": p["display"]}
                                                          for p in d["params"]]},
                "note": "Placed before the tap, so listen hears it."}

    def remove_device(self, track: str, device: int) -> dict:
        state = self._state()
        t = self._track(state, track)
        d = next((d for d in t["devices"] if d["index"] == device), None)
        if d is None:
            raise BridgeError(f"No device {device} on {t['name']}.")
        if d["name"] == TAP:
            raise BridgeError("The MixMatch Tap is the measurement device; leave it in place.")
        self._snapshot_once(state)
        self.live.delete_device(t["key"], device)
        return {"removed": d["name"], "track": t["name"],
                "note": "restore only resets parameters; a removed device is not loaded back."}

    def transport(self, play: bool | None = None, time_beats: float | None = None, loop_start: float | None = None,
                  loop_length: float | None = None, loop: bool | None = None) -> dict:
        kw = {k: v for k, v in (("play", play), ("time", time_beats), ("loop_start", loop_start),
                                ("loop_length", loop_length), ("loop", loop)) if v is not None}
        return self.live.transport(**kw)

    def listen(self, seconds: float | None = None, start_beat: float | None = None, bars: float | None = None,
               tracks_detail: bool = True) -> dict:
        from .agent import fader_gains, peak_percentile, post_fader
        from .analysis import compare, masking, masking_frames, stats
        from .taps import is_reference_name

        state = self._state()
        hub = self.hub
        tempo = state.get("tempo") or 120.0
        if bars is not None and seconds is None:
            seconds = bars * 4 * 60.0 / tempo
        seconds = min(MAX_LISTEN_S, max(2.0, float(seconds or 8.0)))
        woke = self._wake_taps(state)
        started_here = not state.get("is_playing")
        if start_beat is not None:
            length = bars * 4 if bars else seconds * tempo / 60.0
            self.live.transport(loop_start=float(start_beat), loop_length=length, loop=True,
                                time=float(start_beat), play=True)
        elif started_here:
            self.live.transport(play=True)
        self.env.wait(1.0 if woke or started_here else 0.2)   # taps and audio need a moment to settle
        start = self.env.now()
        self.env.wait(seconds)
        end = self.env.now()
        position = self.live.transport()
        if started_here:
            self.live.transport(play=False)

        master_tap = hub.latest("master")
        master = stats(master_tap.window(start, end)) if master_tap else None
        if master is None:
            raise BridgeError("Main stayed silent: is Live playing, is the audio engine on, and is there a tap on "
                              "Main (add_taps)? Muted output is fine, the taps measure before the fader.")
        in_tap = hub.latest("master_in")
        gains = fader_gains(state["tracks"])
        by_name = hub.by_name()
        measured, windows, silent, no_tap = [], {}, [], []
        for t in state["tracks"]:
            if t["kind"] == "master" or (t["kind"] == "track" and is_reference_name(t["name"])):
                continue
            tap = by_name.get((t["kind"], t["name"]))
            if tap is None:
                no_tap.append(t["name"])
                continue
            frames = tap.window(start, end)
            st = post_fader(stats(frames), gains.get(t["key"], 0.0))
            if st is None:
                silent.append(t["name"])
                continue
            windows[t["key"]] = (t, frames)
            entry = {"key": t["key"], "name": t["name"], "loudness_lufs": _r(st.loudness), "peak_dbfs": _r(st.peak_db),
                     "active": _r(st.active, 2), "width_db": _r(st.width_db), "low_side_db": _r(st.low_side_db)}
            if t.get("is_group"):
                entry["group_bus"] = True
            if tracks_detail:
                entry["tonal_balance_db"] = st.summary()["tonal_balance_db"]
            measured.append((st.loudness, entry))

        out = {
            "window": {"seconds": round(end - start, 1), "until_beat": _r(position.get("song_time"), 2)},
            "main": {**master.summary(), "peak99_before_limiter_dbfs": _r(peak_percentile(in_tap.window(start, end)))
                     if in_tap else None},
            "tracks": [e for _, e in sorted(measured, key=lambda x: -x[0])],
            "masking": self._masking(windows, gains, masking, masking_frames),
        }
        if silent:
            out["silent_tracks"] = silent
        if no_tap:
            out["tracks_without_tap"] = no_tap
        if self.target is not None:
            t = self.target
            cmp = compare(master, t.stats, t.loudness, t.tolerance, t.one_sided, offset=t.offset, anchor=t.anchor,
                          lump=t.lump_low)
            out["vs_references"] = {**cmp.summary(),
                                    "meaning": "positive = the mix has more than the references (dB, at equal "
                                               "loudness for tonal bands); largest_* is beyond their spread"}
        out["legend"] = ("loudness ≈ short-term LUFS; tonal_balance_db = band level relative to the track's "
                         "loudness; width_db = side vs mid; low_side_db = side below 150 Hz vs mid (≈ −20 or "
                         "lower is a mono low end); plr = peak to loudness; masking severity 0–1")
        return out

    def _masking(self, windows: dict, gains: dict, masking, masking_frames) -> list[dict]:
        keys = [k for k, (t, _) in windows.items() if not t.get("is_group") and t["kind"] == "track"
                and math.isfinite(gains.get(k, 0.0))]
        frames = {k: masking_frames(windows[k][1], gains.get(k, 0.0)) for k in keys}
        hits = []
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                for fg, bg in ((a, b), (b, a)):
                    hit = masking(frames[fg], frames[bg])
                    if hit and hit["severity"] >= 0.05:
                        hits.append({"masked": windows[fg][0]["name"], "by": windows[bg][0]["name"],
                                     "severity": round(hit["severity"], 2),
                                     "bands_hz": [x["freq_hz"] for x in hit["bands"]],
                                     "overlap": hit["overlap_fraction"]})
        hits.sort(key=lambda h: -h["severity"])
        return hits[:MASKING_LIMIT]

    def _wake_taps(self, state: dict) -> bool:
        from .cli import set_taps_on
        changed = set_taps_on(self.live, True)
        if not any(d["name"] == TAP for t in state["tracks"] for d in t["devices"]):
            raise BridgeError("No MixMatch Tap in this set: call add_taps first.")
        return changed > 0

    def add_taps(self) -> dict:
        from .cli import add_taps
        from .live_client import LiveError
        try:
            n = add_taps(self.live, log=_log)
        except LiveError as e:
            raise BridgeError(f"{e}. Install the MARA connection first (MARA app → Set up the connection).")
        return {"added": n, "note": "Every track, group, return and Main has a tap now."}

    def _files(self, paths: list[str] | None) -> list[str]:
        """Audio files from paths and folders; None means the clips on the set's «REF …» tracks."""
        from .cli import AUDIO

        if paths is None:
            clips = self.live.request("reference_clips").get("clips", [])
            files = [c["file"] for c in clips if c.get("file") and os.path.exists(c["file"])]
            if not files:
                raise BridgeError("No reference given and no audio clip on a track named «REF …» in the set. Ask the "
                                  "user for the file path (in Claude Code they can drag the file into the chat).")
            return files
        files = []
        for p in paths:
            p = os.path.expanduser(str(p).strip().strip("'\""))
            if os.path.isdir(p):
                files += sorted(os.path.join(p, f) for f in os.listdir(p) if f.lower().endswith(AUDIO))
            elif os.path.exists(p):
                files.append(p)
            else:
                raise BridgeError(f"Not found: {p}")
        return files

    def load_references(self, paths: list[str] | None = None, vocal_room: bool = False) -> dict:
        from .reference import ReferenceSet

        files = self._files(paths)
        if not files:
            self.target, self.target_info = None, None
            return {"references": None, "note": "Cleared."}
        refs = ReferenceSet(files, log=_log)
        self.target = refs.target(beat=vocal_room)
        self.target_info = {"files": [os.path.basename(f) for f in files], "paths": files, "vocal_room": vocal_room,
                            "how": self.target.description}
        return {**{k: v for k, v in self.target_info.items() if k != "paths"}, "target": {**self.target.stats.summary(), "loudness_lufs": _r(self.target.loudness)},
                "per_reference_lufs": {os.path.basename(r.path): _r(r.full.loudness) for r in refs.refs}}

    def analyze_audio(self, path: str | None = None, aspects: list[str] | None = None) -> dict:
        from . import character

        files = self._files([path] if path else None)
        out = {}
        for f in files:
            try:
                out[os.path.basename(f)] = character.only(character.describe(f), aspects)
            except ValueError as e:
                raise BridgeError(str(e))
        return {"analysis": out, "legend": CHARACTER_LEGEND}

    def record_and_compare(self, start_beat: float, bars: float = 8, reference: str | None = None,
                           aspects: list[str] | None = None, track: str | None = None) -> dict:
        """Records the passage from Main (or one track after its devices) and compares it with the reference."""
        from . import autoref, character

        if not 1 <= bars <= 64:
            raise BridgeError("bars: 1–64.")
        state = self._state()
        source = self._track(state, track)["key"] if track else None
        if source == "m":
            source = None
        if reference:
            ref_files = self._files([reference])
        elif self.target_info:
            ref_files = self._loaded_reference_files()
        else:
            try:
                ref_files = self._files(None)   # «REF …» tracks in the set
            except BridgeError:
                ref_files = []
        main = next(t for t in state["tracks"] if t["key"] == "m")
        volume = next((p for p in main["mixer"] if p["name"] == "Volume"), None)
        if source is None and volume is not None and volume["value"] <= 1e-6:
            raise BridgeError("Main is at -inf dB: the recording comes from behind the Main fader. Raise it first.")
        was_playing = state.get("is_playing")
        tempo = state.get("tempo") or 120.0
        beats = float(bars) * 4
        self.live.transport(loop_start=float(start_beat), loop_length=beats, loop=True)
        capture = autoref.capture_begin(self.live, beats, source, start=float(start_beat))
        path = autoref.capture_end(self.live, capture, timeout=beats * 60 / tempo + 20)
        if not was_playing:
            self.live.transport(play=False)
        if not path or autoref.silent_file(path):
            raise BridgeError("The recording is missing or silent. Is Live's audio engine on and the passage "
                              "playing something?")
        try:
            mix = character.describe(path)
        except ValueError as e:
            raise BridgeError(str(e))
        out = {"recorded": path, "source": track or "Main", "mix": character.only(mix, aspects)}
        if source is not None:
            out["note"] = ("A single track: its kick and spectrum describe that sound on its own; levels relative to "
                           "the mix and the bass block only mean something on Main.")
        for f in ref_files or []:
            ref = character.describe(f)
            out.setdefault("references", {})[os.path.basename(f)] = {
                "reference": character.only(ref, aspects),
                "mix_minus_reference": character.only(character.difference(mix, ref), aspects)}
        if not ref_files:
            out["note"] = (out.get("note", "") + " No reference: give `reference` or load_references.").strip()
        out["legend"] = CHARACTER_LEGEND
        return out

    def _loaded_reference_files(self) -> list[str]:
        return list(self.target_info.get("paths", [])) if self.target_info else []

    # ── snapshots ───────────────────────────────────────────────────────────
    def _dir(self, state: dict) -> Path:
        name = state.get("name") or Path(state.get("file_path") or "unsaved").stem or "unsaved"
        return self.state_dir / re.sub(r"[^\w .-]", "_", name)

    def _snapshot_once(self, state: dict) -> None:
        if self.started is None:
            self.started = self.snapshot(None, state)["name"]

    def snapshot(self, name: str | None = None, state: dict | None = None) -> dict:
        from . import ab
        state = state or self._state()
        name = re.sub(r"[^\w.-]", "_", name or time.strftime("%Y%m%d-%H%M%S"))
        folder = self._dir(state)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps(ab.snapshot(state)))
        return {"name": name}

    def restore(self, name: str | None = None) -> dict:
        from . import ab
        state = self._state()
        name = name or self.started
        if not name:
            raise BridgeError("Nothing to restore: no snapshot in this session. list_snapshots shows older ones.")
        path = self._dir(state) / f"{name}.json"
        if not path.exists():
            raise BridgeError(f"No snapshot {name!r}.")
        changes = ab.changes_to(json.loads(path.read_text()), state, bypass_new=True)
        result = self.live.batch(changes) if changes else {"errors": []}
        return {"restored": name, "parameters_changed": len(changes), "errors": result.get("errors", []),
                "note": "Devices added since then are switched off, not removed."}

    def list_snapshots(self) -> dict:
        folder = self._dir(self._state())
        names = sorted(p.stem for p in folder.glob("*.json")) if folder.exists() else []
        return {"snapshots": names, "this_session": self.started}

    def sleep_taps(self) -> None:
        """Taps compute 31 filters per channel; switch them off when the client goes away."""
        if self._live is None:
            return
        try:
            from .cli import set_taps_on
            set_taps_on(self._live, False)
        except Exception:
            pass


CHARACTER_LEGEND = (
    "loudness: integrated/loud_part LUFS, true peak dBTP, plr = true peak minus loud part (smaller = denser), "
    "loudness_range LU, short_crest_db = how far 10 ms peaks rise over the 400 ms body (smaller = more limited "
    "or clipped). tonal: band level relative to loudness in the loudest part. stereo: width_db side vs mid, "
    "low_side_db side below 150 Hz (−20 or lower = mono low end). kick (from the full mix by its hits in "
    "35–150 Hz): fundamental_hz, decay_ms_to_-10db, level_vs_mix_db (hit peak vs loudness), over_bass_db (hit "
    "vs what stays between hits: punch against the bass), spectrum_vs_low_db = what a hit adds per region "
    "relative to its own low end (click_2k_6k high = clicky, body_150_400 = knock), sub_vs_low_db. bass: what "
    "sits in 35–150 Hz between the hits, level and sub vs upper low end. mix_minus_reference: positive = the "
    "mix has more. Without stems, a bass louder than the kick hides hits.")

ASPECTS_SCHEMA = {"type": "array", "items": {"type": "string",
                                             "enum": ["loudness", "tonal", "stereo", "kick", "bass"]},
                  "description": "Only these parts (default all)"}

# ── MCP tool table ─────────────────────────────────────────────────────────
_TRACK = {"type": "string", "description": "Track key from get_set (t0, r0, m for Main) or the exact track name"}
_DEVICE = {"description": "\"mixer\" (Volume, Pan, Send A …) or the device index from get_set, or its unique name",
           "anyOf": [{"type": "integer"}, {"type": "string"}]}

TOOLS = [
    ("status", "Connection, tempo, transport, cue points, which tracks lack a measurement tap, loaded references.",
     {}),
    ("get_set", "Tracks with mixer and devices. Without `track` only device names; with `track` (or "
                "parameters=true) every parameter with its display value, raw value and range.",
     {"track": _TRACK, "parameters": {"type": "boolean", "default": False}}),
    ("listen", "Play and measure. Returns Main (loudness, peaks, dynamics, width, mono low end, tonal balance), "
               "every channel after its fader, the strongest masking between channels, and the distance to the "
               "references if loaded. Give start_beat (and bars) to loop a passage, so before/after compare the same "
               "music; otherwise it measures wherever Live is playing.",
     {"seconds": {"type": "number", "description": "2–60, default 8 (or the length of `bars`)"},
      "start_beat": {"type": "number", "description": "Song position in beats (cue points from status)"},
      "bars": {"type": "number", "description": "Loop length in bars (4 beats each)"},
      "tracks_detail": {"type": "boolean", "default": True, "description": "Tonal balance per channel"}}),
    ("set_parameters", "Change parameters in one batch. Prefer `target` in display units (dB, Hz, %, e.g. "
                       "Volume target -3.5); `value` is the raw value. The first change of a session saves a "
                       "snapshot for restore.",
     {"changes": {"type": "array", "items": {"type": "object", "properties": {
         "track": _TRACK, "device": _DEVICE,
         "param": {"description": "Parameter name or index", "anyOf": [{"type": "string"}, {"type": "integer"}]},
         "target": {"type": "number"}, "value": {"type": "number"}}, "required": ["track", "param"]}}}),
    ("add_device", "Load a device at the end of a track's chain (before the tap). source: audio_effects (EQ Eight, "
                   "Compressor, Utility, Glue Compressor, Saturator …), plugins (installed VST3/AU by name) or "
                   "user_library.",
     {"track": _TRACK, "name": {"type": "string"},
      "source": {"type": "string", "enum": ["audio_effects", "plugins", "user_library"], "default": "audio_effects"}}),
    ("remove_device", "Delete a device from a track (not the tap).",
     {"track": _TRACK, "device": {"type": "integer"}}),
    ("transport", "Play/stop, jump (time_beats) and set the loop.",
     {"play": {"type": "boolean"}, "time_beats": {"type": "number"}, "loop_start": {"type": "number"},
      "loop_length": {"type": "number", "description": "beats"}, "loop": {"type": "boolean"}}),
    ("load_references", "Make finished songs (files or folders: WAV, AIFF, FLAC, MP3) the target of `listen`. "
                        "Without paths: the clips on «REF …» tracks in the set. vocal_room=true for a beat that still "
                        "gets vocals: 200 Hz–5 kHz may sit under the references. An empty list clears them.",
     {"paths": {"type": "array", "items": {"type": "string"}}, "vocal_room": {"type": "boolean", "default": False}}),
    ("analyze_audio", "Describe a finished song from its audio file: loudness and density, tonal balance, stereo, "
                      "and the kick and bass as heard in the mix. Without path: the clips on «REF …» tracks.",
     {"path": {"type": "string", "description": "Audio file on this computer"}, "aspects": ASPECTS_SCHEMA}),
    ("record_and_compare", "Record a passage of Main in Live (it plays, audibly; Main must not be at -inf) and "
                           "analyse it like analyze_audio, with mix minus reference. For «make it loud like this», "
                           "«mix it like this», «make the kick like this». Record the same passage again after "
                           "changes. `track` records one track after its devices instead of Main.",
     {"start_beat": {"type": "number"}, "bars": {"type": "number", "default": 8},
      "reference": {"type": "string", "description": "Audio file; default: loaded references or «REF …» tracks"},
      "aspects": ASPECTS_SCHEMA, "track": _TRACK}),
    ("add_taps", "Put the MixMatch Tap measurement device on every track, group, return and Main that lacks one.",
     {}),
    ("snapshot", "Save every parameter of the set under a name.", {"name": {"type": "string"}}),
    ("restore", "Set every parameter back to a snapshot (default: the one before your first change).",
     {"name": {"type": "string"}}),
    ("list_snapshots", "Saved snapshots of this set.", {}),
]

_REQUIRED = {"set_parameters": ["changes"], "add_device": ["track", "name"], "remove_device": ["track", "device"],
             "record_and_compare": ["start_beat"]}


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": _REQUIRED.get(n, [])}}
            for n, d, props in TOOLS]


class Server:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.names = {n for n, _, _ in TOOLS}

    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:            # a notification: never answered
            return None
        try:
            if method == "initialize":
                asked = (msg.get("params") or {}).get("protocolVersion")
                from . import __version__ as version
                result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "mara-bridge", "title": "MARA Bridge", "version": version},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_list()}
            elif method == "tools/call":
                result = self.call((msg.get("params") or {}).get("name"), (msg.get("params") or {}).get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}}
        except Exception as e:   # protocol-level failure
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def call(self, name: str, args: dict) -> dict:
        if name not in self.names:
            return _text({"error": f"Unknown tool {name}"}, error=True)
        try:
            return _text(getattr(self.bridge, name)(**args))
        except BridgeError as e:
            return _text({"error": str(e)}, error=True)
        except TypeError as e:
            return _text({"error": f"Bad arguments: {e}"}, error=True)
        except Exception as e:
            _log(traceback.format_exc())
            return _text({"error": f"{type(e).__name__}: {e}"}, error=True)


def _text(data, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": error}


def serve(stdin=None, stdout=None, bridge: Bridge | None = None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    # Stdout belongs to the protocol: a stray print anywhere in MARA would corrupt it, so prints go to stderr.
    real_stdout, sys.stdout = sys.stdout, sys.stderr
    server = Server(bridge or Bridge())
    _log("MARA Bridge ready (MCP over stdio)")
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                reply = server.handle(msg) if isinstance(msg, dict) else None
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()
    finally:
        server.bridge.sleep_taps()
        sys.stdout = real_stdout


def main() -> None:
    """Entry point of `mara-bridge`."""
    from .cli import load_env
    from .credentials import load as load_credentials

    load_env()
    load_credentials()
    serve()
