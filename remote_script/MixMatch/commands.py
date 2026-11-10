"""Commands of the MixMatch protocol, independent of Live's ControlSurface.

`Commands` works on any object that looks like Live's `Song` (the real one
inside Live, a fake one in the tests). Every command takes a dict and returns
a JSON-serialisable result or raises `CommandError`.

Parameters are addressed as (track, device, param):
  track   "t<n>" (normal track), "r<n>" (return track) or "m" (master)
  device  device index on that track, or "mixer" (volume, pan, send A…)
  param   parameter name (exact, case-insensitive) or index

Values can be set raw (`value`, Live's internal range) or as `target` in the
units Live displays (dB, Hz, %): the script then searches the raw value whose
display matches, so the app never needs to know each device's internal scale.
"""
import math
import re


class CommandError(Exception):
    pass


_NUMBER = re.compile(r"(-?inf|-?\d+(?:\.\d+)?)\s*(k)?", re.IGNORECASE)


def parse_display(text):
    """«-3.0 dB» → -3.0, «1.20 kHz» → 1200, «-inf dB» → -inf, «50L» → -50, «C» → 0."""
    text = str(text).strip()
    if text.upper() == "C":
        return 0.0
    pan = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([LR])", text, re.IGNORECASE)
    if pan:
        return float(pan.group(1)) * (-1 if pan.group(2).upper() == "L" else 1)
    m = _NUMBER.search(text)
    if not m:
        return None
    if m.group(1).lower().endswith("inf"):
        return -math.inf if m.group(1).startswith("-") else math.inf
    return float(m.group(1)) * (1000.0 if m.group(2) else 1.0)


def _display(param, value=None):
    try:
        return param.str_for_value(param.value if value is None else value)
    except Exception:
        return str(param.value if value is None else value)


TAP = "MixMatch Tap"
DUCKER = "MARA Duck"


def _routing_name(route):
    return str(route.get("display_name", "")) if isinstance(route, dict) else str(getattr(route, "display_name", ""))


def _routing_options(device, prop):
    value = getattr(device, prop)
    return list(value.get(prop, [])) if isinstance(value, dict) else list(value)


