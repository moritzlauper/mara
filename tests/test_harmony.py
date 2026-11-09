import numpy as np

from mixmatch import harmony


def notes_for(chords, bar_beats=4):
    out = []
    for i, pitches in enumerate(chords):
        for p in pitches:
            out.append(["t0", p, i * bar_beats, bar_beats, 100])
    return out


def test_key_and_chords_of_a_minor_progression():
    # i – VI – III – VII in Eb minor: Ebm – B (Cb) – Gb – Db, each over its root
    prog = [(39, 51, 54, 58), (35, 47, 51, 54), (42, 54, 58, 61), (37, 49, 53, 56)]
    h = harmony.from_notes(notes_for(prog * 2), 0, 32)
    assert (h["key"], h["mode"]) == ("Ebm", "minor")
    assert h["chords"][:4] == ["Ebm", "B", "Gb", "Db"]


def test_c_major_scale_is_c_major():
    scale = [[60 + i] for i in (0, 2, 4, 5, 7, 9, 11, 0)]
    assert harmony.from_notes(notes_for(scale, 1), 0, 8)["key"] == "C"


def test_progression_merges_repeats_and_bass_makes_a_slash_chord():
    assert harmony.progression([{"chord": "Am"}, {"chord": "Am"}, {"chord": "F"}]) == ["Am ×2", "F"]
    assert harmony.chord(np.eye(12)[[2, 6, 9]].sum(axis=0), bass=6 + 36) == "D/F#"


def test_bass_frequencies_of_eb():
    assert harmony.bass_frequencies("Eb")["tonic"][:2] == [38.9, 77.8]
