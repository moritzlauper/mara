"""Third-octave bands shared by the Max for Live tap and the app."""
BAND_COUNT = 31
BASE_HZ = 20.0
RATIO = 2 ** (1 / 3)
Q = 4.32  # bandwidth of one third of an octave

CENTRES = [BASE_HZ * RATIO**i for i in range(BAND_COUNT)]  # 20 Hz … 20 kHz


def band_index(hz: float) -> int:
    """Nearest band to a frequency."""
    return min(range(BAND_COUNT), key=lambda i: abs(CENTRES[i] - hz))


def bands_between(lo: float, hi: float) -> list[int]:
    return [i for i, c in enumerate(CENTRES) if lo <= c <= hi]
