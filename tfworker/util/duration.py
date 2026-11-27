"""Parse human readable durations such as ``90s``, ``20m`` or ``1h30m``."""

import math
import re
from typing import Union

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_PART = re.compile(r"(\d+(?:\.\d+)?)([smhd])")


def parse_duration(value: Union[str, int, float]) -> float:
    """
    Return a duration in seconds.

    Accepts a number of seconds, a numeric string, or one or more
    ``<number><unit>`` parts with units s, m, h and d (``1h30m``).

    Raises:
        ValueError: if the value is negative or not a duration
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid duration: {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = value.strip().lower()
        try:
            seconds = float(text)
        except ValueError:
            if not text or _PART.sub("", text):
                raise ValueError(
                    f"invalid duration: {value!r}; use seconds or units s, m, h, d such as 15m or 1h30m"
                )
            seconds = sum(
                float(number) * _UNITS[unit] for number, unit in _PART.findall(text)
            )
    if not math.isfinite(seconds):
        raise ValueError(f"invalid duration: {value!r}; it must be finite")
    if seconds < 0:
        raise ValueError(f"invalid duration: {value!r}; it cannot be negative")
    return seconds


def format_duration(seconds: float) -> str:
    """Return a short form such as ``4m12s`` for logs and messages."""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"
