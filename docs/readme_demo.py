"""Serve the real MARA UI with fictional, deterministic README data.

Run: python3 docs/readme_demo.py
Open: http://127.0.0.1:8876/workflow?demo=project
Scenes: project, references, analysis, mix, result.
No Live connection, model requests, credentials or user projects are accessed.
"""
from __future__ import annotations

import copy
import io
import json
import mimetypes
import wave
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STATIC = Path(__file__).resolve().parents[1] / "mixmatch" / "static"
SCENES = {"project", "references", "analysis", "mix", "result"}
ROLES = {
    "Kick": {"role": "kick", "priority": 5, "evidence": "Played drum pads"},
    "Sub Bass": {"role": "sub", "priority": 4, "evidence": "Instrument and MIDI notes"},
    "Snare": {"role": "snare", "priority": 4, "evidence": "Track name and drum pads"},
    "Rhodes": {"role": "piano", "priority": 3, "source": "user"},
    "Atmosphere": {"role": "pad", "priority": 2, "evidence": "Instrument and sustained chords"},
}
PLANNER = {
    "provider": "rules", "profiles": {}, "configured": {}, "suggested_models": {},
    "providers": {"auto": "Automatic", "claude-code": "Claude Code", "anthropic": "Anthropic",
                  "gemini": "Google Gemini", "compatible": "OpenAI-compatible API", "rules": "Built-in rules"},
}
PREVIEW = {"id": "demo-before", "start_bar": 49, "bars": 16, "seconds": 64 * 60 / 174}
CHANGES = [
    {"channel": "Sub Bass", "track": "t1", "device": "MARA Duck Kick#1", "param": "Device", "at": 1794319200,
     "kind": "added", "before": "Not present", "after": "Present",
     "settings": [{"param": "Attack", "value": "2.00 ms"}, {"param": "Release", "value": "120 ms"},
                  {"param": "Dry/Wet", "value": "20.6 %"}],
     "reason": "Give the kick room only when kick and sub play together; retain the sustained bass weight.",
     "verification": "Potential masking decreased on the same passage; dynamics and active layers preserved.",
     "section": "Sub Bass · bars 49–65"},
    {"channel": "Rhodes", "track": "t3", "device": "EQ Eight#1", "param": "3 Gain A", "kind": "parameter", "at": 1794319320,
     "before": "0.00 dB", "after": "−1.50 dB", "reason": "A gentle 315 Hz cut makes space for the snare while preserving Rhodes warmth.",
     "verification": "Measured overlap reduced; tonal change stayed within the passage baseline.",
     "section": "Rhodes · bars 49–65"},
    {"channel": "Main", "track": "m", "device": "Limiter#1", "param": "Gain", "kind": "parameter", "at": 1794319500,
     "before": "0.00 dB", "after": "+1.00 dB", "reason": "Bring the finished balance slightly closer to the references.",
     "verification": "Loudness distance reduced; peak ceiling and dynamics checks passed.",
     "section": "Final check · bars 49–65"},
]


