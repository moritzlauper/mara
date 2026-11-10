from mixmatch.roles import guess


def track(name, *devices):
    return {"key": "t0", "name": name, "devices": [{"name": d, "class_name": ""} for d in devices]}


def test_automatic_names_defer_to_devices():
    assert guess(track("2-Drum Rack", "Drum Rack", "Kick Toner", "Kick Tight", "MixMatch Tap"), None).role == "kick"
    assert guess(track("31-Audio", "2hollisvocalchain"), None).role == "lead_vocal"
    assert guess(track("8-Basic Electric Drive Bass", "Basic Electric Drive Bass"), None).role == "bass"


def test_plain_drum_rack_does_not_imply_a_complete_kit():
    r = guess(track("4-Drum Rack", "Drum Rack", "MixMatch Tap"), None)
    assert (r.role, r.source) == ("percussion", "devices")


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
    assert saved["18-Serum"] == {"role": "pad", "priority": 2, "note": "", "source": "auto", "evidence": "midi"}
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


def drum_track(played, pads=None):
    if pads is None:
        pads = [(36, "DS Kick"), (38, "DS Snare"), (46, "DS HHO"), (49, "DS Cymbal"), (50, "DS Clang")]
    ds = [{"name": "DS Drum Rack", "class_name": "DrumGroupDevice", "type": 1, "enabled": True,
           "drum_pads": [{"name": name, "note": pitch, "muted": False, "solo": False,
                          "chains": [{"name": name, "devices": [{"name": name, "type": 1}]}]}
                         for pitch, name in pads]}]
    return {"key": "t0", "name": "23-DS Drum Rack", "devices": ds, "role_devices": ds, "played_notes": played}


def test_played_pad_identifies_instrument_in_a_fully_loaded_kit():
    for pitch, role in [(36, "kick"), (38, "snare"), (46, "hats"), (49, "cymbals"), (50, "percussion")]:
        r = guess(drum_track({str(pitch): 30}), None)
        assert r.role == role and r.source == "drum pads"
        assert str(pitch) in r.note
    assert guess(drum_track({"36": 20, "38": 12, "46": 40}), None).role == "drums_bus"


def test_custom_pad_mapping_sample_names_and_layered_kicks():
    # Mapping, not MIDI pitch conventions: a cymbal on C1 is still a cymbal.
    assert guess(drum_track({"36": 8}, [(36, "Crash Cymbal")]), None).role == "cymbals"
    t = drum_track({"36": 8}, [(36, "C1")])
    t["role_devices"][0]["drum_pads"][0]["chains"][0]["devices"] = [
        {"name": "Simpler", "type": 1, "sample_name": "808_Kick_Tight.wav"}]
    assert guess(t, None).role == "kick"
    assert guess(drum_track({"36": 8, "37": 8}, [(36, "Kick sub"), (37, "Kick click")]), None).role == "kick"


def test_unplayed_muted_soloed_and_disabled_pads_do_not_choose_role():
    assert guess(drum_track(None), None).role == "percussion"  # no note data: do not guess full loaded kit
    assert guess(drum_track({}), None).role == "percussion"   # no notes actually played
    t = drum_track({"36": 10, "49": 10})
    t["role_devices"][0]["drum_pads"][0]["muted"] = True
    assert guess(t, None).role == "cymbals"
    t = drum_track({"36": 10, "49": 10})
    t["role_devices"][0]["drum_pads"][3]["solo"] = True
    assert guess(t, None).role == "cymbals"
    t = drum_track({"36": 10, "49": 10})
    t["role_devices"][0]["drum_pads"][0]["chains"][0]["devices"][0]["enabled"] = False
    assert guess(t, None).role == "cymbals"


def test_nested_rack_and_drum_notes_do_not_use_melodic_pitch_guess():
    t = drum_track({"49": 16})
    t["role_devices"] = [{"name": "Instrument Rack", "class_name": "InstrumentGroupDevice", "type": 1,
                          "chains": [{"name": "Drums", "devices": t["role_devices"]}]}]
    assert guess(t, None).role == "cymbals"
    # Arbitrary low MIDI pitch could mean any pad, so it is never interpreted as a melodic bass.
    assert guess(drum_track(None), None, [["t0", 30, i, .2, 100] for i in range(20)]).role == "percussion"


def test_drum_buss_effect_is_not_instrument_evidence_and_groups_stay_groups():
    t = track("22-Serum", "Serum", "Drum Buss", "EQ Eight")
    notes = [["t0", 33, i, .3, 100] for i in range(20)]
    assert guess(t, None, notes).role == "bass"
    assert guess({**drum_track({"36": 10}), "is_group": True}, None).role in ("bus", "drums_bus")


def test_drum_rack_with_reverb_is_not_classified_as_ambience():
    assert guess(track("23-DS Drum Rack", "DS Drum Rack", "Reverb"), None).role == "percussion"
