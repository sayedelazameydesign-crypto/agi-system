"""Tiny, dependency-free metrics recorder.

Counters + a few gauges are enough to answer "is the kernel healthy?".  The
recorder is thread-safe and can be flushed to JSON for scraping.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from time import monotonic
from typing import Any, Dict, Optional


class MetricsRecorder:
    """Thread-safe counters, gauges and duration stats."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: Dict[str, float] = {}
        self._gauges: Dict[str, float] = {}
        self._durations: Dict[str, Dict[str, float]] = {}
        self._started = monotonic()

    # -- recording --------------------------------------------------------- #
    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + value

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            stats = self._durations.setdefault(
                name, {"count": 0.0, "total": 0.0, "min": seconds, "max": seconds}
            )
            stats["count"] += 1
            stats["total"] += seconds
            stats["min"] = min(stats["min"], seconds)
            stats["max"] = max(stats["max"], seconds)

    @property
    def counters(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._counters)

    # -- reporting --------------------------------------------------------- #
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            durations = {
                name: {
                    "count": int(s["count"]),
                    "total_ms": round(s["total"] * 1000, 3),
                    "avg_ms": round((s["total"] / s["count"]) * 1000, 3) if s["count"] else 0.0,
                    "min_ms": round(s["min"] * 1000, 3),
                    "max_ms": round(s["max"] * 1000, 3),
                }
                for name, s in self._durations.items()
            }
            return {
                "uptime_s": round(monotonic() - self._started, 3),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "durations": durations,
            }

    def flush(self, path: Optional[Path | str]) -> Optional[Path]:
        """Write the snapshot as JSON; returns the path or ``None``."""
        if not path:
            return None
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.snapshot(), indent=2, sort_keys=True), encoding="utf-8")
        return target

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._durations.clear()
            self._started = monotonic()


__all__ = ["MetricsRecorder"]
