"""A short, durable review window before the first mix change."""
import json
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(folder):
    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    with (path / "analysis-review.lock").open("a+b") as file:
        if os.name == "nt":
            import msvcrt
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)


def read(folder):
    try:
        return json.loads((Path(folder) / "analysis-review.json").read_text())
    except (OSError, ValueError):
        return {}


def write(folder, data):
    path = Path(folder) / "analysis-review.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    os.replace(temporary, path)


def request(folder, run_id, action):
    if action not in {"hold", "continue"}:
        raise ValueError("Choose Edit roles or Continue now.")
    with locked(folder):
        data = read(folder)
        if not run_id or data.get("run_id") != run_id or data.get("closed"):
            raise ValueError("The review window has ended. Corrections apply on the next run.")
        data.update(held=action == "hold", proceed=action == "continue")
        write(folder, data)
    return data


def wait(folder, run_id, seconds, env):
    from . import workflow

    with locked(folder):
        write(folder, {"run_id": run_id, "deadline": env.now() + seconds,
                       "held": False, "proceed": False, "closed": False})
    workflow.update(status="reviewing", phase="analysis", analysis_complete=True)
    while True:
        workflow.check_pause()
        with locked(folder):
            data = read(folder)
            if data.get("proceed") or (not data.get("held") and env.now() >= data["deadline"]):
                write(folder, {**data, "closed": True})
                return
        env.wait(0.2)
