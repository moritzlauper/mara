"""What a track sounds like, and which tracks sound like it.

Uses CLAP, a model that places audio and text descriptions in the same space:
the style of a track is the closest description, and similar tracks are the
closest audio. Embeddings of library files are cached, so a library is listened
to once.

Preferred: LAION's original music checkpoint, which MixMatch sets up by itself
on first use (its own Python 3.11 environment through uv, about 2.5 GB). The
Hugging Face port of that checkpoint is broken on the text side, so it is not
used. Without uv, or if the setup fails, the general model
laion/clap-htsat-unfused (Hugging Face, `pip install "mixmatch[style]"`) is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

import numpy as np

BASE = os.environ.get("MIXMATCH_CACHE", os.path.expanduser("~/.cache/mixmatch"))
CACHE = os.path.join(BASE, "clap")
MODEL = "laion/clap-htsat-unfused"
LAION_ENV = os.path.join(BASE, "laion-env")
LAION_CKPT = os.path.join(BASE, "laion", "music_audioset_epoch_15_esc_90.14.pt")
LAION_URL = "https://huggingface.co/lukewys/laion_clap/resolve/main/music_audioset_epoch_15_esc_90.14.pt"
LAION_PACKAGES = ["laion-clap", "transformers==4.30.2", "numpy<2", "torch", "torchvision"]
SR = 48000            # CLAP's sample rate
CLIP = 10.0           # seconds per clip
AUDIO = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".aac", ".ogg")
SKIP_DIRS = ("ableton", "spitfire audio", "unison", "vital", "$recycle.bin", "samples", "presets")

STYLES = [
    "drum and bass", "liquid drum and bass", "atmospheric drum and bass", "jungle", "neurofunk",
    "PS2-era video game drum and bass", "video game soundtrack", "breakbeat", "breakcore",
    "hyperpop", "digicore", "emo rap", "cloud rap", "trap", "rage beat", "plugg", "drill", "phonk",
    "boom bap hip hop", "lo-fi hip hop", "r&b", "pop", "indie pop", "bedroom pop",
    "house", "deep house", "tech house", "techno", "trance", "uk garage", "2-step", "future garage",
    "dubstep", "riddim", "future bass", "melodic bass", "electro house", "synthwave", "vaporwave",
    "ambient", "idm", "glitch", "chiptune", "jersey club", "footwork", "afrobeats", "reggaeton",
    "rock", "metal", "shoegaze", "emo", "punk", "jazz", "orchestral",
]


# ── audio ──────────────────────────────────────────────────────────────────
def load_mono(path: str, max_seconds: float = 600) -> np.ndarray:
    """Mono float32 at 48 kHz. WAV/AIFF/FLAC/MP3 directly, everything else (m4a, aac) through ffmpeg."""
    try:
        import soundfile as sf
        from scipy.signal import resample_poly

        data, sr = sf.read(path, dtype="float32", always_2d=True, frames=int(max_seconds * 192000))
        mono = data.mean(axis=1)
        if sr != SR:
            g = np.gcd(int(sr), SR)
            mono = resample_poly(mono, SR // g, int(sr) // g).astype(np.float32)
        return mono[: int(max_seconds * SR)]
    except Exception:
        if not shutil.which("ffmpeg"):
            raise
        raw = subprocess.run(["ffmpeg", "-v", "quiet", "-t", str(max_seconds), "-i", path, "-ac", "1", "-ar", str(SR),
                              "-f", "f32le", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, dtype=np.float32).copy()


def duration(path: str) -> float | None:
    try:
        import soundfile as sf

        info = sf.info(path)
        return info.frames / info.samplerate
    except Exception:
        if not shutil.which("ffprobe"):
            return None
        out = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True).stdout.strip()
        try:
            return float(out)
        except ValueError:
            return None


def loudest_clips(mono: np.ndarray, n: int = 3) -> list[np.ndarray]:
    """The n loudest non-overlapping 10 s clips: the drop or chorus says most about a track."""
    size = int(CLIP * SR)
    if len(mono) <= size:
        return [np.pad(mono, (0, size - len(mono)))]
    starts = range(0, len(mono) - size + 1, size)
    ranked = sorted(starts, key=lambda s: -float(np.mean(mono[s:s + size] ** 2)))
    return [mono[s:s + size] for s in ranked[:n]]


def tempo(mono: np.ndarray) -> float | None:
    """Rough tempo from the onset envelope's autocorrelation, folded into 70–180 BPM."""
    hop = 512
    frames = len(mono) // hop
    if frames < 200:
        return None
    energy = np.sqrt((mono[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1))
    onset = np.maximum(0.0, np.diff(np.log(energy + 1e-6)))
    onset -= onset.mean()
    ac = np.correlate(onset, onset, mode="full")[len(onset) - 1:]
    fps = SR / hop
    lo, hi = int(fps * 60 / 180), int(fps * 60 / 70)
    lag = lo + int(np.argmax(ac[lo:hi]))
    return round(60 * fps / lag, 1)


# ── CLAP ───────────────────────────────────────────────────────────────────
def ensure_laion(log=print) -> str | None:
    """Python of the LAION environment, set up on first use; None if that is not possible here."""
    python = os.path.join(LAION_ENV, "bin", "python")
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    try:
        if not os.path.exists(python):
            if not os.path.exists(uv):
                return None
            log("  setting up the LAION music model (once, a few minutes) …")
            subprocess.run([uv, "venv", "-q", "--python", "3.11", "--python-preference", "only-managed", LAION_ENV],
                           check=True, capture_output=True)
            subprocess.run([uv, "pip", "install", "-q", "--python", python, *LAION_PACKAGES],
                           check=True, capture_output=True)
        if not os.path.exists(LAION_CKPT):
            log("  downloading the LAION music checkpoint (2.4 GB, once) …")
            os.makedirs(os.path.dirname(LAION_CKPT), exist_ok=True)
            subprocess.run(["curl", "-sL", "--fail", "-o", LAION_CKPT + ".part", LAION_URL], check=True)
            os.replace(LAION_CKPT + ".part", LAION_CKPT)
        return python
    except (subprocess.CalledProcessError, OSError) as e:
        log(f"  LAION music model not available ({e}); using the general CLAP model")
        return None


class _Laion:
    """The original LAION music checkpoint, in its own process and Python environment."""

    name = "laion-music"
    templates = ("The genre of the music is {}.", "This is a {} song.")  # the phrasing of its training captions

    def __init__(self, python: str):
        import tempfile

        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "laion_worker.py")
        self.proc = subprocess.Popen([python, worker, LAION_CKPT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.tmp = tempfile.mkdtemp(prefix="mixmatch-clap-")
        first = self._read()
        if not first.get("ready"):
            raise RuntimeError(f"LAION worker did not start: {first}")

    def _read(self) -> dict:
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("LAION worker stopped")
            if line.startswith("{"):
                return json.loads(line)

    def _ask(self, req: dict) -> np.ndarray:
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        out = self._read()
        if "error" in out:
            raise RuntimeError(out["error"])
        return np.asarray(out["emb"], dtype=np.float32)

    def text(self, phrases: list[str]) -> np.ndarray:
        return self._ask({"text": phrases})

    def audio(self, clips: list[np.ndarray]) -> np.ndarray:
        path = os.path.join(self.tmp, "clips.npy")
        np.save(path, np.stack(clips).astype(np.float32))
        return self._ask({"audio": path})


class _HF:
    """laion/clap-htsat-unfused through transformers: the general model, the fallback."""

    name = MODEL
    templates = ("{} music", "a {} song", "this is a {} track")

    def __init__(self):
        import torch
        from transformers import ClapModel, ClapProcessor

        self.torch = torch
        self.model = ClapModel.from_pretrained(MODEL).eval()
        self.processor = ClapProcessor.from_pretrained(MODEL)

    def _np(self, x) -> np.ndarray:
        x = getattr(x, "pooler_output", x)  # transformers 5 returns an output object with the 512-d projection
        return x.detach().cpu().numpy().astype(np.float32)

    def text(self, phrases: list[str]) -> np.ndarray:
        inputs = self.processor(text=phrases, return_tensors="pt", padding=True)
        with self.torch.no_grad():
            return self._np(self.model.get_text_features(**inputs))

    def audio(self, clips: list[np.ndarray]) -> np.ndarray:
        inputs = self.processor(audio=clips, sampling_rate=SR, return_tensors="pt")
        with self.torch.no_grad():
            return self._np(self.model.get_audio_features(**inputs))


class Listener:
    """Embeds audio files (cached) and text with the best CLAP model available here."""

    def __init__(self, log=print, backend: str = "auto"):
        self.log = log
        self.backend = None
        if backend in ("auto", "laion"):
            python = ensure_laion(log)
            if python:
                try:
                    self.backend = _Laion(python)
                except Exception as e:
                    log(f"  LAION music model failed to start ({str(e)[:100]}); using the general CLAP model")
        if self.backend is None:
            self.backend = _HF()

    @staticmethod
    def _unit(x: np.ndarray) -> np.ndarray:
        return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-9)

    def text(self, labels: list[str]) -> np.ndarray:
        """One embedding per label, averaged over a few phrasings: single prompts are noisy."""
        out = [self._unit(self.backend.text([t.format(l) for l in labels])) for t in self.backend.templates]
        return self._unit(np.mean(out, axis=0))

    def clips(self, clips: list[np.ndarray]) -> np.ndarray:
        return self._unit(self._unit(self.backend.audio(clips)).mean(axis=0))

    def file(self, path: str) -> np.ndarray:
        st = os.stat(path)
        key = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime}|{self.backend.name}|v1".encode()).hexdigest()[:16]
        cached = os.path.join(CACHE, key + ".npy")
        if os.path.exists(cached):
            return np.load(cached)
        emb = self.clips(loudest_clips(load_mono(path)))
        os.makedirs(CACHE, exist_ok=True)
        np.save(cached, emb)
        return emb


