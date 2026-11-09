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
