"""A small local web interface: start and follow runs, see the song, references and effects, edit roles.

    mixmatch ui            → http://127.0.0.1:8765

Standard library only. Runs `mixmatch run` as a child process and reads its
log; nothing here talks to Live directly, so the interface can be opened and
closed while a run goes on.
"""
from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
AUDIO = (".wav", ".aif", ".aiff", ".flac", ".mp3")
CONVERT = (".m4a", ".aac", ".mp4", ".caf")
FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")   # AAC (e.g. bought on iTunes): turned into WAV with macOS afconvert


def to_wav(path: str) -> str:
    """Converts an AAC file to a 16-bit WAV beside it and removes the original; returns the WAV's path."""
    import subprocess

    out = os.path.splitext(path)[0] + ".wav"
    r = subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16", path, out], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise ValueError(f"could not convert {os.path.basename(path)}: {(r.stderr or '').strip()[:120]}")
    os.remove(path)
    return out


def preview_name(workdir: str, filename: str) -> str | None:
    """«Artist – Title (Apple preview)» for a downloaded Apple preview («mzaf_….plus.aac.p.m4a»), found through the
    suggestion lists and the preview cache; None for any other file."""
    from .style import cached_preview

    stem = os.path.basename(filename).split(".")[0]
    if not stem.startswith("mzaf_"):
        return None
    for path in sorted(glob.glob(os.path.join(workdir, "runs", "*-buy.md")), reverse=True):
        for title in re.findall(r"^- \*\*(.+?)\*\*", open(path).read(), flags=re.M):
            if " – " in title:
                url = cached_preview(*title.split(" – ", 1)).get("preview", "")
                if url.rsplit("/", 1)[-1].split(".")[0] == stem:
                    return f"{title} (Apple preview)".replace("/", "-")
    return None


def duration_s(path: str) -> float:
    import soundfile as sf

    info = sf.info(path)
    return info.frames / info.samplerate


class Runner:
    """The one `mixmatch` child process the interface may start, and its log file."""

    def __init__(self, workdir: str):
        self.workdir = workdir
        self.proc: subprocess.Popen | None = None
        self.log_path: str | None = None
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str]) -> str:
        with self.lock:
            if self.running():
                raise RuntimeError("a run is already going on")
            os.makedirs(os.path.join(self.workdir, "runs"), exist_ok=True)
            # Only runs are «ui-…» logs: the run view, its history and resumed runs are read from them.
            prefix = "ui-" if args[:1] == ["run"] else f"tool-{args[0]}-"
            self.log_path = os.path.join(self.workdir, "runs", time.strftime(prefix + "%Y%m%d-%H%M%S.log"))
            log = open(self.log_path, "w")
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            self.proc = subprocess.Popen([sys.executable, "-m", "mixmatch.cli", *args], cwd=self.workdir,
                                         stdout=log, stderr=subprocess.STDOUT, env=env)
            return self.log_path

    def stop(self) -> None:
        if self.running():
            self.proc.send_signal(signal.SIGINT)

    def latest_log(self) -> str | None:
        if self.log_path and (self.running() or os.path.basename(self.log_path).startswith("ui-")):
            return self.log_path   # a tool (installing taps) is shown while it works, then the last run again
        logs = sorted(glob.glob(os.path.join(self.workdir, "runs", "ui-*.log")))
        return logs[-1] if logs else None


# ── reading a run's log ─────────────────────────────────────────────────────
PARTS = re.compile(r"\(([^()]*)\)\s*$")


def _parts(text: str) -> dict:
    m = PARTS.search(text)
    out = {}
    if m:
        for item in m.group(1).split("·"):
            bits = item.strip().rsplit(" ", 1)
            if len(bits) == 2:
                try:
                    out[bits[0]] = float(bits[1])
                except ValueError:
                    pass
    return out


def checkpoint(workdir: str) -> dict | None:
    """Where the last stopped run stands (runs/checkpoint.json), for «Continue»; None after a complete run."""
    try:
        with open(os.path.join(workdir, "runs", "checkpoint.json")) as f:
            ck = json.load(f)
    except (OSError, ValueError):
        return None
    return {"phase": ck.get("phase"), "next_channel": ck.get("next_channel"), "stamp": ck.get("stamp"),
            "percent": round(100 * ck["done"] / ck["total"]) if ck.get("total") else None}


def phase_label(phase: str | None) -> str:
    p = (phase or "").lower()
    return ("final" if p.startswith("final") else "channels" if p.startswith("channel") else
            "effects" if p.startswith("effects") else "master" if p.startswith("master") else "setup")


def log_chain(path: str) -> list[str]:
    """The log of a run and, if it was resumed, the logs of the runs it continues, oldest first."""
    logs = sorted(glob.glob(os.path.join(os.path.dirname(path), "ui-*.log")))
    chain = [path]
    i = logs.index(path) if path in logs else 0
    while i > 0 and any(m in open(chain[0]).read() for m in ("━━ Channel pass, resumed at", "↻ Continuing the run")):
        i -= 1
        chain.insert(0, logs[i])
    return chain


def parse_log(lines: list[str]) -> dict:
    """Phase, current channel, distance history, decisions, song info and effects from a run's log."""
    state = {"phase": None, "section": None, "distance": None, "parts": {}, "history": [], "events": [],
             "key": None, "style": None, "mood": None, "explain": {}, "effects": [], "references": [], "to_get": [], "done": None,
             "history_phase": [], "history_run": [], "progress": None, "progress_start": None}
    run = 0

    def point(value: float) -> None:
        state["history"].append(value)
        state["history_phase"].append(phase_label(state["phase"]))
        state["history_run"].append(run)

    for raw in lines:
        line = raw.rstrip("\n")
        s = line.strip()
        if s.startswith("Taps:"):            # the first lines of every run: a resumed run starts its own log
            run += 1
            state["progress"] = state["progress_start"] = state["done"] = None
        elif s.startswith("progress {"):
            try:
                p = json.loads(s[len("progress "):])
            except ValueError:
                continue
            state["progress"] = p
            if state["progress_start"] is None:
                state["progress_start"] = p
        elif s.startswith("━━"):
            state["phase"] = s.strip("━ ").strip()
            state["events"].append({"kind": "phase", "text": state["phase"]})
        elif s.startswith("▶"):
            state["section"] = s[1:].strip()
            state["events"].append({"kind": "section", "text": state["section"]})
        elif s.startswith("distance "):
            m = re.match(r"distance ([\d.]+)", s)
            if m:
                state["distance"], state["parts"] = float(m.group(1)), _parts(s)
                point(state["distance"])
        elif s.startswith("✓") and "→" in s:
            m = re.search(r"→ ([\d.]+)", s)
            if m:
                state["distance"], state["parts"] = float(m.group(1)), _parts(s) or state["parts"]
                point(state["distance"])
            state["events"].append({"kind": "kept", "text": s})
        elif s.startswith("↺ undone, no measurable") or s.startswith("= no measurable"):
            state["events"].append({"kind": "neutral", "text": s})
        elif s.startswith("↺"):
            state["events"].append({"kind": "undone", "text": s})
        elif s.startswith("round "):
            state["events"].append({"kind": "round", "text": s})
        elif s.startswith("·"):
            state["events"].append({"kind": "action", "text": s[1:].strip()})
        elif s.startswith("sits well"):
            state["events"].append({"kind": "neutral", "text": s})
        elif s.startswith("explain "):
            try:
                state["explain"] = json.loads(s[len("explain "):])
            except ValueError:
                pass
        elif s.startswith("Key:"):
            state["key"] = s[4:].strip()
        elif s.startswith("sounds like:"):
            state["style"] = s.split(":", 1)[1].strip()
        elif s.startswith(("⊘", "− removed", "✓ kept")):
            state["effects"].append(s)
        elif s.startswith("✓ ") and "[" in s:
            state["references"].append(s[1:].strip())
        elif s.startswith("Stopped:") or s.startswith("Done."):
            state["done"] = s
        elif "Traceback" in s or s.startswith("⚠"):
            state["events"].append({"kind": "warning", "text": s})
    state["events"] = state["events"][-150:]
    return state


