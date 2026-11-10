"""Recorded A/B excerpts for playback independently of Live's silent mixing loop."""
from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import threading
from functools import lru_cache
from pathlib import Path

from . import ab

_MATCH_LOCK = threading.Lock()


@lru_cache(maxsize=32)
def _loudness(path: str, mtime_ns: int, size: int) -> float | None:
    """Integrated loudness of the actual excerpt, not its peak or the mix score."""
    import numpy as np
    import pyloudnorm as pyln
    import soundfile as sf

    data, rate = sf.read(path, always_2d=True)
    if len(data) < rate * 0.4 or not np.isfinite(data).all():
        return None
    value = float(pyln.Meter(rate).integrated_loudness(data))
    return value if math.isfinite(value) else None


def matching(log_dir: str) -> dict:
    """Use the quieter excerpt as target: constant attenuation, no new clipping."""
    versions = available(log_dir)
    if not all(versions.values()):
        return {"ready": False, "reason": "Available when both previews are ready."}
    before, after = versions["before"], versions["after"]
    if (before.get("start_bar"), before.get("bars")) != (after.get("start_bar"), after.get("bars")):
        return {"ready": False, "reason": "The previews cover different passages."}
    if abs(before.get("seconds", 0) - after.get("seconds", 0)) > 0.1:
        return {"ready": False, "reason": "The preview lengths differ."}
    levels, stamps = {}, []
    try:
        for which, meta in versions.items():
            path = os.path.join(log_dir, meta["id"] + ".wav")
            st = os.stat(path)
            levels[which] = _loudness(path, st.st_mtime_ns, st.st_size)
            stamps.append((meta["id"], st.st_mtime_ns, st.st_size))
    except (OSError, ValueError, RuntimeError):
        return {"ready": False, "reason": "Could not measure the audio previews."}
    if any(level is None for level in levels.values()):
        return {"ready": False, "reason": "A preview is silent or too short to measure."}
    target = min(levels.values())
    return {"ready": True, "target_lufs": target,
            "gain_db": {k: min(0.0, target - v) for k, v in levels.items()},
            "version": hashlib.sha256(json.dumps(stamps).encode()).hexdigest()[:16]}


def matched_file(log_dir: str, which: str, identity: str) -> str | None:
    path = selected_file(log_dir, which, identity)
    match = matching(log_dir)
    if path is None or not match["ready"]:
        return None
    gain = match["gain_db"][which]
    if abs(gain) < 1e-6:
        return path
    dest = os.path.splitext(path)[0] + "-equal-" + match["version"] + ".wav"
    with _MATCH_LOCK:
        if not os.path.isfile(dest):
            import soundfile as sf

            data, rate = sf.read(path, always_2d=True)
            sf.write(dest + ".part", data * 10 ** (gain / 20), rate, format="WAV", subtype="PCM_24")
            os.replace(dest + ".part", dest)
    return dest


class RestoreFailed(RuntimeError):
    pass


def audio_path(snapshot: str) -> str:
    return os.path.splitext(snapshot)[0] + ".wav"


def metadata_path(snapshot: str) -> str:
    return os.path.splitext(snapshot)[0] + "-preview.json"


def copy_preview(source: str, destination: str) -> None:
    for path_fn in (audio_path, metadata_path):
        src, dst = path_fn(source), path_fn(destination)
        if src != dst and os.path.isfile(src):
            shutil.copyfile(src, dst)


def available(log_dir: str) -> dict:
    before = ab.latest_before(log_dir)
    result = {"before": None, "after": None}
    if before is None:
        return result
    for which in result:
        snap = before if which == "before" else before.removesuffix("before.json") + "after.json"
        try:
            meta = json.loads(Path(metadata_path(snap)).read_text())
            if os.path.isfile(snap) and os.path.getsize(audio_path(snap)) > 44:
                result[which] = {**meta, "id": Path(snap).stem}
        except (OSError, ValueError):
            pass
    return result


def selected_file(log_dir: str, which: str, identity: str) -> str | None:
    if which not in ("before", "after"):
        return None
    ready = available(log_dir)[which]
    if not ready or ready["id"] != identity:
        return None
    return os.path.join(log_dir, ready["id"] + ".wav")


def _apply(live, target: dict) -> None:
    changes = ab.changes_to(target, live.get_set(), bypass_new=True)
    for i in range(0, len(changes), 200):
        if live.batch(changes[i:i + 200]).get("errors"):
            raise RuntimeError("could not restore all preview parameters")


