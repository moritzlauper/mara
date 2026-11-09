"""Command line.

    mixmatch run --ref a.wav --ref b.mp3 [--mode beat] [--planner auto|claude-code|api|rules] [--minutes 10] [--assist]
    mixmatch refs a.wav b.mp3 [--mode beat]        analyse references (and split vocals) ahead of time
    mixmatch taps                                   put the «MixMatch Tap» on every channel and the master
    mixmatch check                                  connection, taps, roles
    mixmatch demo [--claude]                        run the loop on the built-in simulator

--ref takes files or folders and can be given several times.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from .agent import Agent, Settings
from .planner import ClaudePlanner, RulePlanner, make_planner


def _connect():
    from .live_client import LiveClient
    from .taps import TapHub

    try:
        live = LiveClient().connect()
        live.request("ping")
    except OSError:
        sys.exit("Cannot reach Live on port 9877. Is «MixMatch» selected as a control surface in Live's settings?")
    hub = TapHub()
    hub.listen()
    return live, hub


AUDIO = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg")


def _ref_paths(items):
    import os

    out = []
    for item in items or []:
        if os.path.isdir(item):
            out += sorted(os.path.join(item, f) for f in os.listdir(item) if f.lower().endswith(AUDIO))
        elif os.path.exists(item):
            out.append(item)
        else:
            sys.exit(f"Reference not found: {item}")
    return out


def _refset(args):
    from .reference import ReferenceSet

    paths = _ref_paths(args.ref)
    return ReferenceSet(paths) if paths else None


def cmd_refs(args):
    refset = _refset(args)
    if not refset:
        sys.exit("Give reference files: mixmatch refs a.wav b.mp3")
    for beat in ([True] if args.mode == "beat" else [False] if args.mode == "song" else [False, True]):
        t = refset.target(beat, args.loudness_from)
        print(f"\n{'Beat' if beat else 'Song'} target: {t.description}")
        print(f"  loudness {t.loudness:.1f} LUFS, PLR {t.stats.plr:.1f} dB, width {t.stats.width_db:.1f} dB")


TAP = "MixMatch Tap"


def add_taps(live, log=print, replace: bool = False) -> int:
    """Append the tap device to every track, group, return and the master that has none yet.

    `replace` first removes existing taps, e.g. after the device was rebuilt.
    """
    added = 0
    from .taps import is_reference_name

    for t in live.get_set()["tracks"]:
        if t["kind"] == "track" and is_reference_name(t["name"]):
            continue  # reference tracks are read from their files, not measured
        old = [d["index"] for d in t["devices"] if d["name"] == TAP]
        if old and not replace:
            continue
        for index in sorted(old, reverse=True):
            live.delete_device(t["key"], index)
        live.load_device(t["key"], TAP, source="user_library")
        log(f"  + tap on {t['name']}")
        added += 1
    return added


def cmd_taps(args):
    from .live_client import LiveClient, LiveError

    try:
        live = LiveClient().connect()
    except OSError:
        sys.exit("Cannot reach Live on port 9877. Is «MixMatch» selected as a control surface in Live's settings?")
    try:
        n = add_taps(live, replace=args.replace)
    except LiveError as e:
        sys.exit(f"{e}. Copy «{TAP}.amxd» and mixmatch_tap.js into a folder of your User Library.")
    print(f"{n} tap(s) added." if n else "Every channel already has a tap.")


def cmd_suggest_refs(args):
    """Style of a track, similar tracks in the user's library, and references to buy."""
    try:
        from .style import Listener, describe, library_files, similar, suggest
    except ImportError:
        sys.exit('Needs the style extra: pip install -e ".[style]"')
    if not args.track or not os.path.exists(args.track):
        sys.exit("Give an export of your track: mixmatch suggest-refs my-beat.wav")
    print("Listening (CLAP) …")
    listener = Listener()
    desc = describe(args.track, listener)
    print(f"\nYour track: about {desc['tempo_bpm']} BPM, {desc['loudness_lufs']} LUFS in its loudest part")
    print("Sounds like: " + ", ".join(f"{s['style']} ({s['score']:.2f})" for s in desc["styles_clap"][:5]))
    print(f"Mood: {', '.join(desc['mood_clap'])} · key: {desc['harmony']['key']} ({desc['harmony']['mode']}, estimate) · "
          f"chords: {' – '.join(desc['harmony']['chords'][:8])}")

    if args.library:
        files = library_files(args.library)
        print(f"\nIn your library ({len(files)} songs in {args.library}; the first time takes a while, then it is cached):")
        for path, score in similar(listener, desc["_embedding"], files, top=args.top, exclude=args.track):
            print(f"  {score:.2f}  {path}")

    if not args.no_suggest:
        print("\nAsking Claude for references to buy …")
        from .project import description

        result = suggest(desc, note=args.note or description())
        print(f"Style: {result['style']}")
        for s in result["suggestions"]:
            print(f"\n  {s['artist']} – {s['title']}\n    {s['why']}\n    listen: {s['links']['spotify']}\n"
                  f"    get:    {s['links']['bandcamp']}\n            {s['links']['beatport']}")
        if result.get("free_suggestions"):
            print("\nFree and legal (no purchase):")
            for s in result["free_suggestions"]:
                where = "  ".join(v for k, v in s["links"].items() if k != "spotify")
                print(f"\n  {s['artist']} – {s['title']}  [{s['source']}]\n    {s['why']}\n    {where}")
    print("\nPut the references you pick (WAV, AIFF, FLAC or MP3) into references/ and run mixmatch run.")


