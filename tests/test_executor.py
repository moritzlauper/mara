from mixmatch.actions import Executor
from mixmatch.sim import demo_project


def make():
    song, hub, env, live = demo_project()
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    return song, ex


def test_eq_adds_band_then_adjusts_the_same_band():
    song, ex = make()
    ex.apply([{"type": "eq", "track": "t1", "freq_hz": 250, "gain_db": -2, "q": 1.0, "shape": "bell"}])
    ex.apply([{"type": "eq", "track": "t1", "freq_hz": 280, "gain_db": -2, "q": 1.0, "shape": "bell"}])
    eq = song.tracks[1].devices[0]
    assert eq.p("1 Filter On A").value == 1 and eq.p("2 Filter On A").value == 0
    assert abs(eq.p("1 Gain A").value - -4.0) < 0.06


def test_steps_are_clamped():
    song, ex = make()
    ex.apply([{"type": "volume", "track": "t0", "delta_db": -10}])
    assert ex._param("t0", "mixer", "Volume")["display"] == "-2.0 dB"


def test_undo_restores_everything_without_locking_itself():
    song, ex = make()
    before = song.tracks[1].mixer_device.volume.value
    rr = ex.apply([{"type": "volume", "track": "t1", "delta_db": -1},
                   {"type": "volume", "track": "t1", "delta_db": -1},
                   {"type": "eq", "track": "t1", "freq_hz": 70, "gain_db": -2, "q": 1, "shape": "bell"}])
    ex.undo(rr)
    ex.refresh()
    assert song.tracks[1].mixer_device.volume.value == before
    assert song.tracks[1].devices[0].p("1 Filter On A").value == 0
    assert not ex.locks


def test_a_hand_on_a_fader_locks_it():
    song, ex = make()
    ex.apply([{"type": "volume", "track": "t2", "delta_db": -1}])
    song.tracks[2].mixer_device.volume.value = 0.5     # the user moves the fader
    ex.refresh()
    rr = ex.apply([{"type": "volume", "track": "t2", "delta_db": -1}])
    assert ("t2", "mixer", "Volume") in ex.locks and rr.skipped and not rr.changes


def test_limiter_and_bass_mono_on_master():
    song, ex = make()
    ex.apply([{"type": "limiter", "delta_db": 1.5}, {"type": "width", "track": "m", "bass_mono": True}])
    devs = {d.class_name: d for d in song.master_track.devices}
    assert abs(devs["Limiter"].p("Gain").value - 1.5) < 0.06 and abs(devs["Limiter"].p("Ceiling").value + 1) < 0.06
    assert devs["StereoGain"].p("Bass Mono").value == 1


def test_automated_parameters_are_left_alone():
    from mixmatch.actions import Executor
    from mixmatch.sim import demo_project

    song, hub, env, live = demo_project()
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    next(p for p in ex.track("t0")["mixer"] if p["name"] == "Volume")["automated"] = True
    rr = ex.apply([{"type": "volume", "track": "t0", "delta_db": -1.0, "reason": "test"}])
    assert not rr.changes and "automated" in rr.skipped[0]["error"]


def test_live_12_parameter_names():
    from mixmatch.actions import _aliases

    assert _aliases("3 Resonance A") == ["3 Resonance A", "3 Q A"]
    assert _aliases("Gain") == ["Gain", "Input Gain"]


def test_limiter_gain_is_capped():
    from mixmatch.actions import LIMITER_MAX_GAIN_DB, Executor
    from mixmatch.sim import demo_project

    song, hub, env, live = demo_project()
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    for _ in range(6):
        ex.apply([{"type": "limiter", "delta_db": 2.0, "reason": "test"}])
        ex.refresh()
    assert abs(ex.limiter_gain() - LIMITER_MAX_GAIN_DB) < 0.1


def test_undo_with_live_12_names_does_not_lock():
    from mixmatch.actions import Executor
    from mixmatch.sim import demo_project

    song, hub, env, live = demo_project()
    live.load_device("t0", "EQ Eight")
    for prm in song.tracks[0].devices[-1].parameters:   # rename like Live 12 does
        prm.name = prm.name.replace("Resonance A", "Q A")
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    rr = ex.apply([{"type": "eq", "track": "t0", "freq_hz": 300, "gain_db": -2, "q": 1.5, "shape": "bell",
                    "reason": "test"}])
    assert any(c.param.endswith("Q A") for c in rr.changes)
    ex.undo(rr)
    ex.refresh()
    assert not ex.locks


