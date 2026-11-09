"""Settings of the project that MixMatch keeps between runs (mixmatch.json next to roles.json)."""
from __future__ import annotations

import json
import os

PATH = "mixmatch.json"


def load(path: str = PATH) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(data: dict, path: str = PATH) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def description(path: str = PATH) -> str:
    """The user's own words for the track, e.g. «Hyperpop, happy and bright»; wins over the audio model."""
    return str(load(path).get("description", "")).strip()
