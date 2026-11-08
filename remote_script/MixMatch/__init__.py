"""MixMatch control surface for Ableton Live.

Copy the folder `MixMatch` to
`~/Music/Ableton/User Library/Remote Scripts/` and pick «MixMatch» as a control
surface in Live → Settings → Link, Tempo & MIDI (input and output: None).

The script only exposes the set to the MixMatch app over a local TCP socket
(127.0.0.1:9877, one JSON object per line). It makes no decisions itself.
"""


def create_instance(c_instance):
    # Imported here so the commands can be tested outside Live, where _Framework is missing.
    from .surface import MixMatchSurface

    return MixMatchSurface(c_instance)
