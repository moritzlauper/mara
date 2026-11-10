"""Read the open Live document when the control surface cannot expose its path."""
from __future__ import annotations

import os
import subprocess
import sys
from urllib.parse import unquote, urlsplit


def document_path() -> str:
    """Return the unambiguous macOS AXDocument path, including after Save As."""
    if sys.platform != "darwin":
        return ""
    script = '''tell application "System Events"
if not (exists process "Live") then return ""
tell process "Live" to set liveDocumentURLs to value of attribute "AXDocument" of windows
set AppleScript's text item delimiters to linefeed
return liveDocumentURLs as text
end tell'''
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode:
        return ""
    paths = set()
    for line in result.stdout.splitlines():
        uri = urlsplit(line.strip())
        if uri.scheme == "file" and uri.netloc in ("", "localhost"):
            path = unquote(uri.path)
            if path.lower().endswith(".als") and os.path.isabs(path):
                paths.add(os.path.realpath(path))
    return paths.pop() if len(paths) == 1 else ""


def matches_project(state, project):
    path = state.get("file_path") or document_path()
    saved = project.get("live_path")
    if saved:
        return bool(path and os.path.realpath(path) == saved)
    from .songs import channel_names
    expected = set(project.get("channels", []))
    current = set(channel_names(state))
    return bool(state.get("name") == project.get("name") and expected and
                len(expected & current) / max(len(expected | current), 1) >= .9)
