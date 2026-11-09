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
