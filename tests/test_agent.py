import json
import pytest

from mixmatch.agent import Agent, Settings
from mixmatch.planner import ACTION_SCHEMA, ClaudePlanner, RulePlanner
from mixmatch.sim import demo_project


def run_demo(planner, minutes=3, engine="solver"):
    song, hub, env, live = demo_project()
    env.wait(5)
    logs = []
    agent = Agent(live, hub, planner, Settings(minutes=minutes, settle=1.0, engine=engine), env=env, log=logs.append)
    history = agent.run()
    return agent, history, logs


def test_loop_improves_the_simulated_mix_and_undoes_bad_rounds():
    agent, history, logs = run_demo(RulePlanner())
    kept = [h for h in history if not h.undone and h.after is not None]
    assert history and kept
    assert min(h.after for h in kept) < history[0].before * 0.6
    assert not agent.ex.locks
    roles = {r.name: r.role for r in agent.roles.values()}
    assert roles["Kick"] == "kick" and roles["Bass"] == "bass" and roles["Lead Vox"] == "lead_vocal"
    assert any("Mode: song" in l for l in logs)  # there is a vocal channel


def test_a_project_without_vocals_runs_in_beat_mode():
    song, hub, env, live = demo_project()
    song.tracks = [t for t in song.tracks if t.name != "Lead Vox"]
    for i, t in enumerate(song.tracks):
        hub.on_name(i + 1, "track", t.name)
    env.wait(5)
    logs = []
    agent = Agent(live, hub, RulePlanner(), Settings(minutes=0.5, settle=1.0), env=env, log=logs.append)
    agent.understand()
    assert agent.beat and any("beat" in l for l in logs)


@pytest.mark.parametrize("muted_parent", [False, True])
def test_muted_vocals_do_not_select_song_mode_or_get_channel_work(tmp_path, muted_parent):
    song, hub, env, live = demo_project()
    vocal = next(t for t in song.tracks if t.name == "Lead Vox")
    if muted_parent:
        parent = next(t for t in song.tracks if t.name == "Pad")
        parent.is_foldable, parent.mute = True, True
        vocal.is_grouped, vocal.group_track = True, parent
    else:
        vocal.mute = True
    env.wait(2)
    stub = ChannelStub()
    agent = Agent(live, hub, stub, Settings(minutes=None, complete=True, settle=1,
                  roles_file=str(tmp_path / "roles.json"), log_dir=str(tmp_path)), env=env, log=lambda *_: None)
    agent.run()
    key = next(k for k, r in agent.roles.items() if r.name == "Lead Vox")
    assert agent.roles[key].role == "lead_vocal"  # identity stays, audibility changes the mode
    assert agent.beat and not agent.audible_vocals()
    assert key not in stub.tasks


@pytest.mark.parametrize("mode,muted,expected", [("auto", True, True), ("auto", False, False),
                                                ("song", True, False), ("beat", False, True),
                                                ("instrumental", True, False), ("instrumental", False, False)])
def test_resume_rechecks_vocal_mutes_and_respects_explicit_mode(tmp_path, mode, muted, expected):
    song, hub, env, live = demo_project()
    next(t for t in song.tracks if t.name == "Lead Vox").mute = muted
    env.wait(2)
    agent = Agent(live, hub, RulePlanner(), Settings(mode=mode, roles_file=str(tmp_path / "roles.json")),
                  env=env, log=lambda *_: None)
    agent.understand()
    agent._resume_from({"regions": {}, "phase": "channels", "beat": True, "silent_vocals": []})
    assert agent.beat is expected


def test_instrumental_keeps_full_reference_balance_even_with_silent_vocal_tracks(tmp_path):
    import numpy as np
    from mixmatch.analysis import Stats
    from mixmatch.reference import Reference, ReferenceSet
    song, hub, env, live = demo_project()
    next(t for t in song.tracks if t.name == 'Lead Vox').mute = True
    env.wait(2)
    refs = ReferenceSet.__new__(ReferenceSet)
    refs.refs = [Reference('test.wav', full=Stats(80, -12, np.full(31, -30.),
                            np.full(31, -18.), -2, 10, -10, -30, 1), seconds=200)]
    logs = []
    agent = Agent(live, hub, RulePlanner(), Settings(mode='instrumental', settle=1,
                  roles_file=str(tmp_path / 'roles.json')), references=refs, env=env, log=logs.append)
    agent.understand()
    assert not agent.beat and agent.target.offset is None
    agent.play_loop(0, 16)
    m = agent.measure()
    assert agent.target_no_vocals is None
    assert 'no space reserved for future vocals' in agent.context('master', m)['mode']
    assert any(line.startswith('Mode: instrumental') for line in logs)
    key = agent.quality_key(m, 'full')
    agent.s.mode = 'song'
    assert agent.quality_key(m, 'full') != key
    agent.measure()
    assert agent.target_no_vocals.offset is not None  # song still protects a silent vocal passage
    assert refs.target(True).offset is not None      # beat still reserves space for future vocals


