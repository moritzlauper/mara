import json

from mixmatch.agent import Agent, Settings
from mixmatch.planner import ACTION_SCHEMA, ClaudePlanner, RulePlanner
from mixmatch.sim import demo_project


def run_demo(planner, minutes=3):
    song, hub, env, live = demo_project()
    env.wait(5)
    logs = []
    agent = Agent(live, hub, planner, Settings(minutes=minutes, settle=1.0), env=env, log=logs.append)
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
    agent, history, logs = run_demo(planner, minutes=2)
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
    run_demo(planner, minutes=1)
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


def test_complete_run_visits_every_channel_and_ends_with_a_final_check(tmp_path):
    song, hub, env, live = demo_project()
    env.wait(2)
    logs, stub = [], ChannelStub()
    agent = Agent(live, hub, stub, Settings(minutes=None, complete=True, focus="loud", settle=1.0,
                                            roles_file=str(tmp_path / "roles.json"), log_dir=str(tmp_path)),
                  env=env, log=logs.append)
    agent.run()
    assert set(stub.tasks) == set(agent.roles)                       # every channel got its turn
    assert any("Final check" in l for l in logs)
    assert any("not now" in l for l in logs)                         # stray actions are dropped in the pass
    channel_rounds = [h for h in agent.history if h.section.startswith("channel ")]
    assert channel_rounds and all(a["track"] in agent.roles for h in channel_rounds for a in h.applied)


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
    agent = Agent(live, hub, Broke(), Settings(minutes=1, settle=1.0, roles_file=str(tmp_path / "r.json"),
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


def test_effect_audit_removes_a_neutral_eq_and_bypasses_a_harmful_one(tmp_path):
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

    agent = Agent(live, hub, Idle(), Settings(minutes=None, complete=True, focus="loud", settle=1.0,
                                              roles_file=str(tmp_path / "r.json"), log_dir=str(tmp_path)),
                  env=env, log=logs.append)
    agent.run()
    assert not any(d.class_name == "Eq8" for d in song.tracks[0].devices)          # removed
    bass_eq = next(d for d in song.tracks[1].devices if d.class_name == "Eq8")
    assert bass_eq.p("Device On").value == 0                                       # bypassed, not deleted
    assert any("removed EQ Eight on Kick" in l for l in logs)
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
    env.wait(5)
    logs = []
    agent = Agent(live, hub, TinyPlanner(), Settings(minutes=1, settle=1.0, fine=False), env=env, log=logs.append)
    agent.understand()
    before = live.get_set()
    vol = lambda s: next(p["value"] for t in s["tracks"] if t["key"] == "t5" for p in t["mixer"] if p["name"] == "Volume")
    agent.section("test", env.now() + 600, max_rounds=1)
    assert agent.history and agent.history[-1].undone
    assert any("undone, no measurable change" in l for l in logs)
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
        m = real(scoring, retries)
        calls.append(1)
        return {**m, "cmp": None, "objective": 2.35 if len(calls) % 2 else 2.22}

    agent.measure = masking_only
    agent.audit_one((0.0, 16.0), "t1", "Bass", "EQ Eight", "eq8")
    bass_eq = next(d for d in song.tracks[1].devices if d.class_name == "Eq8")
    assert bass_eq.p("Device On").value == 1
    assert not any("better without it" in l for l in logs)
