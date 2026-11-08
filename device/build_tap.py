"""Builds the «MixMatch Tap» Max for Live audio effect.

    python3 device/build_tap.py

writes `device/MixMatch Tap.maxpat` (open in Max) and `device/MixMatch Tap.amxd`
(drag into Live). The device passes audio through unchanged and every 100 ms
sends one measurement frame to the MixMatch app (UDP 127.0.0.1:9878, OSC):

    /mm/tap  <track id> m_rms s_rms s_low_rms peak_l peak_r band1 … band31
    /mm/name <track id> <kind> <track name>

Bands: 31 third-octave filters from 20 Hz (fffb~) on the mid signal, RMS over
100 ms. s_low_rms is the side signal below 150 Hz (bass that is not mono).
"""
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from mixmatch.bands import BAND_COUNT, BASE_HZ, RATIO, Q  # noqa: E402
PORT = 9878

boxes, lines = [], []


def box(text, x, y, ins, outs, w=None):
    oid = "obj-%d" % (len(boxes) + 1)
    boxes.append({"box": {
        "id": oid, "maxclass": "newobj", "text": text,
        "numinlets": ins, "numoutlets": outs, "outlettype": [""] * outs,
        "patching_rect": [x, y, w or max(60, 7 * len(text)), 22],
    }})
    return oid


def wire(src, out, dst, inp):
    lines.append({"patchline": {"source": [src, out], "destination": [dst, inp]}})


def build():
    values = 5 + BAND_COUNT  # m, s, s_low, peak_l, peak_r, bands

    plugin = box("plugin~ 2", 20, 20, 2, 2)
    plugout = box("plugout~ 2", 20, 600, 2, 0)
    wire(plugin, 0, plugout, 0)
    wire(plugin, 1, plugout, 1)

    add = box("+~", 140, 70, 2, 1)
    sub = box("-~", 260, 70, 2, 1)
    wire(plugin, 0, add, 0); wire(plugin, 1, add, 1)
    wire(plugin, 0, sub, 0); wire(plugin, 1, sub, 1)
    mid = box("*~ 0.5", 140, 100, 2, 1)
    side = box("*~ 0.5", 260, 100, 2, 1)
    wire(add, 0, mid, 0); wire(sub, 0, side, 0)

    this = box("live.thisdevice", 600, 20, 1, 3)
    start = box("t b b", 600, 50, 1, 2)
    metro = box("metro 100", 600, 80, 2, 1)
    wire(this, 0, start, 0)
    wire(start, 0, metro, 0)
    trig = box("t " + " ".join(["b"] * values), 600, 110, 1, values, w=900)
    wire(metro, 0, trig, 0)

    pack = box("pack " + " ".join(["0."] * values), 140, 480, values, 1, w=1200)
    js = box("js mixmatch_tap.js", 140, 520, 1, 1)
    udp = box("udpsend 127.0.0.1 %d" % PORT, 140, 560, 1, 0)
    # On load: start the metro, and tell the script to look up its track.
    init = "obj-%d" % (len(boxes) + 1)
    boxes.append({"box": {"id": init, "maxclass": "message", "text": "init",
                          "numinlets": 2, "numoutlets": 1, "outlettype": [""],
                          "patching_rect": [700, 50, 40, 22]}})
    wire(start, 1, init, 0)
    wire(init, 0, js, 0)
    wire(pack, 0, js, 0)
    wire(js, 0, udp, 0)

    def measured(signal_src, kind, slot, x):
        """signal → measurement object → snapshot~ (banged by trig) → pack slot."""
        if kind == "rms":
            m = box("average~ 4410 rms", x, 250, 2, 1)
        else:
            m = box("peakamp~ 0", x, 250, 2, 1)
        wire(signal_src[0], signal_src[1], m, 0)
        if kind == "rms":
            snap = box("snapshot~", x, 300, 2, 1)
            wire(m, 0, snap, 0)
            out = snap
        else:
            out = m  # peakamp~ 0 reports on bang
        # trigger fires right to left: slot 0 (pack's left inlet) last.
        wire(trig, slot, out, 0)
        wire(out, 0, pack, slot)

    measured((mid, 0), "rms", 0, 20)
    measured((side, 0), "rms", 1, 160)
    low = box("cross~ 150", 300, 140, 2, 2)
    wire(side, 0, low, 0)
    measured((low, 0), "rms", 2, 300)
    measured((plugin, 0), "peak", 3, 440)
    measured((plugin, 1), "peak", 4, 580)

    bank = box("fffb~ %d %g %g %g" % (BAND_COUNT, BASE_HZ, RATIO, Q), 140, 180, 1, BAND_COUNT, w=900)
    wire(mid, 0, bank, 0)
    for i in range(BAND_COUNT):
        measured((bank, i), "rms", 5 + i, 20 + 40 * i)

    return {"patcher": {
        "fileversion": 1,
        "appversion": {"major": 8, "minor": 6, "revision": 0, "architecture": "x64", "modernui": 1},
        "classnamespace": "box",
        "rect": [100, 100, 1400, 700],
        "openinpresentation": 0,
        "default_fontsize": 10.0,
        "boxes": boxes,
        "lines": lines,
        "dependency_cache": [{"name": "mixmatch_tap.js", "bootpath": ".", "type": "TEXT", "implicit": 1}],
        "autosave": 0,
    }}


def write_amxd(patch, path):
    """Max for Live device file: a small header followed by the patcher JSON."""
    data = json.dumps(patch).encode("utf-8") + b"\n\x00"
    header = (b"ampf" + struct.pack("<I", 4) + b"aaaa"
              + b"meta" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
              + b"ptch" + struct.pack("<I", len(data)))
    with open(path, "wb") as f:
        f.write(header + data)


if __name__ == "__main__":
    patch = build()
    with open(os.path.join(HERE, "MixMatch Tap.maxpat"), "w") as f:
        json.dump(patch, f, indent=1)
    write_amxd(patch, os.path.join(HERE, "MixMatch Tap.amxd"))
    print("wrote %d objects, %d connections" % (len(boxes), len(lines)))