# ── what it sounds like, and what sounds like it ───────────────────────────
def styles(listener: Listener, emb: np.ndarray, top: int = 5) -> list[tuple[str, float]]:
    sims = listener.text(STYLES) @ emb
    order = np.argsort(-sims)[:top]
    return [(STYLES[i], float(sims[i])) for i in order]


def library_files(folder: str, min_seconds: float = 90, max_seconds: float = 900) -> list[str]:
    """Songs in a folder: audio files of song length, skipping sample and preset folders."""
    out = []
    for root, dirs, files in os.walk(os.path.expanduser(folder)):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.lower().endswith(AUDIO) and not f.startswith("._"):
                path = os.path.join(root, f)
                d = duration(path)
                if d is not None and min_seconds <= d <= max_seconds:
                    out.append(path)
    return sorted(out)


def similar(listener: Listener, emb: np.ndarray, files: list[str], top: int = 10, exclude: str | None = None,
            log=print) -> list[tuple[str, float]]:
    scored = []
    for i, path in enumerate(files, 1):
        if exclude and os.path.abspath(path) == os.path.abspath(exclude):
            continue
        try:
            scored.append((path, float(listener.file(path) @ emb)))
        except Exception as e:  # unreadable or DRM-protected file
            log(f"  skipped {os.path.basename(path)}: {str(e)[:80]}")
        if i % 50 == 0:
            log(f"  listened to {i}/{len(files)} …")
    return sorted(scored, key=lambda x: -x[1])[:top]