def test_corrected_roles_are_revisited_and_the_queue_survives_another_stop(tmp_path, monkeypatch):
    song, hub, env, live = demo_project()
    env.wait(2)
    roles_path = tmp_path / "roles.json"
    roles_path.write_text(json.dumps({"Kick": {"role": "drums_bus", "source": "auto"}}))
    settings = Settings(resume="auto", roles_file=str(roles_path), log_dir=str(tmp_path))
    agent = Agent(live, hub, RulePlanner(), settings, env=env, log=lambda *_: None)
    agent.understand()
    kick = next(k for k, r in agent.roles.items() if r.name == "Kick")
    pad = next(k for k, r in agent.roles.items() if r.name == "Pad")
    assert agent.role_reviews == {kick}
    agent.regions = {k: (0, 16) for k in agent.roles}
    agent._ck = {"main": ["drop", 0, 16], "regions": agent.regions}
    monkeypatch.setattr(agent, "play_loop", lambda *args: None)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(agent, "section", interrupt)
    with pytest.raises(KeyboardInterrupt):
        agent.channel_pass(("drop", 0, 16), float("inf"), "Pad")
    ck = agent.load_checkpoint()
    assert ck["pending_channels"] == [kick, pad]
    assert ck["next_channel"] == "Kick"

    resumed = Agent(live, hub, RulePlanner(), settings, env=env, log=lambda *_: None)
    resumed.understand()  # roles.json now has the corrected identity
    assert not resumed.role_reviews
    resumed._resume_from(ck)
    visited = []
    monkeypatch.setattr(resumed, "play_loop", lambda *args: None)
    monkeypatch.setattr(resumed, "section", lambda *args, **kwargs: visited.append(kwargs["focus"]))
    resumed.channel_pass(("drop", 0, 16), float("inf"), ck["next_channel"])
    assert visited == [kick, pad]  # no unrelated already-processed channels are repeated
    assert not resumed.pending_channels and not resumed.role_reviews


class FakeClaude:
    """Stands in for the API: checks what is sent, answers like Claude would."""

    def __init__(self):
        self.calls = []
        self.beta = self
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        ctx = json.loads(kw["messages"][0]["content"])
        bass = next(c["track"] for c in ctx["channels"] if c["role"] == "bass")
        text = json.dumps({"assessment": "bass masks the kick", "done": len(self.calls) > 3, "actions": [
            {"type": "eq", "track": bass, "delta_db": None, "value": None, "freq_hz": 63, "gain_db": -2, "q": 1.4,
             "shape": "bell", "width_pct": None, "bass_mono": None, "device_index": None, "param": None,
             "target": None, "name": None, "reason": "kick and bass share 63 Hz"}]})

        class Block:
            type = "text"

        class Resp:
            stop_reason = "end_turn"
            content = [Block()]

        Resp.content[0].text = text
        return Resp()


def test_claude_planner_request_and_loop(monkeypatch):
    planner = ClaudePlanner.__new__(ClaudePlanner)
    planner.client, planner.model, planner.effort = FakeClaude(), "claude-opus-5-5", "medium"
    agent, history, logs = run_demo(planner, minutes=2, engine="rounds")
    call = planner.client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["output_config"]["format"]["schema"] is ACTION_SCHEMA
    ctx = json.loads(call["messages"][0]["content"])
    assert {"channels", "masking", "master_vs_reference", "history", "mode"} <= set(ctx)
    assert history and history[0].applied


def test_haiku_is_the_default_and_gets_no_fallback():
    from mixmatch.planner import MODEL

    assert MODEL == "claude-haiku-5-5"
    planner = ClaudePlanner.__new__(ClaudePlanner)
    planner.client, planner.model, planner.effort = FakeClaude(), MODEL, "medium"
    run_demo(planner, minutes=1, engine="rounds")
    call = planner.client.calls[0]
    assert call["model"] == "claude-haiku-5-5" and "fallbacks" not in call and "betas" not in call


