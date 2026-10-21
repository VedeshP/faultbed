"""Bit-exact port of firmware/lib/fbcore/fb_hash.h.

Any change here must be mirrored in the firmware (and vice versa); the
cross-language test vectors in tests/test_hashing.py guard this.
"""

MASK64 = (1 << 64) - 1

STREAM_LOSS = 1
STREAM_GE = 2
STREAM_CORRUPT = 3
STREAM_CORRUPT_BIT = 4
STREAM_JITTER = 5
STREAM_DUP = 6
STREAM_INTERVAL = 7


def splitmix64(x: int) -> int:
    x = (x + 0x9E3779B97F4A7C15) & MASK64
    z = x
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return z ^ (z >> 31)


def stream_key(seed: int, a: int, b: int, stream: int) -> int:
    return splitmix64((seed ^ (a << 56) ^ (b << 48) ^ (stream << 40)) & MASK64)


def draw(seed: int, a: int, b: int, stream: int, index: int) -> int:
    return splitmix64(stream_key(seed, a, b, stream) ^ (index & MASK64))


def prob_threshold(p: float) -> int:
    if p <= 0.0:
        return 0
    if p >= 1.0:
        return 1 << 53
    return int(p * 9007199254740992.0)


def bernoulli(r: int, threshold: int) -> bool:
    return (r >> 11) < threshold
