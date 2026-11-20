from types import SimpleNamespace as NS

from MARA.commands import Commands
from mixmatch.agent import arrangement_problem

LOOP = [("loop", 0.0, 64.0)]


def state(spans, session=0):
    return {"arrangement": {"spans": spans, "session_clips": session}}


def test_clips_in_range_pass():
    assert arrangement_problem(state([[16.0, 128.0]]), LOOP) is None


def test_unknown_arrangement_is_not_judged():
    assert arrangement_problem({}, LOOP) is None
    assert arrangement_problem({"arrangement": None}, LOOP) is None


def test_session_only_names_session_view():
    msg = arrangement_problem(state([], session=5), LOOP)
    assert "Session View" in msg and "Arrangement" in msg


def test_empty_set():
    assert "no clips" in arrangement_problem(state([]), LOOP)


def test_clips_outside_range_name_the_bars():
    msg = arrangement_problem(state([[256.0, 512.0]]), [("loop", 0.0, 66.0)])
    assert "bars 1–17" in msg


def clip(start, end, muted=False):
    return NS(start_time=start, end_time=end, muted=muted)


def track(name, arrangement=(), session=0):
    return NS(name=name, arrangement_clips=list(arrangement),
              clip_slots=[NS(has_clip=True)] * session + [NS(has_clip=False)])


def test_script_merges_spans_and_skips_refs_and_muted():
    song = NS(tracks=[track("Drums", [clip(0, 16), clip(8, 32)], session=2),
                      track("Bass", [clip(64, 80), clip(100, 120, muted=True)]),
                      track("REF Song", [clip(0, 999)], session=1)],
              return_tracks=[], master_track=NS(name="Main"))
    assert Commands(song)._arrangement() == {"spans": [[0.0, 32.0], [64.0, 80.0]], "session_clips": 2,
                                            "back_to_arranger": False}


def test_script_reports_unknown_without_arrangement_clips():
    song = NS(tracks=[NS(name="Drums", clip_slots=[])], return_tracks=[], master_track=NS(name="Main"))
    assert Commands(song)._arrangement() is None


def test_lit_back_to_arrangement_is_named():
    lit = {"arrangement": {"spans": [[0.0, 64.0]], "session_clips": 3, "back_to_arranger": True}}
    assert "Back to Arrangement" in arrangement_problem(lit, LOOP)


def test_session_only_wins_over_back_to_arrangement():
    lit = {"arrangement": {"spans": [], "session_clips": 3, "back_to_arranger": True}}
    assert "only in Session View" in arrangement_problem(lit, LOOP)


def test_script_reports_back_to_arranger():
    song = NS(tracks=[track("Drums", [clip(0, 16)])], return_tracks=[], master_track=NS(name="Main"),
              back_to_arranger=True)
    assert Commands(song)._arrangement()["back_to_arranger"] is True
