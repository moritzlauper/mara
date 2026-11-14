"""Live listening volume, separate from the mix and its measurement taps."""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import workflow


def main_volume(state: dict) -> dict:
    return next(p for t in state["tracks"] if t["key"] == "m"
                for p in t["mixer"] if p["name"] == "Volume")


def read(folder: str) -> dict:
    try:
        data = json.loads((Path(folder) / "monitor.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if data.get("run_id") == workflow.read(folder).get("id") else {}


def begin(folder: str, volume: float, silent: bool) -> None:
    # A level the UI saved when it muted Main belongs to an earlier run id: keep it, or a run that starts with
    # Main muted loses the user's listening level for good.
    try:
        previous = json.loads((Path(folder) / "monitor.json").read_text())
    except (OSError, ValueError):
        previous = {}
    saved = _saved_level(folder, previous if isinstance(previous, dict) else {}, 1.0)
    _write(folder, {"run_id": workflow.read(folder).get("id"),
                    "volume": volume if _audible(volume, 1.0) else saved, "managed": silent})


def _write(folder: str, data: dict) -> None:
    import os
    import uuid

    path = Path(folder) / "monitor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(data))
    os.replace(temporary, path)


def status(live) -> dict:
    state = live.get_set()
    volume = main_volume(state)
    master = next(t for t in state["tracks"] if t["key"] == "m")
    return {"silent": volume["value"] <= 1e-6 or master.get("output") == "Sends Only",
            "display": volume.get("display", "")}


def _route(folder):
    try:
        return json.loads((Path(folder) / "output-route.json").read_text()) if folder else None
    except (OSError, ValueError):
        return None


def disconnect(live, folder=None):
    """Gate the hardware route as well as the fader; taps keep measuring upstream."""
    master = next(t for t in live.get_set()["tracks"] if t["key"] == "m")
    # Simulators and older bridges without routing metadata retain fader muting.
    if master.get("output") is not None:
        result = live.request("preview_output", muted=True)
        if not result.get("muted"):
            raise RuntimeError("Live did not disconnect the hardware output.")
        # Keep the original route recoverable even if the Live bridge reloads.
        restore = (_route(folder) if master.get("output") == "Sends Only" else None) or result.get("restore")
        if folder and restore:
            import os
            import uuid
            path = Path(folder) / "output-route.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            tmp.write_text(json.dumps(restore))
            os.replace(tmp, path)


def reconnect(live, folder=None):
    master = next(t for t in live.get_set()["tracks"] if t["key"] == "m")
    if master.get("output") is not None:
        restore = _route(folder) if master.get("output") == "Sends Only" else None
        result = live.request("preview_output", muted=False, restore=restore)
        if result.get("muted"):
            raise RuntimeError("Live did not restore the hardware output.")
        if folder:
            (Path(folder) / "output-route.json").unlink(missing_ok=True)


def _audible(value, maximum):
    return isinstance(value, (int, float)) and math.isfinite(value) and 1e-6 < value <= maximum


def _saved_level(folder, data, maximum):
    if _audible(data.get('volume'), maximum):
        return data['volume']
    before = workflow.read(folder).get('before')
    paths = [Path(folder) / before] if before and Path(before).name == before else []
    paths += sorted(Path(folder).glob('*-before.json'), reverse=True)
    for path in paths:
        try:
            value = json.loads(path.read_text())['tracks']['m']['mixer']['Volume']
            if _audible(value, maximum):
                return value
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def set_silent(live, folder: str, silent: bool, target_db=None) -> dict:
    """Change only the Main fader; restore the last audible listening level."""
    volume = main_volume(live.get_set())
    if volume.get("automated"):
        raise ValueError("The Main fader is automated; change its automation in Live first.")
    # Recording temporarily raises the fader behind a disconnected hardware route.
    # Never let the UI reconnect that route halfway through the recording.
    if workflow.read(folder).get("preview_recording"):
        raise ValueError("The preview is recording silently. Current can be unmuted when it finishes.")
    current = volume["value"]
    data = read(folder)
    if current > 1e-6 and abs(current-data.get("applied_raw", -1)) > 1e-6:
        data["volume"] = current
    restore = _saved_level(folder, data, volume.get('max', 1))
    if restore is not None:
        data['volume'] = restore
    data.update(run_id=workflow.read(folder).get("id"), managed=True)
    _write(folder, data)
    target = 0.0 if silent else restore
    if not silent and restore is None and target_db is None:
        target_db = -12.0  # An explicit Unmute still works after recovery without a saved level.
    if silent:
        disconnect(live, folder)
    if target_db is not None and not silent:
        if not math.isfinite(target_db):
            raise ValueError("No valid listening level.")
        result = live.set("m", "mixer", "Volume", target=target_db)
    else:
        result = live.set("m", "mixer", "Volume", value=target)
    if restore is None and not silent:
        data['volume'] = result.get('value')
    data["applied_raw"] = result.get("value")
    _write(folder, data)
    if (target_db is None or silent) and abs(result.get("value", -1) - target) > 1e-6:
        raise RuntimeError("Live did not apply the requested Main volume.")
    if not silent:
        reconnect(live, folder)
    return {"silent": silent, "display": result.get("display", "")}


def normal_db(live, folder):
    from .display import parse_display
    data=read(folder)
    volume=main_volume(live.get_set())
    raw=_saved_level(folder, data, volume.get('max',1))
    if volume["value"] > 1e-6 and abs(volume["value"]-data.get("applied_raw",-1))>1e-6:
        raw=volume["value"]
    if raw is None:
        return -12.0
    result=live.request("format_values",track="m",device="mixer",param="Volume",values=[raw])
    value=parse_display(result["displays"][0])
    if value is None or not math.isfinite(value):
        raise ValueError("No saved audible Main level for loudness matching.")
    return value


def finish(live, folder: str) -> float:
    """Leave a completed run ready to listen at unity, never raw zero (mute)."""
    from .display import parse_display

    result = live.set("m", "mixer", "Volume", target=0.0)
    level = parse_display(result.get("display", ""))
    if level is None or not math.isfinite(level) or abs(level) > 0.05:
        raise RuntimeError("Live did not set the Main fader to 0 dB.")
    raw = result["value"]
    reconnect(live, folder)
    _write(folder, {"run_id": workflow.read(folder).get("id"),
                    "volume": raw, "applied_raw": raw, "managed": False})
    return raw
