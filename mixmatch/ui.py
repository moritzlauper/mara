"""A small local web interface: start and follow runs, see the song, references and effects, edit roles.

    mixmatch ui            → http://127.0.0.1:8765

Standard library only. Runs `mixmatch run` as a child process and reads its
log; nothing here talks to Live directly, so the interface can be opened and
closed while a run goes on.
"""
from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
AUDIO = (".wav", ".aif", ".aiff", ".flac", ".mp3")


class Runner:
    """The one `mixmatch` child process the interface may start, and its log file."""

    def __init__(self, workdir: str):
        self.workdir = workdir
        self.proc: subprocess.Popen | None = None
        self.log_path: str | None = None
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str]) -> str:
        with self.lock:
            if self.running():
                raise RuntimeError("a run is already going on")
            os.makedirs(os.path.join(self.workdir, "runs"), exist_ok=True)
            self.log_path = os.path.join(self.workdir, "runs", time.strftime("ui-%Y%m%d-%H%M%S.log"))
            log = open(self.log_path, "w")
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            self.proc = subprocess.Popen([sys.executable, "-m", "mixmatch.cli", *args], cwd=self.workdir,
                                         stdout=log, stderr=subprocess.STDOUT, env=env)
            return self.log_path

    def stop(self) -> None:
        if self.running():
            self.proc.send_signal(signal.SIGINT)

    def latest_log(self) -> str | None:
        if self.log_path:
            return self.log_path
        logs = sorted(glob.glob(os.path.join(self.workdir, "runs", "ui-*.log")))
        return logs[-1] if logs else None


# ── reading a run's log ─────────────────────────────────────────────────────
PARTS = re.compile(r"\(([^()]*)\)\s*$")


def _parts(text: str) -> dict:
    m = PARTS.search(text)
    out = {}
    if m:
        for item in m.group(1).split("·"):
            bits = item.strip().rsplit(" ", 1)
            if len(bits) == 2:
                try:
                    out[bits[0]] = float(bits[1])
                except ValueError:
                    pass
    return out


def parse_log(lines: list[str]) -> dict:
    """Phase, current channel, distance history, decisions, song info and effects from a run's log."""
    state = {"phase": None, "section": None, "distance": None, "parts": {}, "history": [], "events": [],
             "key": None, "style": None, "mood": None, "effects": [], "references": [], "to_get": [], "done": None}
    for raw in lines:
        line = raw.rstrip("\n")
        s = line.strip()
        if s.startswith("━━"):
            state["phase"] = s.strip("━ ").strip()
            state["events"].append({"kind": "phase", "text": state["phase"]})
        elif s.startswith("▶"):
            state["section"] = s[1:].strip()
            state["events"].append({"kind": "section", "text": state["section"]})
        elif s.startswith("distance "):
            m = re.match(r"distance ([\d.]+)", s)
            if m:
                state["distance"], state["parts"] = float(m.group(1)), _parts(s)
                state["history"].append(state["distance"])
        elif s.startswith("✓") and "→" in s:
            m = re.search(r"→ ([\d.]+)", s)
            if m:
                state["distance"], state["parts"] = float(m.group(1)), _parts(s) or state["parts"]
                state["history"].append(state["distance"])
            state["events"].append({"kind": "kept", "text": s})
        elif s.startswith("↺"):
            state["events"].append({"kind": "undone", "text": s})
        elif s.startswith("= no measurable"):
            state["events"].append({"kind": "neutral", "text": s})
        elif s.startswith("round "):
            state["events"].append({"kind": "round", "text": s})
        elif s.startswith("·"):
            state["events"].append({"kind": "action", "text": s[1:].strip()})
        elif s.startswith("sits well"):
            state["events"].append({"kind": "neutral", "text": s})
        elif s.startswith("Key:"):
            state["key"] = s[4:].strip()
        elif s.startswith("sounds like:"):
            state["style"] = s.split(":", 1)[1].strip()
        elif s.startswith(("⊘", "− removed", "✓ kept")):
            state["effects"].append(s)
        elif s.startswith("✓ ") and "[" in s:
            state["references"].append(s[1:].strip())
        elif s.startswith("Stopped:") or s.startswith("Done."):
            state["done"] = s
        elif "Traceback" in s or s.startswith("⚠"):
            state["events"].append({"kind": "warning", "text": s})
    state["events"] = state["events"][-150:]
    return state


