"""Decides the next mixing steps.

`ClaudePlanner` sends the measurements, the channel roles and the history of
earlier rounds to Claude and gets back a few small actions with reasons.
`RulePlanner` works without an API key: master tonal balance, loudness, bass in
mono and kick/bass masking, by fixed rules. Both return the same structure.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import numpy as np

from .roles import LOW_END_ROLES

from .analysis import TONAL_BANDS, band_label
from .bands import CENTRES

MODEL = os.environ.get("MIXMATCH_MODEL", "claude-haiku-5-5")
# Models that accept the server-side refusal fallback; Haiku 5.5 has none and rejects the parameter.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

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
                    "type": {"type": "string", "enum": ["volume", "pan", "eq", "width", "limiter", "clipper", "send", "param", "load_device"]},
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
- The limiter gain is capped at +5 dB (see "limiter"). After that, a clipper may add up to +4 dB (see "clipper"). Loudness beyond both is not a goal: never raise faders just to make the master louder.
- If the vocal channels are silent in this part, the vocal range 200 Hz–5 kHz only counts where the mix is louder than the target. Do not fill it with instruments.
- Look at the history: do not repeat a move that was undone; build on moves that helped. A move that made no measurable difference is undone too: only changes that help the mix stay, so when nothing clearly helps, stop (done) instead of trying small variations.
- Beat mode (see "mode"): the mix is an instrumental and the target comes from the references with their vocals removed. It must sound complete on its own: tame harshness, keep presence, never boost instruments into 1–5 kHz to imitate a vocal, and do not raise kick, sub or bass to copy the reference's low end.
- Cumulative EQ: before deepening a cut, look at what the band already does (device parameters). More than about -4 dB on one band of a synth or pad is rarely right; try another source instead.
- Several references: the target is their average and "largest_tonal_differences_db" already ignores differences within their spread, so only chase what is listed there.
- Respect the user's notes in the roles and never touch locked parameters.
- Parameters marked "(automated)" follow automation in the arrangement (filter sweeps, build-ups): they are part of the arrangement, not the mix. Never change them; work around them.
- "resonances" (on a channel) and "master_resonances" are narrow peaks found in a high-resolution recording, notes of the song's key already left out. A strong one (6 dB or more) on a pad, synth or bus is usually ringing: cut it narrowly at that exact frequency (q 6–12, -2 to -4 dB, shape bell). Leave weaker ones alone.
- "user_description" is how the user hears the track; it wins over the audio model and over your own guesses about the style.
- If the context has a "task" (channel pass), follow it: actions only on that one track, and none if it sits well.
- If the context has a "final_check", follow it: that is the last look at the master after every channel was done.
- Set "done" to true only when the remaining differences are small and nothing useful is left to try.
- Reasons: one short sentence each, concrete (which channel, which frequency, why).

Actions (unused fields null):
- volume: track, delta_db
- pan: track, value (-50 left … 50 right)
- send: track, name (the return's letter, e.g. "A"), delta_db (send level to that return; see the channel's "sends" and the returns among the channels)
- eq: track, freq_hz, gain_db, q (0.3–4 for tone, up to 12 for a listed resonance; null = default), shape (bell, low_shelf, high_shelf, low_cut, notch). Uses an EQ Eight on the track, added if missing. A band already near that frequency with the same shape is adjusted instead of adding one.
- width: track, width_pct (Utility width, 100 = unchanged), bass_mono (true/false)
- limiter: delta_db (Limiter gain on the master, ceiling fixed at -1 dB)
- clipper: delta_db (drive into a clipper placed right before the master limiter, at most +1.5 dB per round and +4 dB in total; see "clipper" for whether one is installed). Use it once the limiter is at its maximum while the master is still quieter and more dynamic (higher PLR) than the target: it shaves the transient peaks so the master gets louder without the limiter working harder.
- param: track, device_index, param (exact name), target (in the units Live displays, e.g. dB, ms, ratio)
- load_device: track, name (e.g. "Compressor", "Glue Compressor", or an installed plugin from "installed_plugins" such as "Pro-C 2" or "Pro-DS"; prefer a suitable installed plugin over Live's own device; a plugin that turns out to show no parameters is removed again, so do not retry it); configure it with param actions in later rounds
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

        self.client = anthropic.Anthropic(max_retries=6)  # a run takes long; ride out short network drops
        self.model = model
        self.effort = effort

    def plan(self, context: dict) -> dict:
        request = dict(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": ACTION_SCHEMA}},
            messages=[{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
        )
        if self.model in FALLBACK_MODELS:
            response = self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request)
        else:
            response = self.client.messages.create(**request)
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
            other = {m["role_a"], m["role_b"]} - {"kick"}
            if len(other) == 1 and other <= LOW_END_ROLES and "kick" in (m["role_a"], m["role_b"]):
                bass = m["track_b"] if m["role_a"] == "kick" else m["track_a"]
                actions.append({"type": "eq", "track": bass, "freq_hz": m["freq_hz"], "gain_db": -1.5, "q": 1.4,
                                "shape": "bell", "reason": f"bass masks the kick at {m['freq_hz']} Hz"})
        if not actions:
            notes.append("Nothing above the thresholds.")
        return {"assessment": " ".join(notes) or "Rule-based round.", "done": not actions, "actions": actions}


class ClaudeCodePlanner:
    """Plans through the local Claude Code (`claude -p`), on the user's subscription instead of an API key.

    The few, consequential master decisions go to `master_model` (Opus), the many channel-pass rounds to
    `channel_model` (Haiku). Claude Code runs without tools, MCP servers or the user's settings and CLAUDE.md,
    so a request carries only MixMatch's own prompt.
    """

    def __init__(self, master_model: str = "opus", channel_model: str = "haiku", binary: str = "claude",
                 timeout: float = 600, retries: int = 2):
        self.binary = shutil.which(binary) or binary
        self.master_model, self.channel_model = master_model, channel_model
        self.model = f"Claude Code: {master_model} for the master, {channel_model} for channels"
        self.timeout, self.retries = timeout, retries

    def command(self, context: dict) -> tuple[list[str], str]:
        model = self.channel_model if "task" in context else self.master_model
        cmd = [self.binary, "-p", json.dumps(context, ensure_ascii=False), "--model", model,
               "--output-format", "json", "--no-session-persistence", "--strict-mcp-config", "--tools", "",
               "--setting-sources", "", "--exclude-dynamic-system-prompt-sections",
               "--system-prompt", SYSTEM, "--json-schema", json.dumps(ACTION_SCHEMA)]
        return cmd, model

    def plan(self, context: dict) -> dict:
        cmd, model = self.command(context)
        # Without these, Claude Code would bill an API key instead of the subscription.
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        error = ""
        for _ in range(self.retries + 1):
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, env=env,
                                  cwd=tempfile.gettempdir())
            try:
                out = json.loads(proc.stdout)
            except json.JSONDecodeError:
                error = (proc.stderr or proc.stdout).strip()[:300] or f"exit code {proc.returncode}"
                continue
            if out.get("is_error"):
                error = str(out.get("result"))[:300]
                if "log" in error.lower() and "in" in error.lower():   # «Not logged in»: retrying will not help
                    break
                continue
            plan = out.get("structured_output")
            if plan is None and out.get("result"):
                plan = json.loads(out["result"])
            if plan is not None:
                return plan
            error = "no structured output"
        raise RuntimeError(f"claude -p ({model}): {error}")


LIMIT_WORDS = ("limit", "quota", "usage", "rate")


class FallbackPlanner:
    """Claude Code first; when it reports a usage limit, the API (Claude Haiku 5.5) for the rest of the run."""

    def __init__(self, primary, secondary, log=print):
        self.primary, self.secondary, self.log = primary, secondary, log
        self.using = primary
        self.model = f"{primary.model}; API ({secondary.model}) if the subscription hits a limit"

    def plan(self, context: dict) -> dict:
        if self.using is self.primary:
            try:
                return self.primary.plan(context)
            except RuntimeError as e:
                if not any(w in str(e).lower() for w in LIMIT_WORDS):
                    raise
                self.log(f"  ⚠ Claude Code: {str(e)[:120]}; continuing with the API ({self.secondary.model})")
                self.using = self.secondary
        return self.secondary.plan(context)


def make_planner(kind: str = "auto"):
    """auto: the local Claude Code if installed (subscription), falling back to the API with ANTHROPIC_API_KEY
    (Haiku) at a usage limit; else the API; else rules."""
    if kind == "rules":
        return RulePlanner()
    if kind in ("auto", "claude-code") and shutil.which("claude"):
        if kind == "auto" and os.environ.get("ANTHROPIC_API_KEY"):
            return FallbackPlanner(ClaudeCodePlanner(), ClaudePlanner())
        return ClaudeCodePlanner()
    if kind == "claude-code":
        raise SystemExit("Claude Code (the claude command) is not installed or not on the PATH.")
    if kind in ("auto", "api") and os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudePlanner()
    if kind == "api":
        raise SystemExit("--planner api needs ANTHROPIC_API_KEY.")
    return RulePlanner()
