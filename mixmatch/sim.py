"""A small fake Ableton Live for tests and demos, no Live needed.

It mimics the parts of Live's API the remote script uses (tracks, mixer,
EQ Eight, Utility, Limiter, the browser) and a crude sound model: every track
has a spectrum, the mixer and devices change it, the master is the sum. The
taps report what that model "sounds" like. Good enough to check that the loop
measures, decides, applies and undoes correctly; it says nothing about how good
a real mix will sound.
"""
from __future__ import annotations

import math
import os
import random
import sys

import numpy as np

from .bands import CENTRES
from .taps import Frame, TapHub

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "remote_script"))
from MixMatch.commands import Commands  # noqa: E402

F = np.array(CENTRES)


# ── fake Live objects ──────────────────────────────────────────────────────
class Param:
    def __init__(self, name, lo, hi, value, fmt, quantized=False, items=()):
        self.name, self.min, self.max, self.value = name, lo, hi, value
        self.fmt, self.is_quantized, self.value_items = fmt, quantized, list(items)

    def str_for_value(self, v):
        return self.fmt(v)


def _vol_db(v):
    if v <= 0:
        return -math.inf
    return 40 * (v - 0.85) if v >= 0.4 else -18 - (0.4 - v) / 0.4 * 52


def _db_fmt(v):
    return "-inf dB" if v == -math.inf else f"{v:.1f} dB"


def _hz(v):
    return 10 * (2200 ** v)


def _hz_fmt(hz):
    return f"{hz/1000:.2f} kHz" if hz >= 1000 else f"{hz:.0f} Hz"


EQ_TYPES = ["Low Cut 48", "Low Cut 12", "Low Shelf", "Bell", "Notch", "High Shelf", "High Cut 12", "High Cut 48"]


class Device:
    def __init__(self, name, class_name, params):
        self.name, self.class_name, self.parameters = name, class_name, params

    def p(self, name):
        return next(x for x in self.parameters if x.name == name)


def eq_eight():
    params = [Param("Device On", 0, 1, 1, str, True, ["Off", "On"])]
    for n in range(1, 9):
        params += [
            Param(f"{n} Filter On A", 0, 1, 0, lambda v: ["Off", "On"][int(round(v))], True, ["Off", "On"]),
            Param(f"{n} Filter Type A", 0, 7, 3, lambda v: EQ_TYPES[int(round(v))], True, EQ_TYPES),
            Param(f"{n} Frequency A", 0, 1, 0.5, lambda v: _hz_fmt(_hz(v))),
            Param(f"{n} Gain A", -15, 15, 0, lambda v: f"{v:.1f} dB"),
            Param(f"{n} Resonance A", 0, 1, 0.3, lambda v: f"{0.1 + v * 17.9:.2f}"),
        ]
    return Device("EQ Eight", "Eq8", params)


def utility():
    return Device("Utility", "StereoGain", [
        Param("Gain", -35, 35, 0, lambda v: f"{v:.1f} dB"),
        Param("Stereo Width", 0, 4, 1, lambda v: f"{v*100:.0f} %"),
        Param("Bass Mono", 0, 1, 0, lambda v: ["Off", "On"][int(round(v))], True, ["Off", "On"]),
    ])


def limiter():
    return Device("Limiter", "Limiter", [
        Param("Gain", 0, 24, 0, lambda v: f"{v:.1f} dB"),
        Param("Ceiling", -24, 0, 0, lambda v: f"{v:.1f} dB"),
        Param("Release", 0.01, 3, 0.3, lambda v: f"{v*1000:.0f} ms"),
    ])


def tap():
    return Device("MixMatch Tap", "MxDeviceAudioEffect", [Param("Device On", 0, 1, 1, str, True, ["Off", "On"])])


FACTORIES = {"eq eight": eq_eight, "utility": utility, "limiter": limiter, "mixmatch tap.amxd": tap}


class Mixer:
    def __init__(self):
        self.volume = Param("Track Volume", 0, 1, 0.85, lambda v: _db_fmt(_vol_db(v)))
        self.panning = Param("Track Panning", -1, 1, 0,
                             lambda v: "C" if abs(v) < 0.01 else f"{abs(v)*50:.0f}{'L' if v < 0 else 'R'}")
        self.sends = []


class Track:
    def __init__(self, name, spectrum_db, plr=10.0, width_db=-20.0, low_side_db=-40.0, level_db=None):
        self.name, self.mixer_device, self.devices = name, Mixer(), []
        self.spectrum = np.array(spectrum_db, dtype=float)
        if level_db is not None:  # scale the curve so the whole track has this RMS level
            self.spectrum += level_db - 10 * np.log10((10 ** (self.spectrum / 10)).sum())
        self.plr, self.width_db, self.low_side_db = plr, width_db, low_side_db
        self.mute, self.is_foldable, self.is_grouped = False, False, False

    def delete_device(self, index):
        del self.devices[index]


