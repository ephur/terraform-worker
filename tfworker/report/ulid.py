"""Sortable run ids (ULID: 48-bit millisecond time plus 80 random bits)."""

import os
import time
from typing import Optional

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, index = divmod(value, 32)
        chars.append(_ALPHABET[index])
    return "".join(reversed(chars))


def new_ulid(
    timestamp_ms: Optional[int] = None, entropy: Optional[bytes] = None
) -> str:
    """Return a 26 character ULID; ids sort by the time they were made."""
    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000
    if entropy is None:
        entropy = os.urandom(10)
    if not 0 <= timestamp_ms < 2**48:
        raise ValueError("timestamp out of range for a ULID")
    if len(entropy) != 10:
        raise ValueError("a ULID takes 10 bytes of entropy")
    return _encode(timestamp_ms, 10) + _encode(int.from_bytes(entropy, "big"), 16)