def search_links(artist: str, title: str) -> dict:
    from urllib.parse import quote_plus

    q = quote_plus(f"{artist} {title}")
    return {"bandcamp": f"https://bandcamp.com/search?q={q}", "beatport": f"https://www.beatport.com/search?q={q}"}


def describe(path: str, listener: Listener) -> dict:
    """Everything a planner needs to suggest references: style guesses, tempo, tonal summary."""
    from .analysis import band_label, stats
    from .reference import file_frames, loudest_part

    mono = load_mono(path)
    emb = listener.file(path)
    st = stats(loudest_part(file_frames(path)))
    return {
        "file": os.path.basename(path),
        "styles_clap": [{"style": s, "score": round(v, 3)} for s, v in styles(listener, emb, 8)],
        "tempo_bpm": tempo(mono),
        "loudness_lufs": round(st.loudness, 1),
        "plr_db": round(st.plr, 1),
        "width_db": round(st.width_db, 1),
        "tonal_balance_db": {band_label(i): round(float(st.shape_db[i]), 1) for i in range(0, 31, 3)},
        "_embedding": emb,
    }


SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "style": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"artist": {"type": "string"}, "title": {"type": "string"}, "why": {"type": "string"}},
                "required": ["artist", "title", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["style", "suggestions"],
    "additionalProperties": False,
}

SUGGEST_SYSTEM = """You suggest reference tracks for mixing. You get measurements of the user's track: style guesses from an audio model (CLAP scores, higher is closer), tempo, loudness, dynamics, width and tonal balance, plus the user's own description.
Name the style in a few words, then suggest 8 real, released tracks that a producer of this style would mix against: well mixed and mastered, close in style, tempo and energy, ideally available on Bandcamp or Beatport. Only tracks you are sure exist. For each, one short sentence why it fits."""


def suggest(desc: dict, note: str = "", model: str = "opus") -> dict:
    """Asks Claude (local Claude Code, subscription) for reference tracks to buy."""
    payload = {k: v for k, v in desc.items() if not k.startswith("_")}
    if note:
        payload["user_description"] = note
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    cmd = [shutil.which("claude") or "claude", "-p", json.dumps(payload, ensure_ascii=False), "--model", model,
           "--output-format", "json", "--no-session-persistence", "--strict-mcp-config", "--tools", "",
           "--setting-sources", "", "--exclude-dynamic-system-prompt-sections",
           "--system-prompt", SUGGEST_SYSTEM, "--json-schema", json.dumps(SUGGEST_SCHEMA)]
    out = json.loads(subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600).stdout)
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result"))[:300])
    result = out.get("structured_output") or json.loads(out["result"])
    for s in result["suggestions"]:
        s["links"] = search_links(s["artist"], s["title"])
    return result
