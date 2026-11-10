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
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

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


class _AttachedProcess:
    """Control our existing run after the UI restarts, checking against PID reuse."""

    def __init__(self, pid: int, identity: str):
        self.pid, self.identity = pid, identity

    @staticmethod
    def identify(pid: int) -> str:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                                capture_output=True, text=True, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else ""

    def poll(self):
        return None if self.identity and self.identify(self.pid) == self.identity else 0

    def send_signal(self, sig):
        if self.poll() is None:
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                pass

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("attached MARA run", timeout)
            time.sleep(0.1)
        return 0


class Runner:
    """The one `mixmatch` child process the interface may start, and its log file."""

    def __init__(self, root: str):
        self.root = root
        self.proc: subprocess.Popen | _AttachedProcess | None = None
        self.log_path: str | None = None
        self.lock = threading.Lock()
        try:
            with open(self.process_file) as f:
                saved = json.load(f)
            proc = _AttachedProcess(int(saved["pid"]), saved["identity"])
            if proc.poll() is None:
                self.proc, self.log_path = proc, saved["log_path"]
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            pass

    @property
    def process_file(self) -> str:
        return os.path.join(self.workdir, "runs", "ui-process.json")

    @property
    def workdir(self) -> str:
        """The current track's folder (songs/<track>/), or the root in the layout without tracks."""
        from . import songs

        return songs.current_dir(self.root)

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
            try:
                identity = _AttachedProcess.identify(self.proc.pid)
                with open(self.process_file + ".tmp", "w") as f:
                    json.dump({"pid": self.proc.pid, "identity": identity, "log_path": self.log_path}, f)
                os.replace(self.process_file + ".tmp", self.process_file)
            except (OSError, subprocess.SubprocessError):
                pass  # The original UI still owns and controls the child.
            return self.log_path

    def stop(self) -> None:
        """SIGINT, and SIGTERM 15 s later if the run still goes on; both end it in order (see cli._stoppable)."""
        if self.running():
            proc = self.proc
            proc.send_signal(signal.SIGINT)

            def insist():
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.terminate()
            threading.Thread(target=insist, daemon=True).start()

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
            "percent": round(100 * ck["done"] / ck["total"]) if ck.get("total") else None,
            "complete": ck.get("complete", True)}


def reference_candidates(workdir: str) -> list[dict]:
    """Available audio found by the last search, offered for an explicit choice."""
    import hashlib
    from .songs import project_root

    paths = sorted(glob.glob(os.path.join(workdir, "runs", "*-references.json")))
    try:
        found = json.load(open(paths[-1])) if paths else []
    except (OSError, ValueError):
        found = []
    shared = os.path.join(project_root(workdir), "references")
    if os.path.realpath(shared) != os.path.realpath(os.path.join(workdir, "references")):
        for folder, dirs, names in os.walk(shared):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            found += [{"path": os.path.join(folder, name), "source": "shared references"}
                      for name in sorted(names) if not name.startswith(".") and name.lower().endswith(AUDIO)]
    try:
        ck = json.load(open(os.path.join(workdir, "runs", "checkpoint.json")))
        found += [{"path": p, "source": "previous run"} for p in ck.get("references", [])]
    except (OSError, ValueError):
        pass
    out, seen = [], set()
    for item in found:
        path = os.path.realpath(item.get("path", ""))
        if path in seen or not path.lower().endswith(AUDIO) or not os.path.isfile(path):
            continue
        seen.add(path)
        out.append({"id": hashlib.sha256(path.encode()).hexdigest()[:20], "name": os.path.basename(path),
                    "source": item.get("source", "search"), "path": path})
    return out


def choose_reference(workdir: str, candidate_id: str) -> str:
    import shutil

    item = next((c for c in reference_candidates(workdir) if c["id"] == candidate_id), None)
    if item is None:
        raise ValueError("this reference is no longer available; choose another audio file")
    target = safe_reference(workdir, item["name"])
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if not os.path.exists(target):
        shutil.copy2(item["path"], target)
    elif not os.path.samefile(item["path"], target):
        import filecmp

        if not filecmp.cmp(item["path"], target, shallow=False):
            raise ValueError("a different file with this name is already selected; rename it before adding this one")
    return os.path.basename(target)


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
        # A failed connection/measurement startup never replaced the checkpoint.
        # When continuing it, walk past these attempts to the actual previous run.
        while i > 0 and "Stopped: no channel reports through a MixMatch Tap" in open(logs[i]).read():
            i -= 1
        chain.insert(0, logs[i])
    return chain


def live_set() -> dict | None:
    """Channel names of the open Live set and which of them have a tap; None when Live does not answer."""
    try:
        from .live_client import LiveClient
        from .taps import is_reference_name

        live = LiveClient(timeout=4).connect()
        try:
            state = live.get_set()
        finally:
            live.close()
    except Exception:
        return None
    from . import songs

    need = [t for t in state["tracks"] if not (t["kind"] == "track" and is_reference_name(t["name"]))]
    missing = [t["name"] for t in need if not any(d["name"] == "MixMatch Tap" for d in t["devices"])]
    name = state.get("name") or os.path.splitext(os.path.basename(state.get("file_path") or ""))[0]
    return {"name": name, "channels": songs.channel_names(state), "taps": len(need) - len(missing), "total": len(need),
            "missing": missing, "playing": bool(state.get("is_playing"))}


