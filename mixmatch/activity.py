"""Small, structured status updates for long operations, independent of overall progress."""
import json
import time


def report(log, text: str, *, started: float | None = None, current=None, total=None) -> None:
    from . import workflow
    workflow.update(activity={"text": text, "started": started or time.time(), "current": current, "total": total})
    log("activity " + json.dumps({"text": text, "started": started if started is not None else time.time(),
                                  "current": current, "total": total}, ensure_ascii=False))


def focus(log, *, channel: str, device: str | None = None, region=None) -> None:
    """Keep the inspected channel/device separate from measurement and planner activity."""
    from . import workflow
    data = {"channel": channel, "device": device}
    if region:
        data.update(start_bar=region[0] / 4 + 1, end_bar=(region[0] + region[1]) / 4 + 1)
    workflow.update(focus=data)
    log("focus " + json.dumps(data, ensure_ascii=False))