def test_neutral_eq_bands_count_as_free():
    """Live 12 opens EQ Eight with bands 1–4 switched on at 0 dB; they must not block new cuts."""
    from mixmatch.actions import Executor
    from mixmatch.sim import demo_project

    song, hub, env, live = demo_project()
    live.load_device("t0", "EQ Eight")
    eq = song.tracks[0].devices[-1]
    for n in range(1, 9):                       # all eight on: 1–4 neutral bells, 5–8 real cuts
        eq.p(f"{n} Filter On A").value = 1
        eq.p(f"{n} Gain A").value = 0.0 if n <= 4 else -2.0
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    rr = ex.apply([{"type": "eq", "track": "t0", "freq_hz": 3000, "gain_db": -1.5, "q": 1.0, "shape": "bell",
                    "reason": "test"}])
    assert rr.changes and not rr.skipped


def test_installed_plugins_are_loaded_from_the_plugin_browser():
    from mixmatch.actions import Executor
    from mixmatch.sim import FACTORIES, Item, demo_project, limiter

    song, hub, env, live = demo_project()
    live.cmd.browser.plugins = Item("Plug-Ins", [Item("VST3", [Item("FabFilter", [Item("FabFilter Pro-C 2")])])])
    FACTORIES["fabfilter pro-c 2"] = lambda: limiter()   # any device stands in for the plugin
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    assert ex.plugin_named("Pro-C 2") == "FabFilter Pro-C 2"
    rr = ex.apply([{"type": "load_device", "track": "t0", "name": "Pro-C 2", "reason": "test"}])
    assert not rr.skipped and song.tracks[0].devices[-1].class_name == "Limiter"


def _with_plugins(live, names):
    from mixmatch.sim import Item

    live.cmd.browser.plugins = Item("Plug-Ins", [Item("VST3", [Item("FabFilter", [Item(n) for n in names])])])


def test_limiter_prefers_pro_l2_when_installed():
    from mixmatch.actions import Executor
    from mixmatch.sim import FACTORIES, Device, Param, demo_project

    def pro_l2():
        return Device("Pro-L 2", "PluginDevice", [
            Param("Device On", 0, 1, 1, str, True, ["Off", "On"]),
            Param("Gain", 0, 30, 0, lambda v: f"{v:+.2f} dB"),
            Param("Output Level", -30, 0, 0, lambda v: f"{v:.2f} dBTP"),
        ])

    song, hub, env, live = demo_project()
    _with_plugins(live, ["Pro-L 2"])
    FACTORIES["pro-l 2"] = pro_l2
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    rr = ex.apply([{"type": "limiter", "delta_db": 2.0, "reason": "test"}])
    assert not rr.skipped
    lim = next(d for d in song.master_track.devices if d.name == "Pro-L 2")
    assert abs(lim.p("Gain").value - 2.0) < 0.05 and abs(lim.p("Output Level").value + 1.0) < 0.05
    assert abs(ex.limiter_gain() - 2.0) < 0.05


def test_a_plugin_without_parameters_is_removed_again():
    from mixmatch.actions import Executor
    from mixmatch.sim import FACTORIES, Device, Param, demo_project

    song, hub, env, live = demo_project()
    _with_plugins(live, ["Pro-Q 3"])
    FACTORIES["pro-q 3"] = lambda: Device("Pro-Q 3", "PluginDevice", [Param("Device On", 0, 1, 1, str, True, ["Off", "On"])])
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    before = len(song.tracks[0].devices)
    rr = ex.apply([{"type": "load_device", "track": "t0", "name": "Pro-Q 3", "reason": "test"}])
    assert "no parameters" in rr.skipped[0]["error"] and len(song.tracks[0].devices) == before