def test_loudest_window_finds_the_drop():
    from mixmatch.agent import loudest_window
    from mixmatch.taps import Frame

    positioned = []
    for i in range(640):                           # 64 beats, 10 frames per beat
        beat = i / 10
        level = 0.5 if 32 <= beat < 48 else 0.05   # beats 32–48 are loud
        positioned.append((beat, Frame(i, level, level / 4, 0.0, level * 2, [level / 5] * 31)))
    offset, loudness = loudest_window(positioned, total_beats=64, length_beats=16)
    assert offset == 32
    assert loudness > -20


def test_loud_focus_uses_the_real_song_position():
    song, hub, env, live = demo_project()
    song.cue_points = []
    song.loop, song.loop_start, song.loop_length = True, 0.0, 32.0
    env.wait(2)
    agent = Agent(live, hub, RulePlanner(), Settings(focus_bars=4, settle=1.0), env=env, log=lambda *_: None)
    agent.ex.refresh()
    name, start, length = agent.loudest_part(agent.sections())
    assert name == "loudest part" and length == 16 and 0 <= start <= 16


class ChannelStub:
    """Master phase: nothing to do. Channel pass: a small cut on the focus track plus a stray action elsewhere."""

    def __init__(self):
        self.tasks = []

    def plan(self, ctx):
        if "task" not in ctx:
            return {"assessment": "master fine", "done": True, "actions": []}
        focus = ctx["channels"][0]["track"]
        self.tasks.append(focus)
        if self.tasks.count(focus) > 1:
            return {"assessment": "sits well", "done": True, "actions": []}
        return {"assessment": "trim", "done": False, "actions": [
            {"type": "eq", "track": focus, "freq_hz": 250, "gain_db": -1.0, "q": 1.0, "shape": "bell", "reason": "mud"},
            {"type": "volume", "track": "m" if focus != "m" else "t0", "delta_db": 1.0, "reason": "stray"},
        ]}


def test_complete_run_visits_every_channel_and_ends_with_a_final_check(tmp_path, monkeypatch):
    from mixmatch import workflow
    updates = []
    monkeypatch.setattr(workflow, "update", lambda **values: updates.append(values))
    song, hub, env, live = demo_project()
    env.wait(2)
    logs, stub = [], ChannelStub()
    agent = Agent(live, hub, stub, Settings(engine="rounds", minutes=None, complete=True, focus="loud", settle=1.0,
                                            roles_file=str(tmp_path / "roles.json"), log_dir=str(tmp_path)),
                  env=env, log=logs.append)
    agent.run()
    assert set(stub.tasks) == set(agent.roles)                       # every channel got its turn
    assert any("Final check" in l for l in logs)
    assert any("not now" in l for l in logs)                         # stray actions are dropped in the pass
    channel_rounds = [h for h in agent.history if h.section.startswith("channel ")]
    assert channel_rounds and all(a["track"] in agent.roles for h in channel_rounds for a in h.applied)
    report = next(u['quality_check'] for u in updates if 'quality_check' in u)
    assert report['schema'] == 1 and report['samples'] >= 2
    assert any(a.get('before_mix') and a['samples'] >= 2 for a in agent.quality_anchors.values())
    assert not report['listening_judgment']


def test_measurements_start_at_the_loop_start():
    song, hub, env, live = demo_project()
    agent = Agent(live, hub, RulePlanner(), Settings(settle=1.0), env=env, log=lambda *_: None)
    agent.ex.refresh()
    agent.play_loop(16.0, 32.0)
    env.wait(3.3)                                   # somewhere inside the loop
    agent.wait_for_loop_start()
    assert abs(song.current_song_time - 16.0) < 0.5


def test_a_vocal_track_that_never_plays_means_beat_mode(tmp_path):
    song, hub, env, live = demo_project()
    vox = next(t for t in song.tracks if t.name == "Lead Vox")
    vox.mixer_device.volume.value = 0.0             # fader all the way down: silent everywhere
    vox.spectrum = vox.spectrum - 200
    env.wait(2)
    logs = []
    agent = Agent(live, hub, ChannelStub(), Settings(minutes=None, complete=True, focus="loud", settle=1.0,
                                                     roles_file=str(tmp_path / "r.json"), log_dir=str(tmp_path)),
                  env=env, log=logs.append)
    agent.run()
    assert agent.beat and any("treating it as a beat" in l for l in logs)


