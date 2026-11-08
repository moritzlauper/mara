"""Command line.

    mixmatch run --ref a.wav --ref b.mp3 [--mode beat] [--minutes 10] [--assist] [--rules]
    mixmatch refs a.wav b.mp3 [--mode beat]        analyse references (and split vocals) ahead of time
    mixmatch check                                  connection, taps, roles
    mixmatch demo [--claude]                        run the loop on the built-in simulator

--ref takes files or folders and can be given several times.
"""
from __future__ import annotations

import argparse
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


def cmd_check(args):
    live, hub = _connect()
    print("Connected to Live. Play the song for a few seconds so the taps report …")
    time.sleep(args.wait)
    agent = Agent(live, hub, RulePlanner(), Settings(roles_file=args.roles))
    agent.understand()
    print(f"Sections: {', '.join(s[0] for s in agent.sections())}")


def cmd_run(args):
    live, hub = _connect()
    planner = RulePlanner() if args.rules else make_planner()
    print(f"Planner: {'Claude (' + planner.model + ')' if isinstance(planner, ClaudePlanner) else 'rules (no ANTHROPIC_API_KEY or --rules)'}")
    live.transport(play=True)
    time.sleep(args.wait)
    refset = _refset(args)
    Agent(live, hub, planner, Settings(minutes=args.minutes, assist=args.assist, roles_file=args.roles,
                                       mode=args.mode, loudness_from=args.loudness_from),
          references=refset).run()


def cmd_demo(args):
    from .sim import demo_project

    song, hub, env, live = demo_project()
    env.wait(5)
    planner = ClaudePlanner() if args.claude else RulePlanner()
    agent = Agent(live, hub, planner, Settings(minutes=args.minutes, roles_file=args.roles, settle=1.0,
                                               mode=args.mode, loudness_from=args.loudness_from),
                  env=env, references=_refset(args))
    agent.run()


def main(argv=None):
    p = argparse.ArgumentParser(prog="mixmatch", description="Mixing agent for Ableton Live")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("refs", cmd_refs), ("check", cmd_check), ("demo", cmd_demo)):
        s = sub.add_parser(name)
        s.set_defaults(fn=fn)
        s.add_argument("ref_files", nargs="*", help=argparse.SUPPRESS)
        s.add_argument("--ref", action="append", default=[], help="reference file or folder, repeatable")
        s.add_argument("--mode", choices=["auto", "beat", "song"], default="auto",
                       help="beat: no vocals in your mix, references are compared without theirs")
        s.add_argument("--loudness-from", choices=["full", "instrumental"], default="full",
                       help="beat mode: loudness target from the full songs (default) or their instrumentals")
        s.add_argument("--roles", default="roles.json", help="file with channel roles (written on first start)")
        s.add_argument("--minutes", type=float, default=10.0)
        s.add_argument("--wait", type=float, default=6.0, help="seconds of playback before starting")
    sub.choices["run"].add_argument("--assist", action="store_true", help="ask before every round")
    sub.choices["run"].add_argument("--rules", action="store_true", help="rule-based planner instead of Claude")
    sub.choices["demo"].add_argument("--claude", action="store_true", help="use Claude in the simulator too")
    args = p.parse_args(argv)
    args.ref = args.ref + args.ref_files
    args.fn(args)


if __name__ == "__main__":
    main()