def buy_list(workdir: str) -> list[dict]:
    files = sorted(glob.glob(os.path.join(workdir, "runs", "*-buy.md")))
    if not files:
        return []
    out, free = [], False
    for line in open(files[-1]):
        if line.startswith("## "):
            free = line.strip().lower() == "## free"
        m = re.match(r"- \*\*(.+?)\*\*(?: \((.+?)\))?: (.*?)\s*$", line)
        if m:
            out.append({"title": m.group(1), "why": m.group(3), "links": {}, "free": free,
                        **({"source": m.group(2)} if m.group(2) else {})})
        for name, url in re.findall(r"\[([^\]]+)\]\((\S+?)\)", line):
            if out and not m:
                out[-1]["links"][name] = url
    from .style import cached_preview

    missing = []
    for item in out:
        if "Preview" not in item["links"] and " – " in item["title"]:
            artist, title = item["title"].split(" – ", 1)
            found = cached_preview(artist, title)
            if found.get("preview"):
                item["links"]["Preview"] = found["preview"]
            elif (artist, title) not in _PREVIEW_ASKED:
                _PREVIEW_ASKED.add((artist, title))
                missing.append((artist, title))
    if missing:   # looked up once in the background (cached), the next refresh shows the players
        from .style import apple_preview

        threading.Thread(target=lambda: [apple_preview(a, t) for a, t in missing], daemon=True).start()
    return out


_PREVIEW_ASKED: set = set()


def trash(path: str) -> str:
    """Moves a file to the macOS Trash (MIXMATCH_TRASH overrides), so a removed reference can be put back."""
    folder = os.environ.get("MIXMATCH_TRASH") or os.path.expanduser("~/.Trash")
    os.makedirs(folder, exist_ok=True)
    name = os.path.basename(path)
    target = os.path.join(folder, name)
    if os.path.exists(target):
        stem, ext = os.path.splitext(name)
        target = os.path.join(folder, f"{stem} {time.strftime('%H.%M.%S')}{ext}")
    os.replace(path, target)
    return target


def safe_reference(workdir: str, name: str) -> str:
    """Path of a file directly inside references/; refuses anything else (no ../, no other folders)."""
    base = os.path.basename(name or "")
    if not base or base != name or not base.lower().endswith(AUDIO + CONVERT):
        raise ValueError(f"not an audio file MixMatch can read: {name!r} (WAV, AIFF, FLAC, MP3 or M4A)")
    return os.path.join(workdir, "references", base)


def project_references() -> list[dict] | None:
    """Reference tracks inside the open Live set (read-only, quick); None when Live does not answer
    (not open, or busy, e.g. exporting)."""
    try:
        from .live_client import LiveClient

        live = LiveClient(timeout=1.5).connect()
        try:
            return [{"track": c["track"], "file": os.path.basename(c["file"])}
                    for c in live.request("reference_clips")["clips"]]
        finally:
            live.close()
    except Exception:
        return None


def download_reference(workdir: str, url: str, name: str) -> str:
    """Downloads an allowed free track (Jamendo API links only) into references/."""
    import urllib.request
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if not (host == "jamendo.com" or host.endswith(".jamendo.com")):
        raise ValueError("only Jamendo downloads are fetched directly; use «Get» and «Import from Downloads»")
    safe = re.sub(r"[^\w\-. ]+", "", name).strip() or "jamendo"
    path = os.path.join(workdir, "references", safe[:80] + ".mp3")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    urllib.request.urlretrieve(url, path + ".part")
    os.replace(path + ".part", path)
    return os.path.basename(path)