def test_a_failing_planner_stops_the_run_cleanly(tmp_path):
    class Broke:
        def plan(self, ctx):
            raise RuntimeError("Your credit balance is too low")

    song, hub, env, live = demo_project()
    env.wait(2)
    logs = []
    agent = Agent(live, hub, Broke(), Settings(engine="rounds", minutes=1, settle=1.0, roles_file=str(tmp_path / "r.json"),
                                               log_dir=str(tmp_path)), env=env, log=logs.append)
    agent.run()
    assert any("planner failed" in l and "credit" in l for l in logs) and any(l.startswith("\nDone.") for l in logs)


def test_resume_skips_the_master_and_earlier_channels(tmp_path):
    song, hub, env, live = demo_project()
    env.wait(2)
    logs, stub = [], ChannelStub()
    agent = Agent(live, hub, stub, Settings(minutes=None, complete=True, focus="loud", settle=1.0, resume="Pad",
                                            roles_file=str(tmp_path / "r.json"), log_dir=str(tmp_path)),
                  env=env, log=logs.append)
    agent.run()
    names = {agent.roles[k].name for k in stub.tasks}
    assert "Pad" in names and "Kick" not in names                # Pad has the lowest priority: it is last
    assert not any("Master against the reference" in l for l in logs)
    assert any("Final check" in l for l in logs)


def test_a_group_does_not_mask_its_own_tracks():
    song, hub, env, live = demo_project()
    group, member = song.tracks[0], song.tracks[1]   # Kick acts as the group, Bass sits inside it
    group.is_foldable = True
    member.is_grouped, member.group_track = True, group
    env.wait(5)
    agent = Agent(live, hub, RulePlanner(), Settings(settle=1.0), env=env, log=lambda *_: None)
    agent.understand()
    keys = {t["name"]: t["key"] for t in agent.ex.set_state["tracks"]}
    assert keys[member.name] in agent._group_members()[keys[group.name]]
    m = agent.measure()
    pairs = {frozenset((x["track_a"], x["track_b"])) for x in m["masking"]}
    assert frozenset((keys[group.name], keys[member.name])) not in pairs


def test_a_silent_master_stops_the_run_instead_of_looking_perfect(tmp_path, monkeypatch):
    from mixmatch import sim

    song, hub, env, live = demo_project()
    env.wait(2)
    real = sim.master_output
    def silent(song_, refs):                             # audio engine off: the master tap reports silence
        spec, *rest = real(song_, refs)
        return (spec - 300, *rest)
    monkeypatch.setattr(sim, "master_output", silent)
    logs, stub = [], ChannelStub()
    agent = Agent(live, hub, stub, Settings(minutes=None, settle=1.0, roles_file=str(tmp_path / "r.json"),
                                            log_dir=str(tmp_path)), env=env, log=logs.append)
    agent.run()
    assert any("master stayed silent" in l for l in logs)
    assert not any(h.after == 0 for h in agent.history)


def test_claude_code_planner_uses_the_subscription_and_picks_the_model(monkeypatch):
    import subprocess

    from mixmatch.planner import ACTION_SCHEMA, ClaudeCodePlanner

    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        out = {"is_error": False, "structured_output": {"assessment": "ok", "done": True, "actions": []}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    p = ClaudeCodePlanner(binary="claude")
    assert p.plan({"objective": 1.0}) == {"assessment": "ok", "done": True, "actions": []}
    assert p.plan({"objective": 1.0, "task": "Channel pass on t1"})["done"]
    (master_cmd, kw), (channel_cmd, _) = calls
    assert master_cmd[master_cmd.index("--model") + 1] == "opus"
    assert channel_cmd[channel_cmd.index("--model") + 1] == "haiku"
    assert "ANTHROPIC_API_KEY" not in kw["env"]                     # the subscription, not the API key
    assert master_cmd[master_cmd.index("--tools") + 1] == "" and "--setting-sources" in master_cmd
    assert json.loads(master_cmd[master_cmd.index("--json-schema") + 1]) == ACTION_SCHEMA


def test_claude_code_planner_reports_a_missing_login(monkeypatch):
    import subprocess

    import pytest

    from mixmatch.planner import ClaudeCodePlanner

    out = json.dumps({"is_error": True, "result": "Not logged in · Please run /login"})
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, ""))
    with pytest.raises(RuntimeError, match="Not logged in"):
        ClaudeCodePlanner(binary="claude").plan({"objective": 1.0})


