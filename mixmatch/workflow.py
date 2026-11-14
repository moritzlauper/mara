"""Durable run lifecycle. Human-readable logs are diagnostics, never the new run's state machine."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

ACTIVE = {"preparing", "analysing", "reviewing", "mixing", "pausing", "finalizing", "repairing"}
_local = threading.local()


def read(folder: str) -> dict:
    try:
        return json.loads((Path(folder) / "run-state.json").read_text())
    except (OSError, ValueError):
        return {}


def write(folder: str, data: dict) -> dict:
    path = Path(folder) / "run-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**data, "updated_at": time.time()}
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)
    return data


def begin(folder="runs", *, kind="mix", resume=False):
    folder = str(Path(folder).resolve())
    _local.folder = folder
    previous = read(folder) if resume else {}
    data = {"schema": 1, "id": uuid.uuid4().hex, "started_at": time.time(), "kind": kind,
            "status": "analysing" if kind == "analysis" else "preparing", "resumed": resume, "phase": "preparation",
            "mix_complete": False, "output_restored": False, "previews_ready": False, "live_goal_updates": True,
            "before": previous.get("before"), "after": None, "error": None}
    (Path(folder) / "pause.request").unlink(missing_ok=True)
    write(folder, data)
    return data


def update(**values):
    folder = getattr(_local, "folder", None)
    if folder:
        write(folder, {**read(folder), **values})


def end():
    _local.folder = None


def check_pause():
    folder = getattr(_local, "folder", None)
    if folder and (Path(folder) / "pause.request").exists():
        raise KeyboardInterrupt("pause requested at a safe step boundary")


def event(folder: str, data: dict):
    path = Path(folder) / "change-journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"at": time.time(), **data}, ensure_ascii=False, default=str) + "\n")
        f.flush()


def presentation(folder: str, legacy: dict, *, active: bool, previews: dict, checkpoint: dict | None) -> dict:
    """Only the isolated legacy adapter reads log-derived completion; new runs have explicit states."""
    state = read(folder)
    if state:
        state = dict(state)
        if not active and state.get("status") in ACTIVE:
            state.update(status="interrupted", error="MARA was interrupted. The last safe checkpoint is saved.")
        if state.get("status") == "finished" and not all(previews.values()):
            state.update(status="preview_failed", previews_ready=False)
    else:
        done = legacy.get("done") or ""
        complete = done.startswith("Done.") and checkpoint is None
        status = ("mixing" if active else "ready" if checkpoint and checkpoint.get("phase") == "references" else
                  "finished" if complete and all(previews.values()) else "preview_failed" if complete else
                  "paused" if checkpoint else "failed" if done.startswith("Stopped:") else "idle")
        state = {"schema": 0, "status": status, "phase": legacy.get("phase"), "mix_complete": complete,
                 "previews_ready": all(previews.values()), "output_restored": complete,
                 "error": done if status == "failed" else None, "legacy": True}
    if state.get("status") == "reviewing":
        from .analysis_review import read as read_review
        review = read_review(folder)
        if review.get("run_id") == state.get("id"):
            state["review"] = review
    status = state["status"]
    state["stage"] = ("result" if status in {"finished", "preview_failed", "restored"} else
                      # The analysis (with reference search) belongs to «Your mix»; once it is ready, «References».
                      "references" if state.get("purpose") == "references" and status == "ready" else
                      "analysis" if state.get("purpose") == "references" and status in
                      {"analysing", "preparing", "failed", "interrupted", "paused"} else
                      "analysis" if status == "reviewing" or status in {"preparing", "analysing"} and not state.get("resumed") else
                      # A failed analysis (also reference discovery) never reached the mix: show it where it stopped.
                      "analysis" if status == "failed" and state.get("kind") == "analysis" else
                      "mix" if status in ACTIVE | {"paused", "interrupted", "failed"} else
                      "analysis" if status == "ready" else "project")
    return state


def start_over(folder: str) -> dict:
    """Discard a stopped run's progress: the next start analyses the song again. The mix in Live, logs,
    recordings and references stay; only the saved place to continue from (and its analysis) goes."""
    for name in ("checkpoint.json", "understanding.json", "pause.request", "cancel.request"):
        (Path(folder) / name).unlink(missing_ok=True)
    return write(folder, {"schema": 1, "id": uuid.uuid4().hex, "kind": "mix", "status": "idle", "phase": None,
                          "error": None, "mix_complete": False, "previews_ready": False})
