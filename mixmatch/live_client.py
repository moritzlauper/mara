"""Client for the MixMatch remote script inside Live (TCP 127.0.0.1:9877)."""
from __future__ import annotations

import json
import socket
import threading


class LiveError(Exception):
    pass


class LiveClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9877, timeout: float = 5.0):
        self.addr = (host, port)
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.buf = b""
        self.next_id = 1
        self.lock = threading.Lock()

    def connect(self) -> "LiveClient":
        self.sock = socket.create_connection(self.addr, timeout=self.timeout)
        return self

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def request(self, cmd: str, **kw):
        with self.lock:
            if self.sock is None:
                self.connect()
            rid = self.next_id
            self.next_id += 1
            self.sock.sendall((json.dumps({"id": rid, "cmd": cmd, **kw}) + "\n").encode())
            while True:
                while b"\n" not in self.buf:
                    chunk = self.sock.recv(1 << 20)
                    if not chunk:
                        raise LiveError("Live closed the connection")
                    self.buf += chunk
                line, self.buf = self.buf.split(b"\n", 1)
                reply = json.loads(line)
                if reply.get("id") == rid:
                    break
        if not reply.get("ok"):
            raise LiveError(reply.get("error", "unknown error"))
        return reply["result"]

    # Convenience wrappers
    def get_set(self) -> dict:
        return self.request("get_set")

    def set(self, track: str, device, param, *, target: float | None = None, value: float | None = None) -> dict:
        kw = {"track": track, "device": device, "param": param}
        kw["target" if target is not None else "value"] = target if target is not None else value
        return self.request("set", **kw)

    def batch(self, changes: list[dict]) -> dict:
        return self.request("batch", changes=changes)

    def load_device(self, track: str, name: str, source: str = "audio_effects") -> int:
        return self.request("load_device", track=track, name=name, source=source)["index"]

    def transport(self, **kw) -> dict:
        return self.request("transport", **kw)
