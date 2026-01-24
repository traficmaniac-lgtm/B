from __future__ import annotations

import threading

from src.core.config import Config
from src.services.price_feed_manager import (
    ROUTER_MIN_HOLD_MS,
    WS_RECOVERY_HOLD_MS,
    RouterConfig,
    SymbolSubscriptionRegistry,
    WS_ROUTER_NO_FIRST_TICK,
    WS_ROUTER_OK,
    WS_ROUTER_STALE,
    WS_ROUTER_WARMUP,
    PriceFeedManager,
    decide_price_source,
)


def test_warmup_no_tick_no_fallback() -> None:
    config = RouterConfig(warmup_ms=3000, ws_stale_ms=2000)
    source, status, reason = decide_price_source(
        "BTCUSDT",
        now_ms=1000,
        ws_last_tick_ms=None,
        grace_until_ms=2000,
        http_age_ms=None,
        config=config,
    )
    assert source == "WS"
    assert status == WS_ROUTER_WARMUP
    assert "warmup" in reason


def test_after_grace_no_first_tick_fallback() -> None:
    config = RouterConfig(warmup_ms=3000, ws_stale_ms=2000)
    source, status, reason = decide_price_source(
        "BTCUSDT",
        now_ms=3001,
        ws_last_tick_ms=None,
        grace_until_ms=2000,
        http_age_ms=None,
        config=config,
    )
    assert source == "HTTP"
    assert status == WS_ROUTER_NO_FIRST_TICK
    assert "no_first_tick" in reason


def test_tick_recent_prefer_ws() -> None:
    config = RouterConfig(warmup_ms=3000, ws_stale_ms=2000)
    source, status, reason = decide_price_source(
        "BTCUSDT",
        now_ms=5000,
        ws_last_tick_ms=4900,
        grace_until_ms=2000,
        http_age_ms=None,
        config=config,
    )
    assert source == "WS"
    assert status == WS_ROUTER_OK
    assert "recent" in reason


def test_tick_stale_fallback_http() -> None:
    config = RouterConfig(warmup_ms=3000, ws_stale_ms=2000)
    source, status, reason = decide_price_source(
        "BTCUSDT",
        now_ms=5000,
        ws_last_tick_ms=0,
        grace_until_ms=2000,
        http_age_ms=None,
        config=config,
    )
    assert source == "HTTP"
    assert status == WS_ROUTER_STALE
    assert "stale" in reason


def test_transport_test_does_not_unsubscribe_active() -> None:
    registry = SymbolSubscriptionRegistry()
    assert registry.add("BTCUSDT") is True
    assert registry.add("BTCUSDT") is False
    removed = registry.remove("BTCUSDT")
    assert removed is False
    assert registry.count("BTCUSDT") == 1
    removed = registry.remove("BTCUSDT")
    assert removed is True
    assert registry.count("BTCUSDT") == 0


def test_transport_test_waits_first_tick() -> None:
    now_ms = 1000

    def _now() -> int:
        return now_ms

    manager = PriceFeedManager(Config(), now_ms_fn=_now)
    manager._transport_test_enabled = True
    symbol = "BTCUSDT"
    manager.register_symbol(symbol)

    def _emit_tick() -> None:
        manager._handle_ws_message({"s": symbol, "b": "1.0", "a": "1.1", "E": now_ms})

    thread = threading.Thread(target=_emit_tick)
    thread.start()
    report = manager.run_transport_test([symbol])
    thread.join(timeout=1.0)

    assert report
    entry = report[0]
    assert entry["ws_status"] == WS_ROUTER_OK


def test_router_hysteresis_min_hold() -> None:
    now_ms = 10_000

    def _now() -> int:
        return now_ms

    manager = PriceFeedManager(Config(), now_ms_fn=_now)
    symbol = "BTCUSDT"
    manager.register_symbol(symbol)
    with manager._lock:
        state = manager._symbol_state[symbol]
        router = state["router"]
        router.active_source = "HTTP"
        router.last_switch_ts = now_ms
        router.ws_last_tick_ts = now_ms
        router.ws_recovery_start_ts = now_ms - WS_RECOVERY_HOLD_MS - 1

    now_ms += ROUTER_MIN_HOLD_MS - 500
    manager._update_router_state(
        symbol,
        now_ms,
        subscribed=True,
        ws_allowed=True,
        trigger="heartbeat",
    )
    with manager._lock:
        assert state["router"].active_source == "HTTP"

    now_ms += 1000
    manager._update_router_state(
        symbol,
        now_ms,
        subscribed=True,
        ws_allowed=True,
        trigger="heartbeat",
    )
    with manager._lock:
        assert state["router"].active_source == "WS"
