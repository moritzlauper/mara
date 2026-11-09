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
