"""Stable display progress across resumed logs and revised work budgets."""
import json
import math
import threading
from pathlib import Path

_lock = threading.Lock()


def estimate(lines):
    previous = None
    baseline = None
    elapsed = units = peak = 0.0
    last = None
    for line in lines:
        text = line.strip()
        if text.startswith('Taps:'):
            previous = baseline = None
        if not text.startswith('progress {'):
            continue
        try:
            point = json.loads(text[len('progress '):])
            done, total, stamp = (float(point[k]) for k in ('done', 'total', 't'))
            if not all(math.isfinite(v) for v in (done, total, stamp)) or total <= 0 or done < 0:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        peak = max(peak, min(99, math.floor(100 * done / total)))
        if baseline is not None:
            delta, duration = done - baseline['done'], stamp - baseline['t']
            # Resume restores completed units in one jump; it is not newly completed work.
            if delta < 0 or (delta > 5 and duration < 10):
                baseline = point
            elif delta > 0 and duration > 0:
                elapsed += duration
                units += delta
                baseline = point
        else:
            baseline = point
        previous = last = point
    remaining = max(0, last['total'] - last['done']) if last else 0
    seconds = remaining * elapsed / units if units >= 1 and elapsed >= 30 else None
    return peak, seconds, last


def presentation(folder, family, lines, *, finished=False):
    peak, seconds, point = estimate(lines)
    path = Path(folder) / 'progress-display.json'
    with _lock:
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            saved = {}
        if saved.get('family') != family:
            saved = {'family': family}
        percent = 100 if finished else min(99, max(peak, saved.get('percent', 0)))
        signature = [point.get(k) for k in ('t', 'done', 'total')] if point else None
        eta = saved.get('seconds')
        if signature != saved.get('point') and seconds is not None:
            # Update only when work advances, not every browser poll. Avoid large estimate jumps.
            eta = seconds if eta is None else eta + max(-120, min(120, (seconds - eta) * .2))
        data = {'family': family, 'percent': percent, 'seconds': eta, 'point': signature}
        if data != saved:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(data))
            tmp.replace(path)
    return {'percent': percent, 'eta_minutes': max(1, math.ceil(eta / 60)) if eta is not None and not finished else None}
