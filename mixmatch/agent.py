"""The mixing loop: listen, compare, decide, adjust, check, for as long as it helps.

1. Understand the set: tracks, devices, which tap belongs to which track,
   references, roles of all channels.
2. For each section (between cue points, or the current loop): loop it, measure
   a few bars, let the planner decide 1–4 small steps, apply them, wait for the
   sound to settle, measure again. A round that makes the distance to the
   target worse is undone and remembered, so it is not tried again.
3. Stop when the targets are met, nothing helps any more, the time is up or
   the user stops it. Every round is logged to runs/<time>.jsonl.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import time
from dataclasses import dataclass, field, replace

import numpy as np

from . import roles as roles_mod
from .actions import CLIPPER_MAX_GAIN_DB, CLIPPERS, LIMITER_MAX_GAIN_DB, LIMITERS, Executor, RoundResult, neutral_device
from .analysis import TONAL_BANDS, Comparison, compare, masking, objective, objective_parts, stats
from .bands import CENTRES
from .taps import is_reference_name
from .display import parse_display
from .planner import device_summary

VOCAL_ROLES = {"lead_vocal", "backing_vocal", "vocal_fx"}


class RealEnv:
    """Wall clock; the song plays in Live."""

    def now(self) -> float:
        return time.time()

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class Settings:
    minutes: float | None = 10.0  # upper limit; None = until the run is complete
    bars: int = 8                 # measurement window per round, from the loop start (shorter loops: all of it)
    settle: float = 1.5           # seconds after a change before measuring (reverb tails, compressor release)
    max_rounds_per_section: int = 12
    stall_rounds: int = 4         # stop a section after this many rounds without improvement
    min_gain: float = 0.05        # a round must improve the objective by at least this much
    assist: bool = False          # ask before applying
    mode: str = "auto"            # beat (no vocals) | song | auto (beat if no vocal channel is found)
    loudness_from: str = "full"   # kept for old callers; the target always comes from the full references
    roles_file: str = "roles.json"
    log_dir: str = "runs"
    focus: str = "sections"       # sections: work through cue points / loop | loud: only the loudest part
    focus_bars: int = 16          # length of the loudest part
    complete: bool = False        # after the master: one pass over every channel, then a final master check
    channel_bars: int = 8         # loop length when a channel is worked on outside the loudest part
    channel_rounds: int = 2       # rounds per channel in the channel pass
    final_rounds: int = 4         # rounds of the final master check
    resume: str | None = None     # track name: skip the master phase and start the channel pass there
    silent: bool = False          # Main fader down while running (the taps measure before it), put back at the end
    auto_ref: bool = False        # find references by listening to the project (library and Jamendo)
    library: str | None = "~/Music"
    jamendo: bool = False         # also try Creative Commons tracks from Jamendo
    audit: bool = True            # complete runs: remove effects that do nothing, bypass ones that harm the mix
    suggest: bool = True          # complete runs: write references to get and free ones (runs/…-buy.md)
    fine: bool = True             # record the master during its first measurement: narrow resonances
    fine_channels: bool = False   # also record single channels (routing a capture track to a channel once crashed Live)
    audit_bars: int = 4           # A/B window per effect


@dataclass
class Scan:
    """The range played once: every frame of the master and of each channel, with its song position."""

    start: float                  # beat where the range starts
    total: float                  # length in beats
    master: list                  # (beat offset, frame)
    channels: dict                # track key → [(beat offset, frame)]


@dataclass
class Round:
    section: str
    n: int
    before: float
    after: float | None
    plan: dict
    applied: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    undone: bool = False


TAP = "MixMatch Tap"


class PlannerFailed(Exception):
    pass


class MasterSilent(Exception):
    """The master tap reports nothing: Live is not playing, exporting, or its audio engine is off."""


class Agent:
    def __init__(self, live, hub, planner, settings: Settings | None = None, env=None, log=print, ask=input,
                 references=None):
        self.live, self.hub, self.planner = live, hub, planner
        self.s = settings or Settings()
        self.regions: dict = {}       # track key → (start beat, length) where the channel pass works on it
        self.env = env or RealEnv()
        self.log, self.ask = log, ask
        self.ex = Executor(live, log)
        self.history: list[Round] = []
        self.roles: dict[str, roles_mod.Role] = {}
        self.tap_of: dict[str, object] = {}       # track key → tap
        self.references: list = []          # reference taps inside the project
        self.refset = references            # ReferenceSet from audio files (preferred)
        self.target = None
        self.target_no_vocals = None        # used in parts where the vocal channels are silent
        self.loop: tuple[float, float] | None = None   # (start, length) of the loop the agent plays
        self.harmony: dict | None = None                # key and chords, from Live's MIDI clips
        self.resonances: dict[str, list] = {}           # track key → narrow resonances (not notes of the key)
        from . import project

        self.description = project.description()        # the user's own words for the track
        self.beat = False
        self.logfile = None

    # ── 1. understand the set ───────────────────────────────────────────────
    def window_seconds(self) -> float:
        tempo = self.ex.set_state.get("tempo") or 120
        beats = self.s.bars * 4
        if self.loop is not None:  # aligned to the loop: the whole loop, at most `bars`
            beats = min(beats, self.loop[1])
        return max(4.0, beats * 60.0 / tempo)

    def play_loop(self, start: float, length: float) -> None:
        self.live.transport(loop_start=start, loop_length=length, loop=True, time=start, play=True)
        self.loop = (start, length)

    def wait_for_loop_start(self) -> None:
        """Waits until playback is back at the loop start, so every measurement hears the same bars."""
        if self.loop is None:
            return
        start, length = self.loop
        tempo = self.ex.set_state.get("tempo") or 120
        deadline = self.env.now() + length * 60 / tempo + 2
        last = None
        while self.env.now() < deadline:
            pos = self.live.transport()["song_time"]
            if last is not None and pos < last - 0.5:     # wrapped from the loop end to its start
                return
            if last is None and abs(pos - start) < 0.1:   # just started there
                return
            last = pos
            self.env.wait(0.05)

    def match_taps(self) -> None:
        taps = self.hub.by_name()
        self.tap_of, self.references = {}, []
        for t in self.ex.set_state["tracks"]:
            if is_reference_name(t["name"]) and t["kind"] == "track":
                tap = taps.get((t["kind"], t["name"]))
                if tap is not None:
                    self.references.append(tap)
                continue  # a reference track is never mixed
            tap = taps.get((t["kind"], t["name"]))
            if tap is None or t["kind"] == "master":
                continue
            if tap.is_reference:
                self.references.append(tap)
            else:
                self.tap_of[t["key"]] = tap
        missing = [t["name"] for t in self.ex.set_state["tracks"]
                   if t["key"] not in self.tap_of and not is_reference_name(t["name"])
                   and not t.get("is_group") and t["kind"] != "return"
                   and not (t["kind"] == "master" and any(tap.kind == "master" for tap in taps.values()))]
        self.log(f"Taps: {len(self.tap_of)} channels, {len(self.references)} reference(s)"
                 + (f"; no tap on: {', '.join(missing)}" if missing else ""))

    def project_references(self) -> list[str]:
        """Audio files on the project's reference tracks («REF …», «Reference»), muted or not."""
        try:
            clips = self.live.request("reference_clips")["clips"]
        except Exception:
            return []
        files = [c["file"] for c in clips if os.path.exists(c["file"])]
        for c in clips:
            self.log(f"Reference in the project: {os.path.basename(c['file'])} (track «{c['track']}»)")
        return files

    def understand(self) -> None:
        self.ex.refresh()
        self.match_taps()
        in_project = self.project_references()
        if in_project:
            from .reference import ReferenceSet

            paths = list(dict.fromkeys((self.refset.paths if self.refset else []) + in_project))
            self.refset = ReferenceSet(paths, log=self.log)
        since = self.env.now() - 60
        notes: dict[str, list] = {}
        try:
            for n in self.live.request("midi_notes", start=0, end=1e9)["notes"]:
                notes.setdefault(n[0], []).append(n)
        except Exception:
            pass  # no MIDI (or an older remote script): names, devices and sound only
        for t in self.ex.set_state["tracks"]:
            if t["key"] == "m" or t["key"] not in self.tap_of:
                continue
            self.roles[t["key"]] = roles_mod.guess(t, stats(self.tap_of[t["key"]].window(since, self.env.now())),
                                                   notes.get(t["key"]))
        first = not os.path.exists(self.s.roles_file)
        roles_mod.apply_overrides(self.roles, self.s.roles_file)
        roles_mod.save_template(self.roles, self.s.roles_file)
        if first:
            self.log(f"Wrote {self.s.roles_file}: correct the roles there (or in mixmatch ui), they are read at the next start.")
        for r in self.roles.values():
            self.log(f"  {r.name:<24} {r.role:<14} priority {r.priority}  ({r.source})")
        has_vocals = any(r.role in VOCAL_ROLES for r in self.roles.values())
        self.beat = self.s.mode == "beat" or (self.s.mode == "auto" and not has_vocals)
        self.log(f"Mode: {'beat (no vocals)' if self.beat else 'song with vocals'}")
        if self.refset:
            self.target = self.refset.target(self.beat, self.s.loudness_from)
            self.log(f"Target: {self.target.description}, {self.target.loudness:.1f} LUFS")

    def sections(self) -> list[tuple[str, float, float]]:
        cues = self.ex.set_state.get("cue_points") or []
        if len(cues) >= 2:
            return [(c["name"] or f"section {i+1}", c["time"], cues[i + 1]["time"] - c["time"])
                    for i, c in enumerate(cues[:-1]) if cues[i + 1]["time"] > c["time"]]
        loop = self.ex.set_state["loop"]
        return [("loop", loop["start"], loop["length"])]

    # ── effects that do nothing or harm ─────────────────────────────────────
    MIXING_TOOLS = ("eq8", "compressor2", "gluecompressor", "multibanddynamics", "stereogain", "channeleq",
                    "filtereq3", "pro-q", "pro-c", "pro-mb", "pro-ds")
    METERS = ("loudness meter", "span", "spectrum", "analyzer", "analyser", "tuner", "meter", "insight", "youlean")
    # Effects that move over time or add space: an average over a few bars hides what they do, so «does nothing»
    # would be a wrong verdict. They are left alone.
    MOVING = ("phaser", "flanger", "chorus", "ensemble", "auto filter", "auto pan", "autopan", "tremolo", "vibrato",
              "shifter", "frequency shifter", "lfo", "delay", "echo", "reverb", "valhalla", "beat repeat", "grain",
              "spectral", "resonator", "corpus", "vocoder", "roar", "shaperbox", "kickstart", "duck")

    def audit_effects(self, main: tuple[str, float, float], deadline: float) -> None:
        """Removes EQ Eights and Utilities that change nothing; listens to every other audio effect on and off
        and bypasses those that change nothing or (mixing tools only) make the mix worse."""
        self.log("\n━━ Effects: what does nothing or harms")
        self.ex.refresh()
        for t in self.ex.set_state["tracks"]:
            for d in sorted(t["devices"], key=lambda d: -d["index"]):
                if neutral_device(d) and not any(p.get("automated") for p in d["params"]):
                    count = len(t["devices"])
                    self.live.delete_device(t["key"], d["index"])
                    self.ex._deleted(t["key"], d["index"], count)
                    self.log(f"  − removed {d['name']} on {t['name']}: its settings change nothing")
            self.ex.refresh()

        candidates = []
        for t in self.ex.set_state["tracks"]:
            key = t["key"]
            if key != "m" and key not in self.tap_of:
                continue
            for d in t["devices"]:
                name, cls = d["name"].lower(), d.get("class_name", "").lower()
                on = next((p for p in d["params"] if p["name"] == "Device On"), None)
                if (d.get("type", 2) != 2 or d["name"] == TAP or on is None or on["value"] < 0.5
                        or any(p.get("automated") for p in d["params"]) or any(m in name for m in self.METERS)
                        or any(m in name or m in cls for m in self.MOVING)):
                    continue
                if key == "m" and (cls == "limiter" or any(x in name for x in LIMITERS)
                                   or any(c.lower() in name for c, _ in CLIPPERS)):
                    continue  # the loudness chain is set elsewhere
                region = (main[1], main[2]) if key == "m" else self.regions.get(key)
                if region is not None:
                    candidates.append((region, key, t["name"], d["name"], cls))
        self.log(f"  listening to {len(candidates)} effects on and off …")
        self.progress(0.0, "effects", total_add=len(candidates) * self.W_EFFECT - self.W_AUDIT_GUESS)
        bars, self.s.bars = self.s.bars, self.s.audit_bars
        try:
            for region, key, track_name, dev_name, cls in sorted(candidates, key=lambda c: c[0]):
                if self.env.now() >= deadline:
                    self.log("  time is up")
                    return
                self.audit_one(region, key, track_name, dev_name, cls)
                self.progress(self.W_EFFECT, f"effect {dev_name}")
        finally:
            self.s.bars = bars

    def audit_one(self, region, key, track_name, dev_name, cls) -> None:
        if self.loop != region:
            self.play_loop(*region)
        self.ex.refresh()
        idx = next((d["index"] for d in self.ex.track(key)["devices"] if d["name"] == dev_name), None)
        if idx is None:
            return
        scoring = "full" if key == "m" else "balance"
        on = self.measure(scoring)
        rr = RoundResult()
        try:
            self.ex._set(rr, key, idx, "Device On", value=0)
        except Exception as e:
            self.log(f"  {dev_name} on {track_name}: {e}")
            return
        off = self.measure(scoring)
        a = on["master"] if key == "m" else on["tracks"].get(key)
        b = off["master"] if key == "m" else off["tracks"].get(key)
        if a is None or b is None:
            self.ex.undo(rr)
            return
        audible = a.bands_db > a.bands_db.max() - 40
        band = float(np.max(np.abs((b.bands_db - a.bands_db)[audible] - (b.loudness - a.loudness)))) if audible.any() else 0.0
        level, plr, width = b.loudness - a.loudness, b.plr - a.plr, b.width_db - a.width_db
        nothing = abs(level) < 0.3 and band < 0.5 and abs(plr) < 0.3 and abs(width) < 0.5
        # «Better without» needs a reference: the balance between channels alone (masking) says nothing about how
        # an EQ shapes a sound, and a deliberate curve would be thrown away on noise. It must also hold twice.
        margin = max(self.s.min_gain, 0.2)
        harmful = (not nothing and on.get("cmp") is not None
                   and any(t in cls or t in dev_name.lower() for t in self.MIXING_TOOLS)
                   and off["objective"] < on["objective"] - margin)
        if harmful:
            self.ex.undo(rr)
            again = self.measure(scoring)
            self.ex._set(rr := RoundResult(), key, idx, "Device On", value=0)
            off2 = self.measure(scoring)
            harmful = off2["objective"] < again["objective"] - margin
            if not harmful:
                self.log(f"  {dev_name} on {track_name}: better without it only once "
                         f"({on['objective']:.2f} → {off['objective']:.2f}, then {again['objective']:.2f} → "
                         f"{off2['objective']:.2f}), kept")
        what = f"level {level:+.1f} dB, tone ±{band:.1f} dB, PLR {plr:+.1f}, width {width:+.1f}"
        if nothing:
            verdict = f"  ⊘ bypassed {dev_name} on {track_name}: does nothing here ({what})"
        elif harmful:
            verdict = f"  ⊘ bypassed {dev_name} on {track_name}: the mix is better without it ({on['objective']:.2f} → {off['objective']:.2f})"
        else:
            self.ex.undo(rr)
            verdict = f"  ✓ kept {dev_name} on {track_name} ({what})"
        self.log(verdict)
        self._record(Round(f"effects {track_name}", 1, on["objective"], off["objective"],
                           {"assessment": verdict.strip()}, rr.applied, [], undone=not (nothing or harmful)))

    def read_harmony(self, sections: list[tuple[str, float, float]]) -> None:
        """Key and chords of the song from its MIDI clips (exact); quietly skipped if there are none."""
        from . import harmony

        start = min(s[1] for s in sections)
        end = max(s[1] + s[2] for s in sections)
        try:
            notes = self.live.request("midi_notes", start=start, end=end)["notes"]
        except Exception:
            return
        if len(notes) < 8:
            return
        self.harmony = harmony.from_notes(notes, start, end)
        self.harmony["bass_hz"] = harmony.bass_frequencies(self.harmony["tonic"])
        chords = " – ".join(self.harmony["chords"][:8]) + (" …" if len(self.harmony["chords"]) > 8 else "")
        self.log(f"Key: {self.harmony['key']} ({self.harmony['mode']}, from MIDI) · chords: {chords}")

    LINK_NAMES = {"spotify": "Spotify", "bandcamp": "Bandcamp", "beatport": "Beatport", "ncs": "NCS",
                  "fma": "Free Music Archive", "jamendo": "Jamendo", "download": "Download", "preview": "Preview",
                  "apple": "Apple Music"}

    def write_suggestions(self, sugg: dict, stamp: str) -> None:
        """runs/…-buy.md: tracks to get and free ones, with links (the interface shows it too)."""
        if not sugg or not (sugg.get("buy") or sugg.get("free")):
            return
        os.makedirs(self.s.log_dir, exist_ok=True)
        path = os.path.join(self.s.log_dir, stamp + "-buy.md")
        links = lambda s: " · ".join(f"[{self.LINK_NAMES.get(k, k)}]({v})" for k, v in s["links"].items())
        with open(path, "w") as f:
            f.write(f"# References ({sugg['style']})\n\nPut the WAVs into references/ (or on a «ref» track in "
                    "Live); the next run uses them.\n\n## To get\n\n")
            for s in sugg.get("buy", []):
                f.write(f"- **{s['artist']} – {s['title']}**: {s['why']}  \n  {links(s)}\n")
            if sugg.get("free"):
                f.write("\n## Free\n\n")
                for s in sugg["free"]:
                    f.write(f"- **{s['artist']} – {s['title']}** ({s['source']}): {s['why']}  \n  {links(s)}\n")
        self.log(f"  list with links: {path}")

    def suggest_references(self, part: tuple[str, float, float]) -> None:
        """Without --auto-ref: still records the loudest part once and writes suggestions (to get and free)."""
        from . import autoref
        from .style import Listener

        _, start, length = part
        tempo = self.ex.set_state.get("tempo") or 120
        self.log("\n━━ Suggesting references")
        recording = autoref.capture(self.live, start, min(length, 64.0), tempo, self.log)
        if recording:
            listener = Listener(log=self.log)
            self.write_suggestions(autoref.buy_list(recording, listener, self.harmony, note=self.description,
                                                    log=self.log), time.strftime("%Y%m%d-%H%M%S"))

    def find_references(self, part: tuple[str, float, float]) -> None:
        """Listens to the loudest part and picks references from the library and Jamendo (see autoref)."""
        from . import autoref
        from .reference import ReferenceSet

        _, start, length = part
        tempo = self.ex.set_state.get("tempo") or 120
        picked, to_get = autoref.find(self.live, start, min(length, 64.0), tempo, library=self.s.library,
                                      jamendo=self.s.jamendo, harmony=self.harmony, note=self.description,
                                      log=self.log)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.write_suggestions(to_get, stamp)
        if not picked:
            return
        paths = [c["path"] for c in picked] + (list(self.refset.paths) if self.refset else [])
        paths = list(dict.fromkeys(os.path.realpath(x) for x in paths))
        self.refset = ReferenceSet(paths, log=self.log)
        self.target, self.target_no_vocals = self.refset.target(self.beat, self.s.loudness_from), None
        self.log(f"Target: {self.target.description}, {self.target.loudness:.1f} LUFS")
        with open(os.path.join(self.s.log_dir, stamp + "-references.json"), "w") as f:
            json.dump(picked, f, indent=2, ensure_ascii=False)  # sources, links and licenses of what was used

    def scan(self, sections: list[tuple[str, float, float]]) -> Scan | None:
        """Plays the whole range once and records where the master and every channel play, and how loud."""
        start = min(s[1] for s in sections)
        total = max(s[1] + s[2] for s in sections) - start
        tempo = self.ex.set_state.get("tempo") or 120
        self.log(f"Listening to the whole song once: {total / 4:.0f} bars ({total * 60 / tempo:.0f} s) …")
        self.live.transport(loop_start=start, loop_length=total, loop=True, time=start, play=True)
        # Live does not jump to the start at once, so the position is read back instead of assumed.
        t0, marks, last = self.env.now(), [], -1.0
        deadline = t0 + total * 60 / tempo + 10
        while self.env.now() < deadline:
            pos = self.live.transport()["song_time"]
            if marks and pos < last - 1:      # wrapped around: one pass is done
                break
            marks.append((self.env.now(), pos))
            last = pos
            self.env.wait(0.25)
        if len(marks) < 2:
            return None
        times, beats = [m[0] for m in marks], [m[1] - start for m in marks]

        def positioned(tap):
            out = []
            for f in tap.window(t0, self.env.now()):
                if times[0] <= f.t <= times[-1]:
                    b = float(np.interp(f.t, times, beats))
                    if b >= beats[0] - 0.5:
                        out.append((b, f))
            return out

        master_tap = next((t for t in self.hub.snapshot() if t.kind == "master"), None)
        return Scan(start, total, positioned(master_tap) if master_tap else [],
                    {k: positioned(tap) for k, tap in self.tap_of.items()})

    def loudest_part(self, sections: list[tuple[str, float, float]], scan: Scan | None = None) -> tuple[str, float, float]:
        """The loudest `focus_bars` bars, measured at the master.

        The references are judged by their loudest part (drop, chorus), so the mix should be too.
        """
        start = min(s[1] for s in sections)
        total = max(s[1] + s[2] for s in sections) - start
        length = min(total, self.s.focus_bars * 4.0)
        if length >= total:
            return sections[0]
        scan = scan or self.scan(sections)
        found = loudest_window(scan.master, scan.total, length) if scan else None
        if found is None:
            self.log("  the master stayed silent; working on the whole range")
            return sections[0]
        offset, loudness = found
        bar = (scan.start + offset) / 4 + 1
        self.log(f"  loudest part: bars {bar:.0f}–{bar + length / 4:.0f}, {loudness:.1f} LUFS")
        return ("loudest part", scan.start + offset, length)

    def channel_region(self, key: str, scan: Scan, main: tuple[str, float, float] | None) -> tuple[float, float] | None:
        """Where to work on a channel: the loudest part if it plays there, else its own loudest bars."""
        frames = scan.channels.get(key, [])
        if main is not None:
            _, s, length = main
            st = stats([f for b, f in frames if s - scan.start <= b < s - scan.start + length])
            if st is not None and st.active >= 0.5:
                return s, length
        length = min(self.s.channel_bars * 4.0, scan.total)
        found = loudest_window(frames, scan.total, length)
        if found is None:
            return None
        return scan.start + found[0], length

    def channel_pass(self, main: tuple[str, float, float] | None, deadline: float, start_at: str | None = None) -> None:
        """Every channel once, most important first: level, EQ, masking, width, dynamics, each where it plays."""
        order = self._channel_order()
        if start_at:
            names = [r.name for r in order]
            if start_at not in names:
                raise ValueError(f"--resume: no channel named {start_at!r}")
            order = order[names.index(start_at):]
            self.log(f"\n━━ Channel pass, resumed at {start_at}: {len(order)} channels left")
        else:
            self.log(f"\n━━ Channel pass: {len(order)} channels")
        for r in order:
            if self.env.now() >= deadline:
                self.log("  time is up")
                return
            self.save_checkpoint(phase="channels", next_channel=r.name)
            region = self.regions.get(r.key)
            if region is None:
                self.log(f"\n▶ {r.name}: silent in the whole song, skipped")
                self.progress(0.0, r.name, total_add=-self.W_CHANNEL)   # takes no time: out of the estimate
                continue
            start, length = region
            bar = start / 4 + 1
            # Only the loudest part is compared with the references (their loudest part); elsewhere, e.g. an intro
            # without drums, the song is meant to sound different, so only the channels against each other count.
            in_main = main is not None and abs(start - main[1]) < 1e-6
            note = "" if in_main or main is None else " (outside the loudest part: overlap and mono bass only)"
            self.log(f"\n▶ {r.name} ({r.role}, priority {r.priority}), bars {bar:.0f}–{bar + length / 4:.0f}{note}")
            self.play_loop(start, length)
            self.begin_step(self.W_CHANNEL)
            self.section(f"channel {r.name}", deadline, focus=r.key, max_rounds=self.s.channel_rounds,
                         scoring="balance" if in_main or main is None else "local")
            self.finish_step(r.name)

    # ── 2. measure ──────────────────────────────────────────────────────────
    def measure(self, scoring: str = "full", retries: int = 6) -> dict:
        """`scoring` «balance» leaves out loudness and dynamics: a quiet part need not sound like the drop.

        A silent master is never a result: MixMatch waits and measures again, and gives up with MasterSilent.
        """
        for attempt in range(retries + 1):
            self.wait_for_loop_start()
            self.env.wait(self.window_seconds())
            end = self.env.now()
            start = end - self.window_seconds()
            master_tap = next((t for t in self.hub.snapshot() if t.kind == "master"), None)  # «Main» in Live 12
            master = stats(master_tap.window(start, end)) if master_tap else None
            if master_tap is None or (master is not None and master.active >= 0.5):
                break  # a master that plays in less than half the window (audio dropped out) does not count
            if attempt == retries:
                raise MasterSilent("the master stayed silent")
            self.log("  ⚠ the master is silent (Live stopped, exporting, or audio engine off?): waiting …")
            self.env.wait(10)
            try:
                self.live.transport(play=True)
            except Exception:
                pass  # Live busy (e.g. exporting): try again after the next wait
        gains = self.fader_gains()
        tracks = {k: post_fader(stats(tap.window(start, end)), gains.get(k, 0.0)) for k, tap in self.tap_of.items()}
        if self.target:
            target = self.target
            vocals = [k for k, r in self.roles.items() if r.role in VOCAL_ROLES]
            if not self.beat and vocals and all(tracks.get(k) is None for k in vocals) and self.refset:
                # No vocals in this part: like a beat, the vocal range only counts where the mix is louder.
                if self.target_no_vocals is None:
                    self.target_no_vocals = self.refset.target(True, self.s.loudness_from)
                target = self.target_no_vocals
            ref = target.stats
            cmp = compare(master, ref, target.loudness, target.tolerance, target.one_sided,
                          offset=target.offset, anchor=target.anchor) if master else None
        else:
            taps = self.references
            if self.beat and any("inst" in t.name.lower() for t in taps):
                taps = [t for t in taps if "inst" in t.name.lower()]
            refs = [s for s in (stats(r.window(start, end)) for r in taps) if s]
            ref = refs[0] if len(refs) == 1 else _average(refs) if refs else None
            cmp = compare(master, ref) if master and ref else None
        masks = self._maskings(tracks)
        penalty = sum(m["severity"] * min(m["priority_a"], m["priority_b"]) / 5 for m in masks[:4])
        scored = cmp
        if scoring == "local":
            scored = None          # judged without the references: see the fallback below
        elif cmp and scoring == "balance":
            scored = replace(cmp, loudness_diff=0.0, plr_diff=0.0)
        elif cmp and cmp.loudness_diff < 0 and self.loudness_exhausted():
            # Limiter (and clipper) as far as they may go: the rest of the gap is accepted, not chased with faders.
            scored = replace(cmp, loudness_diff=0.0)
        if scored:
            obj, parts = objective(scored, penalty), objective_parts(scored, penalty)
        else:
            # No reference: the balance between channels alone would not see a stereo low end, so it counts against
            # a fixed limit (low end side about 20 dB under mid, where mastered songs sit).
            parts = {"masking": penalty}
            if master is not None:
                parts["low_side"] = max(0.0, master.low_side_db - LOW_SIDE_LIMIT_DB) * 0.5
            obj = sum(parts.values())
        return {"master": master, "ref": ref, "tracks": tracks, "masking": masks, "scoring": scoring,
                "cmp": None if scoring == "local" else cmp,   # the references do not apply outside the loud part
                "objective": obj, "parts": parts}

    def limiter_at_cap(self) -> bool:
        gain = self.ex.limiter_gain() if self.ex.set_state else None
        return gain is not None and gain >= LIMITER_MAX_GAIN_DB - 0.05

    def loudness_exhausted(self) -> bool:
        """Limiter at its cap, and the clipper too (or none installed)."""
        if not self.limiter_at_cap():
            return False
        clip = self.ex.clipper_gain()
        return clip is None or clip >= CLIPPER_MAX_GAIN_DB - 0.05

    def fader_gains(self) -> dict[str, float]:
        """dB the mixer adds after each channel's tap: its own fader plus those of the groups it sits in.

        The tap is the last device, so it measures before the fader; this turns its numbers into what reaches the mix.
        """
        tracks = self.live.get_set()["tracks"]
        groups = {t["name"]: t for t in tracks if t.get("is_group")}

        def volume(t):
            v = next((parse_display(p["display"]) for p in t["mixer"] if p["name"] == "Volume"), None)
            return 0.0 if v is None else v

        out = {}
        for t in tracks:
            if t["kind"] == "master":
                continue
            gain, seen, parent = volume(t), set(), groups.get(t.get("group"))
            while parent is not None and parent["name"] not in seen:
                seen.add(parent["name"])
                gain += volume(parent)
                parent = groups.get(parent.get("group"))
            out[t["key"]] = gain
        return out

    def _group_members(self) -> dict[str, set[str]]:
        """Group track key → keys of every track inside it (also inside nested groups)."""
        tracks = self.ex.set_state.get("tracks", [])
        by_name = {t["name"]: t for t in tracks if t.get("is_group")}
        out: dict[str, set[str]] = {}
        for t in tracks:
            parent, seen = by_name.get(t.get("group")), set()
            while parent is not None and parent["name"] not in seen:
                seen.add(parent["name"])
                out.setdefault(parent["key"], set()).add(t["key"])
                parent = by_name.get(parent.get("group"))
        return out

    def _maskings(self, tracks: dict) -> list[dict]:
        out = []
        keys = [k for k, s in tracks.items() if s is not None and k in self.roles]
        inside = self._group_members()
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if b in inside.get(a, ()) or a in inside.get(b, ()):
                    continue  # a group contains its own tracks: that is not masking
                ra, rb = self.roles[a], self.roles[b]
                if ra.role in roles_mod.BACKGROUND_ROLES or rb.role in roles_mod.BACKGROUND_ROLES:
                    continue
                hits = masking(tracks[a], tracks[b])
                if hits:
                    band, sev = hits[0]
                    out.append({"track_a": a, "track_b": b, "role_a": ra.role, "role_b": rb.role,
                                "freq_hz": round(CENTRES[band]), "severity": round(sev, 2),
                                "priority_a": ra.priority, "priority_b": rb.priority})
        return sorted(out, key=lambda m: -m["severity"] * (m["priority_a"] + m["priority_b"]))

    # ── 3. decide ───────────────────────────────────────────────────────────
    def context(self, section: str, m: dict, focus: str | None = None) -> dict:
        cmp: Comparison | None = m["cmp"]
        channels = []
        for key, r in self.roles.items():
            st = m["tracks"].get(key)
            t = self.ex.track(key)
            channels.append({
                "track": key, "name": r.name, "role": r.role, "priority": r.priority, "note": r.note,
                "volume": next((p["display"] + (" (automated)" if p.get("automated") else "")
                                for p in t["mixer"] if p["name"] == "Volume"), None),
                "pan": next((p["display"] for p in t["mixer"] if p["name"] == "Pan"), None),
                "sends": {p["name"]: p["display"] for p in t["mixer"] if p["name"].startswith("Send ")} or None,
                "kind": "group" if t.get("is_group") else t["kind"],
                "group": t.get("group"),
                "measured": st.summary() if st else "silent in this section",
                "loudness_vs_master_lu": round(st.loudness - m["master"].loudness, 1) if st and m["master"] else None,
                "devices": device_summary(t),
                **({"resonances": self.resonances[key]} if self.resonances.get(key) else {}),
            })
        ctx = {
            "section": section,
            "mode": ("beat without vocals, vocals may be recorded later: the target is the full reference songs. "
                     "Low end, air above 5 kHz, width and dynamics should match them; in 200 Hz–5 kHz the beat may sit "
                     "up to 4 dB under the song (room for a vocal), which the tonal numbers already account for. "
                     "The beat must sound complete on its own: remove harshness and resonances where they stick out, "
                     "keep presence and air, and do not boost instruments into the vocal range to imitate a voice")
                    if self.beat else "song with vocals",
            "target": self.target.description if self.target else "reference tracks playing in the project",
            "objective": round(m["objective"], 2),
            "master": m["master"].summary() if m["master"] else None,
            "reference": m["ref"].summary() if m["ref"] else "no reference (pass --ref files or name tracks «REF …»)",
            "master_vs_reference": {**cmp.summary(), "_tonal_excess": [float(x) for x in cmp.tonal_excess]} if cmp else None,
            **({"scope": "this part is outside the song's loudest part: it is meant to sound different from the "
                         "references (e.g. no drums), so do not match their tonal balance here; only make the channels "
                         "sit well against each other (overlaps, mono low end, harsh resonances)"}
               if m.get("scoring") == "local" else {}),
            "master_devices": device_summary(self.ex.track("m")),
            "master_resonances": self.resonances.get("m") or None,
            "user_description": self.description or None,
            "limiter": {"gain_db": self.ex.limiter_gain(), "max_gain_db": LIMITER_MAX_GAIN_DB,
                        "at_max": self.limiter_at_cap()},
            "clipper": ({"plugin": self.ex.clipper()[0], "gain_db": self.ex.clipper_gain(),
                         "max_gain_db": CLIPPER_MAX_GAIN_DB} if self.ex.clipper() else "none installed"),
            "loudness_note": ("limiter and clipper are at their maximum: the remaining loudness gap is accepted and "
                              "does not count; do not raise faders to chase loudness") if self.loudness_exhausted() else "",
            "harmony": ({"key": self.harmony["key"], "bass_hz": self.harmony["bass_hz"],
                         "note": "kick, sub and bass sit on the tonic and fifth; masking there matters most"}
                        if self.harmony else None),
            "installed_plugins": self.ex.plugins[:80],
            "channels": channels,
            "masking": m["masking"][:8],
            "locked": [list(map(str, l)) for l in sorted(self.ex.locks, key=str)],
            "history": [
                {"section": h.section, "round": h.n, "objective_before": round(h.before, 2),
                 "objective_after": None if h.after is None else round(h.after, 2), "undone": h.undone,
                 "actions": [{k: v for k, v in a.items() if v is not None} for a in h.applied],
                 "assessment": h.plan.get("assessment", "")}
                for h in self.history[-10:]
            ],
        }
        if focus is None and section == "final check":
            ctx["final_check"] = (
                "Final check of the master after the channel pass. If the master is quieter than the target and the "
                "limiter or clipper still has room (see «limiter», «clipper»), raise them first: limiter up to its "
                "maximum, then the clipper. After that only small master corrections (a broad master EQ move of at most "
                "1.5 dB for an overall tilt the channels did not fix). Channels are done; do not change them here.")
        if focus is not None and self.ex.track(focus).get("is_group"):
            r = self.roles[focus]
            ctx["task"] = (
                f"Channel pass on the group (bus) {focus} «{r.name}»: it sums the tracks inside it (see «group» of the "
                "channels). Check its level in the mix, its bus devices (compressor amount: gentle glue, 1–3 dB of gain "
                "reduction; load one only if the group clearly needs it), and EQ that belongs to the whole group. Do not "
                "fix single tracks here. 0 to 3 actions, all on this track; none if it sits well (done=true).")
        elif focus is not None and self.ex.track(focus)["kind"] == "return":
            r = self.roles[focus]
            ctx["task"] = (
                f"Channel pass on the return {focus} «{r.name}» (reverb or delay fed by the channels' sends). Check its "
                "level, EQ (cut lows below ~200–300 Hz and harsh highs so it does not muddy the mix), width, and which "
                "channels send how much (send actions on those channels are allowed in this pass). 0 to 3 actions; "
                "none if it sits well (done=true).")
        elif focus is not None:
            r = self.roles[focus]
            ctx["task"] = (
                f"Channel pass: work only on track {focus} «{r.name}» (role {r.role}, priority {r.priority}"
                + (f", note: {r.note}" if r.note else "") + "). Check, in this order: its level against its role and "
                "priority; EQ (cut what it does not need: rumble below its range, mud, harshness, resonances; make room "
                "where it masks or is masked by a more important channel); width and pan (low end mono); dynamics "
                "(load a Compressor only if it clearly needs control). 0 to 3 actions, all on this track. If it already "
                "sits well, return no actions and done=true. Master loudness is not a goal in this pass.")
            ctx["channels"].sort(key=lambda c: c["track"] != focus)
        return ctx

    # ── 4. the loop ─────────────────────────────────────────────────────────
    def run(self) -> list[Round]:
        os.makedirs(self.s.log_dir, exist_ok=True)
        self.logfile = open(os.path.join(self.s.log_dir, time.strftime("%Y%m%d-%H%M%S") + ".jsonl"), "w")
        deadline = self.env.now() + self.s.minutes * 60 if self.s.minutes else math.inf
        self.understand()
        if not self.references and not self.target:
            self.log("No reference found. Name the reference tracks «REF …» and put a tap on them; "
                     "until then only the balance between channels is worked on.")
        from . import ab

        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.ex.refresh()
        # for the before/after switch; a continued run compares with the state before the run it continues
        ck = self.load_checkpoint() if self.s.resume and self.s.complete else None
        first = os.path.join(self.s.log_dir, ck["before"]) if ck and ck.get("before") else None
        self._before_file = f"{stamp}-before.json"
        if first and os.path.exists(first):
            if os.path.abspath(first) != os.path.abspath(os.path.join(self.s.log_dir, self._before_file)):
                shutil.copyfile(first, os.path.join(self.s.log_dir, self._before_file))
        else:
            ab.save(self.ex.set_state, self.s.log_dir, stamp, "before")
        main_volume = self.mute_main() if self.s.silent else None
        try:
            self._run(deadline)
        except PlannerFailed as e:
            self.log(f"\nStopped: the planner failed ({e}). Everything kept so far stays in the set.")
        except MasterSilent:
            self.log("\nStopped: the master stayed silent for over a minute, so nothing could be measured. "
                     "Everything kept so far stays in the set; continue with --resume once Live plays again.")
        finally:
            if main_volume is not None:
                self.live.batch([{"track": "m", "device": "mixer", "param": "Volume", "value": main_volume}])
                self.log("Main fader back where it was.")
        try:
            self.ex.refresh()
            ab.save(self.ex.set_state, self.s.log_dir, stamp, "after")
            self.log("Before/after saved: switch with «mixmatch ab before» / «mixmatch ab after» or in mixmatch ui.")
        except Exception:
            pass
        self.log("\nDone. " + self.report())
        self.logfile.close()
        return self.history

    def mute_main(self) -> float | None:
        """Silent mode: the Main fader all the way down, so nothing is heard while MixMatch works. Every tap,
        the master's too, sits before the Main fader and keeps measuring. Returns the old value."""
        master = next((t for t in self.ex.set_state["tracks"] if t["key"] == "m"), None)
        vol = next((p for p in master["mixer"] if p["name"] == "Volume"), None) if master else None
        if vol is None:
            return None
        self.live.batch([{"track": "m", "device": "mixer", "param": "Volume", "value": 0.0}])
        self.log("Silent: the Main fader is down while MixMatch works (it measures before the fader).")
        return vol["value"]

    CHECKPOINT = "checkpoint.json"

    def save_checkpoint(self, **changes) -> None:
        """Where a complete run stands, so a stopped run continues there without the preparation:
        the loudest part, every channel's region, beat mode, key, the phase and the next channel."""
        self._ck = {**getattr(self, "_ck", {}), **changes, "before": getattr(self, "_before_file", None),
                    "done": round(getattr(self, "_done", 0.0), 2), "total": round(getattr(self, "_total", 0.0), 2)}
        try:
            with open(os.path.join(self.s.log_dir, self.CHECKPOINT), "w") as f:
                json.dump(self._ck, f)
        except OSError:
            pass

    def load_checkpoint(self) -> dict | None:
        try:
            with open(os.path.join(self.s.log_dir, self.CHECKPOINT)) as f:
                ck = json.load(f)
        except (OSError, ValueError):
            return None
        names = {r.name for r in self.roles.values()}
        return ck if ck.get("phase") and set(ck.get("regions", {})) <= {r.key for r in self.roles.values()} | {"m"} \
            and (ck.get("next_channel") in (None, *names)) else None

    def clear_checkpoint(self) -> None:
        try:
            os.remove(os.path.join(self.s.log_dir, self.CHECKPOINT))
        except OSError:
            pass

    def _channel_order(self) -> list:
        return sorted(self.roles.values(), key=lambda r: (-r.priority, r.name))

    def _run(self, deadline: float) -> None:
        sections = self.sections()
        ck = self.load_checkpoint() if self.s.resume and self.s.complete else None
        if self.s.resume == "auto" and ck is None:
            self.log("Nothing to continue (no stopped run): starting a complete run.")
            self.s.resume = None
        if ck is not None:
            self._resume_from(ck)
            main = [tuple(ck["main"])]
            phase = ck["phase"]
            start_at = ck.get("next_channel") if self.s.resume == "auto" else self.s.resume
            if self.s.resume != "auto":
                phase = "channels"
        else:
            main, phase, start_at = self._prepare(sections, deadline), "master", None
            if self.s.resume:                     # an old run without checkpoint: skip the master, start there
                phase, start_at = "channels", self.s.resume
            if self.s.complete:
                self.save_checkpoint(phase=phase, next_channel=start_at)
        if phase == "master":
            if self.s.complete:
                self.log(f"\n━━ Master against the reference")
            for name, start, length in main:
                if self.env.now() >= deadline:
                    break
                self.log(f"\n▶ {name}")
                self.play_loop(start, length)
                self.begin_step(self.W_MASTER)
                self.section(name, deadline)
                if self.s.complete:
                    self.finish_step("master")
            if not self.s.complete:
                return
            phase = "channels"
            self.save_checkpoint(phase=phase, next_channel=None)
        if phase == "channels":
            self.channel_pass(main[0] if self.s.focus == "loud" else None, deadline, start_at)
            if self.env.now() >= deadline:
                return
            phase = "effects"
            self.save_checkpoint(phase=phase, next_channel=None)
        if phase == "effects":
            if self.s.audit:
                self.audit_effects(main[0], deadline)
            if self.env.now() >= deadline:
                return
            phase = "final"
            self.save_checkpoint(phase=phase)
        name, start, length = main[0]
        if self.env.now() < deadline:
            self.log(f"\n━━ Final check: master, {name}")
            self.play_loop(start, length)
            self.begin_step(self.W_FINAL)
            self.section("final check", deadline, max_rounds=self.s.final_rounds)
            self.finish_step("done")
            self.clear_checkpoint()               # the run is complete: nothing left to continue

    def _prepare(self, sections, deadline) -> list:
        """Key, one pass through the whole song, beat mode, the loudest part, references; then every
        channel's region. Returns the master sections."""
        if self.s.complete:
            channels = len(self.roles)
            if self.s.resume:
                names = [r.name for r in self._channel_order()]
                channels -= names.index(self.s.resume) if self.s.resume in names else 0
            masters = 0 if self.s.resume else (1 if self.s.focus == "loud" else len(sections))
            self._done, self._total = 0.0, (self.W_PREP + masters * self.W_MASTER + channels * self.W_CHANNEL
                                            + (self.W_AUDIT_GUESS if self.s.audit else 0) + self.W_FINAL)
            self.progress(step="harmony and scan")
        self.read_harmony(sections)
        scan = self.scan(sections) if self.s.complete else None
        if scan is not None and not self.beat and self.s.mode == "auto":
            vocals = [k for k, r in self.roles.items() if r.role in VOCAL_ROLES]
            if vocals and all(stats([f for _, f in scan.channels.get(k, [])]) is None for k in vocals):
                self.beat = True
                self.log("The vocal channels are silent in the whole song: treating it as a beat (no vocals).")
                if self.refset:
                    self.target = self.refset.target(True, self.s.loudness_from)
                    self.log(f"Target: {self.target.description}, {self.target.loudness:.1f} LUFS")
        main = [self.loudest_part(sections, scan)] if self.s.focus == "loud" else sections
        if self.s.auto_ref:
            self.find_references(main[0])
        elif self.s.complete and self.s.suggest:
            try:
                self.suggest_references(main[0])
            except Exception as e:  # suggestions are a bonus: never stop a run for them
                self.log(f"  no suggestions ({str(e)[:100]})")
        if scan is not None:
            focus = main[0] if self.s.focus == "loud" else None
            self.regions = {r.key: self.channel_region(r.key, scan, focus) for r in self.roles.values()}
            self._ck = {"stamp": time.strftime("%Y-%m-%d %H:%M"), "main": list(main[0]), "beat": self.beat,
                        "harmony": getattr(self, "harmony", None),
                        "regions": {k: (list(v) if v else None) for k, v in self.regions.items()}}
        if self.s.complete:
            self.progress(self.W_PREP, "master" if not self.s.resume else "channels")
        return main

    def _resume_from(self, ck: dict) -> None:
        """Back to where a stopped run was, without playing the song through again."""
        self._ck = dict(ck)
        self.regions = {k: (tuple(v) if v else None) for k, v in ck["regions"].items()}
        if ck.get("harmony"):
            self.harmony = ck["harmony"]
        if ck.get("beat") and not self.beat:
            self.beat = True
            if self.refset:
                self.target = self.refset.target(True, self.s.loudness_from)
                self.log(f"Target: {self.target.description}, {self.target.loudness:.1f} LUFS")
        self._done, self._total = float(ck.get("done", 0.0)), float(ck.get("total", 0.0))
        where = ck.get("next_channel") or ck["phase"]
        self.log(f"↻ Continuing the run from {ck.get('stamp', '?')} at {where} (no preparation needed)")
        self.progress(0.0, where)

    def explain(self, m: dict) -> None:
        """The distance in plain words and real units, as one «explain» log line (JSON) for the interface."""
        from .analysis import band_label

        cmp, out = m.get("cmp"), {}
        if cmp is not None:
            ld, plr, width = cmp.loudness_diff, cmp.plr_diff, cmp.width_diff
            out["loudness"] = f"{abs(ld):.1f} LU {'quieter' if ld < 0 else 'louder'} than the reference"
            out["dynamics"] = f"{abs(plr):.1f} dB {'more' if plr > 0 else 'less'} dynamic (PLR) than the reference"
            out["width"] = f"{abs(width):.1f} dB {'wider' if width > 0 else 'narrower'} than the reference"
            out["low_side"] = f"bass {cmp.low_side_diff:+.1f} dB more stereo than the reference"
            out["clipping"] = f"peaks at {cmp.peak_db:.1f} dBFS"
            worst = sorted(TONAL_BANDS, key=lambda i: -abs(cmp.tonal_excess[i]))[:3]
            tones = [f"{cmp.tonal_excess[i]:+.1f} dB at {band_label(i)} Hz" for i in worst if abs(cmp.tonal_excess[i]) >= 0.3]
            out["tonal"] = (", ".join(tones) + " vs the reference") if tones else "close to the reference"
        names = {r.key: r.name for r in self.roles.values()}
        pairs = [f"{names.get(x['track_a'], x['track_a'])} ↔ {names.get(x['track_b'], x['track_b'])} at {x['freq_hz']} Hz"
                 for x in m.get("masking", [])[:3]]
        out["masking"] = ", ".join(pairs) if pairs else "no channels covering each other"
        self.log("  explain " + json.dumps(out, ensure_ascii=False))

    # Weights of the steps of a complete run, in «channel» units, for the progress bar and the time left.
    W_PREP, W_MASTER, W_CHANNEL, W_AUDIT_GUESS, W_EFFECT, W_FINAL = 3.0, 7.0, 1.0, 6.0, 0.4, 3.0

    def progress(self, add: float = 0.0, step: str = "", total_add: float = 0.0) -> None:
        """One «progress» log line (JSON): units done of the expected total, with the wall clock, so the
        interface can show how far the run is and estimate the time left."""
        self._done = getattr(self, "_done", 0.0) + add
        self._total = max(getattr(self, "_total", 0.0) + total_add, self._done)
        self.log("  progress " + json.dumps({"done": round(self._done, 2), "total": round(self._total, 2),
                                             "t": round(time.time(), 1), "step": step}, ensure_ascii=False))

    def begin_step(self, weight: float) -> None:
        """The units the next section may use up round by round; finish_step books what is left."""
        self._budget = weight

    def finish_step(self, step: str) -> None:
        self.progress(getattr(self, "_budget", 0.0), step)
        self._budget = 0.0

    def measure_fine(self, scoring: str, key: str) -> dict:
        """The first measurement of a section, recording the channel (or master) at the same time to find narrow
        resonances. Falls back to a plain measurement when recording is not possible (simulator, old script)."""
        from . import autoref, fine

        if not self.s.fine or key in self.resonances or (key != "m" and not self.s.fine_channels):
            return self.measure(scoring)
        try:
            beats = min(self.loop[1] if self.loop else 32.0, self.s.bars * 4.0)
            cap = autoref.capture_begin(self.live, beats, None if key == "m" else key)
        except Exception:
            self.resonances[key] = []
            return self.measure(scoring)
        m = self.measure(scoring)
        try:
            path = autoref.capture_end(self.live, cap)
            found = fine.analyse(path, self.harmony)["resonances"] if path else []
            self.resonances[key] = [r for r in found if r["prominence_db"] >= 5.0][:4]
            if self.resonances[key]:
                self.log("  resonances: " + ", ".join(f"{r['hz']:.0f} Hz +{r['prominence_db']} dB"
                                                      for r in self.resonances[key]))
        except Exception as e:
            self.resonances[key] = []
            self.log(f"  (no fine measurement: {str(e)[:80]})")
        return m

    def section(self, name: str, deadline: float, focus: str | None = None, max_rounds: int | None = None,
                scoring: str = "full") -> None:
        m = self.measure_fine(scoring, focus or "m")
        best, stall = m["objective"], 0
        self.log(f"  distance {best:.2f}   ({_parts(m['parts'])})")
        self.explain(m)
        for n in range(1, (max_rounds or self.s.max_rounds_per_section) + 1):
            if self.env.now() >= deadline:
                self.log("  time is up")
                return
            self.ex.refresh()
            plan = self.plan_with_retries(self.context(name, m, focus))
            self.log(f"  round {n}: {plan.get('assessment', '')}")
            actions = plan.get("actions", [])
            if focus is not None:
                is_return = self.ex.track(focus)["kind"] == "return"
                ok = lambda a: a.get("track") == focus or (is_return and a.get("type") == "send")
                other = [a for a in actions if not ok(a)]
                actions = [a for a in actions if ok(a)]
                for a in other:
                    self.log(f"    – not now: {a['type']} {a.get('track') or 'master'} (channel pass is on {focus})")
            if not actions:
                if focus is not None:
                    self.log("  sits well, nothing to change")
                elif plan.get("done"):
                    self.log("  targets reached for this section")
                return
            if self.s.silent:
                actions = [a for a in actions if not (a.get("type") == "volume" and a.get("track") in (None, "m"))]
            for a in actions:
                self.log(f"    · {a['type']} {a.get('track') or 'master'}: {a.get('reason', '')}")
            if self.s.assist and self.ask("    apply? [Y/n] ").strip().lower() == "n":
                self._record(Round(name, n, best, None, plan))
                continue
            rr = self.ex.apply(actions)
            if not rr.changes:
                self._record(Round(name, n, best, best, plan, rr.applied, rr.skipped))
                stall += 1
                if stall >= self.s.stall_rounds:
                    return
                continue
            self.env.wait(self.s.settle)
            after = self.measure(scoring)
            rnd = Round(name, n, best, after["objective"], plan, rr.applied, rr.skipped)
            if after["objective"] < best - self.s.min_gain:
                self.log(f"    ✓ {best:.2f} → {after['objective']:.2f}   ({_parts(after['parts'])})")
                self.explain(after)
                best, m, stall = after["objective"], after, 0
            elif after["objective"] > best + self.s.min_gain:
                self.ex.undo(rr)
                rnd.undone = True
                self.log(f"    ↺ undone: {best:.2f} → {after['objective']:.2f}")
                self.env.wait(self.s.settle)
                m = self.measure(scoring)
                best = min(best, m["objective"])
                stall += 1
            else:  # no measurable effect: undo it too, only changes that help the mix stay
                self.ex.undo(rr)
                rnd.undone = True
                self.log(f"    ↺ undone, no measurable change ({after['objective']:.2f})")
                self.env.wait(self.s.settle)
                stall += 1
            self._record(rnd)
            if getattr(self, "_budget", 0.0) > 0:     # each round moves the progress bar a little
                piece = self._budget / max(1, (max_rounds or self.s.max_rounds_per_section) - n + 1)
                self._budget -= piece
                self.progress(piece, name)
            if plan.get("done") or stall >= self.s.stall_rounds:
                self.log("  no further improvement in this section" if stall else "  targets reached")
                return

    TRANSIENT = ("can't reach", "cannot reach", "enotfound", "network", "timed out", "timeout", "connection",
                 "econnreset", "overloaded", "529", "503", "502", "temporarily")
    RETRY_WAITS = (15, 30, 60, 120, 120, 120, 120)     # about ten minutes in all

    def plan_with_retries(self, context: dict) -> dict:
        """The planner's next round. A network outage or an overloaded server is waited out (the song keeps
        playing); anything else, or an outage over about ten minutes, stops the run cleanly."""
        for wait in (*self.RETRY_WAITS, None):
            try:
                return self.planner.plan(context)
            except Exception as e:
                msg = str(e)
                if wait is None or not any(w in msg.lower() for w in self.TRANSIENT):
                    raise PlannerFailed(msg) from e
                self.log(f"  ⚠ planner not reachable ({msg[:90]}): trying again in {wait} s")
                self.env.wait(wait)

    def _record(self, rnd: Round) -> None:
        self.history.append(rnd)
        if self.logfile:
            self.logfile.write(json.dumps(rnd.__dict__, default=str, ensure_ascii=False) + "\n")
            self.logfile.flush()

    def report(self) -> str:
        kept = [h for h in self.history if not h.undone and h.after is not None]
        undone = sum(h.undone for h in self.history)
        if not self.history:
            return "Nothing changed."
        first, last = self.history[0].before, min((h.after for h in kept), default=self.history[0].before)
        return (f"{len(self.history)} rounds, {len(kept)} kept, {undone} undone. "
                f"Distance to target {first:.2f} → {last:.2f}.")


