"""Before/after: snapshots of every parameter in the set, and switching between them.

A run saves a snapshot before it changes anything and one at the end
(runs/<time>-before.json, runs/<time>-after.json). Switching sets every
parameter that differs, in one batch, while the song keeps playing. Devices
are matched by name and order on their track (not by position, which moves
when devices are added); devices that only exist in the current set, e.g. an
EQ Eight MixMatch added, are bypassed for «before».
"""
from __future__ import annotations

import glob
import json
import os
import secrets
import time


def _device_ids(devices: list[dict]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for d in devices:
        seen[d["name"]] = seen.get(d["name"], 0) + 1
        out.append(f"{d['name']}#{seen[d['name']]}")
    return out


def snapshot(set_state: dict) -> dict:
    tracks = {}
    displays = {}
    automation = {}
    for t in set_state["tracks"]:
        tracks[t["key"]] = {
            "name": t["name"],
            "mixer": {p["name"]: p["value"] for p in t["mixer"]},
            "devices": {i: {p["name"]: p["value"] for p in d["params"]}
                        for i, d in zip(_device_ids(t["devices"]), t["devices"])},
        }
        automation[t["key"]] = {"Mixer": [p["name"] for p in t["mixer"] if p.get("automated")],
                                 **{i: [p["name"] for p in d["params"] if p.get("automated")]
                                    for i, d in zip(_device_ids(t["devices"]), t["devices"])}}
        displays[t["key"]] = {"Mixer": {p["name"]: p.get("display", str(p["value"])) for p in t["mixer"]},
                              **{i: {p["name"]: p.get("display", str(p["value"])) for p in d["params"]}
                                 for i, d in zip(_device_ids(t["devices"]), t["devices"])}}
    return {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "tracks": tracks, "display": displays, "automation": automation}


def save(set_state: dict, log_dir: str, stamp: str, which: str) -> str:
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{stamp}-{which}.json")
    with open(path, "w") as f:
        json.dump(snapshot(set_state), f)
    return path


def changes_to(target: dict, set_state: dict, bypass_new: bool) -> list[dict]:
    """The parameter changes that turn the current set into `target`."""
    out = []
    for t in set_state["tracks"]:
        want = target["tracks"].get(t["key"])
        if want is None or want["name"] != t["name"]:
            continue
        automated = target.get("automation", {}).get(t["key"], {})
        for p in t["mixer"]:
            if p.get("automated") or p["name"] in automated.get("Mixer", []):
                continue
            v = want["mixer"].get(p["name"])
            if v is not None and abs(v - p["value"]) > 1e-6:
                out.append({"track": t["key"], "device": "mixer", "param": p["name"], "value": v})
        for i, (dev_id, d) in enumerate(zip(_device_ids(t["devices"]), t["devices"])):
            if d["name"] == "MixMatch Tap":
                continue   # the taps are switched by the runs, not part of the mix
            protected = set(automated.get(dev_id, [])) | {p["name"] for p in d["params"] if p.get("automated")}
            params = {p["name"]: p["value"] for p in d["params"]}
            if dev_id not in want["devices"]:
                if bypass_new and "Device On" not in protected and params.get("Device On", 0) >= 0.5 and d["name"] != "MixMatch Tap":
                    out.append({"track": t["key"], "device": i, "param": "Device On", "value": 0.0})
                continue
            for name, v in want["devices"][dev_id].items():
                if name not in protected and name in params and abs(v - params[name]) > 1e-6:
                    out.append({"track": t["key"], "device": i, "param": name, "value": v})
    return out


def latest_pair(log_dir: str) -> tuple[str, str] | None:
    """Paths of the newest run that has both snapshots."""
    for before in sorted(glob.glob(os.path.join(log_dir, "*-before.json")), reverse=True):
        after = before[: -len("before.json")] + "after.json"
        if os.path.exists(after):
            return before, after
    return None


def latest_before(log_dir: str) -> str | None:
    files = sorted(glob.glob(os.path.join(log_dir, "*-before.json")))
    return files[-1] if files else None


def switch(live, which: str, log_dir: str = "runs", *, pair: tuple[str, str] | None = None) -> int:
    """Sets the open Live set to the «before» or «after» state of the newest run; returns the number of changes.
    «before» also works when a run stopped without its «after» snapshot (Live crashed, run stopped)."""
    if which not in ("before", "after"):
        raise ValueError("choose before or after")
    if pair is not None:
        path = pair[0 if which == "before" else 1]
    elif which == "before":
        path = latest_before(log_dir)  # the newest run, even if it has no «after» yet
    else:
        pair = latest_pair(log_dir)
        path = pair[1] if pair else None
    if path is None:
        raise RuntimeError(f"no «{which}» snapshot yet")
    with open(path) as f:
        target = json.load(f)
    changes = changes_to(target, live.get_set(), bypass_new=(which == "before"))
    for i in range(0, len(changes), 200):
        result = live.batch(changes[i:i + 200])
        if result.get("errors"):
            raise RuntimeError("some comparison parameters could not be restored")
    return len(changes)


class BlindComparison:
    """The selection stays on the server until explicitly revealed. Repeats are intentional."""

    def __init__(self):
        self.reset()

    def reset(self):
        self._which = None
        self._log_dir = None

    @staticmethod
    def ready(log_dir: str) -> bool:
        pair = latest_pair(log_dir)
        return pair is not None and pair[0] == latest_before(log_dir)

    def choose(self, live, log_dir: str) -> None:
        self.reset()
        pair = latest_pair(log_dir)
        if pair is None or pair[0] != latest_before(log_dir):
            raise RuntimeError("blind comparison needs both snapshots of the latest run")
        which = secrets.choice(("before", "after"))
        switch(live, which, log_dir, pair=pair)
        self._which, self._log_dir = which, os.path.realpath(log_dir)

    def reveal(self, log_dir: str) -> str | None:
        return self._which if self._log_dir == os.path.realpath(log_dir) else None


VOTES = "blind-votes.jsonl"


def record_vote(log_dir: str, hidden: str, verdict: str, preview_id: str | None = None) -> dict:
    """One blind trial: the hidden version was judged better or worse than the other one."""
    if hidden not in ("before", "after") or verdict not in ("better", "worse"):
        raise ValueError("a vote is «better» or «worse» for a hidden Before or After")
    entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "hidden": hidden, "verdict": verdict,
             "preview": preview_id, "after_preferred": (hidden == "after") == (verdict == "better")}
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, VOTES), "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def vote_summary(log_dir: str) -> dict:
    """How often After won the blind trials, with the one-sided binomial p-value against guessing."""
    import math

    wins = total = 0
    try:
        with open(os.path.join(log_dir, VOTES)) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                total += 1
                wins += bool(entry.get("after_preferred"))
    except OSError:
        pass
    p = sum(math.comb(total, k) for k in range(wins, total + 1)) / 2 ** total if total else None
    return {"after_preferred": wins, "total": total, "p_value": None if p is None else round(p, 4)}
