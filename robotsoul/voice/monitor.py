"""Short amplitude envelopes for the Dashboard audio monitor."""

from __future__ import annotations

import math
from collections import deque
from threading import Lock
from time import monotonic
from typing import Sequence


class AudioLevelMonitor:
    """Keeps a few seconds of min/max levels. It does not store raw audio."""

    def __init__(self, *, buckets: int = 240, window_seconds: float = 0.02) -> None:
        if buckets < 1:
            raise ValueError("buckets must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = window_seconds
        self._input: deque[dict[str, float]] = deque(maxlen=buckets)
        self._output: deque[dict[str, float]] = deque(maxlen=buckets)
        self._input_at = 0.0
        self._output_at = 0.0
        self._lock = Lock()

    def push_input(self, samples: Sequence[float], sample_rate: int) -> None:
        self._push(self._input, samples, sample_rate, input_side=True)

    def push_output(self, samples: Sequence[float], sample_rate: int) -> None:
        self._push(self._output, samples, sample_rate, input_side=False)

    def snapshot(self) -> dict[str, object]:
        now = monotonic()
        with self._lock:
            return {
                "input": list(self._input),
                "output": list(self._output),
                "input_active": now - self._input_at < 2.0,
                "output_active": now - self._output_at < 2.0,
            }

    def _push(self, target: deque[dict[str, float]], samples: Sequence[float], sample_rate: int, *, input_side: bool) -> None:
        points = _envelope(samples, sample_rate, self.window_seconds)
        if not points:
            return
        now = monotonic()
        with self._lock:
            target.extend(points)
            if input_side:
                self._input_at = now
            else:
                self._output_at = now


def _envelope(samples: Sequence[float], sample_rate: int, window_seconds: float) -> list[dict[str, float]]:
    if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or sample_rate <= 0:
        return []
    window = max(1, int(sample_rate * window_seconds))
    points: list[dict[str, float]] = []
    index = 0
    total = len(samples)
    while index < total:
        low = 0.0
        high = 0.0
        seen = False
        end = min(total, index + window)
        for value in samples[index:end]:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            number = max(-1.0, min(1.0, number))
            if not seen:
                low = high = number
                seen = True
            else:
                low = min(low, number)
                high = max(high, number)
        if seen:
            points.append({"min": low, "max": high})
        index = end
    return points
