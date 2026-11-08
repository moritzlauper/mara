"""Reads the numbers Live displays («-3.0 dB», «1.20 kHz», «50L»).

Same logic as `remote_script/MixMatch/commands.py`, which cannot import this
package because it runs inside Live; a test keeps the two in step.
"""
import math
import re


_NUMBER = re.compile(r"(-?inf|-?\d+(?:\.\d+)?)\s*(k)?", re.IGNORECASE)


def parse_display(text):
    """«-3.0 dB» → -3.0, «1.20 kHz» → 1200, «-inf dB» → -inf, «50L» → -50, «C» → 0."""
    text = str(text).strip()
    if text.upper() == "C":
        return 0.0
    pan = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([LR])", text, re.IGNORECASE)
    if pan:
        return float(pan.group(1)) * (-1 if pan.group(2).upper() == "L" else 1)
    m = _NUMBER.search(text)
    if not m:
        return None
    if m.group(1).lower().endswith("inf"):
        return -math.inf if m.group(1).startswith("-") else math.inf
    return float(m.group(1)) * (1000.0 if m.group(2) else 1.0)
