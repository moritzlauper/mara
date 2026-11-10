import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from conftest import write_audio
from mixmatch import ab, preview, ui
from mixmatch.sim import demo_project


def prepared(tmp_path):
    song, hub, env, live = demo_project()
    master = song.master_track
    master.available_output_routing_types = [NS(display_name="Ext. Out"), NS(display_name="Sends Only")]
    master.output_routing_type = master.available_output_routing_types[0]
    master.output_routing_channel = NS(display_name="3/4")
    path = ab.save(live.get_set(), str(tmp_path), "1", "before")
    return song, live, path


@pytest.mark.parametrize("failure", [False, True])
def test_recording_restores_mix_main_routing_and_transport_even_on_failure(tmp_path, monkeypatch, failure):
    import mixmatch.autoref as capture

    song, live, path = prepared(tmp_path)
    live.set("t0", "mixer", "Volume", target=-5)
    live.set("m", "mixer", "Volume", value=0)
    live.transport(loop_start=32, loop_length=32, time=35, play=True)
    saved = ab.snapshot(live.get_set())
    recorded = write_audio(str(tmp_path / "capture.wav"), seconds=2)
    monkeypatch.setattr(capture, "capture_begin", lambda *args: 123)
    def end(*args, **kwargs):
        assert song.master_track.output_routing_type.display_name == "Sends Only"
        assert song.master_track.mixer_device.volume.value > 0
        if failure:
            raise RuntimeError("recorder failed")
        return recorded
    monkeypatch.setattr(capture, "capture_end", end)
    if failure:
        with pytest.raises(RuntimeError, match="recorder failed"):
            preview.render(live, path, 64, 4, log=lambda *_: None)
        assert not Path(preview.metadata_path(path)).exists()
    else:
        assert preview.render(live, path, 64, 4, log=lambda *_: None)["seconds"] == 2
    assert ab.changes_to(saved, live.get_set(), True) == []
    assert song.master_track.output_routing_type.display_name == "Ext. Out"
    assert song.master_track.output_routing_channel.display_name == "3/4"
    assert live.transport() == {"is_playing": True, "song_time": 35.0}
    assert live.get_set()["loop"] == {"on": False, "start": 32.0, "length": 32.0}


def make_audio(snapshot):
    write_audio(preview.audio_path(snapshot), seconds=2)
    Path(preview.metadata_path(snapshot)).write_text(json.dumps({"seconds": 2, "start_bar": 1, "bars": 1}))


def test_resume_copies_before_audio_and_never_pairs_it_with_an_older_after(tmp_path):
    song, live, before = prepared(tmp_path)
    after = ab.save(live.get_set(), str(tmp_path), "1", "after")
    make_audio(before); make_audio(after)
    assert all(preview.available(str(tmp_path)).values())
    new = ab.save(live.get_set(), str(tmp_path), "2", "before")
    preview.copy_preview(before, new)
    available = preview.available(str(tmp_path))
    assert available["before"]["id"] == "2-before" and available["after"] is None
    assert Path(preview.audio_path(before)).read_bytes() == Path(preview.audio_path(new)).read_bytes()
    assert preview.selected_file(str(tmp_path), "before", "../../secrets") is None


def test_player_serves_audio_with_seeking_while_run_is_active_and_blind_is_hidden(tmp_path, monkeypatch):
    folder = tmp_path / "runs"
    folder.mkdir()
    song, live, before = prepared(folder)
    after = ab.save(live.get_set(), str(folder), "1", "after")
    make_audio(before); make_audio(after)
    runner = ui.Runner(str(tmp_path))
    monkeypatch.setattr(runner, "running", lambda: True)
    monkeypatch.setattr(ui.secrets, "choice", lambda _: "before")
    from mixmatch.live_client import LiveClient
    monkeypatch.setattr(LiveClient, "connect", lambda self: pytest.fail("audio playback must not touch Live"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), ui.make_handler(runner, live_set_fn=lambda: None))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        url = base + "/api/preview-audio?which=before&id=1-before"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=0-3"})) as r:
            assert r.status == 206 and r.read() == b"RIFF"
            assert r.headers["Content-Type"] == "audio/wav"
            assert r.headers["Content-Range"].startswith("bytes 0-3/")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/api/preview-audio?which=before&id=../../secrets")
        assert err.value.code == 404
        req = urllib.request.Request(base + "/api/preview/blind", data=b"{}")
        trial = json.load(urllib.request.urlopen(req))
        assert set(trial) == {"url"} and "before" not in trial["url"]
        assert urllib.request.urlopen(base + trial["url"]).read(4) == b"RIFF"
        revealed = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/preview/reveal", data=b"{}")))
        assert revealed == {"which": "before"}
        assert urllib.request.urlopen(base + trial["url"] + "&equal=1").read(4) == b"RIFF"
    finally:
        server.shutdown(); server.server_close()


def test_equal_loudness_matches_integrated_level_without_changing_originals_or_dynamics(tmp_path):
    import numpy as np
    import soundfile as sf
    import pyloudnorm as pyln

    song, live, before = prepared(tmp_path)
    after = ab.save(live.get_set(), str(tmp_path), "1", "after")
    make_audio(before); make_audio(after)
    data, rate = sf.read(preview.audio_path(before), always_2d=True)
    sf.write(preview.audio_path(after), data * 2, rate, subtype="PCM_24")
    originals = {p: Path(preview.audio_path(p)).read_bytes() for p in (before, after)}
    match = preview.matching(str(tmp_path))
    assert match["ready"] and match["gain_db"]["before"] == 0
    assert match["gain_db"]["after"] == pytest.approx(-6.0206, abs=.01)
    a, _ = sf.read(preview.matched_file(str(tmp_path), "before", "1-before"), always_2d=True)
    b, _ = sf.read(preview.matched_file(str(tmp_path), "after", "1-after"), always_2d=True)
    meter = pyln.Meter(rate)
    assert abs(meter.integrated_loudness(a) - meter.integrated_loudness(b)) < .01
    assert np.max(abs(a - b)) < 2e-7  # constant gain preserves waveform and dynamics
    assert np.max(abs(b)) <= np.max(abs(data)) + 2e-7
    assert all(Path(preview.audio_path(p)).read_bytes() == raw for p, raw in originals.items())


def test_equal_loudness_needs_a_valid_pair_and_uses_perceived_level_not_peaks(tmp_path):
    import numpy as np
    import soundfile as sf

    song, live, before = prepared(tmp_path)
    make_audio(before)
    assert not preview.matching(str(tmp_path))["ready"]
    after = ab.save(live.get_set(), str(tmp_path), "1", "after")
    make_audio(after)
    sr = 48000
    t = np.arange(sr * 2) / sr
    for path, hz in ((before, 100), (after, 4000)):
        sf.write(preview.audio_path(path), .2 * np.sin(2 * np.pi * hz * t), sr, subtype="PCM_24")
    match = preview.matching(str(tmp_path))
    assert match["ready"] and match["gain_db"]["after"] < -3  # equal peaks, different K-weighted loudness
    sf.write(preview.audio_path(after), np.zeros(sr * 2), sr)
    assert not preview.matching(str(tmp_path))["ready"]
    assert preview.matched_file(str(tmp_path), "after", "1-after") is None
