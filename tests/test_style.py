import numpy as np
import soundfile as sf

from mixmatch.style import SR, library_files, loudest_clips, search_links, tempo


def test_tempo_of_a_click_track():
    beat = int(SR * 60 / 140)                      # 140 BPM
    mono = np.zeros(SR * 20, dtype=np.float32)
    for i in range(0, len(mono) - 2000, beat):
        mono[i:i + 2000] = np.hanning(2000)
    assert abs(tempo(mono) - 140) < 3


def test_loudest_clips_pick_the_drop():
    quiet, loud = np.full(SR * 30, 0.01, np.float32), np.full(SR * 10, 0.5, np.float32)
    clips = loudest_clips(np.concatenate([quiet, loud, quiet]), n=1)
    assert np.isclose(clips[0].mean(), 0.5)


def test_library_keeps_songs_and_skips_samples(tmp_path):
    def write(path, seconds):
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(path, np.zeros((int(8000 * seconds), 2), np.float32), 8000)

    write(tmp_path / "Album" / "song.wav", 120)
    write(tmp_path / "Album" / "loop.wav", 4)                    # too short for a song
    write(tmp_path / "Samples" / "long_pad.wav", 120)            # sample folder
    assert [p.split("/")[-1] for p in library_files(str(tmp_path))] == ["song.wav"]


def test_search_links_are_encoded():
    links = search_links("glaive & ericdoa", "cloak n dagger")
    assert links["bandcamp"] == "https://bandcamp.com/search?q=glaive+%26+ericdoa+cloak+n+dagger"


def test_jamendo_search_keeps_only_allowed_downloads(monkeypatch):
    import io
    import json as _json

    from mixmatch import autoref

    page = {"headers": {"status": "success"}, "results": [
        {"id": "1", "artist_name": "A", "name": "ok", "audiodownload_allowed": True, "audiodownload": "http://x/1",
         "shareurl": "http://j/1", "license_ccurl": "http://cc/by"},
        {"id": "2", "artist_name": "B", "name": "no", "audiodownload_allowed": False, "audiodownload": "http://x/2"},
        {"id": "1", "artist_name": "A", "name": "dupe", "audiodownload_allowed": True, "audiodownload": "http://x/1"},
    ]}
    monkeypatch.setattr(autoref.urllib.request, "urlopen", lambda url, timeout=0: io.StringIO(_json.dumps(page)))
    found = autoref.jamendo_search(["futurebass", "emo"], "cid", log=lambda *_: None)
    assert [t["title"] for t in found] == ["ok"] and found[0]["license"] == "http://cc/by"


def test_hyperpop_maps_to_an_existing_jamendo_tag():
    from mixmatch.autoref import JAMENDO_TAGS

    assert JAMENDO_TAGS["hyperpop"] == "futurebass"


def test_search_links_include_spotify():
    assert search_links("Burial", "Near Dark")["spotify"] == "https://open.spotify.com/search/Burial%20Near%20Dark"


def test_free_links_point_to_the_source():
    from mixmatch.style import free_links

    assert free_links("Artist", "Song", "NCS")["ncs"] == "https://ncs.io/music-search?q=Song"
    assert "freemusicarchive.org" in free_links("A", "B", "Free Music Archive")["fma"]
    assert "bandcamp.com" in free_links("A", "B", "free download by the artist")["bandcamp"]
