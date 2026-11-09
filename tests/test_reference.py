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


def test_beat_mode_without_demucs_ignores_missing_vocals(tmp_path, monkeypatch):
    import mixmatch.reference as ref
    monkeypatch.setattr(ref, "demucs_available", lambda: False)
    song = write_audio(str(tmp_path / "song.wav"), vocals=True)
    beat = profile(write_audio(str(tmp_path / "beat.wav"), vocals=False, seed=3))
    t = ReferenceSet([song], log=lambda *_: None).target(beat=True)
    assert t.one_sided == VOCAL_RANGE
    cmp = compare(beat, t.stats, t.loudness, t.tolerance, t.one_sided)
    assert (cmp.tonal_diff[VOCAL_RANGE] < -1).any()      # the beat lacks the vocal energy …
    assert (cmp.tonal_excess[VOCAL_RANGE] >= 0).all()   # … but that is not counted as a fault


def test_beat_mode_uses_instrumental_stems_when_available(tmp_path, monkeypatch):
    import mixmatch.reference as ref
    inst = write_audio(str(tmp_path / "inst.wav"), vocals=False, seed=5)
    monkeypatch.setattr(ref, "demucs_available", lambda: True)
    monkeypatch.setattr(ref, "instrumental", lambda path, log=None: inst)
    song = write_audio(str(tmp_path / "song.wav"), vocals=True)
    rs = ReferenceSet([song], log=lambda *_: None)
    t = rs.target(beat=True)
    assert "instrumental" in t.description
    assert t.one_sided == bands_between(20, 120)            # only the low end is one-sided, not the vocal range
    assert abs(t.loudness - rs.refs[0].full.loudness) < 1e-9          # loudness from the full song
    t2 = rs.target(beat=True, loudness_from="instrumental")
    assert abs(t2.loudness - rs.refs[0].inst.loudness) < 1e-9


def test_several_references_widen_the_tolerance_where_they_disagree(tmp_path):
    bright = write_audio(str(tmp_path / "a.wav"), tilt=3, seed=1)
    dark = write_audio(str(tmp_path / "b.wav"), tilt=-3, seed=2)
    t = ReferenceSet([bright, dark], log=lambda *_: None).target(beat=False)
    hi = [i for i in range(31) if i > 25]
    mid = [14, 15, 16]
    assert np.mean(t.tolerance[hi]) > np.mean(t.tolerance[mid])


def test_beat_target_takes_dynamics_from_the_full_songs_when_loudness_does():
    import numpy as np

    from mixmatch.analysis import Stats
    from mixmatch.reference import Reference, ReferenceSet

    def st(loud, plr, width):
        bands = np.full(31, loud - 15.0)
        return Stats(frames=10, loudness=loud, bands_db=bands, shape_db=bands - loud, peak_db=loud + plr, plr=plr,
                     width_db=width, low_side_db=-30.0, active=1.0)

    rs = ReferenceSet.__new__(ReferenceSet)
    rs.log = lambda *_: None
    rs.refs = [Reference("song.wav", full=st(-7.5, 7.5, -15.0), inst=st(-11.0, 12.5, -11.8))]
    rs.separate = lambda: True
    t = rs.target(beat=True)
    assert t.loudness == -7.5 and abs(t.stats.plr - 7.5) < 1e-9      # dynamics of the master as a whole
    assert abs(t.stats.width_db + 11.8) < 1e-9                      # width and tonal balance from the instrumental
    t_inst = rs.target(beat=True, loudness_from="instrumental")
    assert t_inst.loudness == -11.0 and abs(t_inst.stats.plr - 12.5) < 1e-9


def test_beat_target_against_stems_never_asks_for_more_bass_and_tolerates_presence():
    import numpy as np

    from mixmatch.analysis import Stats
    from mixmatch.bands import bands_between
    from mixmatch.reference import Reference, ReferenceSet

    def st(loud):
        bands = np.full(31, loud - 15.0)
        return Stats(frames=10, loudness=loud, bands_db=bands, shape_db=bands - loud, peak_db=loud + 7, plr=7.0,
                     width_db=-12.0, low_side_db=-30.0, active=1.0)

    rs = ReferenceSet.__new__(ReferenceSet)
    rs.log = lambda *_: None
    rs.refs = [Reference("song.wav", full=st(-7.5), inst=st(-11.0))]
    rs.separate = lambda: True
    t = rs.target(beat=True)
    assert set(bands_between(20, 120)) <= set(t.one_sided)
    assert all(t.tolerance[i] >= 3.0 for i in bands_between(1000, 6000))



def test_beat_target_lets_the_air_be_as_bright_as_the_full_song():
    import numpy as np

    from mixmatch.analysis import Stats
    from mixmatch.bands import bands_between
    from mixmatch.reference import Reference, ReferenceSet

    def st(loud, air_db=0.0):
        bands = np.full(31, loud - 15.0)
        bands[bands_between(6000, 16000)] += air_db
        return Stats(frames=10, loudness=loud, bands_db=bands, shape_db=bands - loud, peak_db=loud + 7, plr=7.0,
                     width_db=-12.0, low_side_db=-30.0, active=1.0)

    rs = ReferenceSet.__new__(ReferenceSet)
    rs.log = lambda *_: None
    rs.refs = [Reference("song.wav", full=st(-7.5), inst=st(-11.0, air_db=-3.0))]   # the stem is 3 dB darker up top
    rs.separate = lambda: True
    t = rs.target(beat=True)
    assert all(t.tolerance[i] >= 3.0 for i in bands_between(6000, 16000))
