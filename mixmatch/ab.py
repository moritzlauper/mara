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
    for t in set_state["tracks"]:
        tracks[t["key"]] = {
            "name": t["name"],
            "mixer": {p["name"]: p["value"] for p in t["mixer"]},
            "devices": {i: {p["name"]: p["value"] for p in d["params"]}
                        for i, d in zip(_device_ids(t["devices"]), t["devices"])},
        }
    return {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "tracks": tracks}


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
        for p in t["mixer"]:
            v = want["mixer"].get(p["name"])
            if v is not None and abs(v - p["value"]) > 1e-6:
                out.append({"track": t["key"], "device": "mixer", "param": p["name"], "value": v})
        for i, (dev_id, d) in enumerate(zip(_device_ids(t["devices"]), t["devices"])):
            params = {p["name"]: p["value"] for p in d["params"]}
            if dev_id not in want["devices"]:
                if bypass_new and params.get("Device On", 0) >= 0.5 and d["name"] != "MixMatch Tap":
                    out.append({"track": t["key"], "device": i, "param": "Device On", "value": 0.0})
                continue
            for name, v in want["devices"][dev_id].items():
                if name in params and abs(v - params[name]) > 1e-6:
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


def switch(live, which: str, log_dir: str = "runs") -> int:
    """Sets the open Live set to the «before» or «after» state of the newest run; returns the number of changes.
    «before» also works when a run stopped without its «after» snapshot (Live crashed, run stopped)."""
    if which == "before":
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
        live.batch(changes[i:i + 200])
    return len(changes)