def _stem(name):
    """Browser item name without a device file extension, lower case: «MixMatch Tap.amxd» → «mixmatch tap»."""
    name = name.lower()
    for ext in (".amxd", ".adv", ".adg", ".vst3", ".component", ".vst"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _param_info(index, p):
    return {
        "index": index,
        "name": p.name,
        "value": p.value,
        "min": p.min,
        "max": p.max,
        "display": _display(p),
        "quantized": bool(getattr(p, "is_quantized", False)),
        "automated": int(getattr(p, "automation_state", 0)) != 0,  # 1 playing, 2 overridden by a manual change
        "items": [str(v) for v in getattr(p, "value_items", [])] if getattr(p, "is_quantized", False) else [],
    }


class Commands:
    def __init__(self, song, browser=None, log=None):
        self.song = song
        self.browser = browser
        self.log = log or (lambda *_: None)

    # ── addressing ──────────────────────────────────────────────────────────
    def _tracks(self):
        out = [("t%d" % i, "track", t) for i, t in enumerate(self.song.tracks)]
        out += [("r%d" % i, "return", t) for i, t in enumerate(self.song.return_tracks)]
        out.append(("m", "master", self.song.master_track))
        return out

    def _track(self, key):
        for k, _, t in self._tracks():
            if k == key:
                return t
        raise CommandError("unknown track %r" % key)

    def _mixer_params(self, track):
        mixer = track.mixer_device
        params = [("Volume", mixer.volume)]
        if hasattr(mixer, "panning"):
            params.append(("Pan", mixer.panning))
        for i, s in enumerate(getattr(mixer, "sends", []) or []):
            params.append(("Send %s" % chr(65 + i), s))
        return params

    def _param(self, track, device, param):
        if device == "mixer":
            params = self._mixer_params(track)
            if isinstance(param, int):
                return params[param][1]
            for name, p in params:
                if name.lower() == str(param).lower():
                    return p
            raise CommandError("unknown mixer parameter %r" % param)
        devices = list(track.devices)
        if not isinstance(device, int) or not 0 <= device < len(devices):
            raise CommandError("unknown device %r" % device)
        params = list(devices[device].parameters)
        if isinstance(param, int):
            if not 0 <= param < len(params):
                raise CommandError("unknown parameter %r" % param)
            return params[param]
        for p in params:
            if p.name.lower() == str(param).lower():
                return p
        raise CommandError("device %r has no parameter %r" % (devices[device].name, param))

    # ── commands ────────────────────────────────────────────────────────────
    def ping(self, _):
        return {"pong": True}

    def role_context(self, _):
        """Read instrument identities inside racks once per run, without copying parameters.

        Pad.note is the rack's actual mapping; MIDI note 36 is not assumed to be a
        kick. Only populated pads are returned, with mute/solo and nested devices.
        See https://docs.cycling74.com/apiref/lom/rackdevice/ and /drumpad/.
        """
        def device_info(d, depth, budget):
            if depth > 6 or budget[0] <= 0:
                return None
            budget[0] -= 1
            params = list(getattr(d, "parameters", []))
            out = {"name": d.name, "class_name": getattr(d, "class_name", ""),
                   "type": int(getattr(d, "type", 0)),
                   "enabled": next((p.value >= 0.5 for p in params if p.name == "Device On"), True)}
            try:
                sample = getattr(d, "sample", None)
                path = getattr(sample, "file_path", "") if sample is not None else ""
                if path:
                    out["sample_name"] = str(path).replace("\\", "/").rsplit("/", 1)[-1]
            except Exception:
                pass  # not every instrument exposes a sample

            def chain_info(c):
                children = [device_info(child, depth + 1, budget) for child in c.devices]
                return {"name": c.name, "muted": bool(getattr(c, "mute", False)),
                        "solo": bool(getattr(c, "solo", False)), "devices": [x for x in children if x]}

            pads = list(getattr(d, "drum_pads", [])) if getattr(d, "can_have_drum_pads", False) else []
            if pads:
                out["drum_pads"] = [{"name": p.name, "note": int(p.note), "muted": bool(p.mute),
                                     "solo": bool(p.solo), "chains": [chain_info(c) for c in p.chains]}
                                    for p in pads if len(p.chains)]
            elif getattr(d, "can_have_chains", False):
                out["chains"] = [chain_info(c) for c in d.chains]
            return out

        tracks = {}
        for key, kind, track in self._tracks():
            if kind == "master":
                continue
            budget = [512]
            devices = [device_info(d, 0, budget) for d in track.devices]
            counts = None
            if getattr(track, "has_midi_input", False):
                try:
                    counts = {}
                    clips = list(getattr(track, "arrangement_clips", []))
                    clips += [slot.clip for slot in getattr(track, "clip_slots", []) if slot.has_clip
                              and getattr(slot.clip, "is_playing", False)]
                    for clip in clips:
                        if not clip.is_midi_clip or getattr(clip, "muted", False):
                            continue
                        for note in clip.get_notes_extended(0, 128, clip.start_marker, clip.end_marker - clip.start_marker):
                            if not getattr(note, "mute", False) and note.velocity > 0:
                                pitch = str(int(note.pitch))
                                counts[pitch] = counts.get(pitch, 0) + 1
                except Exception:
                    counts = None  # unavailable is different from a part with no notes
            tracks[key] = {"devices": [d for d in devices if d], "played_notes": counts}
        return {"tracks": tracks}

    def get_set(self, _):
        tracks = []
        for key, kind, t in self._tracks():
            group = None
            if kind == "track" and getattr(t, "is_grouped", False):
                group = getattr(t.group_track, "name", None)
            tracks.append({
                "key": key,
                "kind": kind,
                "name": t.name,
                "is_group": bool(getattr(t, "is_foldable", False)),
                "group": group,
                "muted": bool(getattr(t, "mute", False)) if kind != "master" else False,
                "output": getattr(getattr(t, "output_routing_type", None), "display_name", None),
                "mixer": [dict(_param_info(i, p), name=n) for i, (n, p) in enumerate(self._mixer_params(t))],
                "devices": [
                    {
                        "index": i,
                        "name": d.name,
                        "class_name": getattr(d, "class_name", d.name),
                        "type": int(getattr(d, "type", 0)),  # 1 instrument, 2 audio effect, 4 MIDI effect
                        "params": [_param_info(j, p) for j, p in enumerate(d.parameters)],
                    }
                    for i, d in enumerate(t.devices)
                ],
            })
        return {
            "capabilities": {"sidechain_ducking": True},
            "name": getattr(self.song, "name", "") or "",
            "file_path": getattr(self.song, "file_path", "") or "",
            "tempo": self.song.tempo,
            "is_playing": self.song.is_playing,
            "song_time": self.song.current_song_time,
            "loop": {"on": self.song.loop, "start": self.song.loop_start, "length": self.song.loop_length},
            "cue_points": sorted(
                [{"name": c.name, "time": c.time} for c in getattr(self.song, "cue_points", [])],
                key=lambda c: c["time"],
            ),
            "tracks": tracks,
        }

    def set(self, req):
        p = self._param(self._track(req["track"]), req["device"], req["param"])
        if "target" in req:
            value = self._search(p, float(req["target"]))
        else:
            value = float(req["value"])
        value = max(p.min, min(p.max, value))
        if getattr(p, "is_quantized", False):
            value = round(value)
        p.value = value
        return {"value": p.value, "display": _display(p)}

    def batch(self, req):
        results, errors = [], []
        for change in req.get("changes", []):
            try:
                results.append(self.set(change))
            except Exception as e:  # one bad change must not stop the others
                errors.append({"change": change, "error": str(e)})
                results.append(None)
        return {"results": results, "errors": errors}

    def get(self, req):
        p = self._param(self._track(req["track"]), req["device"], req["param"])
        return {"value": p.value, "display": _display(p), "min": p.min, "max": p.max}

    def load_device(self, req):
        """Load a device into a track's chain: at the end, but before a «MixMatch Tap» so the tap keeps measuring last.

        `source` is «audio_effects» (built-in devices such as «EQ Eight», the
        default), «user_library» (e.g. the «MixMatch Tap» device itself) or
        «plugins» (installed VST3/AU plugins such as «Pro-L 2»).
        """
        if self.browser is None:
            raise CommandError("browser not available")
        source = req.get("source", "audio_effects")
        if source not in ("audio_effects", "user_library", "plugins"):
            raise CommandError("unknown source %r" % source)
        track = self._track(req["track"])
        item = self._find_item(getattr(self.browser, source), req["name"].lower(), 6)
        if item is None:
            raise CommandError("device %r not found in the browser" % req["name"])
        devices = list(track.devices)
        taps = [i for i, d in enumerate(devices) if d.name == TAP]
        at = taps[0] if taps and _stem(req["name"]) != TAP.lower() else len(devices)
        self.song.view.selected_track = track
        self.browser.load_item(item)  # Live appends at the end of the chain of the selected track
        if at < len(devices):
            new = list(track.devices)[-1]
            self.song.move_device(new, track, at)
        return {"index": at}

    def create_ducker(self, req):
        """Create one bounded parallel sidechain Compressor, or leave the set unchanged.

        Routing follows CompressorDevice's input_routing_type/channel API:
        https://docs.cycling74.com/apiref/lom/compressordevice/
        Only a newly created device is configured. Existing compressors are never
        repurposed, and failures remove the new device before returning an error.
        """
        key, source_key = req["track"], req["source_track"]
        if key == source_key or not key.startswith("t") or not source_key.startswith("t"):
            raise CommandError("ducking requires two different normal tracks")
        track, source = self._track(key), self._track(source_key)
        if any(getattr(t, "is_foldable", False) or getattr(t, "mute", False) or getattr(t, "is_frozen", False)
               for t in (track, source)):
            raise CommandError("cannot duck a group, muted or frozen track")
        source_matches = [t for _, _, t in self._tracks() if t.name == source.name]
        if len(source_matches) != 1:
            raise CommandError("sidechain source name is ambiguous")
        for d in track.devices:
            if d.name.startswith(DUCKER):
                raise CommandError("a MARA ducker already exists; do not stack ducking")
            if getattr(d, "class_name", "") in ("Compressor2", "GlueCompressor"):
                params = {p.name: p.value for p in d.parameters}
                if params.get("Device On", 1) < 0.5 or params.get("S/C On", 0) >= 0.5:
                    raise CommandError("existing disabled or sidechain compressor is left alone")
        level = float(req["source_level_dbfs"])
        depth = float(req.get("reduction_db", 2.0))
        release = float(req.get("release_ms", 120.0))
        if not all(math.isfinite(v) for v in (level, depth, release)) or not -70 <= level <= 6:
            raise CommandError("invalid measured source level or ducking settings")
        depth, release = max(0.5, min(3.0, depth)), max(50.0, min(250.0, release))
        before = list(track.devices)
        device = None
        try:
            at = self.load_device({"track": key, "name": "Compressor"})["index"]
            device = list(track.devices)[at]
            if getattr(device, "class_name", "") != "Compressor2":
                raise CommandError("Live did not load its native Compressor")
            params = {p.name: p for p in device.parameters}
            raw = {"Device On": 0, "S/C On": 1, "S/C Listen": 0, "S/C EQ On": 0,
                   "Makeup": 0, "Auto Release On/Off": 0}
            targets = {"Threshold": max(-60.0, min(0.0, level - 12.0)), "Ratio": 4.0,
                       "Attack": 2.0, "Release": release, "Output": 0.0, "S/C Gain": 0.0,
                       "S/C Mix": 100.0, "Dry/Wet": (1 - 10 ** (-depth / 20)) * 100}
            if any(name not in params for name in (*raw, *targets, "Model")):
                raise CommandError("Compressor is missing required sidechain controls")
            params["Device On"].value = 0
            routes = [r for r in _routing_options(device, "available_input_routing_types")
                      if _routing_name(r) == source.name]
            if len(routes) != 1:
                raise CommandError("sidechain source is unavailable or ambiguous")
            device.input_routing_type = routes[0]
            channels = [r for r in _routing_options(device, "available_input_routing_channels")
                        if _routing_name(r).lower() == "post fx"]
            if len(channels) != 1:
                raise CommandError("Post FX sidechain input is unavailable")
            device.input_routing_channel = channels[0]
            model = params["Model"]
            items = list(getattr(model, "value_items", []))
            if "RMS" not in items:
                raise CommandError("Compressor RMS detector is unavailable")
            model.value = model.min + items.index("RMS")
            for name, value in raw.items():
                params[name].value = value
            for name, target in targets.items():
                p = params[name]
                value = self._search(p, target)
                p.value = max(p.min, min(p.max, value))
                actual = parse_display(_display(p))
                if actual is None or abs(actual - target) > max(0.1, abs(target) * 0.03):
                    raise CommandError("could not set Compressor %s" % name)
            # Dry signal bounds the attenuation even if the trigger exceeds its measured level.
            # Round down the wet control if display rounding would exceed that bound.
            wet = params["Dry/Wet"]
            while parse_display(_display(wet)) > targets["Dry/Wet"] + 0.01:
                wet.value = max(wet.min, wet.value - (wet.max - wet.min) / 1000)
            device.name = "%s ← %s" % (DUCKER, source.name)
            params["Device On"].value = 1
            return {"index": at, "name": device.name}
        except Exception:
            # Identity matters: never delete an existing device after a failed browser load.
            for i in reversed(range(len(track.devices))):
                d = track.devices[i]
                if d not in before:
                    track.delete_device(i)
            raise

    def browser_tree(self, req):
        """Diagnostics: a part of Live's browser with the flags of each item."""
        root = getattr(self.browser, req.get("source", "plugins"), None)

        def node(item, depth):
            out = {"name": item.name, "loadable": bool(getattr(item, "is_loadable", False)),
                   "device": bool(getattr(item, "is_device", False)), "folder": bool(getattr(item, "is_folder", False))}
            kids = list(getattr(item, "children", []))
            if kids and depth > 0:
                out["children"] = [node(c, depth - 1) for c in kids[: req.get("limit", 20)]]
            elif kids:
                out["n_children"] = len(kids)
            return out

        return node(root, int(req.get("depth", 2))) if root is not None else None

    def list_plugins(self, _):
        """Names of the installed plugins in Live's browser, without duplicates (VST3 before AU before VST)."""
        root = getattr(self.browser, "plugins", None)
        names, seen = [], set()

        def walk(item, depth):
            for child in getattr(item, "children", []):
                if getattr(child, "is_loadable", False) and not getattr(child, "is_folder", False):
                    key = _stem(child.name)
                    if key not in seen:
                        seen.add(key)
                        names.append(child.name)
                elif depth > 0:
                    walk(child, depth - 1)

        if root is not None:
            order = {"vst3": 0, "audio units": 1, "au": 1, "vst": 2}
            for fmt in sorted(getattr(root, "children", []), key=lambda c: order.get(c.name.lower(), 3)):
                walk(fmt, 4)
        return {"plugins": names}

    def move_device(self, req):
        """Move the device at index `device` of a track to index `to` in the same chain."""
        track = self._track(req["track"])
        devices = list(track.devices)
        if not isinstance(req["device"], int) or not 0 <= req["device"] < len(devices):
            raise CommandError("unknown device %r" % req["device"])
        self.song.move_device(devices[req["device"]], track, int(req["to"]))
        return {"devices": [d.name for d in track.devices]}

    # ── reference tracks inside the project ─────────────────────────────────
    def reference_clips(self, req):
        """Audio files of the clips on tracks named «ref»/«reference» (arrangement and session), muted or not."""
        import re

        out = []
        for key, kind, track in self._tracks():
            if kind != "track" or not re.search(r"(^|[^a-z])ref(erence)?([^a-z]|$)", track.name.strip().lower()):
                continue
            clips = list(getattr(track, "arrangement_clips", []))
            clips += [slot.clip for slot in getattr(track, "clip_slots", []) if getattr(slot, "has_clip", False)]
            for clip in clips:
                path = getattr(clip, "file_path", None) if getattr(clip, "is_audio_clip", False) else None
                if path and path not in [o["file"] for o in out]:
                    out.append({"track": track.name, "key": key, "file": path})
        return {"clips": out}

    # ── harmony: MIDI notes of the arrangement ──────────────────────────────
    def midi_notes(self, req):
        """Unmuted notes of the arrangement MIDI clips of every track in [start, end) beats; drum racks left out."""
        start, end = float(req.get("start", 0.0)), float(req.get("end", 1e9))
        out = []
        for key, kind, track in self._tracks():
            if kind != "track" or getattr(track, "is_foldable", False) or not getattr(track, "has_midi_input", False):
                continue
            if any(getattr(d, "can_have_drum_pads", False) for d in track.devices):
                continue
            for clip in getattr(track, "arrangement_clips", []):
                if not clip.is_midi_clip or clip.end_time <= start or clip.start_time >= end:
                    continue
                for n in clip.get_notes_extended(0, 128, clip.start_marker, clip.end_marker - clip.start_marker):
                    if getattr(n, "mute", False):
                        continue
                    t = clip.start_time + (n.start_time - clip.start_marker)
                    if start <= t < end and t < clip.end_time:
                        out.append([key, n.pitch, round(t, 4), round(n.duration, 4), n.velocity])
        return {"notes": out}

    # ── capturing the master (for listening with CLAP) ──────────────────────
    def preview_output(self, req):
        """Temporarily disconnect hardware output without muting the recorded signal."""
        master = self.song.master_track
        if req.get("muted"):
            if not hasattr(self, "_preview_output"):
                route = next((r for r in getattr(master, "available_output_routing_types", [])
                              if r.display_name == "Sends Only"), None)
                if route is None:
                    raise CommandError("silent preview recording is not available on this output")
                original = (master.output_routing_type, master.output_routing_channel)
                master.output_routing_type = route
                self._preview_output = original
        elif hasattr(self, "_preview_output"):
            master.output_routing_type, master.output_routing_channel = self._preview_output
            del self._preview_output
        return {"muted": hasattr(self, "_preview_output")}

    def capture_start(self, req):
        """New audio track recording the master («Resampling») or, with `source`, one track after its devices,
        into its first clip slot; monitoring off."""
        song = self.song
        source = self._track(req["source"]) if req.get("source") else None
        song.create_audio_track(-1)
        track = list(song.tracks)[-1]
        track.name = "MixMatch Capture"
        inputs = list(getattr(track, "available_input_routing_types", []))
        want = source.name.lower() if source is not None else "resampling"
        routing = next((r for r in inputs if r.display_name.lower() == want), None)
        if routing is None:
            song.delete_track(len(list(song.tracks)) - 1)
            raise CommandError("no input «%s» on the new track" % want)
        track.input_routing_type = routing
        if source is not None:
            channels = list(getattr(track, "available_input_routing_channels", []))
            post = next((c for c in channels if "post fx" in c.display_name.lower()), None)
            if post is not None:
                track.input_routing_channel = post
        track.current_monitoring_state = 2  # Off: hearing the master on it again would feed back
        track.arm = True
        slot = list(track.clip_slots)[0]
        slot.fire(record_length=float(req.get("beats", 64)))
        return {"track": len(list(song.tracks)) - 1}

    def capture_status(self, req):
        track = list(self.song.tracks)[int(req["track"])]
        slot = list(track.clip_slots)[0]
        clip = slot.clip if slot.has_clip else None
        return {"recording": bool(getattr(slot, "is_recording", False)),
                "file": getattr(clip, "file_path", None) if clip is not None else None}

    def capture_cleanup(self, req):
        """Delete the capture track; the recorded file stays in the project's Samples/Recorded folder.

        Never deletes a track that is still recording (that can crash Live): it stops the recording first and
        answers {"pending": true}; the caller asks again a moment later."""
        tracks = list(self.song.tracks)
        index = int(req["track"])
        if 0 <= index < len(tracks) and tracks[index].name == "MixMatch Capture":
            track = tracks[index]
            slot = list(track.clip_slots)[0]
            if getattr(slot, "is_recording", False) or getattr(slot, "is_triggered", False):
                track.arm = False
                slot.stop()
                return {"pending": True}
            track.arm = False
            self.song.delete_track(index)
        self.song.back_to_arranger = False  # the session clip had taken over this track only; play the arrangement
        return {"tracks": len(list(self.song.tracks))}

    def delete_device(self, req):
        """Remove the device at `device` (index) from a track's chain."""
        track = self._track(req["track"])
        index = req["device"]
        if not isinstance(index, int) or not 0 <= index < len(list(track.devices)):
            raise CommandError("unknown device %r" % index)
        track.delete_device(index)
        return {"devices": len(list(track.devices))}

    def re_enable_automation(self, _):
        """Let automation play again after a parameter was changed by hand (Live's «Re-Enable Automation»)."""
        self.song.re_enable_automation()
        return {"ok": True}

    def transport(self, req):
        # Live refuses a loop that reaches past the end of the song, also for a moment in between:
        # shorten first, then move the start, then set the final length.
        if "loop_length" in req:
            self.song.loop_length = min(self.song.loop_length, float(req["loop_length"]))
        if "loop_start" in req:
            self.song.loop_start = float(req["loop_start"])
        if "loop_length" in req:
            self.song.loop_length = float(req["loop_length"])
        if "loop" in req:
            self.song.loop = bool(req["loop"])
        # Live resumes where it last stopped, whatever current_song_time says while stopped,
        # and start_playing() begins at the start marker. So: start first, then jump.
        if req.get("play") is True and not self.song.is_playing:
            self.song.continue_playing()
        elif req.get("play") is False:
            self.song.stop_playing()
        if "time" in req:
            self.song.current_song_time = float(req["time"])
        return {"is_playing": self.song.is_playing, "song_time": self.song.current_song_time}

    # ── helpers ─────────────────────────────────────────────────────────────
    def _search(self, p, target):
        """Raw value whose display is closest to `target`, assuming the display rises with the value."""
        lo, hi = p.min, p.max
        f_lo, f_hi = parse_display(_display(p, lo)), parse_display(_display(p, hi))
        if f_lo is None or f_hi is None:
            raise CommandError("cannot read the display of %r" % p.name)
        if target <= f_lo:
            return lo
        if target >= f_hi:
            return hi
        for _ in range(48):
            mid = (lo + hi) / 2.0
            f = parse_display(_display(p, mid))
            if f is None:
                break
            if f < target:
                lo = mid
            else:
                hi = mid
        return hi  # the smallest value whose display reaches the target, so «-1.5 dB» shows as -1.5

    def _find_item(self, parent, name, depth):
        for child in getattr(parent, "children", []):
            if _stem(child.name) == name and getattr(child, "is_loadable", True):
                return child
        if depth > 0:
            for child in getattr(parent, "children", []):
                found = self._find_item(child, name, depth - 1)
                if found is not None:
                    return found
        return None

    def run(self, req):
        cmd = req.get("cmd")
        fn = getattr(self, cmd, None) if isinstance(cmd, str) and not cmd.startswith("_") and cmd != "run" else None
        if fn is None:
            raise CommandError("unknown command %r" % cmd)
        return fn(req)