def run_process_alive() -> bool:
    """A `mixmatch run` started anywhere (this interface, another window, the command line) is still going."""
    try:
        out = subprocess.run(["pgrep", "-f", r"mixmatch(\.cli)? run"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return True                      # cannot tell: trust the log
    return out.returncode == 0


def parse_log(lines: list[str]) -> dict:
    """Phase, current channel, distance history, decisions, song info and effects from a run's log."""
    state = {"phase": None, "section": None, "distance": None, "parts": {}, "history": [], "events": [],
             "key": None, "style": None, "mood": None, "explain": {}, "effects": [], "references": [], "to_get": [], "done": None,
             "history_phase": [], "history_run": [], "progress": None, "progress_start": None,
             "activity": None, "planner": None, "mode": None}
    run = 0
    traceback_event = None

    def point(value: float) -> None:
        state["history"].append(value)
        state["history_phase"].append(phase_label(state["phase"]))
        state["history_run"].append(run)

    for raw in lines:
        line = raw.rstrip("\n")
        s = line.strip()
        if s.startswith("Taps:"):            # the first lines of every run: a resumed run starts its own log
            run += 1
            traceback_event = None
            state["progress"] = state["progress_start"] = state["done"] = None
            state["mode"] = None
        elif s.startswith("Mode:"):
            state["mode"] = "beat" if s.startswith("Mode: beat") else "song"
        elif "treating it as a beat (no vocals)" in s:
            state["mode"] = "beat"
        elif s == "Traceback (most recent call last):":
            # Older logs include a traceback even for Stop. Show its outcome, not Python's header or frames.
            state["done"] = "Stopped: an error occurred. See the log for details."
            traceback_event = {"kind": "warning", "text": state["done"]}
            state["events"].append(traceback_event)
        elif traceback_event is not None:
            if line == s and re.fullmatch(r"[\w.]+(?::.*)?", s):
                interrupted = s.split(":", 1)[0] == "KeyboardInterrupt"
                state["done"] = "Stopped: run interrupted." if interrupted else f"Stopped: {s}"
                traceback_event.update(kind="neutral" if interrupted else "warning", text=state["done"])
                traceback_event = None
        elif s.startswith("activity {"):
            try:
                update = json.loads(s[len("activity "):])
                if not isinstance(update, dict) or not isinstance(update.get("text"), str):
                    continue
            except ValueError:
                continue
            if not state["activity"] or state["activity"]["text"] != update["text"]:
                state["events"].append({"kind": "activity", "text": update["text"]})
            state["activity"] = update
        elif s.startswith("Planner:"):
            state["planner"] = s.split(":", 1)[1].strip()
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
        elif s.startswith(("Stopped:", "Done.", "Ready:")):
            state["done"] = s
            if s.startswith("Stopped: reverted to the state before the run."):
                for key in ("history", "history_phase", "history_run", "effects"):
                    state[key] = []
                state.update(distance=None, parts={}, explain={}, phase=None, section=None,
                             progress=None, progress_start=None, activity=None)
            if s.startswith(("Stopped:", "Ready:")):
                state["events"].append({"kind": "neutral", "text": s})
        elif s.startswith("⚠"):
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

    def __init__(self, workdir, folder: str | None = None):
        self._workdir, self.folder = workdir, folder or os.path.expanduser("~/Downloads")
        self.started, self.sizes, self.done, self.imported = time.time(), {}, set(), []

    @property
    def workdir(self) -> str:
        """A folder, or a function giving it (the current track's folder, which changes)."""
        return self._workdir() if callable(self._workdir) else self._workdir

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


def make_handler(runner: Runner, watcher: "DownloadWatcher | None" = None, live_set_fn=None):
    from . import ab, songs, preview

    project_refs = _Cached(project_references)
    open_set = _Cached(live_set_fn or live_set, seconds=6.0)

    def track_state(active: bool) -> dict:
        """The current track, the others, and whether the open Live set is this track and has its taps."""
        # Fetch once even if the interface opens during a run, then reuse the snapshot while busy.
        info = open_set.value if active and open_set.value else open_set.get()
        if info and open_set.reachable:
            songs.remember_live_name(runner.root, info.get("name", ""), info["channels"])
        song = songs.current(runner.root)
        check = songs.check_set(runner.root, info["channels"]) if info and open_set.reachable and not active \
            else {"same": True, "other": None}
        return {"track": song["name"] if song else None, "tracks": songs.listing(runner.root),
                "used_references": songs.used_references(runner.root),
                "set": check, "open_set": info if open_set.reachable else None}
    ab_state = {"which": None, "blind": False}
    blind = ab.BlindComparison()
    ab_lock = threading.Lock()
    preview_trial = {}

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
            if urlsplit(self.path).path == "/api/preview-audio":
                q = parse_qs(urlsplit(self.path).query)
                folder = os.path.join(runner.workdir, "runs")
                if "token" in q:
                    trial = dict(preview_trial)
                    if q["token"][0] != trial.get("token") or folder != trial.get("folder"):
                        return self._send(404, {"error": "preview expired"})
                    which, identity = trial["which"], trial["id"]
                else:
                    which, identity = q.get("which", [""])[0], q.get("id", [""])[0]
                path = preview.selected_file(folder, which, identity)
                if q.get("equal", ["0"])[0] == "1":
                    path = preview.matched_file(folder, which, identity)
                if path is None:
                    return self._send(404, {"error": "preview not ready"})
                size = os.path.getsize(path)
                start, end, status = 0, size - 1, 200
                if self.headers.get("Range"):
                    m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers["Range"])
                    if not m or not any(m.groups()):
                        return self._send(416, {"error": "invalid audio range"})
                    if not m[1]:
                        start = max(0, size - int(m[2]))
                    else:
                        start = int(m[1])
                        end = min(end, int(m[2])) if m[2] else end
                    if start > end:
                        return self._send(416, {"error": "audio range outside file"})
                    status = 206
                self.send_response(status)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(end - start + 1))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                try:
                    with open(path, "rb") as f:
                        f.seek(start)
                        left = end - start + 1
                        while left:
                            chunk = f.read(min(left, 65536))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path == "/":
                return self._send(200, PAGE, "text/html")
            if self.path.startswith("/fonts/"):   # bundled fonts (OFL), so the page needs no internet
                name = os.path.basename(self.path.split("?")[0])
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
                # The process must still exist: a run that was stopped or crashed reports no progress either.
                state["active"] = runner.running() or bool(prog and not state.get("done")
                                                           and time.time() - prog.get("t", 0) < 300
                                                           and run_process_alive())
                return self._send(200, {**state, **track_state(state["active"]),
                                        "running": runner.running(), "log": [l.rstrip() for l in lines[-200:]],
                                        "reference_files": references(runner.workdir), "to_get": buy_list(runner.workdir),
                                        "reference_candidates": [{k: v for k, v in c.items() if k != "path"}
                                                                 for c in reference_candidates(runner.workdir)],
                                        "project_references": project_refs.get(), "live": project_refs.reachable,
                                        "ab": ab_state["which"],
                                        "previews": preview.available(os.path.join(runner.workdir, "runs")),
                                        "loudness_match": preview.matching(os.path.join(runner.workdir, "runs")),
                                        "ab_blind": ab_state["blind"],
                                        "ab_blind_ready": ab.BlindComparison.ready(os.path.join(runner.workdir, "runs")),
                                        "auto_imported": watcher.imported[-10:] if watcher else [],
                                        "checkpoint": checkpoint(runner.workdir),
                                        "roles_revision": os.stat(os.path.join(runner.workdir, "roles.json")).st_mtime_ns
                                            if os.path.exists(os.path.join(runner.workdir, "roles.json")) else None,
                                        "ab_ready": __import__("mixmatch.ab", fromlist=["latest_before"]).latest_before(
                                            os.path.join(runner.workdir, "runs")) is not None})
            if self.path == "/api/project":
                from . import project

                return self._send(200, project.load(os.path.join(runner.workdir, project.PATH)))
            if self.path == "/api/roles":
                path = os.path.join(runner.workdir, "roles.json")
                return self._send(200, json.load(open(path)) if os.path.exists(path) else {})
            self._send(404, {"error": "not found"})

        def do_PUT(self):
            """Upload a reference: PUT /api/references/<file name> with the file as body."""
            try:
                from urllib.parse import unquote

                if not self.path.startswith("/api/references/"):
                    return self._send(404, {"error": "not found"})
                path = safe_reference(runner.workdir, unquote(self.path[len("/api/references/"):]))
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
                nice = preview_name(runner.workdir, path)
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
                if self.path in ("/api/preview/blind", "/api/preview/reveal"):
                    folder = os.path.join(runner.workdir, "runs")
                    with ab_lock:
                        if self.path.endswith("/reveal"):
                            if (preview_trial.get("folder") != folder or not preview.selected_file(
                                    folder, preview_trial.get("which"), preview_trial.get("id"))):
                                raise ValueError("choose a blind preview first")
                            return self._send(200, {"which": preview_trial["which"]})
                        versions = preview.available(folder)
                        if not all(versions.values()):
                            raise ValueError("both audio previews must be ready")
                        which = secrets.choice(("before", "after"))
                        token = secrets.token_urlsafe(24)
                        preview_trial.update(folder=folder, which=which, id=versions[which]["id"], token=token)
                        return self._send(200, {"url": f"/api/preview-audio?token={token}"})
                if self.path == "/api/run":
                    ts = track_state(False)
                    if not ts["set"]["same"]:
                        raise RuntimeError(f"Live has another set open than «{ts['track']}»: start a new track "
                                           "for it or switch to its track first")
                    if ts["open_set"] and ts["open_set"]["taps"] == 0:
                        raise RuntimeError("no MixMatch Tap in this set yet: «Set taps» first")
                    mode = body.get("reference_mode", "discover" if body.get("auto_ref") else "selected")
                    if mode not in ("selected", "discover"):
                        raise ValueError("choose your references or ask MARA to find suggestions")
                    args = ["run"]
                    if mode == "discover":
                        args += ["--auto-ref", "--review-references"]
                    else:
                        local = references(runner.workdir)
                        in_live = project_references()
                        if not local and not in_live:
                            raise ValueError("Choose at least one reference track first: add audio files under References "
                                             "or put a reference audio clip on a REF track in Live.")
                        if local:
                            args += ["--ref", "references/"]
                        args += ["--no-auto-ref", "--no-suggest", "--replace-references"]
                    ck = checkpoint(runner.workdir) if body.get("continue") else None
                    if body.get("quick") or (ck and not ck["complete"]):
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
                    with ab_lock:
                        path = runner.start(args)
                        ab_state.update(which=None, blind=False)
                        blind.reset()
                    return self._send(200, {"log": path})
                if self.path in ("/api/ab", "/api/ab/blind", "/api/ab/reveal"):
                    from .live_client import LiveClient

                    with ab_lock:
                        if runner.running() or run_process_alive():
                            raise RuntimeError("wait until the run is finished")
                        folder = os.path.join(runner.workdir, "runs")
                        if self.path == "/api/ab/reveal":
                            which = blind.reveal(folder)
                            if which is None:
                                raise ValueError("make a blind selection first")
                            ab_state.update(which=which, blind=False)
                            return self._send(200, {"which": which})
                        is_blind = self.path == "/api/ab/blind"
                        live = LiveClient(timeout=10).connect()
                        try:
                            if is_blind:
                                ab_state.update(which=None, blind=False)
                                blind.choose(live, folder)
                                ab_state.update(which=None, blind=True)
                                # Neither selection nor parameter count leaves the server.
                                return self._send(200, {"blind": True})
                            which = body.get("which", "after")
                            n = ab.switch(live, which, folder)
                            blind.reset()
                            ab_state.update(which=which, blind=False)
                            return self._send(200, {"changed": n, "which": which})
                        finally:
                            live.close()
                if self.path == "/api/references/reuse":
                    return self._send(200, {"saved": songs.reuse_reference(runner.root, body.get("slug", ""),
                                                                           body.get("file", ""))})
                if self.path == "/api/references/choose":
                    if runner.running():
                        raise RuntimeError("pause the run before changing its references")
                    return self._send(200, {"saved": choose_reference(runner.workdir, body.get("id", ""))})
                if self.path in ("/api/songs/new", "/api/songs/switch", "/api/songs/adopt"):
                    if runner.running():
                        raise RuntimeError("wait until the run is finished")
                    info = open_set.value if open_set.reachable else None
                    if self.path == "/api/songs/new":
                        songs.create(runner.root, body.get("name", ""), body.get("description", ""),
                                     info["channels"] if info else None)
                    elif self.path == "/api/songs/switch":
                        songs.switch(runner.root, body.get("slug", ""))
                    elif info:   # «it is this track»: the open set's channels belong to the current track now
                        songs.remember_channels(runner.root, info["channels"])
                    ab_state.update(which=None, blind=False)
                    blind.reset()
                    return self._send(200, {"track": songs.current(runner.root)})
                if self.path == "/api/taps":
                    return self._send(200, {"log": runner.start(["taps"])})
                if self.path == "/api/taps/remove":
                    return self._send(200, {"log": runner.start(["taps", "--remove"])})
                if self.path == "/api/stop":
                    runner.stop()
                    return self._send(200, {"ok": True})
                if self.path == "/api/references/use":
                    return self._send(200, {"saved": download_reference(runner.workdir, body.get("url", ""),
                                                                        body.get("name", ""))})
                if self.path == "/api/references/import":
                    added = import_downloads(runner.workdir)
                    return self._send(200, {"added": added})
                if self.path == "/api/references/remove":
                    path = safe_reference(runner.workdir, body.get("name", ""))
                    if not os.path.exists(path):
                        raise ValueError("no such reference")
                    return self._send(200, {"trash": trash(path)})
                if self.path == "/api/project":
                    from . import project

                    path = os.path.join(runner.workdir, project.PATH)
                    data = project.load(path)
                    data["description"] = str(body.get("description", "")).strip()
                    project.save(data, path)
                    return self._send(200, data)
                if self.path == "/api/roles":
                    with open(os.path.join(runner.workdir, "roles.json"), "w") as f:
                        json.dump(body, f, indent=2, ensure_ascii=False)
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})
            except Exception as e:
                self._send(400, {"error": str(e)})

    return Handler


