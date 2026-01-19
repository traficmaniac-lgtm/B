from __future__ import annotations

import random
import time


def utc_ms() -> int:
    return int(time.time() * 1000)


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def is_expired(last_ms: int, ttl_ms: int, now_ms: int | None = None) -> bool:
    current = now_ms if now_ms is not None else monotonic_ms()
    return current - last_ms > ttl_ms


def backoff_delay(attempt: int, base_s: float = 0.5, max_s: float = 10.0) -> float:
    exponent = min(attempt, 10)
    delay = base_s * (2**exponent)
    jitter = random.uniform(0.8, 1.2)
    return min(delay * jitter, max_s)