def test_effect_audit_bypasses_neutral_and_harmful_effects_reversibly(tmp_path):
    song, hub, env, live = demo_project()
    from mixmatch.actions import Executor

    live.load_device("t0", "EQ Eight")                       # neutral: all bands off
    ex = Executor(live, log=lambda *_: None)
    ex.refresh()                                              # a real cut where the bass sits
    ex.apply([{"type": "eq", "track": "t1", "freq_hz": 80, "gain_db": -3.0, "q": 1.0, "shape": "bell", "reason": "t"}])
    ex.refresh()
    ex.apply([{"type": "eq", "track": "t1", "freq_hz": 80, "gain_db": -3.0, "q": 1.0, "shape": "bell", "reason": "t"}])
    env.wait(2)
    logs = []
    class Idle:  # changes nothing, so only the audit touches the devices
        def plan(self, ctx):
            return {"assessment": "fine", "done": True, "actions": []}

    agent = Agent(live, hub, Idle(), Settings(engine="rounds", audit=True, minutes=None, complete=True, focus="loud", settle=1.0,
                                              roles_file=str(tmp_path / "r.json"), log_dir=str(tmp_path)),
                  env=env, log=logs.append)
    agent.run()
    assert next(d for d in song.tracks[0].devices if d.class_name == "Eq8").p("Device On").value == 0
    bass_eq = next(d for d in song.tracks[1].devices if d.class_name == "Eq8")
    assert bass_eq.p("Device On").value == 0                                       # bypassed, not deleted
    assert any("bypassed EQ Eight on Kick" in l for l in logs)
    assert any("bypassed EQ Eight on Bass: the mix is better without it" in l for l in logs)


def test_fallback_planner_switches_to_the_api_at_a_usage_limit():
    import pytest

    from mixmatch.planner import FallbackPlanner

    class Code:
        model = "code"

        def __init__(self, error):
            self.error, self.calls = error, 0

        def plan(self, ctx):
            self.calls += 1
            raise RuntimeError(self.error)

    class Api:
        model = "claude-haiku-5-5"

        def plan(self, ctx):
            return {"assessment": "api", "done": True, "actions": []}

    code, logs = Code("claude -p (opus): You've hit your session limit · resets 7:50pm"), []
    p = FallbackPlanner(code, Api(), log=logs.append)
    assert p.plan({})["assessment"] == "api" and p.plan({})["assessment"] == "api"
    assert code.calls == 1 and any("continuing with the API" in l for l in logs)      # switched once, for good
    with pytest.raises(RuntimeError):                                                    # other errors still stop
        FallbackPlanner(Code("Not logged in"), Api(), log=lambda *_: None).plan({})


class TinyPlanner:
    """Always proposes a change too small to measure."""

    def plan(self, ctx):
        return {"assessment": "nudge", "done": False, "actions": [
            {"type": "volume", "track": "t5", "delta_db": 0.01, "reason": "tiny nudge"}]}


def test_a_round_without_measurable_effect_is_undone():
    song, hub, env, live = demo_project()
    env.jitter = 0.0  # isolate the tiny move from the simulated measurement noise
    env.wait(5)
    logs = []
    agent = Agent(live, hub, TinyPlanner(), Settings(minutes=1, settle=1.0, fine=False), env=env, log=logs.append)
    agent.understand()
    before = live.get_set()
    vol = lambda s: next(p["value"] for t in s["tracks"] if t["key"] == "t5" for p in t["mixer"] if p["name"] == "Volume")
    agent.section("test", env.now() + 600, max_rounds=1)
    assert agent.history and agent.history[-1].undone
    assert "no measurable improvement" in agent.history[-1].plan["verification"]
    assert abs(vol(live.get_set()) - vol(before)) < 1e-9


def test_without_a_reference_a_stereo_low_end_still_counts():
    song, hub, env, live = demo_project()
    song.tracks = [t for t in song.tracks if not t.name.startswith("REF")]
    for i, t in enumerate(song.tracks):
        hub.on_name(i + 1, "track", t.name)
    env.wait(5)
    agent = Agent(live, hub, RulePlanner(), Settings(minutes=0.5, settle=1.0), env=env, log=lambda *a: None)
    agent.understand()
    m = agent.measure("balance")
    assert m["cmp"] is None and m["parts"]["low_side"] > 0          # the demo bass is stereo down low