def cmd_ab(args):
    from . import ab
    from .live_client import LiveClient

    try:
        live = LiveClient().connect()
    except OSError:
        sys.exit("Cannot reach Live on port 9877.")
    n = ab.switch(live, args.which)
    print(f"Set to «{args.which}» ({n} parameters changed).")


def cmd_check(args):
    live, hub = _connect()
    print("Connected to Live. Play the song for a few seconds so the taps report …")
    time.sleep(args.wait)
    agent = Agent(live, hub, RulePlanner(), Settings(roles_file=args.roles))
    agent.understand()
    print(f"Sections: {', '.join(s[0] for s in agent.sections())}")


def cmd_run(args):
    live, hub = _connect()
    planner = make_planner("rules" if args.rules else args.planner)
    print(f"Planner: {getattr(planner, 'model', None) or 'rules (no Claude Code, no ANTHROPIC_API_KEY, or --rules)'}")
    refset = _refset(args)
    loop = live.get_set()["loop"]
    live.transport(time=loop["start"], play=True)   # start inside the song, not wherever playback last stopped
    try:
        time.sleep(args.wait)
        Agent(live, hub, planner, Settings(minutes=args.minutes, assist=args.assist, roles_file=args.roles,
                                           mode=args.mode, loudness_from=args.loudness_from,
                                           focus=args.focus, focus_bars=args.focus_bars, complete=not args.quick, resume=args.resume,
                                           auto_ref=args.auto_ref or (not args.ref and not args.no_auto_ref),
                                           library=args.library or None,
                                           jamendo=args.jamendo, audit=not args.no_audit),
              references=refset).run()
    finally:
        live.transport(play=False)


def cmd_demo(args):
    from .sim import demo_project

    song, hub, env, live = demo_project()
    env.wait(5)
    planner = ClaudePlanner() if args.claude else RulePlanner()
    agent = Agent(live, hub, planner, Settings(minutes=args.minutes, roles_file=args.roles, settle=1.0,
                                               mode=args.mode, loudness_from=args.loudness_from),
                  env=env, references=_refset(args))
    agent.run()


def load_env(path: str = "env.local") -> None:
    """KEY=value lines (quotes allowed) from env.local or .env, without overriding the environment."""
    for name in (path, ".env"):
        if not os.path.exists(name):
            continue
        for line in open(name):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'"))


