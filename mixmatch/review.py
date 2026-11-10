"""Snapshot differences for review; attempts are kept separately from the final changes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from . import ab


def fingerprint(state):
    return hashlib.sha256(json.dumps(ab.snapshot(state)["tracks"], sort_keys=True).encode()).hexdigest()


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):
        return {}


def latest_pair(folder):
    before = ab.latest_before(folder)
    if before:
        after = before.removesuffix("before.json") + "after.json"
        if Path(after).is_file():
            return before, after
    return None


def changes(folder: str) -> dict:
    pair = latest_pair(folder)
    before_path = pair[0] if pair else ab.latest_before(folder)
    before = load(before_path)
    partial = pair is None
    if not before:
        return {"kept": [], "rejected": [], "ready": False}
    import copy
    after = load(pair[1]) if pair else copy.deepcopy(before)
    if not after:
        return {"kept": [], "rejected": [], "ready": False}
    # Restrict journal entries to this snapshot family (including resumed runs).
    records = []
    journal = Path(folder) / "change-journal.jsonl"
    if journal.exists():
        for line in journal.read_text().splitlines():
            try:
                record = json.loads(line)
                if record.get("before_time") == before.get("time"):
                    records.append(record)
            except ValueError:
                continue
    if partial:
        # Running tests are excluded; only acknowledged, retained changes enter this preview.
        for record in records:
            if record.get("status") != "kept":
                continue
            for item in record.get("parameters", []):
                track = after["tracks"].get(item.get("track"))
                if track is None or not isinstance(item.get("new_value"), (int, float)):
                    continue
                params = track["mixer"] if item["device"] == "Mixer" else track["devices"].setdefault(item["device"], {})
                params[item["param"]] = item["new_value"]
                after.setdefault("display", {}).setdefault(item["track"], {}).setdefault(item["device"], {})[item["param"]] = item.get("new_display")
    evidence = {}
    for record in records:
        if record.get("status") == "kept":
            for item in record.get("parameters", []):
                evidence[(item["track"], item["device"], item["param"])] = {**record, **item}
    kept = []
    for key in sorted(set(before["tracks"]) | set(after["tracks"])):
        old, new = before["tracks"].get(key), after["tracks"].get(key)
        if not old or not new or old["name"] != new["name"]:
            continue
        if old["name"] == "MixMatch Capture":
            continue
        devices = [("Mixer", old["mixer"], new["mixer"])]
        devices += [(device, old["devices"].get(device), new["devices"].get(device))
                    for device in sorted(set(old["devices"]) | set(new["devices"])) if not device.startswith("MixMatch Tap#")]
        for device, a, b in devices:
            if a is None or b is None:
                settings = []
                meta = {}
                changed_at = None
                if a is None:
                    for param, value in (b or {}).items():
                        proof = evidence.get((key, device, param))
                        if proof:
                            meta = proof
                            changed_at = max(changed_at or 0, proof.get("at") or 0)
                            settings.append({'param': param, 'value': after.get('display', {}).get(key, {}).get(device, {}).get(param)
                                             or proof.get('new_display') or f'{value:.5g} (Live value)'})
                kept.append({"track": key, "channel": old["name"], "device": device, "param": "Device",
                             "before": "Present" if a is not None else "Not present", "after": "Present" if b is not None else "Removed",
                             "kind": "added" if a is None else "removed", "reason": meta.get('reason') or "The device chain changed.",
                             "verification": meta.get('verification'), 'section': meta.get('section'), 'settings': settings, 'at': changed_at})
                continue
            protected = set(before.get("automation", {}).get(key, {}).get(device, [])) | set(after.get("automation", {}).get(key, {}).get(device, []))
            for param in sorted(set(a) & set(b)):
                if param in protected:
                    continue
                if abs(a[param] - b[param]) < 1e-6:
                    continue
                meta = evidence.get((key, device, param), {})
                display_before = before.get("display", {}).get(key, {}).get(device, {}).get(param)
                display_after = after.get("display", {}).get(key, {}).get(device, {}).get(param)
                kept.append({"track": key, "channel": old["name"], "device": device, "param": param,
                             "before": display_before or meta.get("old_display") or f"{a[param]:.5g} (Live value)",
                             "after": display_after or meta.get("new_display") or f"{b[param]:.5g} (Live value)",
                             "raw_before": a[param], "raw_after": b[param], "kind": "parameter",
                             "reason": meta.get("reason") or "Changed in the saved result. This earlier run has no detailed decision journal for this parameter.",
                             "verification": meta.get("verification"), "section": meta.get("section"), "at": meta.get("at")})
    rejected = [r for r in records if r.get("status") == "rejected"]
    return {"ready": True, "kept": kept, "rejected": rejected, "before": Path(before_path).name, "after": Path(pair[1]).name if pair else None, "partial": partial,
            "channels": len({item["channel"] for item in kept}), "legacy": not bool(records)}


def restore_plan(folder: str, current: dict) -> dict:
    pair = latest_pair(folder)
    if not pair:
        raise ValueError("No complete Before/After snapshots are saved.")
    before, after = map(load, pair)
    # Measurement taps are managed by the engine and are not user mix edits.
    for snapshot in (before, after):
        for track in snapshot["tracks"].values():
            track["devices"] = {key: value for key, value in track["devices"].items() if not key.startswith("MixMatch Tap#")}
    for key, original in before["tracks"].items():
        if original["name"] == "MixMatch Capture":
            continue
        live_track = next((t for t in current["tracks"] if t["key"] == key and t["name"] == original["name"]), None)
        if live_track is None:
            raise ValueError("The tracks in Live no longer match this run.")
        available = set(ab._device_ids(live_track["devices"]))
        if {d for d in original["devices"] if not d.startswith("MixMatch Tap#")} - available:
            raise ValueError("An original device is missing. A complete restore requires the original Live set file.")
    if ab.changes_to(after, current, bypass_new=True):
        raise ValueError("Parameters have changed since this run. MARA will not overwrite your manual edits.")
    plan = ab.changes_to(before, current, bypass_new=True)
    return {"token": fingerprint(current), "changes": plan, "count": len(plan)}