def _already_running(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url + "/api/state", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def serve(workdir: str = ".", port: int = PORT, open_browser: bool = True) -> None:
    import errno

    from . import songs

    runner = Runner(os.path.abspath(workdir))
    moved = songs.migrate(runner.root)
    if moved:
        print(f"Roles and runs moved to songs/ for «{moved}». references/ remains the shared library.")
    url = f"http://127.0.0.1:{port}"
    watcher = DownloadWatcher(lambda: runner.workdir)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(runner, watcher))
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        if _already_running(url):   # a second start: show the one that runs (it may be carrying a run)
            print(f"MARA is already running: {url}")
            if open_browser:
                webbrowser.open(url)
            return
        sys.exit(f"Port {port} is used by another program. Start MARA on another one: mixmatch ui --port {port + 1}")
    watcher.run()
    print(f"MARA: {url}  (Ctrl+C to quit)")
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
<link rel="stylesheet" href="/fonts/fonts.css?v=__FONTS_VERSION__">
<style>
:root{
  /* desktop of an old operating system, in MARA's own colours */
  --bg:#b9b2c9;--panel:#dcd7e6;--face:#e9e5f0;--hi:#fbf9ff;--lo:#8d859e;--ink:#1c1824;
  --line:#8d859e;--text:#1c1824;--muted:#5b5469;
  --accent:#6d3fd1;--accent2:#e2479c;--lime:#c8f03a;--lime-edge:#5f7d00;--pink:#ff8cc8;--pink-edge:#c2287c;
  --good:#2f8f4e;--bad:#c4304b;--warn:#a86b00;
  --title:linear-gradient(90deg,#4b2aa0,#b2399a 70%,#e2479c);
  --on-bright:#1c1824;--field:#fff;--strong:#1c1824;--on-strong:#fff;--shadow:#1c1824;--drop:rgba(28,24,36,.18);
  --dis-fg:#a39cb3;--dis-edge:#b9b2c9;--log-fg:#d8ffa0;--on-accent:#fff;--scale-mid:#f3e27a;
  color-scheme:light;
  --display:"Bungee",Impact,sans-serif;--pixel:"Silkscreen",ui-monospace,monospace;
  --mono:"Roboto Mono",ui-monospace,Menlo,monospace;--sans:"Space Grotesk",-apple-system,system-ui,sans-serif;
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
.history-panel{margin:16px 0 8px;padding:12px;border:1px solid var(--lo);background:var(--face)}
.history-heading{display:flex;justify-content:space-between;gap:12px;align-items:baseline;margin-bottom:10px;font-size:12px}
.history-heading b{font-weight:600}.history-heading span{color:var(--muted)}
#chart{display:block;width:100%;height:120px;overflow:visible}
#chart polyline,#chart line{vector-effect:non-scaling-stroke}
.chartlabels{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:10px;font-size:12px;color:var(--muted)}
.chartlabels span{display:inline-flex;align-items:center;gap:6px}
.phase-dot{display:inline-block;flex:none;width:8px;height:8px;background:var(--phase-color,var(--accent))}
.history-master{--phase-color:var(--accent)}.history-channels{--phase-color:var(--accent2)}
.history-effects{--phase-color:var(--warn)}.history-final{--phase-color:var(--good)}
#chart polyline{stroke:var(--phase-color,var(--accent))}#chart circle{fill:var(--phase-color,var(--accent))}
.resume-key{width:14px;border-top:1px dashed var(--muted)}
.drop{margin-top:12px;border:2px dashed var(--line);border-radius:10px;padding:34px 16px;min-height:110px;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);font-size:14px}
.drop.over{border-color:var(--accent);color:var(--accent)}.reflist li{display:flex;justify-content:space-between;gap:8px;align-items:center}
.reflist li span{flex:1;min-width:0;overflow-wrap:anywhere}.reflist li button{flex-shrink:0}
button.small{padding:2px 8px;font-size:12px}button.danger{background:var(--bad);color:#fff;border-color:var(--bad)}

/* dark: the same desktop at night */
:root[data-theme="dark"]{--bg:#110f17;--panel:#1f1b28;--face:#2a2535;--hi:#4b4360;--lo:#08070b;--ink:#ece8f6;--line:#4b4360;--text:#ece8f6;--muted:#a69eb8;
  --accent:#a98bff;--accent2:#ff7ac0;--good:#6fd38e;--bad:#ff6b85;--warn:#f0b54a;
  --title:linear-gradient(90deg,#3a1f86,#8f2a83 70%,#c23584);
  --field:#15121d;--strong:#08070b;--on-strong:#ece8f6;--shadow:#000;--drop:rgba(0,0,0,.45);
  --dis-fg:#5e5672;--dis-edge:#3a3448;--log-fg:#c8f03a;--on-accent:#110f17;--scale-mid:#d8c24f;color-scheme:dark;}
/* ── retro desktop layer ── */
[hidden]{display:none !important}
body{background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}
header{background:var(--panel);border-bottom:3px solid var(--ink);padding:10px 20px;flex-wrap:wrap}
header h1{font:400 42px/1 var(--display);letter-spacing:.01em;margin:0 8px 0 0;cursor:default;color:var(--ink);transition:color .15s}
header h1:hover{color:var(--accent2);text-shadow:3px 3px 0 var(--lime)}
.pill{border-radius:0;background:var(--face);color:var(--ink);font:12px var(--pixel);padding:2px 8px;
  border:1px solid;border-color:var(--lo) var(--hi) var(--hi) var(--lo)}
.pill.on{background:var(--lime);color:var(--on-bright);border-color:var(--lime-edge)}
#phase{font-family:var(--mono);font-size:12px}
header{min-width:0}
header #phase{min-width:0;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
header .prog{min-width:0;max-width:100%;flex-wrap:wrap;gap:6px}
#prog_text{min-width:0;white-space:normal}
main{gap:0 18px;padding:20px;grid-template-columns:repeat(auto-fit,minmax(min(100%,400px),1fr));align-items:start;grid-auto-rows:4px}
section{background:var(--panel);border-radius:0;padding:0 14px 14px;
  border:2px solid;border-color:var(--hi) var(--lo) var(--lo) var(--hi);box-shadow:1px 1px 0 var(--shadow),4px 4px 0 var(--drop)}
section>h2:first-child{margin:0 -14px 12px;padding:5px 10px;background:var(--title);color:#fff;
  font:400 12px var(--pixel);letter-spacing:.06em;text-transform:uppercase;display:flex;align-items:center;gap:8px}
section h2{font:400 12px var(--pixel);letter-spacing:.05em;color:var(--ink)}
button{border-radius:0;background:var(--lime);color:var(--on-bright);border:2px solid var(--lime-edge);font:400 13px var(--pixel);
  padding:6px 12px;box-shadow:inset -2px -2px 0 rgba(0,0,0,.18),inset 2px 2px 0 rgba(255,255,255,.55)}
button:hover:not(:disabled){filter:brightness(1.05)}
button:active:not(:disabled){box-shadow:inset 2px 2px 0 rgba(0,0,0,.25);transform:translate(1px,1px)}
button.ghost{background:var(--face);color:var(--ink);border:2px solid;border-color:var(--hi) var(--lo) var(--lo) var(--hi);box-shadow:none}
button.small{padding:2px 8px;font-size:12px}
#stop{background:var(--pink);border-color:var(--pink-edge)}
select,input,textarea{border-radius:0;background:var(--field);color:var(--ink);font:13px var(--mono);
  border:2px solid;border-color:var(--lo) var(--hi) var(--hi) var(--lo)}
label{color:var(--ink)}.muted{color:var(--muted)}
.big{font:400 50px/1 var(--display);color:var(--accent)}
.bar i{border-radius:0;background:var(--title);height:9px}
.bar span{font-family:var(--mono)}
svg polyline{stroke:var(--accent)}svg circle{fill:var(--accent)}
.prog i{border-radius:0;background:var(--field);border:2px solid;border-color:var(--lo) var(--hi) var(--hi) var(--lo);height:14px;width:180px}
.prog b{background:repeating-linear-gradient(90deg,var(--accent) 0 9px,transparent 9px 11px)}
.ev{border-radius:0;background:var(--face);border-left:4px solid var(--lo)}
.ev.kept{border-color:var(--good)}.ev.undone{border-color:var(--bad)}.ev.warning{border-color:var(--warn)}
.ev.phase{background:var(--strong);color:var(--on-strong);border-color:var(--lime);font-family:var(--mono)}
.ev.section{color:var(--accent);font-weight:600}
pre{border-radius:0;background:var(--strong);color:var(--log-fg);font:12px/1.45 var(--mono)}
.drop{border-radius:0;border:2px dashed var(--accent);background:var(--face);color:var(--accent);font-family:var(--mono)}
.drop.over{background:var(--lime);color:var(--on-bright);border-color:var(--lime-edge)}
a{color:var(--accent)}a:hover{background:var(--accent);color:var(--on-accent)}
td{border-bottom:1px solid var(--lo)}
#roles table{table-layout:fixed}
#roles td:first-child{width:35%;overflow-wrap:anywhere}
#roles td:last-child{width:64px}
#roles select,#roles input{width:100%;min-width:0;max-width:100%}
.activity-status{display:flex;gap:8px;align-items:baseline;margin:6px 0 10px;font-size:13px;overflow-wrap:anywhere}
.activity-light{width:8px;height:8px;flex:none;background:var(--lime);animation:working 1.4s ease-in-out infinite}
.activity-age{display:block;font:11px var(--mono);color:var(--muted);margin-top:3px}
@keyframes working{50%{opacity:.3}}
.bigprog.indeterminate b{width:30%!important;animation:waiting 1.8s ease-in-out infinite}
.bigprog{overflow:hidden}
@keyframes waiting{0%{transform:translateX(-100%)}100%{transform:translateX(340%)}}
@media(prefers-reduced-motion:reduce){.activity-light,.bigprog.indeterminate b{animation:none}}
button.primary{display:block;width:100%;font:400 19px var(--display);letter-spacing:.02em;padding:12px 16px;margin:4px 0 6px}
button.danger{background:var(--pink);color:var(--on-bright);border-color:var(--pink-edge);font-size:14px;padding:8px 18px;margin-right:10px}
#cont+#start.ghost{display:inline-block;width:auto;margin:0 0 6px}
.hint{color:var(--muted);font-size:12.5px;margin:4px 0 10px}
.now{display:flex;flex-direction:column;gap:2px;margin:2px 0 10px}.now b{font:400 18px var(--display)}
.now span,#scope{overflow-wrap:anywhere;min-width:0}
.bigprog{height:22px;background:var(--field);border:2px solid;border-color:var(--lo) var(--hi) var(--hi) var(--lo);margin-bottom:4px}
.bigprog b{display:block;height:100%;width:0;background:repeating-linear-gradient(90deg,var(--accent) 0 12px,transparent 12px 15px);transition:width .4s}
details.opts{margin:10px 0;background:var(--face);border:2px solid;border-color:var(--hi) var(--lo) var(--lo) var(--hi);padding:0 10px}
details.opts summary{cursor:pointer;font:400 12px var(--pixel);text-transform:uppercase;letter-spacing:.06em;padding:7px 0}
details.opts[open]{padding-bottom:8px}details.opts label{margin:6px 0}
.compare{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:12px 0 8px;font:400 12px var(--pixel);text-transform:uppercase;letter-spacing:.05em}
.seg{display:inline-flex}.seg button{background:var(--face);border:2px solid;border-color:var(--hi) var(--lo) var(--lo) var(--hi);box-shadow:none;min-width:84px}
.seg button+button{margin-left:-2px}.seg button.on{background:var(--accent);color:var(--on-accent);border-color:var(--shadow);box-shadow:inset 2px 2px 0 rgba(0,0,0,.3)}
.setup{border-top:1px solid var(--lo);padding-top:8px;margin-top:10px;font-size:12.5px;color:var(--muted)}
button.link{background:none;border:0;box-shadow:none;padding:0;color:var(--accent);font:400 12.5px var(--pixel);text-decoration:underline;cursor:pointer}
button.link:active{transform:none}
button:disabled,button.ghost:disabled,.seg button:disabled{opacity:1;color:var(--dis-fg);background:var(--face);
  border:2px solid var(--dis-edge);box-shadow:none;cursor:default;filter:none;transform:none}
.verdict{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.verdict b{font:400 30px/1.1 var(--display);color:var(--accent)}
.score{font:12px var(--mono);color:var(--muted)}
.scale{position:relative;height:12px;margin:10px 0 3px;background:linear-gradient(90deg,var(--lime),var(--scale-mid) 45%,var(--pink));
  border:2px solid;border-color:var(--lo) var(--hi) var(--hi) var(--lo)}
.scale i{position:absolute;top:-6px;width:4px;height:20px;margin-left:-2px;background:var(--shadow);transition:left .5s}
.scale-labels{display:flex;justify-content:space-between;font:11px var(--mono);color:var(--muted)}
.phase-table{table-layout:fixed;margin-top:12px;font-size:12px}
.phase-table th,.phase-table td{padding:9px 4px;border-bottom:1px solid var(--lo);text-align:right;vertical-align:middle}
.phase-table thead th{font-size:11px;font-weight:400;color:var(--muted)}
.phase-table th:first-child{width:35%;text-align:left;padding-left:0}
.phase-table th:last-child{width:25%}.phase-table td:last-child{padding-right:0}
.phase-table tbody th{font-weight:400}.phase-table .phase-name{display:flex;align-items:center;gap:7px}
.phase-table td{font-variant-numeric:tabular-nums;white-space:nowrap}
.phase-table .good{color:var(--good);font-weight:600}.mono{font-family:var(--mono)}
h3.sub{font:400 12px var(--pixel);text-transform:uppercase;letter-spacing:.06em;margin:16px 0 6px}
.part{margin:0 0 10px}.part-head{display:grid;grid-template-columns:minmax(150px,auto) 1fr;gap:10px;align-items:center;font-weight:600;font-size:13.5px}
.part-head i{display:block;height:9px;background:var(--title)}.part-why{font-size:12.5px;color:var(--muted);margin:3px 0 0}
ul.part-why{padding-left:18px}ul.part-why li{margin:2px 0}
.trackbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 10px}
.trackbar .label{font:400 12px var(--pixel);text-transform:uppercase;letter-spacing:.06em}
.trackbar select{flex:1;min-width:140px;font:600 15px var(--sans)}
.newform{background:var(--face);border:2px solid;border-color:var(--hi) var(--lo) var(--lo) var(--hi);padding:10px;margin:0 0 12px}
.newform label{display:block;margin:0 0 8px;font-size:13px}.newform input,.newform textarea{width:100%;box-sizing:border-box;margin-top:3px}
.reference-choice{margin:0 0 12px;padding:10px;background:var(--face);border:1px solid var(--line)}
.reference-choice>label{display:block;font:12px var(--pixel);margin-bottom:5px}
.reference-choice select{width:100%;min-width:0;margin-bottom:6px}
.candidate-list{list-style:none;padding:0;margin:8px 0}.candidate-list li{padding:6px 0;border-bottom:1px dotted var(--lo);overflow-wrap:anywhere}
.candidate-list button{margin:4px 0}
.banner{border:2px solid var(--warn);background:var(--field);padding:10px;margin:0 0 12px;font-size:13.5px}
.banner .row{margin-top:8px;flex-wrap:wrap;gap:8px}
ol.steps{list-style:none;padding:0;margin:0 0 12px;counter-reset:step}
ol.steps li{display:grid;grid-template-columns:22px 1fr;gap:8px;padding:6px 0;border-bottom:1px dotted var(--lo);font-size:13.5px}
ol.steps li i{font-style:normal;font-weight:700;text-align:center}
ol.steps li.ok i{color:var(--good)}ol.steps li.bad i{color:var(--bad)}ol.steps li.warn i{color:var(--warn)}ol.steps li.info i{color:var(--muted)}
ol.steps .sub{color:var(--muted);font-size:12.5px;display:block;margin-top:2px}
ol.steps button{margin:4px 8px 0 0}
#used ul{padding-left:0;list-style:none;margin:6px 0}#used li{display:flex;justify-content:space-between;gap:8px;padding:3px 0;font-size:13px}
#used h4{font:400 12px var(--pixel);text-transform:uppercase;letter-spacing:.05em;margin:10px 0 2px}
.overlap{font-size:13px;margin:2px 0 0}.overlap b{font-family:var(--pixel);font-weight:400;color:var(--accent2)}
</style>
</head>
<body>
<header><h1 title="Mix And Reference Assistant">MARA</h1><button id="theme" class="ghost small" title="Auto: light from 07:00 to 19:00 local time, dark at night. Click for light or dark.">Theme: auto</button><span id="status" class="pill">idle</span><span id="live" class="pill">Live …</span><span id="phase" class="muted"></span>
<span id="prog" class="prog" hidden><i><b id="prog_bar"></b></i><span id="prog_text" class="muted"></span></span></header>
<main>
<section>
  <h2>Run</h2>
  <p class="hint" id="mix_mode" hidden></p>
  <div class="trackbar"><span class="label">Track</span><select id="track_pick" title="Each track has its own references, roles and runs"></select>
    <button id="new_track" class="ghost small">New track</button></div>
  <div id="newform" class="newform" hidden>
    <label>Name of the new track<input id="nt_name" placeholder="e.g. Cleopatra beat" maxlength="60"></label>
    <label>Describe it (optional)<textarea id="nt_desc" rows="2" placeholder="genre, mood, what it should feel like"></textarea></label>
    <p class="hint">Starts empty: its own references, roles and runs. The set that is open in Live now belongs to it.
      References of earlier tracks can be picked again under References → Used before.</p>
    <div class="row"><button id="nt_create">Create track</button><button id="nt_cancel" class="ghost">Cancel</button></div>
  </div>
  <div id="mismatch" class="banner" hidden></div>
  <div id="reference_choice" class="reference-choice">
    <label for="reference_mode">Reference tracks</label>
    <select id="reference_mode">
      <option value="selected">I already have reference tracks</option>
      <option value="discover">Find reference suggestions for me</option>
    </select>
    <p id="reference_hint" class="hint"></p>
    <button id="choose_refs" class="ghost small">Choose reference tracks</button>
  </div>
  <ol id="steps" class="steps"></ol>
  <p class="hint" id="run_wait">Asking MARA what it is doing …</p>
  <div id="run_idle" hidden>
    <button id="cont" class="primary" hidden>Continue</button>
    <button id="start" class="primary">Mix the whole song</button>
    <p class="hint" id="start_hint">Plays the set, listens, and keeps only changes that measurably help.</p>
  </div>
  <div id="run_busy" hidden>
    <div class="now"><b id="now_phase">Working …</b><span id="now_section" class="muted"></span></div>
    <div class="activity-status"><i class="activity-light" aria-hidden="true"></i><div>
      <span id="activity_text" role="status">Starting …</span><span id="activity_age" class="activity-age"></span>
    </div></div>
    <div class="bigprog"><b id="bigprog_bar"></b></div><div class="hint" id="bigprog_text"></div>
    <button id="stop" class="danger">Pause</button>
    <span class="hint">Resume later from where you paused.</span>
  </div>
  <details class="opts"><summary>Options</summary>
    <label title="The Main fader goes down while MARA works and comes back at the end. It measures before the fader, so the results are the same."><input type="checkbox" id="silent" checked> silent: Main fader down while it works</label>
    <label><input type="checkbox" id="quick"> master only (quick, about 15 min)</label>
    <label><input type="checkbox" id="no_audit"> keep every effect (no check for useless ones)</label>
  </details>
  <div class="compare"><span>Compare the last run</span>
    <div class="seg"><button id="ab_before">Before</button><button id="ab_after">After</button></div></div>
  <audio id="ab_player" controls preload="metadata" style="width:100%;margin:8px 0" aria-label="Mix comparison audio player"></audio>
  <label><input type="checkbox" id="equal_loudness" checked disabled> Equal loudness</label>
  <span class="hint" id="equal_status">Available when both previews are ready.</span>
  <p class="hint" id="preview_status" aria-live="polite">Preparing the Before audio preview. After becomes available when the run finishes.</p>
  <div class="row"><button id="ab_blind" aria-keyshortcuts="B">Blind random · B</button>
    <button id="ab_reveal" class="ghost small" disabled>Reveal</button></div>
  <p class="hint" id="ab_hint" aria-live="polite">Close your eyes and press B, or focus the blind button and press Space. Each press is random; repeats are possible.</p>
  <div class="setup">Taps in Live: <button id="taps" class="link">set</button> · <button id="untaps" class="link" title="Takes every MixMatch tap out of the Live set">remove</button> <span id="taps_state"></span></div>
  <p class="muted" id="msg"></p>
</section>
<section>
  <h2>How close to the reference</h2>
  <div class="verdict"><b id="verdict">No run yet</b><span id="distance" class="score" title="Points from tone, loudness, punch, width, bass and clipping: 0 = measures like the reference"></span></div>
  <div class="overlap" id="overlap"></div>
  <div class="scale"><i id="scale_mark"></i></div>
  <div class="scale-labels"><span>like the reference</span><span>far off</span></div>
  <p class="hint" id="scope"></p>
  <div class="history-panel" id="history_panel" hidden>
    <div class="history-heading"><b>Score history</b><span>Lower is better</span></div>
    <svg id="chart" viewBox="0 0 300 90" preserveAspectRatio="none" role="img" aria-label="Score history, separated by mixing phase and resumed run"></svg>
    <div id="chartlabels" class="chartlabels"></div>
  </div>
  <div id="phases"></div>
  <h3 class="sub">What is still different</h3>
  <div class="parts" id="parts"><span class="muted">–</span></div>

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
<section id="references_panel">
  <h2>References</h2>
  <p class="hint">These are the tracks MARA will mix against. Add the ones you want, then start the mix.</p>
  <div id="refs_used"></div>
  <div class="row"><button id="pick_files" class="ghost">Add audio files</button>
    <input id="reference_upload" type="file" accept=".wav,.aif,.aiff,.flac,.mp3,.m4a,.aac" multiple hidden></div>
  <div id="found_refs"></div>
  <div class="row"><button id="used_btn" class="ghost small">Used before</button><span class="muted" id="used_count"></span></div>
  <div id="used" hidden></div>
  <div id="drop" class="drop">Drop reference audio files here</div><h2 style="margin-top:12px">Suggestions to get</h2><div id="to_get" class="muted">–</div>
</section>
<section>
  <h2>Roles</h2><div id="roles" class="muted">–</div><div class="row"><button id="save_roles" class="ghost">Save roles</button></div>
</section>
<section>
  <h2>Effects</h2><div id="effects" class="muted">–</div>
</section>
<section class="wide"><h2>Log</h2><pre id="log"></pre></section>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  const j = await r.json(); $("msg").textContent = j.error || ""; return j;
}
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  const hour = new Date().getHours();
  document.documentElement.dataset.theme = t === "auto" ? (hour >= 7 && hour < 19 ? "light" : "dark") : t;
  $("theme").textContent = `Theme: ${t}`;
}
let theme = "auto";
try { theme = localStorage.getItem("mm_theme") || "auto"; } catch {}
if (!THEMES.includes(theme)) theme = "auto";
applyTheme(theme);
setInterval(() => { if (theme === "auto") applyTheme(theme); }, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) applyTheme(theme); });
$("theme").onclick = () => { theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]; applyTheme(theme);
  try { localStorage.setItem("mm_theme", theme); } catch {} };