def test_without_a_reference_the_audit_keeps_a_deliberate_eq():
    """Like the live set: no reference, and the masking happens to be lower with the EQ off."""
    song, hub, env, live = demo_project()
    from mixmatch.actions import Executor

    ex = Executor(live, log=lambda *_: None)
    ex.refresh()
    ex.apply([{"type": "eq", "track": "t1", "freq_hz": 80, "gain_db": -3.0, "q": 1.0, "shape": "bell", "reason": "t"}])
    env.wait(5)
    logs = []
    agent = Agent(live, hub, RulePlanner(), Settings(minutes=1, settle=1.0), env=env, log=logs.append)
    agent.understand()
    real, calls = agent.measure, []

    def masking_only(scoring="full", retries=6):
        # The UI must know what is being checked before a measurement starts.
        import json
        focus = json.loads(next(line[6:] for line in reversed(logs) if line.startswith("focus ")))
        assert focus == {"channel": "Bass", "device": "EQ Eight", "start_bar": 1.0, "end_bar": 5.0}
        m = real(scoring, retries)
        calls.append(1)
        return {**m, "cmp": None, "objective": 2.35 if len(calls) % 2 else 2.22}

    agent.measure = masking_only
    agent.audit_one((0.0, 16.0), "t1", "Bass", "EQ Eight", "eq8")
    bass_eq = next(d for d in song.tracks[1].devices if d.class_name == "Eq8")
    assert bass_eq.p("Device On").value == 1
    assert not any("better without it" in l for l in logs)


def test_silent_mode_turns_the_main_fader_down_and_back():
    song, hub, env, live = demo_project()
    env.wait(5)
    vol = lambda: next(p["value"] for t in live.get_set()["tracks"] if t["key"] == "m"
                       for p in t["mixer"] if p["name"] == "Volume")
    before, during = vol(), []

    class Watch(RulePlanner):
        def plan(self, ctx):
            during.append(vol())
            return super().plan(ctx)

    logs = []
    agent = Agent(live, hub, Watch(), Settings(engine="rounds", minutes=1, settle=1.0, silent=True), env=env, log=logs.append)
    history = agent.run()
    assert during and all(v == 0.0 for v in during)       # down while it worked …
    assert vol() == before                                  # … and back afterwards
    assert any(h.after is not None and not h.undone for h in history)   # it still measured and improved


def test_resumed_silent_run_keeps_original_gain_in_after_snapshot(tmp_path, monkeypatch):
    from mixmatch import ab
    from pathlib import Path

    song, hub, env, live = demo_project()
    env.wait(5)
    original = song.master_track.mixer_device.volume.value
    first = ab.save(live.get_set(), str(tmp_path), "20000101-000000", "before")
    live.set("m", "mixer", "Volume", value=0)
    agent = Agent(live, hub, RulePlanner(), Settings(silent=True, resume="auto", log_dir=str(tmp_path)),
                  env=env, log=lambda *_: None)
    monkeypatch.setattr(agent, "load_checkpoint", lambda: {"before": Path(first).name})
    monkeypatch.setattr(agent, "_run", lambda deadline: None)
    agent.run(main_volume=0.0)
    before, after = ab.latest_pair(str(tmp_path))
    assert json.loads(Path(before).read_text())["tracks"]["m"]["mixer"]["Volume"] == original
    assert json.loads(Path(after).read_text())["tracks"]["m"]["mixer"]["Volume"] == original
    assert song.master_track.mixer_device.volume.value == 0  # no unintended unmute


@pytest.mark.parametrize("signal", ["tail", "never_quiet", "missing"])
def test_silent_stop_waits_for_effect_tails_and_does_not_guess_silence(signal):
    song, hub, env, live = demo_project()
    agent = Agent(live, hub, RulePlanner(), Settings(silent=True), env=env, log=lambda *_: None)
    agent.ex.refresh()
    original = agent.mute_main()
    started, wait = env.now(), env.wait

    def decay(seconds):
        if env.now() - started < 6.0:                     # muted while waiting for the tails
            assert song.master_track.mixer_device.volume.value == 0
        wait(seconds)
        tap = next(t for t in hub.snapshot() if t.kind == "master")
        if signal == "missing":
            tap.frames.clear()
        elif signal == "never_quiet" or env.now() - started < 2 or 2.5 < env.now() - started < 2.9:
            tap.frames[-1].peak = 0.1  # a delayed echo interrupts an otherwise quiet interval

    env.wait = decay
    restored = agent.restore_main_when_quiet(original)
    assert not song.is_playing
    if signal == "tail":
        assert restored and env.now() - started >= 4.0
        assert song.master_track.mixer_device.volume.value == original
    else:   # tails that never settle (or no measurement): faded back in after the wait, never left muted
        assert restored and env.now() - started >= 6.0
        assert song.master_track.mixer_device.volume.value == pytest.approx(original)


