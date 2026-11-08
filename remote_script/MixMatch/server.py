"""Non-blocking line-based JSON server, polled from Live's main thread.

Live's API may only be touched from the main thread, so there is no
background thread: `poll()` is called about every 100 ms from
`update_display` and handles whatever arrived since.
"""
import json
import socket


class LineServer:
    def __init__(self, handler, host="127.0.0.1", port=9877, log=None):
        self.handler = handler
        self.log = log or (lambda *_: None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(4)
        self.sock.setblocking(False)
        self.clients = {}

    def poll(self):
        try:
            while True:
                conn, _ = self.sock.accept()
                conn.setblocking(False)
                self.clients[conn] = b""
        except (BlockingIOError, OSError):
            pass
        for conn in list(self.clients):
            try:
                data = conn.recv(65536)
                if not data:
                    self._drop(conn)
                    continue
                self.clients[conn] += data
            except BlockingIOError:
                pass
            except OSError:
                self._drop(conn)
                continue
            while conn in self.clients and b"\n" in self.clients[conn]:
                line, self.clients[conn] = self.clients[conn].split(b"\n", 1)
                if line.strip():
                    self._answer(conn, line)

    def _answer(self, conn, line):
        req = {}
        try:
            req = json.loads(line.decode("utf-8"))
            reply = {"id": req.get("id"), "ok": True, "result": self.handler(req)}
        except Exception as e:
            reply = {"id": req.get("id") if isinstance(req, dict) else None, "ok": False, "error": str(e)}
        try:
            conn.setblocking(True)
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            conn.setblocking(False)
        except OSError:
            self._drop(conn)

    def _drop(self, conn):
        self.clients.pop(conn, None)
        try:
            conn.close()
        except OSError:
            pass

    def close(self):
        for conn in list(self.clients):
            self._drop(conn)
        self.sock.close()
