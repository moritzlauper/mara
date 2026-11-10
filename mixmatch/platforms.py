"""OS boundaries shared by the browser UI and packaged desktop engine."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path


def engine_command(args):
    return [sys.executable, *args] if getattr(sys, "frozen", False) else [sys.executable, "-m", "mixmatch.cli", *args]


def process_identity(pid):
    import psutil
    try:
        proc = psutil.Process(pid)
        return json.dumps([proc.create_time(), proc.cmdline()], separators=(",", ":")) if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE else ""
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def run_alive():
    import psutil
    for proc in psutil.process_iter(["cmdline", "status"]):
        args = proc.info["cmdline"] or []
        if "run" in args and any("mixmatch" in a or "mara-engine" in a for a in args) and proc.info["status"] != psutil.STATUS_ZOMBIE:
            return True
    return False


def workspace():
    if os.environ.get("MARA_WORKSPACE"):
        return Path(os.environ["MARA_WORKSPACE"]).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/MARA"
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "MARA"


def resources():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def engine_build():
    try:
        return (resources() / "engine-build.txt").read_text().strip()
    except OSError:
        return "development"


def library_default():
    return Path.home() / ("Music" if sys.platform == "darwin" else "Documents") / "Ableton/User Library"


def install_bridge(library):
    import shutil
    destination = Path(library).expanduser().resolve()
    if not destination.is_dir():
        raise ValueError("Choose your existing Ableton User Library folder.")
    root = resources()
    script = destination / "Remote Scripts/MixMatch"
    device = destination / "MARA"
    for source, target in [(root / "remote_script/MixMatch", script), (root / "device", device)]:
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.suffix in {".py", ".amxd", ".js"}:
                old = target / item.name
                if old.exists():
                    shutil.copy2(old, old.with_suffix(old.suffix + ".previous"))
                shutil.copy2(item, old)
    return {"library": str(destination), "message": "Installed. Restart Live and select MixMatch in Settings → Link/Tempo/MIDI → Control Surface. Then choose Set taps."}
