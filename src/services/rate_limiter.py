from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}

    def allow(self, key: str, min_interval_s: float) -> bool:
        if min_interval_s <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            last = self._last_call.get(key)
            if last is not None and now - last < min_interval_s:
                return False
            self._last_call[key] = now
        return True