def state(scene):
    active = scene in {"mix", "analysis"}
    mixing = scene == "mix"
    measured = scene in {"mix", "result"}
    prepared = scene in {"analysis", "mix", "result"}
    finished = scene == "result"
    components = [{"tonal": t, "width": w, "loudness": 0, "dynamics": 0,
                   "low_side": 0, "clipping": 0, "masking": m}
                  for t, w, m in [(2.4, .2, 2.1), (2.1, .2, 1.7), (1.7, .18, 1.3),
                                   (1.4, .15, .9), (1.2, .14, .65), (1.1, .12, .45)]]
    parts = copy.deepcopy(components[-1])
    if finished:
        parts.update(tonal=.8, loudness=.1, dynamics=.3)
    return {
        "project_id": "night-drive-demo", "track": "Night Drive · Demo", "following_live": True,
        "open_set": {"name": "Night Drive · Demo", "path": "Demo project · illustrative data · no Live connection", "taps": 6, "total": 6},
        "tracks": [], "active": active, "running": active,
        "project": {"description": "Liquid drum & bass. Keep the Rhodes warm and the drums punchy. Give the kick and sub more space; leave room for a future vocal.", "mode": "beat"},
        "workflow": {"id": "readme-demo", "status": "finished" if finished else "reviewing" if scene == "analysis" else "mixing" if active else "idle",
                     "started_at": time.time()-72, "analysis_complete": prepared,
                     "review": {"deadline": time.time()+24, "held": False} if scene == "analysis" else None,
                     "stage": scene, "live_goal_updates": True},
        "phase": "final check" if finished else "channel Sub Bass" if mixing else "analysis",
        "activity": {"text": "Analysis complete · review instrument roles" if scene == "analysis" else "Checking kick / sub separation · same 16-bar passage" if mixing else "Demo project · illustrative data"},
        "checkpoint": {"phase": "references" if scene == "analysis" else "channels"} if prepared and not finished else None,
        "progress": {"step": "Sub Bass"}, "display_progress": {"percent": 100 if finished else 58 if mixing else 0, "eta_minutes": 4 if mixing else None},
        "output": {"silent": True}, "roles_revision": 1,
        "role_groups": {"Drums": ["kick", "snare"], "Low end": ["sub", "bass"], "Instruments": ["piano", "keys", "pad", "lead_synth"]},
        "reference_files": ["01 — Glass Skyline (demo reference).wav", "02 — Late Train (demo reference).wav"],
        "project_references": [], "reference_candidates": [{"id": "demo-extra", "name": "03 — Blue Hour (demo reference).wav"}],
        "to_get": [], "song": "Liquid drum & bass · 174 BPM · F minor · 96 bars. Focus passage: bars 49–65.",
        "previews": {"before": PREVIEW if prepared else None, "after": {**PREVIEW, "id": "demo-after"} if finished else None},
        "loudness_match": {"ready": finished, "version": "demo", "target_lufs": -12.1},
        "parts": parts if measured else {}, "target_lufs": -10.5,
        "explain": {"loudness": "0.6 LU quieter than the references — remaining gap accepted",
                    "dynamics": "1.0 dB more dynamic (PLR) than the references",
                    "width": "0.6 dB wider than the references",
                    "low_side": "Low-end side energy below the reference target",
                    "clipping": "Measured peaks at −1.0 dBFS",
                    "tonal": "+0.8 dB at 315 Hz; warm Rhodes character retained",
                    "masking": "Kick ↔ Sub Bass at 63 Hz · reduced, with 36% simultaneous activity"} if measured else {},
        "measurement_groups": [{"run": "demo", "phase": "channels", "scoring": "balance",
                                "section": "Channel balance · bars 49–65", "values": [round(sum(p.values()), 2) for p in components],
                                "components": components, "verification": "Retained changes reduced the measured conflict on this passage."}] if measured else [],
        "review": {"ready": finished or mixing, "partial": mixing, "channels": 3 if finished else 2,
                   "kept": CHANGES if finished else CHANGES[:2] if mixing else [],
                   "rejected": [{"section": "Master high shelf", "verification": "No measured improvement in the intended tonal balance; original restored.",
                                 "parameters": [{"track": "Main", "device": "EQ Eight", "param": "High shelf", "old_display": "0.0 dB", "new_display": "+1.0 dB"}]}] if finished else []},
        "events": [{"kind": "kept", "text": "Kept · gentle kick-triggered ducking on Sub Bass; measured conflict reduced."},
                   {"kind": "undone", "text": "Reverted · master high shelf brought no verified tonal benefit."}],
        "log": ["Demo data for documentation. No audio quality claim or real session result."],
    }


class Handler(BaseHTTPRequestHandler):
    scene = "project"

    def send(self, body, content_type="application/json", code=200):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path in {"/", "/workflow"}:
            scene = parse_qs(url.query).get("demo", ["project"])[0]
            Handler.scene = scene if scene in SCENES else "project"
            html = (STATIC / "app.html").read_text().replace("<body>", f'<body data-demo-stage="{Handler.scene}">')
            html = html.replace('<span id="connection"', '<span class="badge">DEMO DATA</span><span id="connection"', 1)
            if Handler.scene == "analysis":
                html = html.replace('id="roles-details"', 'id="roles-details" open', 1)
            return self.send(html.encode(), "text/html; charset=utf-8")
        if url.path == "/api/state":
            return self.send(state(Handler.scene))
        if url.path == "/api/setup":
            return self.send({"library": "~/Music/Ableton/User Library", "planner_settings": PLANNER})
        if url.path == "/api/preferences":
            return self.send({"workspace": "~/Library/Application Support/MARA", "reference_library": "~/Music/References"})
        if url.path == "/api/roles":
            return self.send(ROLES)
        if url.path == "/api/preview-audio":
            # A silent placeholder only supplies the player duration for screenshots.
            output = io.BytesIO()
            with wave.open(output, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\0\0" * int(PREVIEW["seconds"] * 8000))
            return self.send(output.getvalue(), "audio/wav")
        path = (STATIC / url.path.removeprefix("/app/").lstrip("/")).resolve()
        if path.is_relative_to(STATIC) and path.is_file():
            return self.send(path.read_bytes(), mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send({"error": "Not available in the documentation demo."}, code=404)

    def do_POST(self):
        # Documentation fixture only: never proxy a command to the actual engine.
        self.send({"error": "Read-only demo. Choose a scene using ?demo=project|references|analysis|mix|result."}, code=409)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    print("README demo: http://127.0.0.1:8876/workflow?demo=project", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8876), Handler).serve_forever()
