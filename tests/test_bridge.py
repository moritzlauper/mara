import io
import json

import numpy as np
import pytest
import soundfile as sf

from mixmatch import bridge as br
from mixmatch import character
from mixmatch.sim import demo_project, tap


def _bridge(tmp_path):
    song, hub, env, live = demo_project()
    for t in [*song.tracks, song.master_track]:
        t.devices.append(tap())
    return br.Bridge(live=live, hub=hub, env=env, state_dir=tmp_path / "bridge"), song


def _song(path, f0=55.0, decay=0.15, click=0.3, bass=0.15, gain=0.5, seconds=12, sr=44100, seed=0):
    """Four-on-the-floor kick (pitch sweep to f0), sidechained 45 Hz bass, pink noise."""
    rng = np.random.default_rng(seed)
    n, t = int(seconds * sr), np.arange(int(seconds * sr)) / sr
    tk = np.arange(int(0.5 * sr)) / sr
    kick = np.sin(2 * np.pi * np.cumsum(f0 + 2 * f0 * np.exp(-tk / 0.02)) / sr) * np.exp(-tk / decay)
    kick[:int(0.004 * sr)] += click * rng.standard_normal(int(0.004 * sr))
    x = np.zeros(n)
    for s in np.arange(0, seconds - 0.5, 0.5):
        i = int(s * sr)
        x[i:i + len(kick)] += kick
    x += bass * np.sin(2 * np.pi * 45 * t) * np.minimum(1, (t % 0.5) / 0.15)
    f = np.fft.rfftfreq(n, 1 / sr)
    noise = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) / np.sqrt(np.maximum(f, 20)), n=n)
    x += noise * 0.08 / np.std(noise)
    x *= gain / np.max(np.abs(x))
    sf.write(path, np.stack([x, x], 1).astype(np.float32), sr)
    return str(path)


def _rpc(server, method, params=None, mid=1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def _call(server, name, **args):
    result = _rpc(server, "tools/call", {"name": name, "arguments": args})["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def test_protocol_handshake_and_tool_list(tmp_path):
    server = br.Server(_bridge(tmp_path)[0])
    init = _rpc(server, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})["result"]
    assert init["protocolVersion"] == "2025-03-26"
    assert init["serverInfo"]["name"] == "mara-bridge"
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in _rpc(server, "tools/list")["result"]["tools"]}
    assert {"listen", "set_parameters", "record_and_compare", "analyze_audio", "restore"} <= names
    assert _rpc(server, "nope")["error"]["code"] == -32601
    data, error = _call(server, "nope")
    assert error and "Unknown tool" in data["error"]


def test_listen_change_and_restore(tmp_path):
    bridge, song = _bridge(tmp_path)
    server = br.Server(bridge)
    heard, error = _call(server, "listen", seconds=4)
    assert not error, heard
    assert heard["main"]["loudness_lufs"] < 0
    names = [t["name"] for t in heard["tracks"]]
    assert "Kick" in names and "REF Song" not in names
    assert any(m["by"] == "Bass" or m["masked"] == "Bass" for m in heard["masking"])

    kick = next(t for t in heard["tracks"] if t["name"] == "Kick")
    done, error = _call(server, "set_parameters", changes=[{"track": "Kick", "param": "Volume", "target": -6}])
    assert not error, done
    assert done["applied"][0]["now"].startswith("-6")
    louder, _ = _call(server, "listen", seconds=4)
    assert next(t for t in louder["tracks"] if t["name"] == "Kick")["loudness_lufs"] < kick["loudness_lufs"] - 3

    restored, error = _call(server, "restore")
    assert not error and restored["parameters_changed"] == 1
    assert song.tracks[0].mixer_device.volume.value == pytest.approx(0.85)


def test_tap_is_protected_and_errors_are_tool_errors(tmp_path):
    bridge, _ = _bridge(tmp_path)
    server = br.Server(bridge)
    state = bridge.live.get_set()
    kick = next(t for t in state["tracks"] if t["name"] == "Kick")
    index = next(d["index"] for d in kick["devices"] if d["name"] == br.TAP)
    data, error = _call(server, "set_parameters",
                        changes=[{"track": "Kick", "device": index, "param": "Device On", "value": 0}])
    assert error and "measurement device" in data["error"]
    data, error = _call(server, "set_parameters", changes=[{"track": "Nope", "param": "Volume", "target": 0}])
    assert error and "No track" in data["error"]


def test_kick_character_from_a_full_mix(tmp_path):
    a = character.describe(_song(tmp_path / "a.wav", f0=55, decay=0.15))
    b = character.describe(_song(tmp_path / "b.wav", f0=48, decay=0.35, click=1.2, gain=0.95))
    assert 21 <= a["kick"]["hits"] <= 24            # 23 hits in 12 s at 120 BPM
    assert a["kick"]["fundamental_hz"] == pytest.approx(55, abs=3)
    assert b["kick"]["fundamental_hz"] == pytest.approx(48, abs=3)
    diff = character.difference(a, b)
    assert diff["kick"]["decay_ms_to_-10db"] < -100          # the reference kick rings longer
    assert diff["kick"]["spectrum_vs_low_db"]["click_2k_6k"] > 0 or diff["kick"]["over_bass_db"] != 0
    assert diff["loudness"]["integrated_lufs"] < -1          # the reference is louder
    assert set(character.only(a, ["kick"])) == {"seconds", "kick"}


def test_record_and_compare_against_a_reference(tmp_path, monkeypatch):
    from mixmatch import autoref

    bridge, _ = _bridge(tmp_path)
    mix = _song(tmp_path / "mix.wav", gain=0.4)
    ref = _song(tmp_path / "ref.wav", f0=48, gain=0.95, seed=1)
    calls = []
    monkeypatch.setattr(autoref, "capture_begin", lambda live, beats, source, start=None: calls.append(
        (beats, source, start)) or 7)
    monkeypatch.setattr(autoref, "capture_end", lambda live, track, timeout=20: mix)
    out = bridge.record_and_compare(start_beat=32, bars=4, reference=ref, aspects=["loudness", "kick"])
    assert calls == [(16.0, None, 32.0)]
    result = out["references"]["ref.wav"]["mix_minus_reference"]
    assert result["loudness"]["integrated_lufs"] < -3
    assert "tonal" not in result


def test_serve_keeps_stdout_for_the_protocol(tmp_path, capsys):
    bridge, _ = _bridge(tmp_path)
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
             "not json",
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "add_taps", "arguments": {}}})]
    out = io.StringIO()
    br.serve(io.StringIO("\n".join(lines) + "\n"), out, bridge)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700