def buy_list(workdir: str) -> list[dict]:
    files = sorted(glob.glob(os.path.join(workdir, "runs", "*-buy.md")))
    if not files:
        return []
    out = []
    for line in open(files[-1]):
        m = re.match(r"- \*\*(.+?)\*\*: (.*?)\s*$", line)
        if m:
            out.append({"title": m.group(1), "why": m.group(2), "links": {}})
        for name, url in re.findall(r"\[(Spotify|Bandcamp|Beatport)\]\((\S+?)\)", line):
            if out:
                out[-1]["links"][name.lower()] = url
    return out


def trash(path: str) -> str:
    """Moves a file to the macOS Trash (MIXMATCH_TRASH overrides), so a removed reference can be put back."""
    folder = os.environ.get("MIXMATCH_TRASH") or os.path.expanduser("~/.Trash")
    os.makedirs(folder, exist_ok=True)
    name = os.path.basename(path)
    target = os.path.join(folder, name)
    if os.path.exists(target):
        stem, ext = os.path.splitext(name)
        target = os.path.join(folder, f"{stem} {time.strftime('%H.%M.%S')}{ext}")
    os.replace(path, target)
    return target


def safe_reference(workdir: str, name: str) -> str:
    """Path of a file directly inside references/; refuses anything else (no ../, no other folders)."""
    base = os.path.basename(name or "")
    if not base or base != name or not base.lower().endswith(AUDIO):
        raise ValueError(f"not an audio file name: {name!r}")
    return os.path.join(workdir, "references", base)


def references(workdir: str) -> list[str]:
    folder = os.path.join(workdir, "references")
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(AUDIO))


# ── HTTP ────────────────────────────────────────────────────────────────────
def make_handler(runner: Runner):
    workdir = runner.workdir

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code: int, body, kind: str = "application/json"):
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path == "/":
                return self._send(200, PAGE, "text/html")
            if self.path == "/api/state":
                path = runner.latest_log()
                lines = open(path).readlines() if path and os.path.exists(path) else []
                state = parse_log(lines)
                return self._send(200, {**state, "running": runner.running(), "log": [l.rstrip() for l in lines[-200:]],
                                        "reference_files": references(workdir), "to_get": buy_list(workdir)})
            if self.path == "/api/roles":
                path = os.path.join(workdir, "roles.json")
                return self._send(200, json.load(open(path)) if os.path.exists(path) else {})
            self._send(404, {"error": "not found"})

        def do_PUT(self):
            """Upload a reference: PUT /api/references/<file name> with the file as body."""
            try:
                from urllib.parse import unquote

                if not self.path.startswith("/api/references/"):
                    return self._send(404, {"error": "not found"})
                path = safe_reference(workdir, unquote(self.path[len("/api/references/"):]))
                size = int(self.headers.get("Content-Length") or 0)
                if size <= 0 or size > 1_000_000_000:
                    raise ValueError("empty or too large")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".part", "wb") as f:
                    left = size
                    while left:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            raise ValueError("upload cut off")
                        f.write(chunk)
                        left -= len(chunk)
                os.replace(path + ".part", path)
                self._send(200, {"saved": os.path.basename(path)})
            except Exception as e:
                self._send(400, {"error": str(e)})

        def do_POST(self):
            try:
                body = self._body()
                if self.path == "/api/run":
                    args = ["run"]
                    if body.get("refs"):
                        args += ["--ref", "references/"]
                    if body.get("auto_ref"):
                        args += ["--auto-ref"]
                    if body.get("quick"):
                        args += ["--quick"]
                    if body.get("no_audit"):
                        args += ["--no-audit"]
                    if body.get("resume"):
                        args += ["--resume", body["resume"]]
                    if body.get("minutes"):
                        args += ["--minutes", str(float(body["minutes"]))]
                    return self._send(200, {"log": runner.start(args)})
                if self.path == "/api/taps":
                    return self._send(200, {"log": runner.start(["taps"])})
                if self.path == "/api/stop":
                    runner.stop()
                    return self._send(200, {"ok": True})
                if self.path == "/api/references/remove":
                    path = safe_reference(workdir, body.get("name", ""))
                    if not os.path.exists(path):
                        raise ValueError("no such reference")
                    return self._send(200, {"trash": trash(path)})
                if self.path == "/api/roles":
                    with open(os.path.join(workdir, "roles.json"), "w") as f:
                        json.dump(body, f, indent=2, ensure_ascii=False)
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})
            except Exception as e:
                self._send(400, {"error": str(e)})

    return Handler


