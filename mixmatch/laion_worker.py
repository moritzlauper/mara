"""Runs LAION-CLAP (the original music checkpoint) in its own Python environment.

Started by `mixmatch.style` with the environment's Python. Reads one JSON
request per line from stdin and answers one JSON line on stdout:

    {"text": ["hyperpop", ...]}          → {"emb": [[...512 floats], ...]}
    {"audio": "/path/to/clips.npy"}      → {"emb": [[...], ...]}   (clips: N × samples, 48 kHz, float32)

Kept free of MixMatch imports: it runs with a different Python and packages.
"""
import json
import sys

import numpy as np


def main(checkpoint: str) -> None:
    import laion_clap

    model = laion_clap.CLAP_Module(enable_fusion=False, amodel="HTSAT-base")
    model.load_ckpt(checkpoint)
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            if "text" in req:
                emb = model.get_text_embedding(req["text"], use_tensor=False)
            else:
                clips = np.load(req["audio"]).astype(np.float32)
                emb = model.get_audio_embedding_from_data(x=clips, use_tensor=False)
            print(json.dumps({"emb": np.asarray(emb, dtype=np.float32).tolist()}), flush=True)
        except Exception as e:  # answer instead of dying, the caller decides
            print(json.dumps({"error": str(e)[:300]}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
