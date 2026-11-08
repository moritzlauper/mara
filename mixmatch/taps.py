"""Receives the measurement frames from the «MixMatch Tap» devices (OSC over UDP 9878)."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .bands import BAND_COUNT

FRAME_LEN = 5 + BAND_COUNT


@dataclass
class Frame:
    t: float
    m_rms: float
    s_rms: float
    s_low_rms: float
    peak: float
    bands: list[float]


@dataclass
class Tap:
    tap_id: int
    name: str = ""
    kind: str = "track"
    frames: deque = field(default_factory=lambda: deque(maxlen=3000))  # ~5 minutes at 10 fps

    @property
    def is_reference(self) -> bool:
        n = self.name.strip().lower()
        return n.startswith("ref") or n.startswith("[ref]")

    def window(self, since: float, until: float | None = None) -> list[Frame]:
        until = until or time.time()
        return [f for f in self.frames if since <= f.t <= until]


class TapHub:
    """Thread-safe store of everything the taps sent."""

    def __init__(self):
        self.taps: dict[int, Tap] = {}
        self.lock = threading.Lock()
        self.server = None

    def _tap(self, tap_id: int) -> Tap:
        if tap_id not in self.taps:
            self.taps[tap_id] = Tap(tap_id)
        return self.taps[tap_id]

    def on_name(self, tap_id, kind, *name):
        with self.lock:
            tap = self._tap(int(tap_id))
            tap.kind = str(kind)
            tap.name = " ".join(str(n) for n in name)

    def on_frame(self, tap_id, *values, t: float | None = None):
        if len(values) != FRAME_LEN:
            return
        v = [float(x) for x in values]
        with self.lock:
            self._tap(int(tap_id)).frames.append(
                Frame(t or time.time(), v[0], v[1], v[2], max(v[3], v[4]), v[5:])
            )

    def snapshot(self) -> list[Tap]:
        with self.lock:
            return list(self.taps.values())

    def by_name(self) -> dict[tuple[str, str], Tap]:
        """(kind, name) → tap, to match taps to the tracks the remote script reports."""
        return {(t.kind, t.name): t for t in self.snapshot() if t.name}

    def listen(self, host: str = "127.0.0.1", port: int = 9878) -> None:
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import ThreadingOSCUDPServer

        d = Dispatcher()
        d.map("/mm/tap", lambda _addr, *a: self.on_frame(*a))
        d.map("/mm/name", lambda _addr, *a: self.on_name(*a))
        self.server = ThreadingOSCUDPServer((host, port), d)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

