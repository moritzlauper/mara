"""Decides the next mixing steps.

`ClaudePlanner` sends the measurements, the channel roles and the history of
earlier rounds to Claude and gets back a few small actions with reasons.
`RulePlanner` works without an API key: master tonal balance, loudness, bass in
mono and time-aligned masking, by fixed rules. Both return the same structure.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile

import numpy as np

from .analysis import TONAL_BANDS, band_label
from .bands import CENTRES
from .display import parse_display

MODEL = os.environ.get("MIXMATCH_MODEL", "claude-haiku-5-5")
# Models that accept the server-side refusal fallback; Haiku 5.5 has none and rejects the parameter.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {"type": "string"},
        "mix_intent": {
            "type": "object",
            "properties": {
                "priority": {"type": "string", "enum": ["masking", "tonal", "dynamics", "width", "loudness", "low_side", "clipping", "none"]},
                "evidence": {"type": "string"},
                "preserve": {"type": "string"},
                "reference_relevance": {"type": "string"},
            },
            "required": ["priority", "evidence", "preserve", "reference_relevance"],
            "additionalProperties": False,
        },
        "done": {"type": "boolean"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["volume", "pan", "eq", "width", "limiter", "clipper", "send", "param", "load_device", "duck"]},
                    "track": {"type": ["string", "null"]},
                    "source_track": {"type": ["string", "null"]},
                    "reduction_db": {"type": ["number", "null"]},
                    "release_ms": {"type": ["number", "null"]},
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
                "required": ["type", "track", "source_track", "reduction_db", "release_ms", "delta_db", "value", "freq_hz", "gain_db", "q", "shape", "width_pct",
                             "bass_mono", "device_index", "param", "target", "name", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assessment", "mix_intent", "done", "actions"],
    "additionalProperties": False,
}

SYSTEM = """You are the decision-making part of MixMatch, a mixing agent running inside an Ableton Live project.
You cannot hear. Software measures every channel, the master and the reference tracks while the song plays and gives you numbers. You diagnose what this mix needs, propose small steps, and the software checks the intended benefit with fresh measurements. Reference distance is a guide, not a perceptual quality score.

Goal: improve this track on its own musical terms, using the references as an important direction for tonal balance, impact, loudness and space. Preserve its arrangement, timbres, groove, useful dynamics and foreground/background hierarchy. Decide which reference differences are relevant problems and which belong to this track. A lower reference distance alone does not prove a better mix.