def test_progress_rises_round_by_round_and_ends_at_the_total(tmp_path):
    song, hub, env, live = demo_project()
    env.wait(5)
    logs = []
    agent = Agent(live, hub, RulePlanner(), Settings(minutes=None, complete=True, focus="loud", settle=1.0,
                                                     roles_file=str(tmp_path / "r.json"), log_dir=str(tmp_path)),
                  env=env, log=logs.append)
    agent.run()
    points = [json.loads(l.split("progress ", 1)[1]) for l in logs if l.strip().startswith("progress {")]
    done = [p["done"] for p in points]
    assert done == sorted(done) and len(set(done)) > 6          # it moves often, never back
    assert abs(points[-1]["done"] - points[-1]["total"]) < 0.01  # 100% at the end


def test_a_network_outage_is_waited_out():
    song, hub, env, live = demo_project()
    env.wait(5)

    class Flaky(RulePlanner):
        calls = 0

        def plan(self, ctx):
            Flaky.calls += 1
            if Flaky.calls <= 2:
                raise RuntimeError("claude -p (haiku): API Error: Can't reach the API server (ENOTFOUND)")
            return super().plan(ctx)

    logs = []
    agent = Agent(live, hub, Flaky(), Settings(engine="rounds", minutes=1, settle=1.0), env=env, log=logs.append)
    history = agent.run()
    assert history and not any("Stopped" in l for l in logs)
    assert sum("trying again" in l for l in logs) == 2


def test_a_real_planner_error_still_stops_the_run():
    song, hub, env, live = demo_project()
    env.wait(5)

    class Broken(RulePlanner):
        def plan(self, ctx):
            raise RuntimeError("invalid schema")

    logs = []
    Agent(live, hub, Broken(), Settings(engine="rounds", minutes=1, settle=1.0), env=env, log=logs.append).run()
    assert any("Stopped: the planner failed" in l for l in logs)


def test_a_stopped_run_continues_exactly_where_it_stopped(tmp_path):
    import os

    song, hub, env, live = demo_project()
    env.wait(5)
    settings = lambda **kw: Settings(engine="rounds", minutes=None, complete=True, focus="loud", settle=1.0,
                                     roles_file=str(tmp_path / "r.json"), log_dir=str(tmp_path), **kw)

    class DiesInTheChannelPass(RulePlanner):
        def plan(self, ctx):
            if ctx.get("section") == "channel Snare":       # stop in the channel pass
                raise RuntimeError("invalid schema")
            return super().plan(ctx)

    first = []
    Agent(live, hub, DiesInTheChannelPass(), settings(), env=env, log=first.append).run()
    assert any("Stopped: the planner failed" in l for l in first)
    ck = json.load(open(tmp_path / "checkpoint.json"))
    assert ck["phase"] == "channels" and ck["next_channel"] == "Snare"
    original_before = (tmp_path / ck["before"]).read_text()
    live.batch([{"track": "t0", "device": "mixer", "param": "Volume", "value": 0.3}])   # a change before continuing

    second = []
    Agent(live, hub, RulePlanner(), settings(resume="auto"), env=env, log=second.append).run()
    text = "\n".join(second)
    assert "↻ Continuing the run" in text and "resumed at Snare" in text
    assert "Suggesting references" not in text and "Master against the reference" not in text
    assert "Final check" in text and not os.path.exists(tmp_path / "checkpoint.json")   # complete: nothing left
    first_progress = [json.loads(l.split("progress ", 1)[1]) for l in second if l.strip().startswith("progress {")]
    assert first_progress[0]["done"] == ck["done"]                                       # the bar goes on from there
    from mixmatch import ab
    assert open(ab.latest_before(str(tmp_path))).read() == original_before   # «before» is the state before the first part


def test_resume_preserves_automatically_selected_references(tmp_path):
    from conftest import write_audio
    from mixmatch.reference import ReferenceSet

    song, hub, env, live = demo_project()
    path = write_audio(str(tmp_path / "reference.wav"))
    refs = ReferenceSet([path], log=lambda *_: None)
    settings = Settings(log_dir=str(tmp_path))
    first = Agent(live, hub, RulePlanner(), settings, env=env, log=lambda *_: None, references=refs)
    first.save_checkpoint(phase="master", regions={}, beat=True)
    saved = json.loads((tmp_path / "checkpoint.json").read_text())
    resumed = Agent(live, hub, RulePlanner(), settings, env=env, log=lambda *_: None)
    resumed._resume_from(saved)
    assert resumed.refset.paths == [path]
    assert resumed.beat and resumed.target.loudness == refs.target(True).loudness
    assert resumed.target.description == refs.target(True).description


