"""Small, structured status updates for long operations, independent of overall progress."""
import json
import time


def report(log, text: str, *, started: float | None = None, current=None, total=None) -> None:
    log("activity " + json.dumps({"text": text, "started": started if started is not None else time.time(),
                                  "current": current, "total": total}, ensure_ascii=False))
