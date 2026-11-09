"""Decides the next mixing steps.

`ClaudePlanner` sends the measurements, the channel roles and the history of
earlier rounds to Claude and gets back a few small actions with reasons.
`RulePlanner` works without an API key: master tonal balance, loudness, bass in
mono and kick/bass masking, by fixed rules. Both return the same structure.
"""
from __future__ import annotations

import json
import os

import numpy as np

from .analysis import TONAL_BANDS, band_label
from .bands import CENTRES

MODEL = os.environ.get("MIXMATCH_MODEL", "claude-opus-5-5")

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {"type": "string"},
        "done": {"type": "boolean"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["volume", "pan", "eq", "width", "limiter", "param", "load_device"]},
                    "track": {"type": ["string", "null"]},
                    "delta_db": {"type": ["number", "null"]},
                    "value": {"type": ["number", "null"]},
                    "freq_hz": {"type": ["number", "null"]},
                    "gain_db": {"type": ["number", "null"]},
                    "q": {"type": ["number", "null"]},
                    "shape": {"anyOf": [{"type": "string", "enum": ["bell", "low_shelf", "high_shelf", "low_cut", "notch"]},
                                        {"type": "null"}]},
                    "width_pct": {"type": ["number", "null"]},
                    "bass_mono": {"type": ["boolean", "null"]},
                    "device_index": {"type": ["integer", "null"]},
                    "param": {"type": ["string", "null"]},
                    "target": {"type": ["number", "null"]},
                    "name": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["type", "track", "delta_db", "value", "freq_hz", "gain_db", "q", "shape", "width_pct",
                             "bass_mono", "device_index", "param", "target", "name", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assessment", "done", "actions"],
    "additionalProperties": False,
}

SYSTEM = """You are the decision-making part of MixMatch, a mixing agent running inside an Ableton Live project.
You cannot hear. Software measures every channel, the master and the reference tracks while the song plays and gives you numbers. You decide the next small steps; the software applies them, measures again and undoes any round that made the overall distance to the target worse.

Goal: the master should match the reference tracks in loudness, tonal balance, dynamics and stereo image, and the mix should be well balanced: every channel audible according to its role and priority, no channels masking each other (kick and bass especially), low end mono, nothing clipping.

How to work:
- Fix big problems before details, roughly in this order: gain staging and balance, low-end separation (kick/bass), masking, tonal balance of individual channels, width, compression, master loudness last.
- Prefer cutting over boosting. Prefer fixing a channel over EQ on the master. Use the master EQ only for small overall tonal moves the channels cannot explain.
- Small steps: volume at most ±2 dB, EQ at most ±3 dB per round, limiter at most +2 dB. Give 1 to 4 actions per round.
- The limiter gain is capped at +6 dB (see "limiter"). Loudness beyond that is not a goal: never raise faders just to make the master louder.
- If the vocal channels are silent in this part, the vocal range 200 Hz–5 kHz only counts where the mix is louder than the target. Do not fill it with instruments.
- Look at the history: do not repeat a move that was undone; build on moves that helped.
- Beat mode (see "mode"): the mix is an instrumental and the target comes from the references with their vocals removed. A beat that will get vocals later must leave room in the vocal range; never boost instruments into 1–5 kHz to imitate a vocal.
- Several references: the target is their average and "largest_tonal_differences_db" already ignores differences within their spread, so only chase what is listed there.
- Respect the user's notes in the roles and never touch locked parameters.
- Parameters marked "(automated)" follow automation in the arrangement (filter sweeps, build-ups): they are part of the arrangement, not the mix. Never change them; work around them.
- Set "done" to true only when the remaining differences are small and nothing useful is left to try.
- Reasons: one short sentence each, concrete (which channel, which frequency, why).

Actions (unused fields null):
- volume: track, delta_db
- pan: track, value (-50 left … 50 right)
- eq: track, freq_hz, gain_db, q (0.3–4, null = default), shape (bell, low_shelf, high_shelf, low_cut, notch). Uses an EQ Eight on the track, added if missing. A band already near that frequency with the same shape is adjusted instead of adding one.
- width: track, width_pct (Utility width, 100 = unchanged), bass_mono (true/false)
- limiter: delta_db (Limiter gain on the master, ceiling fixed at -1 dB)
- param: track, device_index, param (exact name), target (in the units Live displays, e.g. dB, ms, ratio)
- load_device: track, name (e.g. "Compressor", "Glue Compressor"); configure it with param actions in later rounds
Track keys: "t0", "t1", … normal tracks, "r0" … returns, "m" master."""


def device_summary(track: dict) -> list[dict]:
    """Only what helps a decision: device names and their (non-default looking) parameters, compact."""
    out = []
    for d in track.get("devices", []):
        params = [f"{p['name']}={p['display']}" + (" (automated)" if p.get("automated") else "")
                  for p in d["params"] if not p["name"].startswith("Device On")][:40]
        out.append({"index": d["index"], "name": d["name"], "params": params})
    return out


class ClaudePlanner:
    def __init__(self, model: str = MODEL, effort: str = "medium"):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort

    def plan(self, context: dict) -> dict:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": ACTION_SCHEMA}},
            messages=[{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
        )
        if response.stop_reason == "refusal":
            return {"assessment": "Claude declined this round.", "done": False, "actions": []}
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)


class RulePlanner:
    """Deterministic fallback: master tonal balance, loudness, mono bass, kick/bass masking."""

    def plan(self, context: dict) -> dict:
        actions, notes = [], []
        cmp = context.get("master_vs_reference")
        if cmp:
            diff = np.array(cmp["_tonal_excess"])
            worst = sorted(TONAL_BANDS, key=lambda i: -abs(diff[i]))[:2]
            for i in worst:
                if abs(diff[i]) > 1.0:
                    gain = float(np.clip(-diff[i] * 0.5, -1.5, 1.5))
                    actions.append({"type": "eq", "track": "m", "freq_hz": round(CENTRES[i]), "gain_db": round(gain, 1),
                                    "q": 1.0, "shape": "bell",
                                    "reason": f"master {diff[i]:+.1f} dB vs reference at {band_label(i)} Hz"})
            if cmp["low_side_diff_db"] > 3:
                actions.append({"type": "width", "track": "m", "bass_mono": True,
                                "reason": "more stereo bass than the reference"})
            loud = cmp["loudness_diff_lu"]
            if abs(loud) > 0.5:
                actions.append({"type": "limiter", "delta_db": float(np.clip(-loud, -2, 2)),
                                "reason": f"master {loud:+.1f} LU vs reference"})
        for m in context.get("masking", [])[:1]:
            if {m["role_a"], m["role_b"]} == {"kick", "bass"} or {m["role_a"], m["role_b"]} == {"kick", "sub"}:
                bass = m["track_b"] if m["role_a"] == "kick" else m["track_a"]
                actions.append({"type": "eq", "track": bass, "freq_hz": m["freq_hz"], "gain_db": -1.5, "q": 1.4,
                                "shape": "bell", "reason": f"bass masks the kick at {m['freq_hz']} Hz"})
        if not actions:
            notes.append("Nothing above the thresholds.")
        return {"assessment": " ".join(notes) or "Rule-based round.", "done": not actions, "actions": actions}


def make_planner(prefer_claude: bool = True):
    if prefer_claude and os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudePlanner()
    return RulePlanner()
