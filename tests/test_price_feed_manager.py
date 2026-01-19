from src.services.price_feed_manager import (
    WS_CONNECTED,
    WS_DEGRADED,
    WS_LOST,
    calculate_backoff,
    estimate_spread,
    resolve_ws_status,
)


def test_estimate_spread() -> None:
    spread_abs, spread_pct, mid = estimate_spread(100.0, 101.0)
    assert spread_abs == 1.0
    assert mid == 100.5
    assert round(spread_pct or 0.0, 6) == round((1.0 / 100.5) * 100, 6)


def test_heartbeat_status_transitions() -> None:
    assert resolve_ws_status(1000, degraded_after_ms=4000, lost_after_ms=12000) == WS_CONNECTED
    assert resolve_ws_status(4000, degraded_after_ms=4000, lost_after_ms=12000) == WS_DEGRADED
    assert resolve_ws_status(12000, degraded_after_ms=4000, lost_after_ms=12000) == WS_LOST


def test_backoff_calculator() -> None:
    assert calculate_backoff(0, base_s=1.0, max_s=30.0) == 1.0
    assert calculate_backoff(1, base_s=1.0, max_s=30.0) == 2.0
    assert calculate_backoff(4, base_s=1.0, max_s=30.0) == 16.0
    assert calculate_backoff(10, base_s=1.0, max_s=30.0) == 30.0