def test_clipper_goes_in_front_of_the_limiter_and_is_capped():
    from mixmatch.actions import CLIPPER_MAX_GAIN_DB, Executor
    from mixmatch.sim import FACTORIES, Device, Param, demo_project

    FACTORIES["classic clipper"] = lambda: Device("Classic Clipper", "PluginDevice", [
        Param("Device On", 0, 1, 1, str, True, ["Off", "On"]),
        Param("Gain", -12, 12, 0, lambda v: f"{v:.1f} dB"),
        Param("Output", -12, 12, 0, lambda v: f"{v:.1f} dB"),
    ])
    song, hub, env, live = demo_project()
    _with_plugins(live, ["Classic Clipper"])
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    ex.apply([{"type": "limiter", "delta_db": 2.0, "reason": "test"}])
    ex.refresh()
    for _ in range(4):
        rr = ex.apply([{"type": "clipper", "delta_db": 1.5, "reason": "test"}])
        assert not rr.skipped, rr.skipped
        ex.refresh()
    names = [d.name for d in song.master_track.devices]
    assert names.index("Classic Clipper") < names.index("Limiter")
    assert abs(ex.clipper_gain() - CLIPPER_MAX_GAIN_DB) < 0.1


def test_send_starts_from_off_and_never_passes_0_db():
    from mixmatch.actions import Executor
    from mixmatch.sim import Param, _db_fmt, _vol_db, demo_project

    song, hub, env, live = demo_project()
    song.tracks[0].mixer_device.sends = [Param("Send A", 0, 1, 0.0, lambda v: _db_fmt(_vol_db(v)))]
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    rr = ex.apply([{"type": "send", "track": "t0", "name": "A", "delta_db": 3.0, "reason": "test"}])
    assert not rr.skipped, rr.skipped
    assert abs(rr.changes[0].new["value"] - song.tracks[0].mixer_device.sends[0].value) < 1e-9
    assert -28.5 < float(rr.changes[0].new["display"].split()[0]) < -26.5        # -30 dB start + 3 dB


def test_neutral_device_detection():
    from mixmatch.actions import neutral_device

    def eq(**bands):
        params = [{"name": "Output", "display": "0.00 dB"}]
        for n in range(1, 9):
            on, kind, gain = bands.get(str(n), ("Off", "Bell", "0.00 dB"))
            params += [{"name": f"{n} Filter On A", "display": on}, {"name": f"{n} Filter Type A", "display": kind},
                       {"name": f"{n} Gain A", "display": gain}]
        return {"class_name": "Eq8", "params": params}

    assert neutral_device(eq(**{"1": ("On", "Low Shelf", "0.00 dB"), "2": ("On", "Bell", "0.00 dB")}))
    assert not neutral_device(eq(**{"3": ("On", "Bell", "-2.00 dB")}))
    assert not neutral_device(eq(**{"5": ("On", "High Pass 12dB", "0.00 dB")}))     # a cut acts even at 0 dB

    def utility(**over):
        P = {"Left Inv": "Off", "Right Inv": "Off", "Channel Mode": "Stereo", "Stereo Width": "100 %", "Mono": "Off",
             "Bass Mono": "Off", "Balance": "C", "Output": "0.00 dB", "Mute": "Off", "DC Filter": "Off", **over}
        return {"class_name": "StereoGain", "params": [{"name": k, "display": v} for k, v in P.items()]}

    assert neutral_device(utility())
    assert not neutral_device(utility(**{"Bass Mono": "On"}))
    assert not neutral_device(utility(**{"Stereo Width": "80 %"}))


def test_master_eq_cuts_at_most_three_db_in_sum():
    import pytest

    from mixmatch.actions import Executor
    from mixmatch.sim import demo_project

    song, hub, env, live = demo_project()
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    shelf = lambda f, g: {"type": "eq", "track": "m", "freq_hz": f, "gain_db": g, "q": 0.71, "shape": "high_shelf",
                          "reason": "t"}
    ex.apply([shelf(3000, -2.0)]); ex.refresh()
    rr = ex.apply([shelf(8000, -2.0)])                     # together about -4 dB up top: refused
    assert not rr.changes and any("master EQ would cut" in s for s in map(str, rr.skipped))
    rr = ex.apply([shelf(8000, 1.0)])                      # a boost is always fine
    assert rr.changes
