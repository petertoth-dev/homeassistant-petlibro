"""Reconnect delays for the live-updates connection."""

from __future__ import annotations

from .const import RECONNECT_MAX_SECONDS, RECONNECT_MIN_SECONDS


class ExponentialBackoff:
    """Delays that double after each failure up to a cap, and reset after a success."""

    def __init__(self, minimum: float = RECONNECT_MIN_SECONDS, maximum: float = RECONNECT_MAX_SECONDS) -> None:
        """Initialize with the first and the largest delay in seconds."""
        if minimum <= 0 or maximum < minimum:
            raise ValueError("Backoff needs 0 < minimum <= maximum")
        self._minimum = minimum
        self._maximum = maximum
        self._next = minimum

    def next_delay(self) -> float:
        """Return the delay before the next attempt and double the one after it."""
        delay = self._next
        self._next = min(self._next * 2, self._maximum)
        return delay

    def reset(self) -> None:
        """Start again from the shortest delay."""
        self._next = self._minimum