const OPTS = ["silent", "quick", "no_audit"];
try { const saved = JSON.parse(localStorage.getItem("mm_opts") || "{}");   // remembered per browser, a convenience
  for (const k of OPTS) if (k in saved) $(k).checked = saved[k]; } catch {}
for (const k of OPTS) $(k).addEventListener("change", () => {
  try { localStorage.setItem("mm_opts", JSON.stringify(Object.fromEntries(OPTS.map(o => [o, $(o).checked])))); } catch {} });
$("start").onclick = () => post("/api/run", {reference_mode: $("reference_mode").value, quick: $("quick").checked,
  no_audit: $("no_audit").checked, silent: $("silent").checked});
$("stop").onclick = () => post("/api/stop");
$("cont").onclick = () => post("/api/run", {continue: true, reference_mode: "selected", silent: $("silent").checked,
  no_audit: $("no_audit").checked});
$("reference_mode").onchange = () => { referenceModeChosen = true; tick(); };
$("choose_refs").onclick = () => $("references_panel").scrollIntoView({behavior: "smooth", block: "start"});
$("pick_files").onclick = () => $("reference_upload").click();
$("reference_upload").onchange = async e => { await uploadReferences([...e.target.files]); e.target.value = ""; };
$("taps").onclick = () => post("/api/taps");
$("untaps").onclick = () => { if (confirm("Remove every MixMatch tap from the Live set? «Set taps» puts them back.")) post("/api/taps/remove"); };
let previewVersions = {}, previewSelected = null, previewHidden = false, loudnessMatch = {ready: false};
let previewBaseUrl = "";
const player = $("ab_player");
player.volume = 0.5;
function loadPreview(url, play = false, position = 0) {
  previewBaseUrl = url;
  player.src = url + ($("equal_loudness").checked && loudnessMatch.ready ? "&equal=1&match=" + loudnessMatch.version : "");
  player.onloadedmetadata = () => { if (position) player.currentTime = Math.min(position, Math.max(0, player.duration - 0.1)); };
  if (play) player.play().catch(() => { $("preview_status").textContent = "Press Play in the audio player to listen."; });
}
$("equal_loudness").onchange = () => {
  if (previewBaseUrl) loadPreview(previewBaseUrl, !player.paused, player.currentTime || 0);
  updatePreviews();
};
function choosePreview(which, play = true) {
  const p = previewVersions[which]; if (!p) return;
  const position = player.currentTime || 0;
  previewSelected = which; previewHidden = false;
  loadPreview(`/api/preview-audio?which=${which}&id=${encodeURIComponent(p.id)}`, play, position);
  $("ab_hint").textContent = "Close your eyes and press B for a random version. Repeats are possible.";
  updatePreviews();
}
function updatePreviews() {
  $("equal_loudness").disabled = !loudnessMatch.ready;
  $("equal_status").textContent = !loudnessMatch.ready ? loudnessMatch.reason || "Available when both previews are ready."
    : $("equal_loudness").checked ? "Matched listening level · original dynamics preserved." : "Original listening levels.";
  for (const w of ["before", "after"]) { $("ab_" + w).disabled = abBusy || !previewVersions[w];
    $("ab_" + w).className = !previewHidden && previewSelected === w ? "on" : ""; }
  $("ab_blind").disabled = abBusy || !previewVersions.before || !previewVersions.after;
  $("ab_reveal").disabled = abBusy || !previewHidden;
  const p = previewVersions[previewSelected];
  $("preview_status").textContent = p
    ? `${previewHidden ? "Blind preview" : previewSelected === "before" ? "Before" : "After"} · ${p.bars} bars from bar ${p.start_bar} · plays here while MARA mixes silently.` + (!previewVersions.after ? " After is not ready yet." : "")
    : "Preparing the Before audio preview. After becomes available when the run finishes.";
}
$("ab_before").onclick = () => choosePreview("before");
$("ab_after").onclick = () => choosePreview("after");
let abBusy = false;
$("ab_blind").onclick = async () => {
  if (abBusy || $("ab_blind").disabled) return;
  abBusy = true; $("ab_blind").disabled = true;
  $("msg").textContent = "";
  $("ab_hint").textContent = "Choosing a hidden version …";
  for (const w of ["before", "after"]) { $("ab_" + w).className = ""; $("ab_" + w).disabled = true; }
  try { const j = await post("/api/preview/blind");
    if (!j.error) { previewHidden = true; loadPreview(j.url, true, player.currentTime || 0);
      $("ab_hint").textContent = "Playing a hidden version. Press B again or Reveal when you are ready."; }
  } finally { abBusy = false; tick(); }
};
$("ab_reveal").onclick = async () => { const j = await post("/api/preview/reveal");
  if (!j.error) { previewSelected = j.which; previewHidden = false; updatePreviews();
    $("ab_hint").textContent = "You are hearing: " + (j.which === "before" ? "Before" : "After"); } };