class Cue:
    def __init__(self, name, time):
        self.name, self.time = name, time


class View:
    selected_track = None
    selected_device = None

    def select_device(self, device):
        self.selected_device = device


class Song:
    def __init__(self, tracks, master, references=()):
        self.tracks = list(tracks) + list(references)
        self.return_tracks, self.master_track = [], master
        self.tempo, self.is_playing, self.current_song_time = 128.0, False, 0.0
        self.loop, self.loop_start, self.loop_length = False, 0.0, 16.0
        self.cue_points, self.view = [], View()

    def move_device(self, device, track, index):
        for t in self.tracks + [self.master_track]:
            if device in t.devices:
                t.devices.remove(device)
        track.devices.insert(index, device)

    def continue_playing(self):
        self.is_playing = True

    def start_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False


class Item:
    def __init__(self, name, children=()):
        self.name, self.children, self.is_loadable = name, list(children), not children


class Browser:
    def __init__(self, song):
        self.song = song
        self.audio_effects = Item("Audio Effects", [Item("EQ & Filters", [Item("EQ Eight")]),
                                                    Item("Utilities", [Item("Utility")]),
                                                    Item("Dynamics", [Item("Limiter")])])
        self.user_library = Item("User Library", [Item("MixMatch", [Item("MixMatch Tap.amxd")])])
        self.plugins = Item("Plug-Ins", [])   # none installed in the simulator

    def load_item(self, item):
        # Like Live: always appended to the chain of the selected track.
        self.song.view.selected_track.devices.append(FACTORIES[item.name.lower()]())


# ── sound model ────────────────────────────────────────────────────────────
def eq_response(device) -> np.ndarray:
    out = np.zeros(len(F))
    for n in range(1, 9):
        if device.p(f"{n} Filter On A").value < 0.5:
            continue
        typ = EQ_TYPES[int(round(device.p(f"{n} Filter Type A").value))]
        f0, g = _hz(device.p(f"{n} Frequency A").value), device.p(f"{n} Gain A").value
        q = 0.1 + device.p(f"{n} Resonance A").value * 17.9
        octaves = np.log2(F / f0)
        if typ == "Bell":
            out += g * np.exp(-0.5 * (octaves * q * 1.2) ** 2)
        elif typ == "Low Shelf":
            out += g / (1 + np.exp(octaves * 4))
        elif typ == "High Shelf":
            out += g / (1 + np.exp(-octaves * 4))
        elif typ.startswith("Low Cut"):
            out += np.minimum(0, octaves * (48 if "48" in typ else 12))
        elif typ == "Notch":
            out += -30 * np.exp(-0.5 * (octaves * q * 3) ** 2)
    return out


def track_output(t: Track, fader: bool = True) -> tuple[np.ndarray, float, float, float]:
    """Band levels (dB), plr, width, low side after the devices and, with `fader`, the mixer.

    Like in Live, a tap as the last device measures before the fader: fader=False.
    """
    spec = t.spectrum + (_vol_db(t.mixer_device.volume.value) if fader else 0.0)
    width, low_side, plr = t.width_db, t.low_side_db, t.plr
    for d in t.devices:
        if d.class_name == "Eq8":
            spec = spec + eq_response(d)
        elif d.class_name == "StereoGain":
            spec = spec + d.p("Gain").value
            w = d.p("Stereo Width").value
            width += 20 * math.log10(max(w, 1e-3))
            if d.p("Bass Mono").value >= 0.5:
                low_side = -60.0
    return spec, plr, width, low_side


def power_sum(levels: list[np.ndarray]) -> np.ndarray:
    return 10 * np.log10(np.maximum(sum(10 ** (l / 10) for l in levels), 1e-18))


def master_output(song: Song, refs: set[str]):
    outs = [track_output(t) for t in song.tracks if t.name not in refs]
    spec = power_sum([o[0] for o in outs])
    plr = float(np.mean([o[1] for o in outs]))
    width = float(np.mean([o[2] for o in outs]))
    low_side = float(max(o[3] for o in outs))
    peak_extra = 0.0
    for d in song.master_track.devices:
        if d.class_name == "Eq8":
            spec = spec + eq_response(d)
        elif d.class_name == "StereoGain":
            spec = spec + d.p("Gain").value
            if d.p("Bass Mono").value >= 0.5:
                low_side = -60.0
        elif d.class_name == "Limiter":
            gain, ceiling = d.p("Gain").value, d.p("Ceiling").value
            loud = 10 * np.log10((10 ** (spec / 10)).sum())
            peak = loud + plr + gain
            over = max(0.0, peak - ceiling)
            spec = spec + gain - 0.3 * over       # limiting eats part of the gain
            plr = max(4.0, plr - 0.7 * over)      # and squashes the peaks
            peak_extra = -over
    return spec, plr, width, low_side, peak_extra