def main(argv=None):
    load_env()
    p = argparse.ArgumentParser(prog="mixmatch", description="Mixing agent for Ableton Live")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("refs", cmd_refs), ("check", cmd_check), ("demo", cmd_demo),
                     ("taps", cmd_taps)):
        s = sub.add_parser(name)
        s.set_defaults(fn=fn)
        s.add_argument("ref_files", nargs="*", help=argparse.SUPPRESS)
        s.add_argument("--ref", action="append", default=[], help="reference file or folder, repeatable")
        s.add_argument("--mode", choices=["auto", "beat", "song"], default="auto",
                       help="beat: no vocals in your mix, references are compared without theirs")
        s.add_argument("--loudness-from", choices=["full", "instrumental"], default="full",
                       help="beat mode: loudness target from the full songs (default) or their instrumentals")
        s.add_argument("--roles", default="roles.json", help="file with channel roles (written on first start)")
        s.add_argument("--minutes", type=float, default=None,
                       help="upper time limit; without it a run goes through every channel until it is complete")
        s.add_argument("--wait", type=float, default=6.0, help="seconds of playback before starting")
    sub.choices["taps"].add_argument("--replace", action="store_true",
                                     help="remove existing taps first and put fresh ones (after rebuilding the device)")
    sub.choices["run"].add_argument("--assist", action="store_true", help="ask before every round")
    sub.choices["run"].add_argument("--resume", metavar="TRACK",
                                    help="continue a stopped run: channel pass from this track on, then the final check")
    sub.choices["run"].add_argument("--auto-ref", action="store_true",
                                    help="find references by listening to the project (default when no --ref is given)")
    sub.choices["run"].add_argument("--no-auto-ref", action="store_true",
                                    help="do not look for references: only --ref and the project's reference tracks")
    sub.choices["run"].add_argument("--library", default="~/Music",
                                    help="folder searched for references with --auto-ref (empty to skip)")
    sub.choices["run"].add_argument("--no-audit", action="store_true",
                                    help="keep every effect (no check for effects that do nothing or harm)")
    sub.choices["run"].add_argument("--jamendo", action="store_true",
                                    help="with --auto-ref also try Creative Commons tracks from Jamendo (JAMENDO_CLIENT_ID)")
    sub.choices["run"].add_argument("--quick", action="store_true",
                                    help="only match the master to the reference, no pass over every channel")
    sub.choices["run"].add_argument("--focus", choices=["sections", "loud"], default="loud",
                                    help="loud: find the loudest part of the song once and work only there, "
                                         "like the references are measured at their loudest part")
    sub.choices["run"].add_argument("--focus-bars", type=int, default=16, help="length of the loudest part in bars")
    sub.choices["run"].add_argument("--planner", choices=["auto", "claude-code", "api", "rules"], default="auto",
                                    help="auto: local Claude Code on your subscription if installed, else the API key")
    sub.choices["run"].add_argument("--rules", action="store_true", help="rule-based planner instead of Claude")
    sub.choices["demo"].add_argument("--claude", action="store_true", help="use Claude in the simulator too")
    abp = sub.add_parser("ab", help="switch the set to the state before or after the last run")
    abp.add_argument("which", choices=["before", "after"])
    abp.set_defaults(fn=cmd_ab)
    ui = sub.add_parser("ui", help="local web interface: start and follow runs, references, effects, roles")
    ui.set_defaults(fn=lambda a: __import__("mixmatch.ui", fromlist=["serve"]).serve(".", a.port, not a.no_browser))
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    sr = sub.add_parser("suggest-refs", help="style of your track, similar songs in your library, references to buy")
    sr.set_defaults(fn=cmd_suggest_refs)
    sr.add_argument("track", nargs="?", help="an export of your track (WAV, AIFF, FLAC, MP3)")
    sr.add_argument("--library", nargs="?", const="~/Music", default=None,
                    help="also search this folder for similar songs (default ~/Music); cached after the first time")
    sr.add_argument("--top", type=int, default=10, help="how many similar songs to list")
    sr.add_argument("--note", help="describe your track in your own words, e.g. «PS2-style DnB»")
    sr.add_argument("--no-suggest", action="store_true", help="do not ask Claude for references to buy")
    args = p.parse_args(argv)
    if hasattr(args, "ref_files"):
        args.ref = args.ref + args.ref_files
    args.fn(args)


if __name__ == "__main__":
    main()
