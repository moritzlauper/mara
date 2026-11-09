from mixmatch.roles import guess


def track(name, *devices):
    return {"key": "t0", "name": name, "devices": [{"name": d, "class_name": ""} for d in devices]}


def test_automatic_names_defer_to_devices():
    assert guess(track("2-Drum Rack", "Drum Rack", "Kick Toner", "Kick Tight", "MixMatch Tap"), None).role == "kick"
    assert guess(track("31-Audio", "2hollisvocalchain"), None).role == "lead_vocal"
    assert guess(track("8-Basic Electric Drive Bass", "Basic Electric Drive Bass"), None).role == "bass"


def test_plain_drum_rack_is_a_drum_bus():
    r = guess(track("4-Drum Rack", "Drum Rack", "MixMatch Tap"), None)
    assert (r.role, r.source) == ("drums_bus", "devices")


def test_user_names_win_over_devices():
    assert guess(track("Snare Top", "Kick Toner"), None).role == "snare"


def test_roles_from_midi():
    from mixmatch.roles import from_midi

    def notes(chords, length, step=4.0):
        return [["t", p, i * step, length, 100] for i, chord in enumerate(chords) for p in chord]

    pad = notes([(51, 58, 63)] * 10, 4.0)
    stabs = notes([(51, 54, 58, 61, 63)] * 10, 0.25, step=0.5)
    hook = notes([(84 + i % 5,) for i in range(20)], 0.25, step=0.5)
    bass = notes([(27,)] * 12, 0.25, step=1)
    assert [from_midi(x) for x in (pad, stabs, hook, bass)] == ["pad", "pluck", "lead_synth", "bass"]
    assert from_midi(notes([(60,)], 1.0)) is None                       # too few notes to say


def test_serum_tracks_get_roles_from_midi():
    pad = [["t0", p, i * 4.0, 4.0, 100] for i in range(10) for p in (51, 58, 63)]
    r = guess(track("18-Serum", "Serum", "EQ Eight"), None, pad)
    assert (r.role, r.source) == ("pad", "midi")


def test_auto_entries_are_guessed_again_but_user_entries_stay(tmp_path):
    import json

    from mixmatch.roles import Role, apply_overrides, save_template

    path = tmp_path / "roles.json"
    path.write_text(json.dumps({"18-Serum": {"role": "other", "priority": 2},          # old fallback: not the user's
                                "Kick": {"role": "kick", "priority": 5},               # older entry: the user's
                                "Pad": {"role": "keys", "priority": 3, "source": "auto"}}))
    roles = {"a": Role("a", "18-Serum", "pad", 2, "midi"), "b": Role("b", "Kick", "snare", 4, "name"),
             "c": Role("c", "Pad", "pad", 2, "name")}
    apply_overrides(roles, str(path))
    assert [roles[k].role for k in "abc"] == ["pad", "kick", "pad"]
    save_template(roles, str(path))
    saved = json.loads(path.read_text())
    assert saved["18-Serum"] == {"role": "pad", "priority": 2, "note": "", "source": "auto"}
    assert saved["Kick"] == {"role": "kick", "priority": 5}


def test_more_instruments_by_name():
    from mixmatch.roles import ROLES, from_name
    cases = {"808 Glide": "808", "Reese Bass": "synth_bass", "Crash": "cymbals", "Shaker Loop": "shaker",
             "Top Loop": "drum_loop", "Vox Chop": "vocal_chop", "Choir": "choir", "Supersaw Lead": "supersaw",
             "Arp 2": "arp", "Chord Stab": "chords", "Bell": "bell", "Organ": "organ", "Flute": "woodwinds",
             "Marimba": "mallets", "Riser": "riser", "Impact": "impact", "Vinyl Crackle": "texture",
             "Chord Minor Pluck": "pluck", "Basic Electric Drive Bass": "bass", "Basic Sub Sine": "sub"}
    for name, role in cases.items():
        assert from_name(name) == role, name
        assert role in ROLES
