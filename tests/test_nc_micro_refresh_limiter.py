from __future__ import annotations

from math import ceil

from src.core.nc_micro_refresh import compute_refresh_allowed


def test_nc_micro_refresh_limiter_no_changes() -> None:
    now_ms = 0
    last_refresh_ms = None
    last_force_refresh_ms = None
    refresh_calls = 0
    stable_cycles = 0
    tick_interval_ms = 500
    ticks = 20
    for _ in range(ticks):
        decision = compute_refresh_allowed(
            now_ms=now_ms,
            last_refresh_ms=last_refresh_ms,
            last_force_refresh_ms=last_force_refresh_ms,
            refresh_min_interval_ms=3000,
            orders_age_ms=5000,
            stale_ms=2000,
            registry_dirty=False,
            hash_changed=False,
            stable_hash_cycles=stable_cycles,
            stable_hash_limit=3,
            hard_max_age_ms=15_000,
        )
        if decision.allowed:
            refresh_calls += 1
            last_refresh_ms = now_ms
            last_force_refresh_ms = now_ms
            stable_cycles = 0
        else:
            stable_cycles += 1
        now_ms += tick_interval_ms
    duration_ms = tick_interval_ms * ticks
    max_calls = ceil(duration_ms / 3000) + 1
    assert refresh_calls <= max_calls