def serve(workdir: str = ".", port: int = PORT, open_browser: bool = True) -> None:
    runner = Runner(os.path.abspath(workdir))
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(runner))
    url = f"http://127.0.0.1:{port}"
    print(f"MixMatch interface: {url}  (Ctrl+C to quit)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        runner.stop()
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MixMatch</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--text:#e7e9ee;--muted:#8a93a6;--accent:#7c9cff;--good:#4fc38a;--bad:#ef6b6b;--warn:#e5b450}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#f6f7f9;--panel:#fff;--line:#e3e6ec;--text:#1b1f27;--muted:#646d7e;--accent:#3b5bdb;--good:#1f9d63;--bad:#d64545;--warn:#b7791f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,system-ui,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line)}
header h1{font-size:17px;margin:0}.pill{padding:2px 9px;border-radius:99px;font-size:12px;background:var(--line);color:var(--muted)}
.pill.on{background:color-mix(in srgb,var(--good) 22%,transparent);color:var(--good)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;padding:16px 20px;max-width:1500px;margin:auto}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
section h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 10px}
.wide{grid-column:1/-1}button{background:var(--accent);color:#fff;border:0;border-radius:7px;padding:7px 13px;font:inherit;cursor:pointer}
button.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}button:disabled{opacity:.45;cursor:default}
label{display:flex;gap:6px;align-items:center;color:var(--muted)}.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:6px 0}
.big{font-size:30px;font-weight:600}.muted{color:var(--muted)}.parts{display:flex;flex-direction:column;gap:5px;margin-top:8px}
.bar{display:grid;grid-template-columns:90px 1fr 42px;gap:8px;align-items:center;font-size:12px}.bar i{display:block;height:7px;border-radius:4px;background:var(--accent)}
.events{max-height:420px;overflow:auto;font-size:13px;display:flex;flex-direction:column;gap:4px}
.ev{padding:5px 8px;border-radius:6px;border-left:3px solid var(--line)}.ev.kept{border-color:var(--good)}.ev.undone{border-color:var(--bad)}
.ev.phase{font-weight:600;border-color:var(--accent)}.ev.section{color:var(--accent)}.ev.action{color:var(--muted);padding-left:18px}.ev.warning{border-color:var(--warn)}
ul{margin:0;padding-left:18px}li{margin:3px 0}a{color:var(--accent)}table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:4px 6px;border-bottom:1px solid var(--line)}select,input{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:4px 6px;font:inherit}
pre{max-height:260px;overflow:auto;font-size:12px;background:var(--bg);padding:10px;border-radius:8px;margin:0;white-space:pre-wrap}
svg{width:100%;height:90px}
.drop{margin-top:10px;border:1.5px dashed var(--line);border-radius:8px;padding:12px;text-align:center;color:var(--muted);font-size:13px}
.drop.over{border-color:var(--accent);color:var(--accent)}.reflist li{display:flex;justify-content:space-between;gap:8px;align-items:center}
button.small{padding:2px 8px;font-size:12px}button.danger{background:var(--bad);color:#fff;border-color:var(--bad)}
</style>
</head>
<body>
<header><h1>MixMatch</h1><span id="status" class="pill">idle</span><span id="phase" class="muted"></span></header>
<main>
<section>
  <h2>Run</h2>
  <div class="row"><label><input type="checkbox" id="auto_ref" checked> find references (auto)</label>
  <label><input type="checkbox" id="refs" checked> use references/</label></div>
  <div class="row"><label><input type="checkbox" id="quick"> master only (quick)</label>
  <label><input type="checkbox" id="no_audit"> keep every effect</label></div>
  <div class="row"><label>resume at track <input id="resume" size="14" placeholder="optional"></label></div>
  <div class="row"><button id="start">Start</button><button id="stop" class="ghost">Stop</button><button id="taps" class="ghost">Set taps</button></div>
  <p class="muted" id="msg"></p>
</section>
<section>
  <h2>Distance to target</h2>
  <div class="big" id="distance">–</div><div class="muted" id="section"></div>
  <svg id="chart" viewBox="0 0 300 90" preserveAspectRatio="none"></svg>
  <div class="parts" id="parts"></div>
</section>
<section>
  <h2>Song</h2>
  <div id="song" class="muted">No run yet.</div>
</section>
<section class="wide">
  <h2>What MixMatch does</h2>
  <div class="events" id="events"></div>
</section>
<section>
  <h2>References</h2>
  <div id="refs_used"></div>
  <div id="drop" class="drop">Drop audio files here to add them to references/</div><h2 style="margin-top:12px">To get</h2><div id="to_get" class="muted">–</div>
</section>
<section>
  <h2>Effects</h2><div id="effects" class="muted">–</div>
</section>
<section>
  <h2>Roles</h2><div id="roles" class="muted">–</div><div class="row"><button id="save_roles" class="ghost">Save roles</button></div>
</section>
<section class="wide"><h2>Log</h2><pre id="log"></pre></section>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  const j = await r.json(); $("msg").textContent = j.error || ""; return j;
}
$("start").onclick = () => post("/api/run", {auto_ref: $("auto_ref").checked, refs: $("refs").checked, quick: $("quick").checked,
  no_audit: $("no_audit").checked, resume: $("resume").value.trim()});