document.addEventListener("keydown", e => {
  if (e.repeat || e.ctrlKey || e.metaKey || e.altKey || e.target.closest("input, textarea, select, [contenteditable]")) return;
  if (e.key.toLowerCase() === "b" && !$("ab_blind").disabled) { e.preventDefault(); $("ab_blind").click(); }
});
const ROLE_GROUPS = __ROLE_GROUPS__;
let rolesEdited = false, rolesLoading = false, lastRolesRevision = null;
async function loadRoles() {
  if (rolesEdited || rolesLoading) return false;
  rolesLoading = true;
  try {
  const roles = await (await fetch("/api/roles")).json(); const names = Object.keys(roles);
  if (rolesEdited) return false;
  if (!names.length) { $("roles").textContent = "No roles.json yet (written by the first run)."; return true; }
  $("roles").innerHTML = "<table>" + names.map(n => `<tr><td>${esc(n)}</td><td><select title="${esc(roles[n].note || '')}" data-n="${esc(n)}">${Object.entries(ROLE_GROUPS).map(([g, rs]) => `<optgroup label="${esc(g)}">` + rs.map(r =>
    `<option ${r===roles[n].role?"selected":""}>${r}</option>`).join("") + "</optgroup>").join("")}</select></td><td><input data-p="${esc(n)}" type="number" min="1" max="5" value="${roles[n].priority}"></td></tr>`).join("") + "</table>";
  const current = () => names.map(n => document.querySelector(`[data-n="${CSS.escape(n)}"]`).value + "|" +
    document.querySelector(`[data-p="${CSS.escape(n)}"]`).value).join(",");
  let saved = current();
  const rolesDirty = () => { rolesEdited = current() !== saved; $("save_roles").disabled = !rolesEdited; };
  $("roles").oninput = rolesDirty; $("roles").onchange = rolesDirty; rolesDirty();
  $("save_roles").onclick = async () => {
    for (const n of names) { const role = document.querySelector(`[data-n="${CSS.escape(n)}"]`).value;
      const priority = +document.querySelector(`[data-p="${CSS.escape(n)}"]`).value;
      if (role !== roles[n].role || priority !== roles[n].priority) roles[n] = {...roles[n], role, priority, source: "user"}; }
    const result = await post("/api/roles", roles); if (result.error) return;
    saved = current(); rolesDirty();
    $("msg").textContent = "Roles saved; the next run uses them.";
  };
  return true;
  } finally { rolesLoading = false; }
}
function chart(values, phases, runs) {
  $("history_panel").hidden = !values.length;
  if (!values.length) { $("chart").innerHTML = ""; $("chartlabels").innerHTML = ""; return; }
  phases = phases || []; runs = runs || [];
  const max = Math.max(1, ...values) * 1.05, w = 300, h = 90;
  const x = i => 3 + (i/Math.max(1, values.length-1))*(w-6), y = v => h-3 - (v/max)*(h-6);
  // one line per phase: master and channels are measured differently, so their numbers are not joined
  let svg = [0, 1, 2].map(i => `<line x1="0" x2="300" y1="${3+i*42}" y2="${3+i*42}" stroke="var(--lo)" opacity=".25"/>`).join("");
  let from = 0, resumed = false;
  for (let i = 1; i <= values.length; i++) {
    if (i < values.length && phases[i] === phases[from] && runs[i] === runs[from]) continue;
    const seg = values.slice(from, i).map((v, j) => `${x(from + j)},${y(v)}`).join(" ");
    const cls = `history-${esc(phases[from] || "setup")}`;
    svg += i - from > 1 ? `<polyline class="${cls}" fill="none" stroke-width="2" points="${seg}"/>`
                        : `<circle class="${cls}" cx="${x(from)}" cy="${y(values[from])}" r="1.5"/>`;
    if (from > 0) {
      const xx = (x(from) + x(from-1))/2, restart = runs[from] !== runs[from-1];
      resumed ||= restart;
      svg += `<line x1="${xx}" x2="${xx}" y1="0" y2="${h}" stroke="var(--muted)" opacity=".5" stroke-dasharray="${restart ? "4 4" : "0"}"/>`;
    }
    from = i;
  }
  let labels = [...new Set(phases.filter(Boolean))].map(p => `<span class="history-${esc(p)}"><i class="phase-dot"></i>${esc(PHASE_NAMES[p] || p)}</span>`).join("");
  if (resumed) labels += '<span><i class="resume-key"></i>Resumed run</span>';
  $("chart").innerHTML = svg; $("chartlabels").innerHTML = labels;
}
const PART_NAMES = {masking: "Potential masking between instruments", tonal: "Tone balance", loudness: "Loudness",
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
  // The verdict is about the references only. Instruments covering each other is a matter inside the song:
  // shown on its own line, not mixed into «how close».
  const p = s.parts || {}, ref = Object.entries(p).filter(([k]) => k !== "masking").reduce((a, [, v]) => a + v, 0);
  const waiting = !s.active && s.checkpoint?.phase === "references";
  const local = (s.section || "").includes("outside the loudest part");
  const compared = !waiting && "tonal" in (s.explain || {});
  const [word, pos] = compared ? verdict(ref) : [waiting ? "Choose references" : local ? "Not compared here" : "Waiting for comparison", 0];
  $("verdict").textContent = word;
  $("distance").textContent = compared ? ref.toFixed(2) + " points" : local && !waiting ? "this part is not meant to sound like the references" : "";
  const ov = p.masking || 0;
  $("overlap").innerHTML = ov >= 0.05 ? `Potential masking during simultaneous playback: <b>${ov < 1 ? "small" : ov < 2.5 ? "noticeable" : "strong"}</b> <span class="score">${ov.toFixed(2)} points</span>` : "";
  $("scale_mark").style.left = (pos * 100).toFixed(1) + "%";
  $("scale_mark").parentElement.hidden = !compared;
  document.querySelector(".scale-labels").hidden = !compared;
  const phase = (s.history_phase || []).slice(-1)[0];
  const where = (s.section || "a channel").replace(/ \(outside the loudest part[^)]*\)/, "").replace(/_/g, " ");
  $("scope").textContent = waiting ? "Choose your reference tracks below, then mix with them. The song scan is saved."
    : !compared && !local ? "Results appear once reference tracks are chosen and mixing begins."
    : phase === "channels"
    ? (local ? `Now on ${where}. This part is outside the loudest part, so it is not compared with the references: only how the instruments cover each other and a mono bass count.`
             : `Now on ${where}. While MARA works on single channels, loudness and punch are left out: that part of the song need not sound like the drop.`)
    : (s.section ? `Now on the master, ${s.section}. Tone, loudness, punch and width are compared with the references.` : "");
  // one row per phase: where it started and where it is now
  const rows = [], hist = s.history || [], ph = s.history_phase || [];
  for (let i = 0; i < hist.length; i++) {
    const last = rows[rows.length - 1];
    if (last && last.phase === ph[i]) { last.now = hist[i]; last.best = Math.min(last.best, hist[i]); }
    else rows.push({phase: ph[i], start: hist[i], now: hist[i], best: hist[i]});
  }
  const phaseRows = rows.filter(r => r.phase !== "setup").map(r => {
    const gain = r.start > 0 ? Math.round(100 * (r.start - r.best) / r.start) : 0;
    return `<tr><th scope="row"><span class="phase-name history-${esc(r.phase)}"><i class="phase-dot"></i>${esc(PHASE_NAMES[r.phase] || r.phase)}</span></th>` +
      `<td class="mono">${r.start.toFixed(2)}</td><td class="mono">${r.best.toFixed(2)}</td>` +
      `<td class="${gain > 0 ? "good" : "muted"}">${gain > 0 ? "−" + gain + "%" : "Unchanged"}</td></tr>`;
  }).join("");
  $("phases").innerHTML = phaseRows ? `<table class="phase-table" aria-label="Scores by mixing phase"><thead><tr><th scope="col">Phase</th><th scope="col">Start</th><th scope="col">Best</th><th scope="col">Change</th></tr></thead><tbody>${phaseRows}</tbody></table>` : "";
  const parts = Object.entries(s.parts || {}).filter(([, v]) => v >= 0.05).sort((a, b) => b[1] - a[1]);
  const top = Math.max(1, ...parts.map(p => p[1])), ex = s.explain || {};
  $("parts").innerHTML = parts.length ? parts.map(([k, v]) =>
    `<div class="part"><div class="part-head"><span>${esc(PART_NAMES[k] || k)}</span><i style="width:${(v/top)*100}%"></i></div>` +
    (ex[k] ? why(k, ex[k]) : "") + "</div>").join("") : `<span class='muted'>${compared || local ? "Nothing worth mentioning." : "Comparison has not started yet."}</span>`;
}
function progress(s) {
  const p = s.progress, p0 = s.progress_start, el = $("prog");
  const preparing = s.active && (!p || !p.done);
  $("bigprog_bar").parentElement.classList.toggle("indeterminate", preparing && !s.activity?.total);
  if (preparing) {
    el.hidden = true;
    const a = s.activity;
    $("bigprog_bar").style.width = a?.total ? `${Math.min(100, 100 * a.current / a.total)}%` : "30%";
    $("bigprog_text").textContent = a?.total ? `Song scan · ${a.current} of ${a.total} bars` : "Preparing · duration depends on the current step";
    return;
  }
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
  $("prog_text").textContent = `${Math.round(frac*100)}%${left}`;
  $("bigprog_bar").style.width = (frac*100).toFixed(1) + "%"; $("bigprog_text").textContent = `${Math.round(frac*100)}%${left}`;
}
let currentActivity = null, referenceModeChosen = false;
function selectedReferences() {
  $("reference_mode").value = "selected";
  referenceModeChosen = true;
  lastRefs = "";
}
function activityAge() {
  const a = currentActivity;
  if (!a) { $("activity_age").textContent = ""; return; }
  const seconds = Math.max(0, Math.floor(Date.now()/1000 - a.started));
  $("activity_age").textContent = `This step: ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2, "0")}`;
}
async function tick() {
  let s; try {
    const response = await fetch("/api/state");
    if (!response.ok) throw new Error("state unavailable");
    s = await response.json();
  } catch {
    currentActivity = null;
    $("status").textContent = "reconnecting";
    $("activity_text").textContent = "Connection to MARA lost · trying again …";
    activityAge();
    return;
  }
  currentActivity = s.active && s.activity && Number.isFinite(s.activity.started) ? s.activity : null;
  $("activity_text").textContent = s.activity?.text || "Starting the run · waiting for the first update …";
  activityAge();
  const ck = s.checkpoint, refs = s.reference_files.length + s.project_references.length;
  if (!referenceModeChosen) {
    $("reference_mode").value = ck || refs ? "selected" : "discover";
    referenceModeChosen = true;
  }
  const discover = $("reference_mode").value === "discover", choosing = ck?.phase === "references";
  $("reference_mode").disabled = s.active;
  $("reference_choice").hidden = s.active;
  $("reference_hint").textContent = discover
    ? "MARA listens to your song and suggests tracks. You choose the references before mixing starts."
    : refs ? `Use the ${refs} reference track${refs > 1 ? "s" : ""} listed below. Reference search will be skipped.`
           : "Choose at least one reference track below: add audio files or use a suggestion.";
  $("status").textContent = s.active ? "running" : choosing ? "choose references" : (s.done ? (s.done.startsWith("Stopped:") ? "stopped" : "finished") : "ready"); $("status").className = "pill" + (s.active ? " on" : "");
  $("phase").textContent = choosing && !s.active ? "Song scan saved · waiting for your reference choice" : s.phase || "";
  $("mix_mode").hidden = !s.mode;
  $("mix_mode").textContent = s.mode === "beat" ? "Beat mode · muted vocal tracks are excluded."
    : "Song mode · full reference balance.";
  if (s.roles_revision !== lastRolesRevision && !rolesEdited && await loadRoles()) lastRolesRevision = s.roles_revision;
  tracks(s); usedRefs(s); const { ready } = steps(s);
  $("start").disabled = s.active || !ready; $("cont").disabled = s.active || !ready || !refs;
  $("run_wait").hidden = true; $("run_idle").hidden = s.active; $("run_busy").hidden = !s.active;
  $("cont").hidden = !ck || discover;
  if (ck) $("cont").textContent = ({references: "Mix with selected references", master: "Continue mixing with references",
    channels: "Continue mixing channels", effects: "Continue checking effects", final: "Continue final check"})[ck.phase] || "Continue mixing with references";
  $("start").hidden = choosing && !discover;
  $("start").textContent = discover ? "Find reference suggestions" : ck ? "Start mix over" : "Mix with selected references";
  $("start").className = ck && !discover ? "ghost" : "primary";
  $("start_hint").textContent = discover ? "Find suggestions first. MARA pauses for your choice before changing the mix."
    : choosing ? "The mix is paused. Choose your references, then continue here. The saved song scan will be reused."
    : ck ? "Continue with the selected references. Completed steps will be skipped."
         : "Skip reference search and mix directly with the tracks you selected.";
  $("now_phase").textContent = s.phase || "Preparing …"; $("now_section").textContent = s.section || "";
  if (s.auto_imported && s.auto_imported.length !== lastImported) {
    if (lastImported >= 0 && s.auto_imported.length) $("msg").textContent = "Picked up from Downloads: " + s.auto_imported.slice(-3).join(", ");
    lastImported = s.auto_imported.length; lastRefs = ""; }
  const previewChanged = previewVersions.before?.id !== s.previews?.before?.id;
  const matchChanged = loudnessMatch.version !== s.loudness_match?.version;
  loudnessMatch = s.loudness_match || {ready: false};
  previewVersions = s.previews || {};
  if (previewChanged) {
    player.pause(); previewSelected = null; previewHidden = false;
    if (previewVersions.before) choosePreview("before", false);
    else { previewBaseUrl = ""; player.removeAttribute("src"); player.load(); }
  }
  else if (matchChanged && previewBaseUrl && $("equal_loudness").checked) {
    loadPreview(previewBaseUrl, !player.paused, player.currentTime || 0);
  }
  updatePreviews();
  $("live").textContent = s.live === null ? "Live …" : (s.live ? "Live connected" : "Live busy or closed (exporting?)");
  $("live").className = "pill" + (s.live ? " on" : "");
  closeness(s); chart(s.history, s.history_phase, s.history_run); progress(s);
  $("song").innerHTML = [s.key && `<div><b>${esc(s.key)}</b></div>`, s.style && `<div class="muted">sounds like ${esc(s.style)}</div>`].filter(Boolean).join("") || "No run yet.";
  $("events").innerHTML = s.events.slice().reverse().map(e => `<div class="ev ${e.kind}">${esc(e.text)}</div>`).join("");
  const refsKey = JSON.stringify(s.reference_files) + JSON.stringify(s.project_references) + JSON.stringify(s.reference_candidates) + s.active;
  if (refsKey !== lastRefs) { lastRefs = refsKey;
    $("refs_used").innerHTML = (s.project_references.length ? "<div class='muted'>in the Live set (always used):</div><ul>" +
      s.project_references.map(r => `<li><b>${esc(r.track)}</b>: ${esc(r.file)}</li>`).join("") + "</ul><div class='muted'>in references/:</div>" : "")
      + (s.reference_files.length ? "<ul class='reflist'>" + s.reference_files.map(f =>
      `<li><span>${esc(f)}</span><button class="ghost small" data-remove="${esc(f)}">remove</button></li>`).join("") + "</ul>"
      : "<span class='muted'>No reference files selected yet.</span>");
    $("found_refs").innerHTML = s.reference_candidates?.length ? "<h3 class='sub'>Available references — choose for this song</h3><p class='hint'>The shared references/ folder is detected automatically. Use the tracks you want for this song.</p><ul class='candidate-list'>" +
      s.reference_candidates.map(c => `<li><span>${esc(c.name)} <span class='muted'>(${esc(c.source)})</span></span>` +
        (s.reference_files.includes(c.name) ? "<span class='muted'>Selected</span>"
          : `<button class='small' data-pick-reference='${esc(c.id)}' ${s.active ? "disabled" : ""}>Use this reference</button>`) + "</li>").join("") + "</ul>" : "";
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
    b.textContent = j.error ? "failed" : "added"; if (!j.error) { $("msg").textContent = "Added to references/: " + j.saved; selectedReferences(); tick(); }
  });
  if ($("import_dl")) $("import_dl").onclick = async () => { const j = await post("/api/references/import");
    if (!j.error) { $("msg").textContent = j.added.length ? "Imported: " + j.added.join(", ") : "Nothing new in Downloads (last hour)."; if (j.added.length) selectedReferences(); tick(); } };
  }
  $("effects").innerHTML = s.effects.length ? "<ul>" + s.effects.map(e => `<li>${esc(e)}</li>`).join("") + "</ul>"
    : "<span class='hint'>Fills in during the effect check near the end of a run: what was kept, switched off or removed.</span>";
  const log = $("log"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 5;
  log.textContent = s.log.join("\n"); if (atEnd) log.scrollTop = log.scrollHeight;
}
let lastRefs = "", lastGet = "", lastImported = -1, lastTracks = "", lastUsed = "";
const STEP_ICON = {ok: "✓", bad: "✗", warn: "!", info: "○"};
function step(kind, text, sub, buttons) {
  return `<li class="${kind}"><i>${STEP_ICON[kind]}</i><span>${text}${sub ? `<span class="sub">${sub}</span>` : ""}${buttons || ""}</span></li>`;
}
function tracks(s) {
  // the track picker, the form for a new one, and the warning when Live has another set open
  const key = JSON.stringify(s.tracks) + s.track;
  if (key !== lastTracks) { lastTracks = key;
    $("track_pick").innerHTML = s.tracks.length ? s.tracks.map(t => `<option value="${esc(t.slug)}" ${t.current ? "selected" : ""}>${esc(t.name)}</option>`).join("")
      : `<option>this folder (no tracks yet)</option>`;
  }
  $("track_pick").disabled = s.active || s.tracks.length < 2; $("new_track").disabled = s.active;
  const m = s.set || {same: true};
  $("mismatch").hidden = s.active || m.same;
  if (!m.same) {
    const key2 = JSON.stringify(m) + s.track;
    if ($("mismatch").dataset.key !== key2) { $("mismatch").dataset.key = key2;
      $("mismatch").innerHTML = `<b>Live has a different set open than «${esc(s.track)}».</b> Its channels do not match this track.` +
        (m.other ? ` They match «${esc(m.other.name)}».` : " Is it a new track?") +
        `<div class="row">${m.other ? `<button data-switch="${esc(m.other.slug)}">Switch to «${esc(m.other.name)}»</button>` : ""}` +
        `<button data-act="newtrack" class="${m.other ? "ghost" : ""}">New track for this set</button>` +
        `<button data-act="adopt" class="ghost">It is «${esc(s.track)}» (channels renamed)</button></div>`;
    }
  }
}
function steps(s) {
  if (s.active) { $("steps").innerHTML = ""; $("taps_state").textContent = ""; return { ready: false }; }
  const out = [], o = s.open_set, refs = s.reference_files.length + s.project_references.length;
  out.push(s.live ? step("ok", "Live is connected")
    : step("bad", "Live is not reachable", "Open the set in Live; «MixMatch» must be the control surface (Settings → Link, Tempo & MIDI)."));
  if (!o) out.push(step("info", "Taps: unknown until Live answers"));
  else if (o.taps === o.total) out.push(step("ok", `Taps are set in this set: all ${o.total} channels`));
  else if (o.taps === 0) out.push(step("bad", "No taps in this set yet", "MARA measures every channel through a MixMatch Tap.",
    `<br><button data-act="taps">Set taps</button>`));
  else out.push(step("warn", `Taps on ${o.taps} of ${o.total} channels`, "Without a tap: " + o.missing.slice(0, 8).map(esc).join(", ") + (o.missing.length > 8 ? ` and ${o.missing.length - 8} more` : ""),
    `<br><button data-act="taps">Set the missing taps</button>`));
  const used = s.used_references.length;
  if (refs) out.push(step("ok", `${refs} reference track${refs > 1 ? "s" : ""} for this track`, "Listed under References."));
  else if ($("reference_mode").value === "discover") out.push(step("info", "Find suggestions, then choose references",
    "MARA will pause before mixing. You can also add your own under References" + (used ? `, or pick from ${used} used before.` : "."),
    used ? `<br><button data-act="used" class="ghost small">Show used references</button>` : ""));
  else out.push(step("warn", "Choose your reference tracks", "Add your audio files or choose from the suggestions below.",
    `<br><button data-act="references" class="small">Choose references</button>`));
  out.push(savedDesc.trim() ? step("ok", "Described: " + esc(savedDesc.trim().slice(0, 80)))
    : step("info", "Not described yet (optional)", "A few words under «Describe your track» help MARA pick references and decide."));
  $("steps").innerHTML = out.join("");
  $("taps_state").textContent = o ? `(${o.taps} of ${o.total} channels)` : "";
  const ready = s.live && (s.set || {same: true}).same && !(o && o.taps === 0) && (refs > 0 || $("reference_mode").value === "discover");
  return { ready };
}
function usedRefs(s) {
  const key = JSON.stringify(s.used_references) + JSON.stringify(s.reference_files);
  $("used_count").textContent = s.used_references.length ? `${s.used_references.length} from earlier tracks` : "none from earlier tracks";
  $("used_btn").disabled = !s.used_references.length;
  if (key === lastUsed) return; lastUsed = key;
  const byTrack = {};
  for (const r of s.used_references) (byTrack[r.track] = byTrack[r.track] || []).push(r);
  $("used").innerHTML = Object.entries(byTrack).map(([t, rs]) => `<h4>${esc(t)}</h4><ul>` + rs.map(r =>
    `<li><span>${esc(r.file)}</span>` + (s.reference_files.includes(r.file) ? `<span class="muted">in this track</span>`
      : `<button class="small" data-reuse="${esc(r.file)}" data-slug="${esc(r.slug)}">Use</button>`) + "</li>").join("") + "</ul>").join("");
}
async function reload(path, body) { const j = await post(path, body); if (!j.error) location.reload(); }
$("track_pick").onchange = e => reload("/api/songs/switch", {slug: e.target.value});
$("new_track").onclick = () => { $("newform").hidden = !$("newform").hidden; if (!$("newform").hidden) $("nt_name").focus(); pack(); };
$("nt_cancel").onclick = () => { $("newform").hidden = true; pack(); };
$("nt_create").onclick = () => reload("/api/songs/new", {name: $("nt_name").value, description: $("nt_desc").value});
$("used_btn").onclick = () => { $("used").hidden = !$("used").hidden; $("used_btn").textContent = $("used").hidden ? "Used before" : "Hide used"; pack(); };
document.addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.switch) return reload("/api/songs/switch", {slug: b.dataset.switch});
  if (b.dataset.pickReference) { b.disabled = true; const j = await post("/api/references/choose", {id: b.dataset.pickReference});
    if (!j.error) { $("msg").textContent = "Selected: " + j.saved; selectedReferences(); }
    lastRefs = ""; tick(); return; }
  if (b.dataset.reuse) { b.disabled = true; const j = await post("/api/references/reuse", {slug: b.dataset.slug, file: b.dataset.reuse});
    if (!j.error) { $("msg").textContent = "Added to this track: " + j.saved; selectedReferences(); lastUsed = ""; tick(); } return; }
  const act = b.dataset.act;
  if (act === "taps") { b.disabled = true; b.textContent = "Setting taps …"; post("/api/taps"); }
  else if (act === "newtrack") { $("newform").hidden = false; $("nt_name").focus(); pack(); }
  else if (act === "adopt") reload("/api/songs/adopt");
  else if (act === "references") $("references_panel").scrollIntoView({behavior: "smooth", block: "start"});
  else if (act === "used") { $("used").hidden = false; $("used_btn").textContent = "Hide used";
    document.querySelector("#used").scrollIntoView({behavior: "smooth", block: "center"}); pack(); }
});
const drop = $("drop");
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => uploadReferences([...e.dataTransfer.files]));
async function uploadReferences(files) {
  if (!files.length) return;
  drop.textContent = `Adding ${files.length} file${files.length > 1 ? "s" : ""} …`;
  const results = await Promise.all(files.map(async file => {   // all at once, each with its own answer
    try {
      const r = await fetch("/api/references/" + encodeURIComponent(file.name), {method: "PUT", body: file});
      const j = await r.json();
      return j.error ? `${file.name}: ${j.error}` : `Added ${j.saved}` + (j.warning ? ` (${j.warning})` : "");
    } catch (err) { return `${file.name}: upload failed (${err.message || err})`; }
  }));
  $("msg").textContent = results.join(" · ");
  drop.textContent = "Drop reference audio files here"; selectedReferences(); tick();
}
// «Save» only when something changed: greyed out otherwise
let savedDesc = "";
const descDirty = () => { $("save_desc").disabled = $("desc").value.trim() === savedDesc.trim(); };
$("save_desc").disabled = true;
fetch("/api/project").then(r => r.json()).then(p => { savedDesc = $("desc").value = p.description || ""; descDirty(); });
$("desc").addEventListener("input", descDirty);
$("save_desc").onclick = async () => { const j = await post("/api/project", {description: $("desc").value});
  if (!j.error) { savedDesc = $("desc").value; descDirty(); $("msg").textContent = "Description saved."; } };
// Masonry: grid rows of 4 px, each window spans as many as its height needs, so a short window does not leave
// a gap under itself when its neighbour is tall (narrow screens with two columns especially).
let packing = 0;
function pack() {
  if (packing) return;
  packing = requestAnimationFrame(() => {
    const gap = 18, unit = 4;                    // rows of 4 px, no row gap: 18 px of span keep windows apart
    for (const sec of document.querySelector("main").children)
      sec.style.gridRowEnd = "span " + Math.ceil((sec.scrollHeight + 4 + gap) / unit);
    packing = 0;
  });
}
const packer = new ResizeObserver(pack);
packer.observe(document.querySelector("main"));
for (const sec of document.querySelector("main").children) packer.observe(sec);
loadRoles(); tick(); setInterval(tick, 1500); setInterval(activityAge, 1000);
</script>
</body>
</html>
"""
PAGE = PAGE.replace("__FONTS_VERSION__", str(int(os.path.getmtime(os.path.join(FONTS, "fonts.css")))))
PAGE = PAGE.replace("__ROLE_GROUPS__", json.dumps(__import__("mixmatch.roles", fromlist=["ROLE_GROUPS"]).ROLE_GROUPS))