def import_downloads(workdir: str, folder: str | None = None, minutes: float = 60) -> list[str]:
    """Audio files (also inside ZIPs) that arrived in the Downloads folder in the last hour, copied to references/."""
    import shutil
    import zipfile

    folder = folder or os.path.expanduser("~/Downloads")
    since = time.time() - minutes * 60
    target = os.path.join(workdir, "references")
    os.makedirs(target, exist_ok=True)
    added = []
    for name in sorted(os.listdir(folder), key=lambda n: -os.path.getmtime(os.path.join(folder, n))):
        path = os.path.join(folder, name)
        if os.path.getmtime(path) < since or not os.path.isfile(path):
            continue
        if name.lower().endswith(AUDIO):
            if not os.path.exists(os.path.join(target, name)):
                shutil.copy2(path, os.path.join(target, name))
                added.append(name)
        elif name.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                for member in z.namelist():
                    base = os.path.basename(member)
                    if base.lower().endswith(AUDIO) and not base.startswith("._") and \
                            not os.path.exists(os.path.join(target, base)):
                        with z.open(member) as src, open(os.path.join(target, base), "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        added.append(base)
    return added


def _words(text: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " " + re.sub(r"[^a-z0-9]+", " ", re.sub(r"\(.*?\)|\[.*?\]", " ", text)).strip() + " "


def matches(name: str, suggestion: dict) -> bool:
    """A downloaded file name belongs to a suggestion: its title as whole words, and its lead artist."""
    title_artist = suggestion["title"].split(" – ", 1)
    if len(title_artist) != 2:
        return False
    artist, title = title_artist
    lead = re.split(r",|&| x | and ", artist, maxsplit=1)[0]
    n = _words(name)
    return _words(title) in n and _words(lead) in n


class DownloadWatcher:
    """Picks up purchased or free downloads of suggested references: new, finished audio files or ZIPs in the
    Downloads folder whose names match a suggestion go to references/ by themselves."""

    PARTIAL = (".crdownload", ".part", ".download", ".tmp")

    def __init__(self, workdir: str, folder: str | None = None):
        self.workdir, self.folder = workdir, folder or os.path.expanduser("~/Downloads")
        self.started, self.sizes, self.done, self.imported = time.time(), {}, set(), []

    def scan(self, suggestions: list[dict]) -> list[str]:
        import shutil
        import zipfile

        if not suggestions or not os.path.isdir(self.folder):
            return []
        added = []
        target = os.path.join(self.workdir, "references")
        for name in os.listdir(self.folder):
            path = os.path.join(self.folder, name)
            if path in self.done or name.lower().endswith(self.PARTIAL) or not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if stat.st_mtime < self.started:
                continue
            if self.sizes.get(path) != stat.st_size:     # still growing: look again next time
                self.sizes[path] = stat.st_size
                continue
            self.done.add(path)
            os.makedirs(target, exist_ok=True)
            if name.lower().endswith(AUDIO) and any(matches(name, s) for s in suggestions):
                shutil.copy2(path, os.path.join(target, name))
                added.append(name)
            elif name.lower().endswith(".zip"):
                try:
                    with zipfile.ZipFile(path) as z:
                        for member in z.namelist():
                            base = os.path.basename(member)
                            if base.lower().endswith(AUDIO) and not base.startswith("._") and \
                                    any(matches(base, s) or (matches(name, s) and len(z.namelist()) <= 3)
                                        for s in suggestions):
                                with z.open(member) as src, open(os.path.join(target, base), "wb") as dst:
                                    shutil.copyfileobj(src, dst)
                                added.append(base)
                except zipfile.BadZipFile:
                    continue
        self.imported += added
        return added

    def run(self, every: float = 3.0):
        def loop():
            while True:
                try:
                    self.scan([t for t in buy_list(self.workdir)])
                except Exception:
                    pass
                time.sleep(every)

        threading.Thread(target=loop, daemon=True).start()


def references(workdir: str) -> list[str]:
    folder = os.path.join(workdir, "references")
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(AUDIO))


# ── HTTP ────────────────────────────────────────────────────────────────────
class _Cached:
    """Asks Live at most every few seconds (the page polls more often), in the background so a busy Live never
    blocks the page. When Live does not answer, the last answer stays and `reachable` turns False."""

    def __init__(self, fn, seconds: float = 4.0):
        self.fn, self.seconds, self.at, self.value, self.reachable = fn, seconds, 0.0, [], None
        self.busy = False

    def _refresh(self):
        try:
            value = self.fn()
            self.reachable = value is not None
            if value is not None:
                self.value = value
        finally:
            self.busy = False

    def get(self):
        if not self.busy and time.time() - self.at > self.seconds:
            self.busy, self.at = True, time.time()
            threading.Thread(target=self._refresh, daemon=True).start()
        return self.value


def make_handler(runner: Runner, watcher: "DownloadWatcher | None" = None):
    workdir = runner.workdir
    project_refs = _Cached(project_references)
    ab_state = {"which": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code: int, body, kind: str = "application/json"):
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path == "/":
                return self._send(200, PAGE, "text/html")
            if self.path.startswith("/fonts/"):   # bundled fonts (OFL), so the page needs no internet
                name = os.path.basename(self.path)
                path = os.path.join(FONTS, name)
                if name.endswith((".woff2", ".css")) and os.path.isfile(path):
                    with open(path, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "font/woff2" if name.endswith(".woff2") else "text/css")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    return self.wfile.write(data)
                return self._send(404, {"error": "not found"})
            if self.path == "/api/state":
                path = runner.latest_log()
                lines = open(path).readlines() if path and os.path.exists(path) else []
                history = [l for p in log_chain(path) for l in open(p).readlines()] if lines else []
                state = parse_log(history)
                prog = state.get("progress")
                # A run counts as going on when this interface started it, or when its log reported progress in the
                # last minutes (started elsewhere, e.g. from the command line or a second window).
                state["active"] = runner.running() or bool(prog and not state.get("done")
                                                           and time.time() - prog.get("t", 0) < 300)
                return self._send(200, {**state, "running": runner.running(), "log": [l.rstrip() for l in lines[-200:]],
                                        "reference_files": references(workdir), "to_get": buy_list(workdir),
                                        "project_references": project_refs.get(), "live": project_refs.reachable,
                                        "ab": ab_state["which"],
                                        "auto_imported": watcher.imported[-10:] if watcher else [],
                                        "checkpoint": checkpoint(workdir),
                                        "ab_ready": __import__("mixmatch.ab", fromlist=["latest_before"]).latest_before(
                                            os.path.join(workdir, "runs")) is not None})
            if self.path == "/api/project":
                from . import project

                return self._send(200, project.load(os.path.join(workdir, project.PATH)))
            if self.path == "/api/roles":
                path = os.path.join(workdir, "roles.json")
                return self._send(200, json.load(open(path)) if os.path.exists(path) else {})
            self._send(404, {"error": "not found"})

        def do_PUT(self):
            """Upload a reference: PUT /api/references/<file name> with the file as body."""
            try:
                from urllib.parse import unquote

                if not self.path.startswith("/api/references/"):
                    return self._send(404, {"error": "not found"})
                path = safe_reference(workdir, unquote(self.path[len("/api/references/"):]))
                size = int(self.headers.get("Content-Length") or 0)
                if size <= 0 or size > 1_000_000_000:
                    raise ValueError("empty or too large")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".part", "wb") as f:
                    left = size
                    while left:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            raise ValueError("upload cut off")
                        f.write(chunk)
                        left -= len(chunk)
                os.replace(path + ".part", path)
                if path.lower().endswith(CONVERT):
                    path = to_wav(path)
                nice = preview_name(workdir, path)
                if nice:   # an Apple preview: named after the track instead of Apple's file name
                    target = os.path.join(os.path.dirname(path), nice + os.path.splitext(path)[1])
                    os.replace(path, target)
                    path = target
                out = {"saved": os.path.basename(path)}
                try:
                    secs = duration_s(path)
                except Exception:   # unreadable here (e.g. an MP3 libsndfile cannot open): no hint, still saved
                    secs = None
                if secs is not None and secs < 60:
                    out["warning"] = (f"only {secs:.0f} s long: a reference needs its loud part and enough music "
                                      "to measure; a full track works much better")
                self._send(200, out)
            except Exception as e:
                self._send(400, {"error": str(e)})

        def do_POST(self):
            try:
                body = self._body()
                if self.path == "/api/run":
                    args = ["run"]
                    if body.get("refs"):
                        args += ["--ref", "references/"]
                    if body.get("auto_ref"):
                        args += ["--auto-ref"]
                    elif not body.get("refs"):
                        args += ["--no-auto-ref"]   # only the project's reference tracks
                    if body.get("quick"):
                        args += ["--quick"]
                    if body.get("silent"):
                        args += ["--silent"]
                    if body.get("no_audit"):
                        args += ["--no-audit"]
                    if body.get("continue"):
                        args += ["--continue"]
                    elif body.get("resume"):
                        args += ["--resume", body["resume"]]
                    if body.get("minutes"):
                        args += ["--minutes", str(float(body["minutes"]))]
                    return self._send(200, {"log": runner.start(args)})
                if self.path == "/api/ab":
                    if runner.running():
                        raise RuntimeError("wait until the run is finished")
                    from . import ab
                    from .live_client import LiveClient

                    live = LiveClient(timeout=10).connect()
                    try:
                        n = ab.switch(live, body.get("which", "after"), os.path.join(workdir, "runs"))
                    finally:
                        live.close()
                    ab_state["which"] = body.get("which")
                    return self._send(200, {"changed": n, "which": ab_state["which"]})
                if self.path == "/api/taps":
                    return self._send(200, {"log": runner.start(["taps"])})
                if self.path == "/api/taps/remove":
                    return self._send(200, {"log": runner.start(["taps", "--remove"])})
                if self.path == "/api/stop":
                    runner.stop()
                    return self._send(200, {"ok": True})
                if self.path == "/api/references/use":
                    return self._send(200, {"saved": download_reference(workdir, body.get("url", ""),
                                                                        body.get("name", ""))})
                if self.path == "/api/references/import":
                    added = import_downloads(workdir)
                    return self._send(200, {"added": added})
                if self.path == "/api/references/remove":
                    path = safe_reference(workdir, body.get("name", ""))
                    if not os.path.exists(path):
                        raise ValueError("no such reference")
                    return self._send(200, {"trash": trash(path)})
                if self.path == "/api/project":
                    from . import project

                    path = os.path.join(workdir, project.PATH)
                    data = project.load(path)
                    data["description"] = str(body.get("description", "")).strip()
                    project.save(data, path)
                    return self._send(200, data)
                if self.path == "/api/roles":
                    with open(os.path.join(workdir, "roles.json"), "w") as f:
                        json.dump(body, f, indent=2, ensure_ascii=False)
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})
            except Exception as e:
                self._send(400, {"error": str(e)})

    return Handler


