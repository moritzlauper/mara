"""Workspace preferences, separate from a Live project's mixing goals."""
import json
import os
from pathlib import Path


def read(root):
    root = Path(root).resolve()
    try:
        data = json.loads((root / "preferences.json").read_text())
    except (OSError, ValueError):
        data = {}
    return {"workspace": str(root), "reference_library": data.get("reference_library", str(root / "references"))}


def save(root, library):
    if not isinstance(library, str) or not library.strip():
        raise ValueError("Enter an existing reference library folder.")
    path = Path(library).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("The reference library folder does not exist.")
    root = Path(root)
    temporary = root / "preferences.json.tmp"
    temporary.write_text(json.dumps({"reference_library": str(path)}))
    os.replace(temporary, root / "preferences.json")
    return read(root)
