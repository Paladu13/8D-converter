"""
Polyfill audioop pour Python 3.13+ (audioop retiré de la stdlib).
DOIT être importé AVANT pydub, car pydub importe audioop.
"""
import struct
import sys


def _unpack(data, width):
    """Helper: unpack audio samples based on width (1=8bit, 2=16bit)."""
    if width == 2:
        count = len(data) // 2
        return struct.unpack(f"<{count}h", data)
    elif width == 1:
        return list(data)
    return []


def _pack(samples, width):
    """Helper: pack samples back into bytes based on width."""
    if width == 2:
        samples = [max(-32768, min(32767, s)) for s in samples]
        return struct.pack(f"<{len(samples)}h", *samples)
    elif width == 1:
        samples = [max(-128, min(127, s)) for s in samples]
        return bytes(samples)
    return b""


class _audioop:
    @staticmethod
    def tostereo(data, width, lfactor, rfactor):
        samples = _unpack(data, width)
        left = [max(-32768, min(32767, int(s * lfactor))) for s in samples]
        right = [max(-32768, min(32767, int(s * rfactor))) for s in samples]
        result = bytearray(len(samples) * 4)
        for i, (l, r) in enumerate(zip(left, right)):
            struct.pack_into("<hh", result, i * 4, l, r)
        return bytes(result)

    @staticmethod
    def max(data, width):
        samples = _unpack(data, width)
        if not samples:
            return 0
        return max(abs(s) for s in samples)

    @staticmethod
    def avg(data, width):
        samples = _unpack(data, width)
        if not samples:
            return 0
        return sum(samples) // len(samples)

    @staticmethod
    def avgpp(data, width):
        return 0

    @staticmethod
    def maxpp(data, width):
        return 0

    @staticmethod
    def cross(data, width):
        samples = _unpack(data, width)
        crosses = 0
        for i in range(1, len(samples)):
            if (samples[i-1] < 0 and samples[i] >= 0) or \
               (samples[i-1] >= 0 and samples[i] < 0):
                crosses += 1
        return crosses

    @staticmethod
    def mul(data, width, factor):
        samples = _unpack(data, width)
        samples = [int(s * factor) for s in samples]
        return _pack(samples, width)

    @staticmethod
    def bias(data, width, bias_val):
        samples = _unpack(data, width)
        samples = [s + bias_val for s in samples]
        return _pack(samples, width)

    @staticmethod
    def lin2lin(data, width, newwidth):
        samples = _unpack(data, width)
        if width == 2 and newwidth == 1:
            samples = [s >> 2 for s in samples]
        elif width == 1 and newwidth == 2:
            samples = [s << 2 for s in samples]
        return _pack(samples, newwidth)

    @staticmethod
    def getsample(data, width, index):
        samples = _unpack(data, width)
        if 0 <= index < len(samples):
            return samples[index]
        return 0

    @staticmethod
    def add(data1, data2, width):
        samples1 = _unpack(data1, width)
        samples2 = _unpack(data2, width)
        count = min(len(samples1), len(samples2))
        samples = [samples1[i] + samples2[i] for i in range(count)]
        return _pack(samples, width)

    @staticmethod
    def minmax(data, width):
        samples = _unpack(data, width)
        if not samples:
            return (0, 0)
        return (min(samples), max(samples))

    @staticmethod
    def findfactor(data, reference):
        return 1.0

    @staticmethod
    def findmax(data, length):
        return 0


def install_audioop():
    """Installe le polyfill audioop si le module natif n'est pas disponible."""
    try:
        import audioop  # noqa: F401
    except ImportError:
        audioop = _audioop()
        sys.modules['audioop'] = audioop