$("stop").onclick = () => post("/api/stop");
$("taps").onclick = () => post("/api/taps");
const ROLES = ["kick","snare","clap","hats","percussion","drums_bus","sub","bass","lead_vocal","backing_vocal","vocal_fx","lead_synth","pluck","pad","keys","piano","guitar","strings","brass","fx","ambience","bus","other"];
async function loadRoles() {
  const roles = await (await fetch("/api/roles")).json(); const names = Object.keys(roles);
  if (!names.length) { $("roles").textContent = "No roles.json yet (written by the first run)."; return; }
  $("roles").innerHTML = "<table>" + names.map(n => `<tr><td>${esc(n)}</td><td><select data-n="${esc(n)}">${ROLES.map(r =>
    `<option ${r===roles[n].role?"selected":""}>${r}</option>`).join("")}</select></td><td><input data-p="${esc(n)}" type="number" min="1" max="5" value="${roles[n].priority}" style="width:52px"></td></tr>`).join("") + "</table>";
  $("save_roles").onclick = async () => {
    for (const n of names) { roles[n].role = document.querySelector(`[data-n="${CSS.escape(n)}"]`).value;
      roles[n].priority = +document.querySelector(`[data-p="${CSS.escape(n)}"]`).value; }
    await post("/api/roles", roles); $("msg").textContent = "Roles saved; the next run uses them.";
  };
}
function chart(values) {
  if (values.length < 2) { $("chart").innerHTML = ""; return; }
  const max = Math.max(...values) * 1.05, w = 300, h = 90;
  const pts = values.map((v, i) => `${(i/(values.length-1))*w},${h - (v/max)*h}`).join(" ");
  $("chart").innerHTML = `<polyline fill="none" stroke="var(--accent)" stroke-width="2" points="${pts}"/>`;
}
async function tick() {
  let s; try { s = await (await fetch("/api/state")).json(); } catch { return; }
  $("status").textContent = s.running ? "running" : (s.done ? "finished" : "idle"); $("status").className = "pill" + (s.running ? " on" : "");
  $("phase").textContent = s.phase || ""; $("start").disabled = s.running;
  $("distance").textContent = s.distance != null ? s.distance.toFixed(2) : "–"; $("section").textContent = s.section || "";
  chart(s.history);
  const parts = Object.entries(s.parts || {}); const top = Math.max(1, ...parts.map(p => p[1]));
  $("parts").innerHTML = parts.map(([k, v]) => `<div class="bar"><span>${esc(k)}</span><i style="width:${(v/top)*100}%"></i><span>${v.toFixed(2)}</span></div>`).join("");
  $("song").innerHTML = [s.key && `<div><b>${esc(s.key)}</b></div>`, s.style && `<div class="muted">sounds like ${esc(s.style)}</div>`].filter(Boolean).join("") || "No run yet.";
  $("events").innerHTML = s.events.slice().reverse().map(e => `<div class="ev ${e.kind}">${esc(e.text)}</div>`).join("");
  const refsKey = JSON.stringify(s.reference_files) + s.references.length;
  if (refsKey !== lastRefs) { lastRefs = refsKey;
    $("refs_used").innerHTML = (s.reference_files.length ? "<ul class='reflist'>" + s.reference_files.map(f =>
      `<li><span>${esc(f)}</span><button class="ghost small" data-remove="${esc(f)}">remove</button></li>`).join("") + "</ul>"
      : "<span class='muted'>references/ is empty</span>")
      + (s.references.length ? "<div class='muted' style='margin-top:6px'>picked: " + s.references.map(esc).join("<br>") + "</div>" : "");
    document.querySelectorAll("[data-remove]").forEach(b => b.onclick = async () => {
      if (!b.classList.contains("danger")) { b.classList.add("danger"); b.textContent = "really? (to Trash)"; return; }
      const j = await post("/api/references/remove", {name: b.dataset.remove});
      if (!j.error) $("msg").textContent = "Moved to the Trash: " + b.dataset.remove; lastRefs = ""; tick();
    });
  }
  $("to_get").innerHTML = s.to_get.length ? "<ul>" + s.to_get.map(t => `<li><b>${esc(t.title)}</b><br><span class="muted">${esc(t.why)}</span><br>` +
    Object.entries(t.links).map(([k, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener">${k}</a>`).join(" · ") + "</li>").join("") + "</ul>" : "–";
  $("effects").innerHTML = s.effects.length ? "<ul>" + s.effects.map(e => `<li>${esc(e)}</li>`).join("") + "</ul>" : "–";
  const log = $("log"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 5;
  log.textContent = s.log.join("\n"); if (atEnd) log.scrollTop = log.scrollHeight;
}
let lastRefs = "";
const drop = $("drop");
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", async e => {
  for (const file of e.dataTransfer.files) {
    drop.textContent = "Adding " + file.name + " …";
    const r = await fetch("/api/references/" + encodeURIComponent(file.name), {method: "PUT", body: file});
    const j = await r.json(); $("msg").textContent = j.error ? file.name + ": " + j.error : "Added " + j.saved;
  }
  drop.textContent = "Drop audio files here to add them to references/"; lastRefs = ""; tick();
});
loadRoles(); tick(); setInterval(tick, 1500);
</script>
</body>
</html>
"""