def render(live, snapshot: str, start: float, length: float, log=print) -> dict:
    """Temporarily recall the snapshot, record it silently, then restore the set.

    Keep Main's original gain in the recording. Silence the hardware route,
    not the fader; otherwise Live's Resampling records silence as well.
    The caller must own Live exclusively (no mixing round can run concurrently).
    """
    import numpy as np
    import soundfile as sf
    from .autoref import capture_begin, capture_end
    from .activity import report as activity

    target = json.loads(Path(snapshot).read_text())
    current = live.get_set()
    saved = ab.snapshot(current)
    loop = current["loop"]
    position, playing = current.get("song_time", loop["start"]), current.get("is_playing", False)
    activity(log, f"Recording {Path(snapshot).stem.rsplit('-', 1)[-1]} preview · {length / 4:g} bars")
    disconnected = False
    restore_output = None
    try:
        live.transport(play=False)
        routing = live.request("preview_output", muted=True)
        restore_output = routing.get("restore")
        disconnected = True
        _apply(live, target)
        aligned = current.get("capabilities", {}).get("aligned_capture", False)
        if aligned:
            live.transport(loop_start=start, loop_length=length, loop=True, time=start, play=False)
            cap = capture_begin(live, length, start=start)
        else:
            # Older bridges retain compatibility; metadata explicitly identifies them.
            live.transport(loop_start=start, loop_length=length, loop=True, time=max(0, start - 4), play=True)
            cap = capture_begin(live, length)
        path = capture_end(live, cap, timeout=(length + 4) * 60 / current["tempo"] + 20)
        if path is None:
            raise RuntimeError("preview recording did not finish")
        data, sr = sf.read(path, always_2d=True)
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        seconds = len(data) / sr
        if not np.isfinite(data).all() or peak < 1e-5 or seconds < length * 60 / current["tempo"] * 0.9:
            raise RuntimeError(f"preview recording is silent or incomplete ({seconds:.2f} s, peak {peak:.6f})")
        dest = audio_path(snapshot)
        sf.write(dest + ".part", data, sr, format="WAV", subtype="PCM_24")
        os.replace(dest + ".part", dest)
        meta = {"start_bar": start / 4 + 1, "bars": length / 4, "seconds": seconds,
                "peak_dbfs": 20 * float(np.log10(peak)), "aligned_start": aligned}
        tmp = metadata_path(snapshot) + ".part"
        Path(tmp).write_text(json.dumps(meta))
        os.replace(tmp, metadata_path(snapshot))
        return meta
    finally:
        try:
            live.transport(play=False)
            _apply(live, saved)
            live.transport(loop_start=loop["start"], loop_length=loop["length"], loop=loop["on"], time=position)
        except Exception as e:
            # Keep hardware disconnected if restoring the mix failed.
            raise RestoreFailed("Preview stopped: could not restore the mix; output stays disconnected") from e
        if disconnected:
            try:
                live.request("preview_output", muted=False, restore=restore_output)
            except Exception as e:
                raise RestoreFailed("Preview stopped: could not restore the hardware output") from e
        if playing:
            live.transport(play=True, time=position)


def repair(log_dir="runs", log=print):
    """Finish only missing audio previews; never rerun planning or mixing."""
    from . import workflow
    from .live_client import LiveClient
    pair = ab.latest_pair(log_dir)
    if not pair:
        raise ValueError("No saved result is available to record a preview.")
    state = workflow.read(log_dir)
    if (Path(log_dir) / "checkpoint.json").exists() and not state.get("mix_complete"):
        raise ValueError("Resume the mixing pass before repairing the final preview.")
    meta = None
    for path in pair:
        try:
            meta = json.loads(Path(metadata_path(path)).read_text())
            break
        except (OSError, ValueError):
            pass
    if not meta:
        try:
            ck = json.loads((Path(log_dir) / "checkpoint.json").read_text())
            meta = {"start_bar": ck["main"][1] / 4 + 1, "bars": min(ck["main"][2], 64) / 4}
        except (OSError, ValueError, KeyError):
            raise ValueError("The original preview passage is missing. Analyse the mix again.")
    workflow.begin(log_dir, kind="repair", resume=True)
    workflow.update(status="repairing", mix_complete=True, before=Path(pair[0]).name, after=Path(pair[1]).name)
    live = LiveClient()
    try:
        live.connect()
        for which, snapshot in zip(("before", "after"), pair):
            if not available(log_dir)[which]:
                render(live, snapshot, (meta["start_bar"]-1)*4, meta["bars"]*4, log)
        if not matching(log_dir)["ready"]:
            raise RuntimeError("These previews cannot be loudness matched together.")
        workflow.update(status="finished", phase="result", output_restored=True, previews_ready=True, error=None)
        log("Done. Audio previews repaired; mix unchanged.")
    except BaseException as e:
        workflow.update(status="preview_failed", error=str(e))
        raise
    finally:
        live.close()
        workflow.end()


def listening_file(folder, identity, target):
    """A constant, attenuation-only gain for the live comparison player."""
    path=selected_file(folder,"before",identity)
    if not path or not math.isfinite(target):
        return None
    st=os.stat(path)
    level=_loudness(path,st.st_mtime_ns,st.st_size)
    if level is None or target > level + .01 or target < -80:
        return None
    gain=min(0.0,target-level)
    dest=os.path.splitext(path)[0]+f"-listen-{target:.4f}.wav"
    with _MATCH_LOCK:
        if not os.path.isfile(dest):
            import soundfile as sf
            data,rate=sf.read(path,always_2d=True)
            sf.write(dest+".part",data*10**(gain/20),rate,format="WAV",subtype="PCM_24")
            os.replace(dest+".part",dest)
    return dest