def serve(workdir: str = ".", port: int = PORT, open_browser: bool = True) -> None:
    runner = Runner(os.path.abspath(workdir))
    watcher = DownloadWatcher(runner.workdir)
    watcher.run()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(runner, watcher))
    url = f"http://127.0.0.1:{port}"
    print(f"MixMatch interface: {url}  (Ctrl+C to quit)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        runner.stop()
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MARA</title>
<link rel="stylesheet" href="/fonts/fonts.css">
<style>
:root{
  /* desktop of an old operating system, in MARA's own colours */
  --bg:#b9b2c9;--panel:#dcd7e6;--face:#e9e5f0;--hi:#fbf9ff;--lo:#8d859e;--ink:#1c1824;
  --line:#8d859e;--text:#1c1824;--muted:#5b5469;
  --accent:#6d3fd1;--accent2:#e2479c;--lime:#c8f03a;--lime-edge:#5f7d00;--pink:#ff8cc8;--pink-edge:#c2287c;
  --good:#2f8f4e;--bad:#c4304b;--warn:#a86b00;
  --title:linear-gradient(90deg,#4b2aa0,#b2399a 70%,#e2479c);
  --slab:"Montagu Slab",Georgia,serif;--mono:"Roboto Mono",ui-monospace,Menlo,monospace;--sans:"Open Sans",-apple-system,system-ui,sans-serif;
}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,system-ui,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line)}
header h1{font-size:17px;margin:0}.pill{padding:2px 9px;border-radius:99px;font-size:12px;background:var(--line);color:var(--muted)}
.pill.on{background:color-mix(in srgb,var(--good) 22%,transparent);color:var(--good)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;padding:16px 20px;max-width:1500px;margin:auto}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
section h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 10px}
.wide{grid-column:1/-1}button{background:var(--accent);color:#fff;border:0;border-radius:7px;padding:7px 13px;font:inherit;cursor:pointer}
button.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}button:disabled{opacity:.45;cursor:default}
label{display:flex;gap:6px;align-items:center;color:var(--muted)}.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:6px 0}
.big{font-size:30px;font-weight:600}.muted{color:var(--muted)}.parts{display:flex;flex-direction:column;gap:5px;margin-top:8px}
.bar{display:grid;grid-template-columns:90px 1fr 42px;gap:8px;align-items:center;font-size:12px}.bar i{display:block;height:7px;border-radius:4px;background:var(--accent)}
.events{max-height:420px;overflow:auto;font-size:13px;display:flex;flex-direction:column;gap:4px}
.ev{padding:5px 8px;border-radius:6px;border-left:3px solid var(--line)}.ev.kept{border-color:var(--good)}.ev.undone{border-color:var(--bad)}
.ev.phase{font-weight:600;border-color:var(--accent)}.ev.section{color:var(--accent)}.ev.action{color:var(--muted);padding-left:18px}.ev.warning{border-color:var(--warn)}
ul{margin:0;padding-left:18px}li{margin:3px 0}a{color:var(--accent)}table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:4px 6px;border-bottom:1px solid var(--line)}select,input,textarea{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:4px 6px;font:inherit}
pre{max-height:260px;overflow:auto;font-size:12px;background:var(--bg);padding:10px;border-radius:8px;margin:0;white-space:pre-wrap}
svg{width:100%;height:90px}.prog{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:12px}
.prog i{display:block;width:160px;height:7px;border-radius:4px;background:var(--line);overflow:hidden}.prog b{display:block;height:100%;background:var(--accent)}
.chartlabels{position:relative;height:16px;font-size:11px;color:var(--muted)}.chartlabels span{position:absolute;white-space:nowrap}
.drop{margin-top:12px;border:2px dashed var(--line);border-radius:10px;padding:34px 16px;min-height:110px;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);font-size:14px}
.drop.over{border-color:var(--accent);color:var(--accent)}.reflist li{display:flex;justify-content:space-between;gap:8px;align-items:center}
.reflist li span{flex:1;min-width:0;overflow-wrap:anywhere}.reflist li button{flex-shrink:0}
button.small{padding:2px 8px;font-size:12px}button.danger{background:var(--bad);color:#fff;border-color:var(--bad)}

/* ── retro desktop layer ── */
body{background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}
header{background:var(--panel);border-bottom:3px solid var(--ink);padding:10px 20px;flex-wrap:wrap}
header h1{font:700 44px/1 var(--slab);letter-spacing:-.01em;margin:0 8px 0 0;cursor:default;color:var(--ink);transition:color .15s}
header h1:hover{color:var(--accent2);text-shadow:3px 3px 0 var(--lime)}
.pill{border-radius:0;background:var(--face);color:var(--ink);font:12px var(--mono);padding:2px 8px;
  border:1px solid;border-color:var(--lo) var(--hi) var(--hi) var(--lo)}
.pill.on{background:var(--lime);color:var(--ink);border-color:var(--lime-edge)}
#phase{font-family:var(--mono);font-size:12px}
main{gap:18px;padding:20px;grid-template-columns:repeat(auto-fit,minmax(min(100%,400px),1fr))}
section{background:var(--panel);border-radius:0;padding:0 14px 14px;
  border:2px solid;border-color:var(--hi) var(--lo) var(--lo) var(--hi);box-shadow:1px 1px 0 var(--ink),4px 4px 0 rgba(28,24,36,.18)}
section>h2:first-child{margin:0 -14px 12px;padding:5px 10px;background:var(--title);color:#fff;
  font:600 12px var(--mono);letter-spacing:.08em;text-transform:uppercase;display:flex;align-items:center;gap:8px}
section>h2:first-child::after{content:"";margin-left:auto;width:11px;height:11px;background:var(--face);
  border:1px solid;border-color:var(--hi) var(--lo) var(--lo) var(--hi)}
section h2{font:600 12px var(--mono);letter-spacing:.06em;color:var(--ink)}
button{border-radius:0;background:var(--lime);color:var(--ink);border:2px solid var(--lime-edge);font:600 13px var(--mono);
  padding:6px 12px;box-shadow:inset -2px -2px 0 rgba(0,0,0,.18),inset 2px 2px 0 rgba(255,255,255,.55)}
button:hover:not(:disabled){filter:brightness(1.05)}
button:active:not(:disabled){box-shadow:inset 2px 2px 0 rgba(0,0,0,.25);transform:translate(1px,1px)}
button.ghost{background:var(--face);color:var(--ink);border:2px solid;border-color:var(--hi) var(--lo) var(--lo) var(--hi);box-shadow:none}
button.small{padding:2px 8px;font-size:12px}
#stop{background:var(--pink);border-color:var(--pink-edge)}
select,input,textarea{border-radius:0;background:#fff;color:var(--ink);font:13px var(--mono);
  border:2px solid;border-color:var(--lo) var(--hi) var(--hi) var(--lo)}
label{color:var(--ink)}.muted{color:var(--muted)}
.big{font:700 54px/1 var(--slab);color:var(--accent)}
.bar i{border-radius:0;background:var(--title);height:9px}
.bar span{font-family:var(--mono)}
svg polyline{stroke:var(--accent)}svg circle{fill:var(--accent)}
.prog i{border-radius:0;background:#fff;border:2px solid;border-color:var(--lo) var(--hi) var(--hi) var(--lo);height:14px;width:180px}
.prog b{background:repeating-linear-gradient(90deg,var(--accent) 0 9px,transparent 9px 11px)}
.ev{border-radius:0;background:var(--face);border-left:4px solid var(--lo)}
.ev.kept{border-color:var(--good)}.ev.undone{border-color:var(--bad)}.ev.warning{border-color:var(--warn)}
.ev.phase{background:var(--ink);color:#fff;border-color:var(--lime);font-family:var(--mono)}
.ev.section{color:var(--accent);font-weight:600}
pre{border-radius:0;background:var(--ink);color:#d8ffa0;font:12px/1.45 var(--mono)}
.drop{border-radius:0;border:2px dashed var(--accent);background:var(--face);color:var(--accent);font-family:var(--mono)}
.drop.over{background:var(--lime);color:var(--ink);border-color:var(--lime-edge)}
a{color:var(--accent)}a:hover{background:var(--accent);color:#fff}
td{border-bottom:1px solid var(--lo)}
button.primary{display:block;width:100%;font:700 18px var(--slab);letter-spacing:.01em;padding:12px 16px;margin:4px 0 6px}
button.danger{background:var(--pink);border-color:var(--pink-edge);font-size:14px;padding:8px 18px;margin-right:10px}
#cont+#start.ghost{display:inline-block;width:auto;margin:0 0 6px}
.hint{color:var(--muted);font-size:12.5px;margin:4px 0 10px}
.now{display:flex;flex-direction:column;gap:2px;margin:2px 0 10px}.now b{font:700 18px var(--slab)}
.bigprog{height:22px;background:#fff;border:2px solid;border-color:var(--lo) var(--hi) var(--hi) var(--lo);margin-bottom:4px}
.bigprog b{display:block;height:100%;width:0;background:repeating-linear-gradient(90deg,var(--accent) 0 12px,transparent 12px 15px);transition:width .4s}
details.opts{margin:10px 0;background:var(--face);border:2px solid;border-color:var(--hi) var(--lo) var(--lo) var(--hi);padding:0 10px}
details.opts summary{cursor:pointer;font:600 12px var(--mono);text-transform:uppercase;letter-spacing:.06em;padding:7px 0}
details.opts[open]{padding-bottom:8px}details.opts label{margin:6px 0}
.compare{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:12px 0 8px;font:600 12px var(--mono);text-transform:uppercase;letter-spacing:.05em}
.seg{display:inline-flex}.seg button{background:var(--face);border:2px solid;border-color:var(--hi) var(--lo) var(--lo) var(--hi);box-shadow:none;min-width:84px}
.seg button+button{margin-left:-2px}.seg button.on{background:var(--accent);color:#fff;border-color:var(--ink);box-shadow:inset 2px 2px 0 rgba(0,0,0,.3)}
.setup{border-top:1px solid var(--lo);padding-top:8px;margin-top:10px;font-size:12.5px;color:var(--muted)}
button.link{background:none;border:0;box-shadow:none;padding:0;color:var(--accent);font:600 12.5px var(--mono);text-decoration:underline;cursor:pointer}
button.link:active{transform:none}
.verdict{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.verdict b{font:700 32px/1.1 var(--slab);color:var(--accent)}
.score{font:12px var(--mono);color:var(--muted)}
.scale{position:relative;height:12px;margin:10px 0 3px;background:linear-gradient(90deg,var(--lime),#f3e27a 45%,var(--pink));
  border:2px solid;border-color:var(--lo) var(--hi) var(--hi) var(--lo)}
.scale i{position:absolute;top:-6px;width:4px;height:20px;margin-left:-2px;background:var(--ink);transition:left .5s}
.scale-labels{display:flex;justify-content:space-between;font:11px var(--mono);color:var(--muted)}
.phase-row{display:grid;grid-template-columns:1fr auto auto;gap:12px;padding:5px 0;border-bottom:1px dotted var(--lo);font-size:13px}
.phase-row .good{color:var(--good);font-weight:600}.mono{font-family:var(--mono)}
h3.sub{font:600 12px var(--mono);text-transform:uppercase;letter-spacing:.06em;margin:16px 0 6px}
.part{margin:0 0 10px}.part-head{display:grid;grid-template-columns:minmax(150px,auto) 1fr;gap:10px;align-items:center;font-weight:600;font-size:13.5px}
.part-head i{display:block;height:9px;background:var(--title)}.part-why{font-size:12.5px;color:var(--muted);margin:3px 0 0}
ul.part-why{padding-left:18px}ul.part-why li{margin:2px 0}
</style>
</head>
<body>
<header><h1 title="Mix And Reference Assistant">MARA</h1><span id="status" class="pill">idle</span><span id="live" class="pill">Live …</span><span id="phase" class="muted"></span>
<span id="prog" class="prog" hidden><i><b id="prog_bar"></b></i><span id="prog_text" class="muted"></span></span></header>
<main>
<section>
  <h2>Run</h2>
  <div id="run_idle">
    <button id="cont" class="primary" hidden>Continue</button>
    <button id="start" class="primary">Mix the whole song</button>
    <p class="hint" id="start_hint">Plays the set, listens, and keeps only changes that measurably help.</p>
  </div>
  <div id="run_busy" hidden>
    <div class="now"><b id="now_phase">Working …</b><span id="now_section" class="muted"></span></div>
    <div class="bigprog"><b id="bigprog_bar"></b></div><div class="hint" id="bigprog_text"></div>
    <button id="stop" class="danger">Stop</button>
    <span class="hint">It can continue later from where it stopped.</span>
  </div>
  <details class="opts"><summary>Options</summary>
    <label title="The Main fader goes down while MARA works and comes back at the end. It measures before the fader, so the results are the same."><input type="checkbox" id="silent" checked> silent: Main fader down while it works</label>
    <label><input type="checkbox" id="refs" checked> use the files in references/</label>
    <label><input type="checkbox" id="auto_ref" checked> look for more references by itself</label>
    <label><input type="checkbox" id="quick"> master only (quick, about 15 min)</label>
    <label><input type="checkbox" id="no_audit"> keep every effect (no check for useless ones)</label>
  </details>
  <div class="compare"><span>Compare the last run</span>
    <div class="seg"><button id="ab_before">Before</button><button id="ab_after">After</button></div></div>
  <div class="setup">Taps in Live: <button id="taps" class="link">set</button> · <button id="untaps" class="link" title="Takes every MixMatch tap out of the Live set">remove</button></div>
  <p class="muted" id="msg"></p>
</section>
<section>
  <h2>How close to the reference</h2>
  <div class="verdict"><b id="verdict">No run yet</b><span id="distance" class="score" title="Distance in points: 0 = measures like the reference"></span></div>
  <div class="scale"><i id="scale_mark"></i></div>
  <div class="scale-labels"><span>like the reference</span><span>far off</span></div>
  <p class="hint" id="scope"></p>
  <div id="phases"></div>
  <h3 class="sub">What is still different</h3>
  <div class="parts" id="parts"><span class="muted">–</span></div>
  <svg id="chart" hidden></svg><div id="chartlabels" hidden></div>
</section>
<section>
  <h2>Song</h2>
  <div id="song" class="muted">No run yet.</div>
  <h2 style="margin-top:12px">Describe your track</h2>
  <textarea id="desc" rows="2" style="width:100%" placeholder="e.g. Hyperpop, happy and bright; vocals come later"></textarea>
  <div class="row"><button id="save_desc" class="ghost">Save</button><span class="muted">wins over the audio model, used by every run</span></div>
</section>
<section class="wide">
  <h2>What MARA does</h2>
  <div class="events" id="events"></div>
</section>
<section>
  <h2>References</h2>
  <div id="refs_used"></div>
  <div id="drop" class="drop">Drop audio files here to add them to references/</div><h2 style="margin-top:12px">To get</h2><div id="to_get" class="muted">–</div>
</section>
<section>
  <h2>Effects</h2><div id="effects" class="muted">–</div>
</section>
<section>
  <h2>Roles</h2><div id="roles" class="muted">–</div><div class="row"><button id="save_roles" class="ghost">Save roles</button></div>
</section>
<section class="wide"><h2>Log</h2><pre id="log"></pre></section>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  const j = await r.json(); $("msg").textContent = j.error || ""; return j;
}
const OPTS = ["silent", "refs", "auto_ref", "quick", "no_audit"];
try { const saved = JSON.parse(localStorage.getItem("mm_opts") || "{}");   // remembered per browser, a convenience
  for (const k of OPTS) if (k in saved) $(k).checked = saved[k]; } catch {}
for (const k of OPTS) $(k).addEventListener("change", () => {
  try { localStorage.setItem("mm_opts", JSON.stringify(Object.fromEntries(OPTS.map(o => [o, $(o).checked])))); } catch {} });
$("start").onclick = () => post("/api/run", {auto_ref: $("auto_ref").checked, refs: $("refs").checked, quick: $("quick").checked,
  no_audit: $("no_audit").checked, silent: $("silent").checked});
$("stop").onclick = () => post("/api/stop");
$("cont").onclick = () => post("/api/run", {continue: true, refs: $("refs").checked, silent: $("silent").checked,
  no_audit: $("no_audit").checked});
$("taps").onclick = () => post("/api/taps");
$("untaps").onclick = () => { if (confirm("Remove every MixMatch tap from the Live set? «Set taps» puts them back.")) post("/api/taps/remove"); };
$("ab_before").onclick = async () => { const j = await post("/api/ab", {which: "before"}); if (!j.error) $("msg").textContent = `Before (${j.changed} parameters)`; };
$("ab_after").onclick = async () => { const j = await post("/api/ab", {which: "after"}); if (!j.error) $("msg").textContent = `After (${j.changed} parameters)`; };
const ROLE_GROUPS = __ROLE_GROUPS__;
async function loadRoles() {
  const roles = await (await fetch("/api/roles")).json(); const names = Object.keys(roles);
  if (!names.length) { $("roles").textContent = "No roles.json yet (written by the first run)."; return; }
  $("roles").innerHTML = "<table>" + names.map(n => `<tr><td>${esc(n)}</td><td><select data-n="${esc(n)}">${Object.entries(ROLE_GROUPS).map(([g, rs]) => `<optgroup label="${esc(g)}">` + rs.map(r =>
    `<option ${r===roles[n].role?"selected":""}>${r}</option>`).join("") + "</optgroup>").join("")}</select></td><td><input data-p="${esc(n)}" type="number" min="1" max="5" value="${roles[n].priority}" style="width:52px"></td></tr>`).join("") + "</table>";
  $("save_roles").onclick = async () => {
    for (const n of names) { roles[n].role = document.querySelector(`[data-n="${CSS.escape(n)}"]`).value;
      roles[n].priority = +document.querySelector(`[data-p="${CSS.escape(n)}"]`).value; roles[n].source = "user"; }
    await post("/api/roles", roles); $("msg").textContent = "Roles saved; the next run uses them.";
  };
}
function chart(values, phases, runs) {
  return;   // replaced by the phase rows in «How close to the reference»
  if (values.length < 2) { $("chart").innerHTML = ""; $("chartlabels").innerHTML = ""; return; }
  phases = phases || []; runs = runs || [];
  const max = Math.max(...values) * 1.05, w = 300, h = 90, x = i => (i/(values.length-1))*w, y = v => h - (v/max)*h;
  // one line per phase: master and channels are measured differently, so their numbers are not joined
  let svg = "", labels = "", from = 0;
  for (let i = 1; i <= values.length; i++) {
    if (i < values.length && phases[i] === phases[from] && runs[i] === runs[from]) continue;
    const seg = values.slice(from, i).map((v, j) => `${x(from + j)},${y(v)}`).join(" ");
    svg += i - from > 1 ? `<polyline fill="none" stroke="var(--accent)" stroke-width="2" points="${seg}"/>`
                        : `<circle cx="${x(from)}" cy="${y(values[from])}" r="2" fill="var(--accent)"/>`;
    if (from > 0) svg += `<line x1="${x(from) - w/(values.length-1)/2}" x2="${x(from) - w/(values.length-1)/2}" y1="0" y2="${h}" stroke="var(--line)" stroke-dasharray="${runs[from] !== runs[from-1] ? "4 3" : "0"}"/>`;
    if (phases[from] !== phases[from-1] && phases[from])
      labels += `<span style="left:${(x(from)/w)*100}%">${esc(phases[from])}${runs[from] !== runs[from-1] && from ? " (resumed)" : ""}</span>`;
    from = i;
  }
  $("chart").innerHTML = svg; $("chartlabels").innerHTML = labels;
}
const PART_NAMES = {masking: "Instruments covering each other", tonal: "Tone balance", loudness: "Loudness",
  dynamics: "Punch (dynamics)", width: "Stereo width", low_side: "Bass in stereo", clipping: "Clipping"};
const PHASE_NAMES = {master: "Master", channels: "Single channels", effects: "Effects", final: "Final check", setup: "Start"};
function verdict(d) {
  return d == null ? ["No run yet", 0] : d < 1 ? ["Very close", d / 6] : d < 2.5 ? ["Close", d / 6]
       : d < 4 ? ["Audible differences", d / 6] : ["Still far off", Math.min(1, d / 6)];
}
function why(k, text) {
  if (k === "masking" && text.includes("↔")) {   // «A ↔ B at 254 Hz, …» → one plain sentence per pair
    return "<ul class='part-why'>" + text.split(", ").map(x =>
      `<li>${esc(x.replace(" ↔ ", " and ").replace(/ at (\d+) Hz/, " overlap at $1 Hz"))}</li>`).join("") + "</ul>";
  }
  return `<div class="part-why">${esc(text)}</div>`;
}
function closeness(s) {
  const [word, pos] = verdict(s.distance);
  $("verdict").textContent = word; $("distance").textContent = s.distance != null ? s.distance.toFixed(2) + " points" : "";
  $("scale_mark").style.left = (pos * 100).toFixed(1) + "%";
  const phase = (s.history_phase || []).slice(-1)[0];
  $("scope").textContent = phase === "channels"
    ? `Now on ${(s.section || "a channel").replace(/_/g, " ")}. While MARA works on single channels, loudness and punch are left out: that part of the song need not sound like the drop.`
    : (s.section ? `Now on the master, ${s.section}. Tone, loudness, punch and width are compared with the references.` : "");
  // one row per phase: where it started and where it is now
  const rows = [], hist = s.history || [], ph = s.history_phase || [];
  for (let i = 0; i < hist.length; i++) {
    const last = rows[rows.length - 1];
    if (last && last.phase === ph[i]) { last.now = hist[i]; last.best = Math.min(last.best, hist[i]); }
    else rows.push({phase: ph[i], start: hist[i], now: hist[i], best: hist[i]});
  }
  $("phases").innerHTML = rows.filter(r => r.phase !== "setup").map(r => {
    const gain = r.start > 0 ? Math.round(100 * (r.start - r.best) / r.start) : 0;
    return `<div class="phase-row"><span>${esc(PHASE_NAMES[r.phase] || r.phase)}</span>` +
      `<span class="mono">${r.start.toFixed(2)} → ${r.best.toFixed(2)}</span>` +
      `<span class="${gain > 0 ? "good" : "muted"}">${gain > 0 ? gain + "% closer" : "no change yet"}</span></div>`;
  }).join("");
  const parts = Object.entries(s.parts || {}).filter(([, v]) => v >= 0.05).sort((a, b) => b[1] - a[1]);
  const top = Math.max(1, ...parts.map(p => p[1])), ex = s.explain || {};
  $("parts").innerHTML = parts.length ? parts.map(([k, v]) =>
    `<div class="part"><div class="part-head"><span>${esc(PART_NAMES[k] || k)}</span><i style="width:${(v/top)*100}%"></i></div>` +
    (ex[k] ? why(k, ex[k]) : "") + "</div>").join("") : "<span class='muted'>Nothing worth mentioning.</span>";
}
function progress(s) {
  const p = s.progress, p0 = s.progress_start, el = $("prog");
  if (!p || !p.total) { el.hidden = true; return; }
  el.hidden = false;
  const frac = Math.min(1, p.done / p.total); $("prog_bar").style.width = (frac*100).toFixed(1) + "%";
  let left = "";
  if (s.active && p0 && p.done > p0.done) {
    // seconds per unit from all the time so far, including the time since the last step: waiting raises it
    const rate = (Date.now()/1000 - p0.t) / (p.done - p0.done);
    const m = Math.round(rate * (p.total - p.done) / 60);
    left = m >= 60 ? ` · about ${Math.floor(m/60)} h ${m%60} min left` : ` · about ${Math.max(1, m)} min left`;
  } else if (s.active) left = " · estimating time …";
  $("prog_text").textContent = `${Math.round(frac*100)}%${left}` + (s.active && s.section ? ` · ${s.section}` : "");
  $("bigprog_bar").style.width = (frac*100).toFixed(1) + "%"; $("bigprog_text").textContent = `${Math.round(frac*100)}%${left}`;
}
async function tick() {
  let s; try { s = await (await fetch("/api/state")).json(); } catch { return; }
  $("status").textContent = s.active ? "running" : (s.done ? "finished" : "ready"); $("status").className = "pill" + (s.active ? " on" : "");
  $("phase").textContent = s.phase || ""; $("start").disabled = s.active;
  $("run_idle").hidden = s.active; $("run_busy").hidden = !s.active;
  const ck = s.checkpoint; $("cont").hidden = !ck;
  if (ck) $("cont").textContent = `Continue at ${ck.next_channel || ck.phase}` + (ck.percent != null ? ` · ${ck.percent}%` : "");
  $("start").textContent = ck ? "Start over" : "Mix the whole song";
  $("start").className = ck ? "ghost" : "primary";
  $("start_hint").textContent = ck ? "The last run stopped. Continue skips everything it already did."
                                   : "Plays the set, listens, and keeps only changes that measurably help.";
  $("now_phase").textContent = s.phase || "Preparing …"; $("now_section").textContent = s.section || "";
  if (s.auto_imported && s.auto_imported.length !== lastImported) {
    if (lastImported >= 0 && s.auto_imported.length) $("msg").textContent = "Picked up from Downloads: " + s.auto_imported.slice(-3).join(", ");
    lastImported = s.auto_imported.length; lastRefs = ""; }
  for (const w of ["before", "after"]) { const b = $("ab_" + w); b.disabled = s.active || !s.ab_ready;
    b.className = s.ab === w ? "on" : ""; }
  $("live").textContent = s.live === null ? "Live …" : (s.live ? "Live connected" : "Live busy or closed (exporting?)");
  $("live").className = "pill" + (s.live ? " on" : "");
  closeness(s); progress(s);
  $("song").innerHTML = [s.key && `<div><b>${esc(s.key)}</b></div>`, s.style && `<div class="muted">sounds like ${esc(s.style)}</div>`].filter(Boolean).join("") || "No run yet.";
  $("events").innerHTML = s.events.slice().reverse().map(e => `<div class="ev ${e.kind}">${esc(e.text)}</div>`).join("");
  const refsKey = JSON.stringify(s.reference_files) + JSON.stringify(s.project_references) + s.references.length;
  if (refsKey !== lastRefs) { lastRefs = refsKey;
    $("refs_used").innerHTML = (s.project_references.length ? "<div class='muted'>in the Live set (always used):</div><ul>" +
      s.project_references.map(r => `<li><b>${esc(r.track)}</b>: ${esc(r.file)}</li>`).join("") + "</ul><div class='muted'>in references/:</div>" : "")
      + (s.reference_files.length ? "<ul class='reflist'>" + s.reference_files.map(f =>
      `<li><span>${esc(f)}</span><button class="ghost small" data-remove="${esc(f)}">remove</button></li>`).join("") + "</ul>"
      : "<span class='muted'>references/ is empty</span>")
      + (s.references.length ? "<div class='muted' style='margin-top:6px'>picked: " + s.references.map(esc).join("<br>") + "</div>" : "");
    document.querySelectorAll("[data-remove]").forEach(b => b.onclick = async () => {
      if (!b.classList.contains("danger")) { b.classList.add("danger"); b.textContent = "really? (to Trash)"; return; }
      const j = await post("/api/references/remove", {name: b.dataset.remove});
      if (!j.error) $("msg").textContent = "Moved to the Trash: " + b.dataset.remove; lastRefs = ""; tick();
    });
  }
  const item = t => {
    const dl = t.links.Download, page = Object.entries(t.links).find(([k]) => !["Spotify", "Download", "Preview"].includes(k));
    const listen = t.links.Preview || (t.free && dl);
    const player = listen ? `<br><audio controls preload="none" src="${esc(listen)}" style="width:100%;height:30px;margin-top:4px"></audio>` : "";
    const shop = t.links.Bandcamp || (page && page[1]);
    const btn = t.free ? (dl ? `<button class="small" data-use="${esc(dl)}" data-name="${esc(t.title)}">Use</button>`
      : (page ? `<a href="${esc(page[1])}" target="_blank" rel="noopener"><button class="ghost small">Get</button></a>` : ""))
      : (shop ? `<a href="${esc(shop)}" target="_blank" rel="noopener"><button class="ghost small">Buy</button></a>` : "");
    return `<li><b>${esc(t.title)}</b>${t.source ? ` <span class="pill">${esc(t.source)}</span>` : ""} ${btn}<br><span class="muted">${esc(t.why)}</span><br>` +
      Object.entries(t.links).filter(([k]) => k !== "Preview").map(([k, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(k)}</a>`).join(" · ") + player + "</li>";
  };
  const buy = s.to_get.filter(t => !t.free), free = s.to_get.filter(t => t.free);
  const getKey = JSON.stringify(s.to_get);
  if (getKey !== lastGet) { lastGet = getKey;
  $("to_get").innerHTML = s.to_get.length ? (buy.length ? "<div class='muted' style='font-size:12px'>«Buy», download (WAV/FLAC/ZIP): MARA picks the file up from Downloads by itself.</div><ul>" + buy.map(item).join("") + "</ul>" : "")
    + (free.length ? "<h2 style='margin-top:12px'>Free (no purchase)</h2><ul>" + free.map(item).join("") + "</ul>"
      + "<div class='row'><button id='import_dl' class='ghost small'>Import from Downloads</button><span class='muted'>after «Get»: newest audio or ZIP from the last hour</span></div>" : "") : "–";
  document.querySelectorAll("[data-use]").forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = "…"; const j = await post("/api/references/use", {url: b.dataset.use, name: b.dataset.name});
    b.textContent = j.error ? "failed" : "added"; if (!j.error) { $("msg").textContent = "Added to references/: " + j.saved; lastRefs = ""; }
  });
  if ($("import_dl")) $("import_dl").onclick = async () => { const j = await post("/api/references/import");
    if (!j.error) { $("msg").textContent = j.added.length ? "Imported: " + j.added.join(", ") : "Nothing new in Downloads (last hour)."; lastRefs = ""; } };
  }
  $("effects").innerHTML = s.effects.length ? "<ul>" + s.effects.map(e => `<li>${esc(e)}</li>`).join("") + "</ul>" : "–";
  const log = $("log"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 5;
  log.textContent = s.log.join("\n"); if (atEnd) log.scrollTop = log.scrollHeight;
}
let lastRefs = "", lastGet = "", lastImported = -1;
const drop = $("drop");
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", async e => {
  const files = [...e.dataTransfer.files];
  drop.textContent = `Adding ${files.length} file${files.length > 1 ? "s" : ""} …`;
  const results = await Promise.all(files.map(async file => {   // all at once, each with its own answer
    try {
      const r = await fetch("/api/references/" + encodeURIComponent(file.name), {method: "PUT", body: file});
      const j = await r.json();
      return j.error ? `${file.name}: ${j.error}` : `Added ${j.saved}` + (j.warning ? ` (${j.warning})` : "");
    } catch (err) { return `${file.name}: upload failed (${err.message || err})`; }
  }));
  $("msg").textContent = results.join(" · ");
  drop.textContent = "Drop audio files here to add them to references/"; lastRefs = ""; tick();
});
fetch("/api/project").then(r => r.json()).then(p => { $("desc").value = p.description || ""; });
$("save_desc").onclick = async () => { const j = await post("/api/project", {description: $("desc").value});
  if (!j.error) $("msg").textContent = "Description saved."; };
loadRoles(); tick(); setInterval(tick, 1500);
</script>
</body>
</html>
"""
PAGE = PAGE.replace("__ROLE_GROUPS__", json.dumps(__import__("mixmatch.roles", fromlist=["ROLE_GROUPS"]).ROLE_GROUPS))
