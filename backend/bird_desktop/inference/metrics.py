from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import threading
import time


class InferenceMetrics:
    """Small, thread-safe stage totals; overlapping stages must not be summed."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seconds = defaultdict(float)
        self._counts = defaultdict(int)

    @contextmanager
    def measure(self, stage: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            with self._lock:
                self._seconds[stage] += time.perf_counter() - start
                self._counts[stage] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {name: {"seconds": value, "calls": self._counts[name]}
                    for name, value in self._seconds.items()}