How to work:
- First read mix_diagnosis: examine the whole spectrum and its strongest source channels, their roles, the arrangement, user description, and existing processing. Explain the main need, likely cause, evidence and uncertainty before choosing an intervention. Broad spectral level is not proof of harshness or masking.
- Fill mix_intent: choose one measurable priority for this round, cite concrete evidence, say what character must be preserved, and explain why the reference applies (or why a difference should remain). Pick none and no actions when evidence is insufficient or the mix already works. Do not claim to hear what numbers cannot establish.
- References are orientation, never a curve to copy. Different instrumentation, register, sparse arrangements and deliberate warmth/brightness can explain deviations. Treat roughly 1.5 dB tonal, 2 dB width and 1.5 dB PLR differences as small unless independent evidence or user intent makes them relevant. More treble in the reference does not automatically justify a shelf: check whether a source lacks presence or competing mids obscure it. Never EQ a single instrument to resemble the full reference spectrum.
- Verification requires the chosen priority to improve. A modest reference-distance increase may be accepted for less masking, safer peaks or cleaner low end, provided spectrum, layers and punch remain protected. No-op changes are undone. Reference moves must improve the combined reference dimensions reliably while respecting independent mix-health limits. Small masking fluctuations within measured variation do not veto a reference improvement. Do not promise arbitrary timbral improvements can be verified by these meters.
- Reference convergence is an explicit goal alongside mix health. Prioritize persistent, meaningful tonal and balance mismatches supported by the source spectrum. Do not spend every round on masking just because its summed score is numerically larger: the dimensions have different scales. Name a concrete remaining problem, test a useful correction, then verify it. Preservation is not a blanket reason to leave measured excess unchanged.
- Measurement spread describes repeated passes of the same passage. Improvements must exceed that spread. A single failed small EQ test does not prove the source is fine: consider a different source, level balance or a broader correction within the action limits; do not repeat the same rejected move.
- Fix big problems before details, roughly in this order: gain staging and balance, low-end separation (kick/bass), masking, tonal balance of individual channels, width, compression, master loudness last.
- Masking entries are potential collisions verified in simultaneous 100 ms frames, not proof that a musical layer is wrong. Protect the listed foreground and make space on the background. Do not mute layers or erase intentional unison/doubling. The bands list shows distinct regions; overlap_fraction is the share of the window with both channels active.
- For repeated intermittent collisions (intermittent=true), especially kick/bass, prefer a gentle duck on the background triggered by the foreground if sidechain_ducking=true. It lowers the background only during trigger activity and release. Start with reduction_db=2 and release_ms=120. Do not stack ducking on an existing sidechain compressor, change existing routing, or use a disabled compressor. Positive observed_ducking_db means the background already gets quieter during foreground activity: check remaining severity before doing more.
- For sustained collisions, use a small bell cut on the lower-priority background at a listed frequency (usually -1 to -1.5 dB). Keep cumulative masking cuts near -4 dB at most. Panning may help the stereo mix but is not evidence that mono masking is solved. Never claim to have configured dynamic EQ: duck is bounded broadband compression, EQ is static.
- Prefer cutting over boosting. Prefer fixing a channel over EQ on the master. Use the master EQ only for small overall tonal moves the channels cannot explain.
- Small steps: volume at most ±2 dB, EQ at most ±3 dB per round, limiter at most +2 dB. Give 1 to 4 actions per round.
- The limiter gain is capped at +5 dB (see "limiter"). After that, a clipper may add up to +4 dB (see "clipper"). Loudness beyond both is not a goal: never raise faders just to make the master louder.
- In song mode, silent vocal passages use a vocal-range allowance: do not fill that space just to imitate the reference voice. This does not apply to explicit instrumental mode.
- Look at the history and verification reasons: do not repeat a move that was undone; build on moves that helped. A move that made no measurable difference is undone too. When nothing clearly helps, stop (done), even if reference differences remain.
- Beat mode (see "mode"): the mix is an instrumental and the target is the full reference songs; in 200 Hz–5 kHz the beat may sit up to 4 dB under them (room for a vocal, already in the tonal numbers). It must sound complete on its own: tame harshness, keep presence and air, never boost instruments into the vocal range to imitate a vocal.
- Instrumental mode (see "mode"): this is the finished arrangement, with no future vocals planned. Do not reserve a vocal pocket. Use the full reference balance as direction and let instruments carry the foreground; differences caused by absent reference vocals are not an instruction to boost or crowd the mids.
- Cumulative EQ: before deepening a cut, look at what the band already does (device parameters). More than about -4 dB on one band of a synth or pad is rarely right; try another source instead.
- A device that is switched off (Device On 0) was switched off on purpose: do not use it, and do not add another device of the same kind on that track.
- Master EQ: all its bands together may cut at most 3 dB at any frequency (the executor refuses more). A broad excess on the master comes from channels: cut the channels that cause it, not the master.
- Several references: their average and spread describe a direction, not an obligation. "largest_tonal_differences_db" ignores differences within their spread; still explain why any remaining difference needs a change in this arrangement.
- Respect the user's notes in the roles and never touch locked parameters.
- Parameters marked "(automated)" follow automation in the arrangement (filter sweeps, build-ups): they are part of the arrangement, not the mix. Never change them; work around them.
- "resonances" (on a channel) and "master_resonances" are narrow peaks found in a high-resolution recording, notes of the song's key already left out. A strong one (6 dB or more) on a pad, synth or bus is usually ringing: cut it narrowly at that exact frequency (q 6–12, -2 to -4 dB, shape bell). Leave weaker ones alone.
- "user_description" is how the user hears the track; it wins over the audio model and over your own guesses about the style.
- If the context has a "task" (channel pass), follow it: actions only on that one track, and none if it sits well.
- If the context has a "final_check", follow it: that is the last look at the master after every channel was done.
- Set "done" to true when no further useful, evidenced intervention remains. Meaningful differences from the reference may remain; explain which ones you preserve and why.
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
- duck: track (background), source_track (foreground from a measured intermittent masking pair), reduction_db (0.5–3 dB maximum attenuation), release_ms (50–250). Creates a dedicated native Compressor with Post FX external sidechain, RMS detection, no makeup, and a dry/wet bound. It is removed if the round fails. Requires sidechain_ducking=true. Existing disabled or sidechain compressors are left alone.
Track keys: "t0", "t1", … normal tracks, "r0" … returns, "m" master."""


def device_summary(track: dict) -> list[dict]:
    """Only what helps a decision: device names and their (non-default looking) parameters, compact."""
    out = []
    for d in track.get("devices", []):
        params = [f"{p['name']}={p['display']}" + (" (automated)" if p.get("automated") else "")
                  for p in d["params"] if not p["name"].startswith("Device On")][:40]
        item = {"index": d["index"], "name": d["name"], "params": params,
                "enabled": next((p["value"] >= 0.5 for p in d["params"] if p["name"] == "Device On"), True)}
        if d.get("class_name") == "Eq8":
            values = {p["name"]: p for p in d["params"]}
            item["eq_bands"] = []
            for n in range(1, 9):
                if values.get(f"{n} Filter On A", {}).get("value", 0) < 0.5:
                    continue
                get = lambda name, default: parse_display(values.get(f"{n} {name} A", {}).get("display", default))
                item["eq_bands"].append({"on": True, "freq": get("Frequency", "1000"),
                                         "gain": get("Gain", "0"),
                                         "q": get("Q", values.get(f"{n} Resonance A", {}).get("display", "0.71")),
                                         "kind": values.get(f"{n} Filter Type A", {}).get("display", "Bell")})
        out.append(item)
    return out


def masking_action(context: dict) -> dict | None:
    """A conservative fallback for any measured pair, including the focused channel.

    No stereo pan shortcut: the tap's mono spectra cannot verify that improvement.
    Existing EQ response, disabled devices and rejected moves all limit the recipe.
    """
    from .actions import eq_curve

    if context.get("final_check"):
        return None
    channels = {c["track"]: c for c in context.get("channels", [])}
    for m in context.get("masking", []):
        key, source = m.get("background"), m.get("foreground")
        if not key or not source or m["severity"] < 0.08:
            continue
        if context.get("focus_track") not in (None, key):
            continue
        channel = channels.get(key, {})
        if channel.get("kind") in ("group", "return"):
            continue
        devices = channel.get("devices", [])
        blocked_duck = any(d["name"].startswith("MARA Duck") or
                           ("compressor" in d["name"].lower() and
                            (not d.get("enabled", True) or any(p.startswith("S/C On=On") for p in d["params"])))
                           for d in devices)
        rejected = [a for h in context.get("history", []) if h.get("undone")
                    for a in h.get("actions", []) if a.get("track") == key]
        rejected += [a for h in context.get("history", []) for a in h.get("skipped", []) if a.get("track") == key]
        if (context.get("sidechain_ducking") and m.get("intermittent") and not blocked_duck
                and (m.get("observed_ducking_db") or 0) < 3
                and not any(a.get("type") == "duck" and a.get("source_track") == source for a in rejected)):
            return {"type": "duck", "track": key, "source_track": source, "reduction_db": 2.0,
                    "release_ms": 120.0, "reason": f"make room for {source} only during its repeated collisions"}
        freq = m["freq_hz"]
        if any(a.get("type") == "eq" and a.get("freq_hz") and abs(math.log2(a["freq_hz"] / freq)) < 0.4
               for a in rejected):
            continue
        if any(d["name"] == "EQ Eight" and not d.get("enabled", True) for d in devices):
            continue
        bands = [b for d in devices if d.get("enabled", True) for b in d.get("eq_bands", [])]
        current = float(eq_curve(bands, [freq])[0])
        cut = max(-1.5, -4.0 - current)
        if cut > -0.5:
            continue
        return {"type": "eq", "track": key, "freq_hz": freq, "gain_db": round(cut, 2), "q": 1.4,
                "shape": "bell", "reason": f"make room for {source} at {freq} Hz during measured collisions"}
    return None


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
    """Deterministic fallback: master balance, mono bass, and temporal masking corrections."""

    def plan(self, context: dict) -> dict:
        actions, notes = [], []
        cmp = context.get("master_vs_reference")
        if cmp and context.get("focus_track") is None:
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
        unmask = masking_action(context)
        if unmask:
            actions.append(unmask)
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
    from . import planner_settings as settings
    config = settings.read()
    provider = config['provider'] if kind == 'auto' else {'api': 'anthropic'}.get(kind, kind)
    profile = config['profiles'].get(provider, {})
    model = profile.get('model') or os.environ.get('MIXMATCH_MODEL') or settings.HAIKU
    if provider == 'rules':
        return RulePlanner()
    if provider in ('gemini', 'compatible'):
        profile = settings.validated(provider, profile.get('model', ''), profile.get('base_url', ''))
        token = settings.key(provider, profile['base_url'])
        if provider == 'gemini' and not token:
            raise RuntimeError('Google Gemini needs an API key. Add it in Planner settings.')
        return CompatiblePlanner(profile['model'], profile['base_url'], token, settings.PROVIDERS[provider])
    if provider in ('auto', 'claude-code') and shutil.which('claude'):
        code_model = config['profiles'].get('claude-code', {}).get('model') or settings.HAIKU
        primary = ClaudeCodePlanner(master_model=code_model, channel_model=code_model)
        if provider == 'auto' and settings.key('anthropic'):
            os.environ['ANTHROPIC_API_KEY'] = settings.key('anthropic')
            return FallbackPlanner(primary, ClaudePlanner(model=config['profiles']['anthropic']['model']))
        return primary
    if provider == 'claude-code':
        raise RuntimeError('Claude Code is not installed or not on the PATH.')
    if provider in ('auto', 'anthropic') and settings.key('anthropic'):
        os.environ['ANTHROPIC_API_KEY'] = settings.key('anthropic')
        return ClaudePlanner(model=config['profiles']['anthropic'].get('model') or model)
    if provider == 'anthropic':
        raise RuntimeError('Anthropic needs an API key. Add it in Planner settings.')
    return RulePlanner()


class CompatiblePlanner:
    """Chat Completions providers share MARA's prompt and validated action contract."""
    def __init__(self, model, base_url, api_key='', provider='Compatible API'):
        import httpx
        self.model, self.base_url, self.provider = model, base_url.rstrip('/'), provider
        self.client = httpx.Client(timeout=180, headers={'Authorization': 'Bearer ' + api_key} if api_key else {})

    def plan(self, context):
        import httpx
        from jsonschema import validate, ValidationError
        request = {'model': self.model, 'response_format': {'type': 'json_object'},
                   'messages': [{'role': 'system', 'content': SYSTEM + '\nReturn only a JSON object matching this schema: ' + json.dumps(ACTION_SCHEMA)},
                                {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]}
        try:
            response = self.client.post(self.base_url + '/chat/completions', json=request)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f'{self.provider} request failed (HTTP {e.response.status_code}). Check the model, key and provider limits.') from None
        except httpx.RequestError:
            raise RuntimeError(f'{self.provider} could not be reached. Check the API URL and connection.') from None
        try:
            choice = response.json()['choices'][0]
            if choice.get('finish_reason') not in (None, 'stop'):
                raise ValueError('incomplete response')
            plan = json.loads(choice['message']['content'], parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            validate(plan, ACTION_SCHEMA)
            return plan
        except (ValueError, KeyError, IndexError, TypeError, ValidationError):
            raise RuntimeError(f'{self.provider} returned an incomplete or invalid mixing plan. No actions from this response were applied.') from None
