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