# ── environment that drives time and taps ──────────────────────────────────
class SimEnv:
    def __init__(self, song: Song, hub: TapHub, refs: set[str], seed: int = 1, jitter_db: float = 0.3):
        self.song, self.hub, self.refs = song, hub, refs
        self.t = 1_000_000.0
        self.rng = random.Random(seed)
        self.jitter = jitter_db
        for i, tr in enumerate(song.tracks):
            hub.on_name(i + 1, "track", tr.name)
        hub.on_name(999, "master", song.master_track.name)

    def now(self):
        return self.t

    def _frame(self, spec, plr, width, low_side, peak_extra=0.0):
        noisy = spec + np.array([self.rng.gauss(0, self.jitter) for _ in spec])
        bands = 10 ** (noisy / 20)
        m = float(np.sqrt((bands**2).sum()))
        return Frame(self.t, m, m * 10 ** (width / 20), m * 10 ** (low_side / 20),
                     m * 10 ** ((plr + peak_extra) / 20), list(bands))

    def wait(self, seconds: float):
        steps = max(1, int(round(seconds * 10)))
        for _ in range(steps):
            self.t += 0.1
            song = self.song
            if song.is_playing:  # the song position moves like in Live, wrapping at the loop end
                song.current_song_time += 0.1 * song.tempo / 60
                if song.loop and song.current_song_time >= song.loop_start + song.loop_length:
                    song.current_song_time -= song.loop_length
            for i, tr in enumerate(self.song.tracks):
                f = self._frame(*track_output(tr, fader=False))
                self.hub.taps[i + 1].frames.append(f)
            self.hub.taps[999].frames.append(self._frame(*master_output(self.song, self.refs)))


class SimLive:
    """Same interface as LiveClient, talking to the fake song directly."""

    def __init__(self, song: Song):
        self.cmd = Commands(song, Browser(song))

    def request(self, cmd, **kw):
        return self.cmd.run({"cmd": cmd, **kw})

    def get_set(self):
        return self.request("get_set")

    def set(self, track, device, param, *, target=None, value=None):
        kw = {"track": track, "device": device, "param": param}
        kw["target" if target is not None else "value"] = target if target is not None else value
        return self.request("set", **kw)

    def batch(self, changes):
        return self.request("batch", changes=changes)

    def load_device(self, track, name, source="audio_effects"):
        return self.request("load_device", track=track, name=name, source=source)["index"]

    def delete_device(self, track, device):
        self.request("delete_device", track=track, device=device)

    def transport(self, **kw):
        return self.request("transport", **kw)


def bump(centre: float, width_oct: float, height: float) -> np.ndarray:
    return height * np.exp(-0.5 * (np.log2(F / centre) / width_oct) ** 2)


def lowpass(fc: float, db_per_oct: float = 12) -> np.ndarray:
    return np.minimum(0, -db_per_oct * np.log2(F / fc))


def highpass(fc: float, db_per_oct: float = 12) -> np.ndarray:
    return np.minimum(0, db_per_oct * np.log2(F / fc))


def pink(level: float) -> np.ndarray:
    return level - 3 * np.log2(F / 1000)


def demo_project():
    """A muddy, quiet mix: bass masks the kick, too much 200–400 Hz, dull top, stereo bass, ~6 LU too quiet."""
    kick = -24 + bump(55, 0.4, 14) + highpass(35, 24) + lowpass(150, 18) + bump(3500, 0.5, 2)
    bass = -26 + bump(65, 0.6, 12) + bump(250, 0.6, 6) + highpass(35, 24) + lowpass(900, 18)
    snare = pink(-30) + bump(220, 0.5, 8) + bump(4500, 0.8, 4) + highpass(120, 18)
    hats = pink(-38) + bump(9000, 0.7, 8) + highpass(4000, 24)
    vocal = pink(-26) + bump(1800, 1.0, 4) + bump(300, 0.6, 5) + highpass(120, 18) + lowpass(12000, 12)
    pad = pink(-28) + bump(350, 0.8, 7) + highpass(150, 12) + lowpass(5000, 12)
    ref = pink(-12) + bump(55, 0.5, 6) + highpass(30, 24) + lowpass(18000, 24)
    tracks = [
        Track("Kick", kick, plr=16, level_db=-20), Track("Bass", bass, plr=9, low_side_db=-14, level_db=-21),
        Track("Snare", snare, plr=15, level_db=-25), Track("Hats", hats, plr=14, width_db=-10, level_db=-32),
        Track("Lead Vox", vocal, plr=11, level_db=-24), Track("Pad", pad, plr=8, width_db=-3, level_db=-24),
    ]
    reference = Track("REF Song", ref, plr=8, width_db=-12, low_side_db=-30, level_db=-10)
    master = Track("Main", np.zeros(len(F)))
    song = Song(tracks, master, [reference])
    song.cue_points = [Cue("Drop", 0.0), Cue("End", 32.0)]
    hub = TapHub()
    env = SimEnv(song, hub, refs={"REF Song"})
    return song, hub, env, SimLive(song)
