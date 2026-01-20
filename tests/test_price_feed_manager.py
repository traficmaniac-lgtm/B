from src.core.config import Config
from src.services.price_feed_manager import (
    WS_CONNECTED,
    WS_DEGRADED,
    WS_LOST,
    PriceFeedManager,
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


def test_ws_state_machine_hysteresis() -> None:
    now_ms = 0

    def _now() -> int:
        return now_ms

    manager = PriceFeedManager(Config(), now_ms_fn=_now)
    symbol = "EURIUSDT"

    for tick in (0, 500, 900):
        now_ms = tick
        manager._handle_ws_message({"s": symbol, "b": "1.0", "a": "1.1", "E": tick})
    manager._heartbeat_symbol(symbol, now_ms)
    assert manager._symbol_state[symbol]["ws_status"] == WS_CONNECTED

    now_ms = 6000
    manager._heartbeat_symbol(symbol, now_ms)
    assert manager._symbol_state[symbol]["ws_status"] == WS_DEGRADED

    now_ms = 16000
    manager._heartbeat_symbol(symbol, now_ms)
    assert manager._symbol_state[symbol]["ws_status"] == WS_LOST
    assert manager._symbol_state[symbol]["http_fallback_enabled"] is True

    for tick in (16100, 16200, 16300):
        now_ms = tick
        manager._handle_ws_message({"s": symbol, "b": "1.0", "a": "1.1", "E": tick})
    manager._heartbeat_symbol(symbol, now_ms)
    assert manager._symbol_state[symbol]["ws_status"] == WS_CONNECTED
    assert manager._symbol_state[symbol]["http_fallback_enabled"] is True

    now_ms = 16300 + 3000
    manager._heartbeat_symbol(symbol, now_ms)
    assert manager._symbol_state[symbol]["http_fallback_enabled"] is False
