"""Read the open Live document when the control surface cannot expose its path.

Live's Python API exposes neither the set's name nor its file, so MARA asks the
operating system: on macOS the document window's AXDocument (a full path), on
Windows the main window title (the set name only).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

# Why the open set could not be identified; shown to the user by the UI.
NOT_RUNNING, PERMISSION, UNSAVED, SLOW, UNKNOWN = "not_running", "permission", "unsaved", "slow", "unknown"

_MAC_SCRIPT = '''tell application "System Events"
if not (exists process "Live") then return "MARA:NOLIVE"
tell process "Live" to set liveDocumentURLs to value of attribute "AXDocument" of windows
set AppleScript's text item delimiters to linefeed
return liveDocumentURLs as text
end tell'''
# -1743: no Automation permission for System Events; -1719/-25211: no Accessibility permission.
_MAC_DENIED = ("-1743", "-1719", "-25211", "not allowed assistive", "not authorized")


def _live_running() -> bool | None:
    """Whether the macOS Live process runs; None when the process list is unavailable."""
    try:
        import psutil
        return any(p.info["name"] == "Live" for p in psutil.process_iter(["name"]))
    except Exception:
        return None


def _mac_document() -> tuple[str, str]:
    """(path, problem) from the AXDocument of Live's windows."""
    if _live_running() is False:
        return "", NOT_RUNNING
    # System Events often takes several seconds to answer, and longer while macOS shows a permission prompt.
    try:
        result = subprocess.run(["osascript", "-e", _MAC_SCRIPT], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return "", SLOW
    except OSError:
        return "", UNKNOWN
    if result.returncode:
        err = (result.stderr or "").lower()
        return "", PERMISSION if any(code in err for code in _MAC_DENIED) else UNKNOWN
    if result.stdout.strip() == "MARA:NOLIVE":
        return "", NOT_RUNNING
    paths = set()
    for line in result.stdout.splitlines():
        uri = urlsplit(line.strip())
        if uri.scheme == "file" and uri.netloc in ("", "localhost"):
            path = unquote(uri.path)
            if path.lower().endswith(".als") and os.path.isabs(path):
                paths.add(os.path.realpath(path))
    if len(paths) == 1:
        return paths.pop(), ""
    return "", UNSAVED if not paths else UNKNOWN


def document_path() -> str:
    """Return the unambiguous macOS AXDocument path, including after Save As."""
    if sys.platform != "darwin":
        return ""
    return _mac_document()[0]


def set_name_from_title(title: str) -> str:
    """'Song [Song Project] - Ableton Live 12 Suite' -> 'Song'; '' for other windows and unsaved sets."""
    m = re.match(r"^(.*?)\s+-\s+Ableton Live\b", title.strip())
    if not m:
        return ""
    name = re.sub(r"\s*\[[^\]]*\]\s*$", "", m.group(1)).strip().rstrip("*").strip()
    return "" if name.lower() in ("", "untitled") else name


def _windows_titles() -> list[str] | None:
    """Titles of the visible top-level windows of Ableton Live; None when Live is not running."""
    import ctypes
    from ctypes import wintypes

    import psutil

    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("ableton live")}
    if not pids:
        return None
    user32 = ctypes.windll.user32
    titles: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            length = user32.GetWindowTextLengthW(hwnd)
            if pid.value in pids and length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(collect, 0)
    return titles


def _windows_document() -> tuple[str, str]:
    """(set name, problem) from Live's main window title."""
    try:
        titles = _windows_titles()
    except Exception:
        return "", UNKNOWN
    if titles is None:
        return "", NOT_RUNNING
    names = {set_name_from_title(t) for t in titles if re.search(r"-\s+Ableton Live\b", t)}
    names.discard("")
    if len(names) == 1:
        return names.pop(), ""
    return "", UNSAVED if not names else UNKNOWN


def identify() -> dict:
    """{'path', 'name', 'problem'} of the open Live document as far as the OS reveals it."""
    if sys.platform == "darwin":
        path, problem = _mac_document()
        return {"path": path, "name": os.path.splitext(os.path.basename(path))[0] if path else "", "problem": problem}
    if sys.platform == "win32":
        name, problem = _windows_document()
        return {"path": "", "name": name, "problem": problem}
    return {"path": "", "name": "", "problem": UNKNOWN}


def unidentified_message(problem: str) -> str:
    """What the user can do when the open set could not be identified."""
    if problem == PERMISSION:
        return ("MARA is not allowed to read Live's document window. In System Settings → Privacy & Security, "
                "turn on MARA (or the Terminal you started it from) under Accessibility and under Automation → "
                "System Events, then try again.")
    if problem == SLOW:
        return ("macOS did not report Live's open document in time. If a dialog asks whether MARA may control "
                "System Events, allow it, then try again.")
    if problem == NOT_RUNNING:
        return "Ableton Live is not running. Open your Live set and try again."
    if problem == UNSAVED:
        return "Save the Live set first (File → Save Live Set), then try again."
    return ("The open Ableton project could not be identified. Save the Live set, keep only one Live set open "
            "and try again.")


def matches_project(state, project):
    path = state.get("file_path") or document_path()
    saved = project.get("live_path")
    if saved:
        return bool(path and os.path.realpath(path) == saved)
    from .songs import channel_names
    expected = set(project.get("channels", []))
    current = set(channel_names(state))
    name = state.get("name") or (_windows_document()[0] if sys.platform == "win32" else "")
    return bool(name == project.get("name") and expected and
                len(expected & current) / max(len(expected | current), 1) >= .9)
