"""Injectable clock.

Time is a dependency, not a global.  Injecting it makes runs reproducible in
tests (``FrozenClock``) and lets the kernel compute honest durations.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Anything that can tell the current time."""

    def now(self) -> float:
        """Seconds since the epoch (wall clock)."""

    def monotonic(self) -> float:
        """Monotonic seconds -- never goes backwards, used for timeouts."""


class SystemClock:
    """Default clock backed by :mod:`time`."""

    __slots__ = ()

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "SystemClock()"


class FrozenClock:
    """Deterministic clock for tests: only advances when you say so."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._value = float(start)

    def now(self) -> float:
        return self._value

    def monotonic(self) -> float:
        return self._value

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._value += float(seconds)

    def set(self, value: float) -> None:
        self._value = float(value)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FrozenClock({self._value})"


DEFAULT_CLOCK = SystemClock()


__all__ = ["Clock", "SystemClock", "FrozenClock", "DEFAULT_CLOCK"]
