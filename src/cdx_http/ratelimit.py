"""Adaptive per-host token bucket."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    """Token bucket whose rate halves on every 429 and slowly recovers.

    After ``recovery_s`` seconds without a penalty, the rate grows by 25% per
    further ``recovery_s``, up to the configured ceiling.
    """

    def __init__(
        self,
        rate: float,
        burst: float = 1.0,
        min_rate: float = 0.05,
        recovery_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")
        self.max_rate = rate
        self.rate = rate
        self.burst = burst
        self.min_rate = min_rate
        self.recovery_s = recovery_s
        self._clock = clock
        self._sleep = sleep
        self._tokens = burst
        self._last = clock()
        self._last_change = self._last
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        if self.rate < self.max_rate and now - self._last_change >= self.recovery_s:
            steps = int((now - self._last_change) // self.recovery_s)
            self.rate = min(self.max_rate, self.rate * (1.25 ** steps))
            self._last_change += steps * self.recovery_s
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self) -> float:
        """Block until a token is available. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._refill(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                need = (1.0 - self._tokens) / self.rate
            self._sleep(need)
            waited += need

    def penalize(self) -> None:
        """Halve the rate (a 429 was received)."""
        with self._lock:
            now = self._clock()
            self._refill(now)
            self.rate = max(self.min_rate, self.rate / 2.0)
            self._tokens = min(self._tokens, 0.0)
            self._last_change = now