@pytest.mark.parametrize("complete", [False, True])
def test_reference_search_pauses_and_only_explicit_selection_resumes(tmp_path, monkeypatch, complete):
    from conftest import write_audio
    from mixmatch import ab
    from mixmatch.reference import ReferenceSet

    song, hub, env, live = demo_project()
    env.wait(5)
    found = ReferenceSet([write_audio(str(tmp_path / "found.wav"))], log=lambda *_: None)
    chosen = ReferenceSet([write_audio(str(tmp_path / "chosen.wav"), seed=1)], log=lambda *_: None)
    before = ab.snapshot(live.get_set())
    calls = []

    def find(agent, part):
        calls.append("search")
        agent.refset = found

    def section(agent, *args, **kwargs):
        calls.append(("mix", agent.refset.paths))

    monkeypatch.setattr(Agent, "find_references", find)
    monkeypatch.setattr(Agent, "section", section)
    settings = lambda **kw: Settings(engine="rounds", minutes=None, complete=complete, mode="song", focus="loud", settle=1,
                                    fine=False, audit=False, log_dir=str(tmp_path / "runs"), **kw)
    first = Agent(live, hub, RulePlanner(), settings(auto_ref=True, review_references=True), env=env, log=lambda *_: None)
    first.run()
    ck = first.load_checkpoint()
    assert calls == ["search"] and ck["phase"] == "references" and ck["complete"] == complete
    assert not ab.changes_to(before, live.get_set(), bypass_new=True)
    assert ab.snapshot(live.get_set())["tracks"] == before["tracks"]

    # Continue without a new choice must not silently reuse the search result.
    waiting = Agent(live, hub, RulePlanner(), settings(resume="auto"), env=env, log=lambda *_: None)
    waiting.run()
    assert calls == ["search"] and waiting.load_checkpoint()["phase"] == "references"

    monkeypatch.setattr(Agent, "scan", lambda *a: pytest.fail("saved scan must be reused"))
    resumed = Agent(live, hub, RulePlanner(), settings(resume="auto", replace_references=True),
                    references=chosen, env=env, log=lambda *_: None)
    resumed.run()
    assert calls[1:] and all(c == ("mix", chosen.paths) for c in calls[1:])
    assert resumed.load_checkpoint() is None


def test_a_channel_outside_the_loudest_part_is_not_compared_with_the_references():
    song, hub, env, live = demo_project()
    env.wait(5)
    agent = Agent(live, hub, RulePlanner(), Settings(minutes=1, settle=1.0), env=env, log=lambda *_: None)
    agent.understand()
    m = agent.measure("local")
    assert m["cmp"] is None and set(m["parts"]) <= {"masking", "low_side", "clipping"}
    assert m["parts"]["clipping"] == pytest.approx(max(0, m["master"].peak_db + 1) * 4)
    ctx = agent.context("channel Bass", m, focus="t1")
    assert "outside the song's loudest part" in ctx["scope"] and ctx["master_vs_reference"] is None


def test_effect_audit_leaves_moving_effects_alone(tmp_path):
    from mixmatch.sim import Device, Param

    song, hub, env, live = demo_project()
    phaser = Device("Phaser-Flanger", "PhaserNew", [Param("Device On", 0, 1, 1, str, True, ["Off", "On"]),
                                                     Param("Amount", 0, 1, 0.5, lambda v: f"{v * 100:.0f} %")])
    song.tracks[3].devices.append(phaser)
    env.wait(5)
    logs = []

    class Idle:
        def plan(self, ctx):
            return {"assessment": "fine", "done": True, "actions": []}

    agent = Agent(live, hub, Idle(), Settings(minutes=None, complete=True, focus="loud", settle=1.0,
                                              roles_file=str(tmp_path / "r.json"), log_dir=str(tmp_path)),
                  env=env, log=logs.append)
    agent.run()
    assert not any("Phaser-Flanger" in l and ("bypassed" in l or "kept" in l) for l in logs)


def test_a_run_without_taps_stops_at_once(tmp_path):
    song, hub, env, live = demo_project()
    env.wait(5)
    hub.by_name = lambda: {}
    lines = []
    agent = Agent(live, hub, RulePlanner(), Settings(minutes=None, complete=True, settle=1.0, log_dir=str(tmp_path),
                                                     roles_file=str(tmp_path / "r.json")), env=env, log=lines.append)
    start = env.now()
    assert agent.run() == []
    assert any("Stopped: no channel reports through a MixMatch Tap" in l for l in lines)
    assert env.now() - start < 30                      # no listening to the whole song first