LOW_SIDE_LIMIT_DB = -20.0   # without a reference: low end side this far under mid counts as mono enough


def _parts(parts: dict) -> str:
    """«tonal 0.9 · loudness 0.5 · …», largest first, small ones left out."""
    big = sorted(((v, k) for k, v in parts.items() if v >= 0.05), reverse=True)
    return " · ".join(f"{k} {v:.2f}" for v, k in big) or "nothing left"


def post_fader(st, gain_db: float):
    """Stats shifted by the mixer gain after the tap; None when the fader is all the way down."""
    if st is None or gain_db == -math.inf:
        return None
    if gain_db == 0.0:
        return st
    return replace(st, loudness=st.loudness + gain_db, bands_db=st.bands_db + gain_db, peak_db=st.peak_db + gain_db)


def loudest_window(positioned, total_beats: float, length_beats: float,
                   step_beats: float = 4.0) -> tuple[float, float] | None:
    """Offset in beats (from the start of the range) and loudness of the loudest window, on a one-bar grid.

    `positioned` are (beat offset, frame) pairs recorded while the range played once.
    """
    best = None
    offset = 0.0
    while offset + length_beats <= total_beats + 1e-9:
        st = stats([f for b, f in positioned if offset <= b < offset + length_beats])
        if st is not None and (best is None or st.loudness > best[1]):
            best = (offset, st.loudness)
        offset += step_beats
    return best


def _average(refs):
    """Several references: average their levels, so no single track dictates the target."""
    import numpy as np
    from .analysis import Stats

    mean = lambda attr: float(np.mean([getattr(r, attr) for r in refs]))
    bands = np.mean([r.bands_db for r in refs], axis=0)
    loud = mean("loudness")
    return Stats(frames=sum(r.frames for r in refs), loudness=loud, bands_db=bands, shape_db=bands - loud,
                 peak_db=mean("peak_db"), plr=mean("plr"), width_db=mean("width_db"),
                 low_side_db=mean("low_side_db"), active=1.0)
