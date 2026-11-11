import numpy as np

from conftest import write_audio
from mixmatch.analysis import compare
from mixmatch.bands import bands_between
from mixmatch.reference import VOCAL_RANGE, ReferenceSet, profile


def test_profile_of_a_file_is_cached_and_plausible(tmp_path):
    path = write_audio(str(tmp_path / "song.wav"))
    st = profile(path)
    assert -30 < st.loudness < -5 and st.peak_db <= 0
    assert profile(path).loudness == st.loudness  # second call comes from the cache


def test_several_references_widen_the_tolerance_where_they_disagree(tmp_path):
    bright = write_audio(str(tmp_path / "a.wav"), tilt=3, seed=1)
    dark = write_audio(str(tmp_path / "b.wav"), tilt=-3, seed=2)
    t = ReferenceSet([bright, dark], log=lambda *_: None).target(beat=False)
    hi = [i for i in range(31) if i > 25]
    mid = [14, 15, 16]
    assert np.mean(t.tolerance[hi]) > np.mean(t.tolerance[mid])


def _beat_case(vocal_db):
    """A song (flat) and a beat that matches it outside the vocal range and sits `vocal_db` off inside it."""
    from mixmatch.analysis import Stats
    from mixmatch.reference import Reference

    def st(bands, loud):
        return Stats(frames=10, loudness=loud, bands_db=bands, shape_db=bands - loud, peak_db=loud + 7, plr=7.0,
                     width_db=-10.0, low_side_db=-25.0, active=1.0)

    song = np.full(31, -22.0)
    beat = song.copy()
    beat[VOCAL_RANGE] += vocal_db
    rs = ReferenceSet.__new__(ReferenceSet)
    rs.log = lambda *_: None
    rs.refs = [Reference("song.wav", full=st(song, -7.5))]
    t = rs.target(beat=True)
    # the beat is quieter without the vocal energy: its shape relative to loudness shifts
    return t, compare(st(beat, -7.5 + min(0.0, vocal_db) * 0.8), t.stats, t.loudness, t.tolerance, t.one_sided,
                      offset=t.offset, anchor=t.anchor)


def test_beat_with_room_for_a_vocal_is_on_target():
    t, cmp = _beat_case(-3.0)                                   # 3 dB under the song in the vocal range
    assert np.abs(cmp.tonal_excess).max() < 1e-6


def test_beat_louder_than_the_song_in_the_vocal_range_counts():
    t, cmp = _beat_case(+2.0)
    assert cmp.tonal_excess[VOCAL_RANGE].max() > 1.0


def test_hollowed_out_beat_counts():
    t, cmp = _beat_case(-8.0)
    assert cmp.tonal_excess[VOCAL_RANGE].min() < -3.0


def test_beat_target_takes_everything_from_the_full_song():
    t, cmp = _beat_case(0.0)
    assert t.loudness == -7.5 and abs(t.stats.plr - 7.0) < 1e-9 and abs(t.stats.width_db + 10.0) < 1e-9
    assert "full references" in t.description and not t.one_sided


def test_loudest_part_leaves_out_fades_and_quiet_blocks():
    from mixmatch.reference import BLOCK, HOP, loudest_part
    from mixmatch.taps import Frame

    def block(level):
        return [Frame(0.0, level, level * 0.3, 0.0, level * 2, [level] * 31) for _ in range(int(BLOCK / HOP))]

    # a 30 s clip: fade in, loud chorus (2 blocks), quieter verse, fade out
    frames = block(0.02) + block(0.3) + block(0.3) + block(0.08) + block(0.08) + block(0.1) + block(0.08) + \
        block(0.05) + block(0.03) + block(0.01)
    picked = loudest_part(frames)
    assert len(picked) == 2 * int(BLOCK / HOP) and all(f.m_rms == 0.3 for f in picked)


def test_short_clips_weigh_half_and_whole_tracks_set_loudness():
    from mixmatch.analysis import Stats
    from mixmatch.reference import Reference

    def st(loud, tilt):
        bands = np.full(31, loud - 15.0) + tilt
        return Stats(frames=10, loudness=loud, bands_db=bands, shape_db=bands - loud, peak_db=loud + 7, plr=7.0,
                     width_db=-10.0, low_side_db=-25.0, active=1.0)

    rs = ReferenceSet.__new__(ReferenceSet)
    rs.log = lambda *_: None
    rs.refs = [Reference("song.wav", full=st(-7.5, 0.0), seconds=200.0),
               Reference("clip.wav", full=st(-10.0, 3.0), seconds=30.0)]
    t = rs.target(beat=False)
    assert t.loudness == -7.5                                       # loudness from the whole track only
    assert abs((t.stats.shape_db - st(-7.5, 0.0).shape_db).mean() - 1.0) < 1e-6   # clip counts 1/3, not 1/2
    assert "half weight" in t.description
