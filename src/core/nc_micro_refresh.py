from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RefreshDecision:
    allowed: bool
    reasons: tuple[str, ...]
    dt_since_last_ms: int | None
    blocked_by_interval: bool
    blocked_by_hash_stable: bool


def compute_refresh_allowed(
    *,
    now_ms: int,
    last_refresh_ms: int | None,
    last_force_refresh_ms: int | None,
    refresh_min_interval_ms: int,
    orders_age_ms: int | None,
    stale_ms: int,
    registry_dirty: bool,
    hash_changed: bool,
    stable_hash_cycles: int,
    stable_hash_limit: int,
    hard_max_age_ms: int,
) -> RefreshDecision:
    dt_since_last_ms = None if last_refresh_ms is None else max(now_ms - last_refresh_ms, 0)
    min_interval_ok = last_refresh_ms is None or (dt_since_last_ms or 0) >= refresh_min_interval_ms
    age_trigger = orders_age_ms is not None and orders_age_ms >= stale_ms
    dirty_trigger = registry_dirty
    hash_trigger = hash_changed
    hard_max_trigger = last_force_refresh_ms is None or (now_ms - last_force_refresh_ms) >= hard_max_age_ms
    reasons: list[str] = []
    if age_trigger:
        reasons.append("age")
    if dirty_trigger:
        reasons.append("dirty")
    if hash_trigger:
        reasons.append("hash")
    if hard_max_trigger:
        reasons.append("hard_max")
    if stable_hash_cycles >= stable_hash_limit and not hard_max_trigger:
        return RefreshDecision(
            allowed=False,
            reasons=tuple(reasons),
            dt_since_last_ms=dt_since_last_ms,
            blocked_by_interval=False,
            blocked_by_hash_stable=True,
        )
    allowed = (min_interval_ok and (age_trigger or dirty_trigger or hash_trigger)) or hard_max_trigger
    return RefreshDecision(
        allowed=allowed,
        reasons=tuple(reasons),
        dt_since_last_ms=dt_since_last_ms,
        blocked_by_interval=not min_interval_ok and not hard_max_trigger,
        blocked_by_hash_stable=False,
    )
