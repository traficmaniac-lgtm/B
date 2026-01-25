from src.core.config import Config
import asyncio

from src.services.price_feed_manager import (
    WS_CONNECTED,
    WS_LOST,
    WS_RECOVERY_HOLD_MS,
    PriceUpdate,
    PriceFeedManager,
    _BinanceBookTickerWsThread,
    calculate_backoff,
    estimate_spread,
)


def test_estimate_spread() -> None:
    spread_abs, spread_pct, mid = estimate_spread(100.0, 101.0)
    assert spread_abs == 1.0
    assert mid == 100.5
    assert round(spread_pct or 0.0, 6) == round((1.0 / 100.5) * 100, 6)


def test_backoff_calculator() -> None:
    assert calculate_backoff(0) == 1.0
    assert calculate_backoff(1) == 1.0
    assert calculate_backoff(2) == 2.0
    assert calculate_backoff(4) == 10.0


def test_ws_state_machine_hysteresis() -> None:
    now_ms = 0

    def _now() -> int:
        return now_ms

    manager = PriceFeedManager(Config(), now_ms_fn=_now)
    symbol = "EURIUSDT"

    for tick in (0, 500, 900):
        now_ms = tick
        manager._handle_ws_message({"s": symbol, "b": "1.0", "a": "1.1", "E": tick})
    manager._heartbeat_symbol(symbol, now_ms, subscribed=True)
    assert manager._symbol_state[symbol]["ws_status"] == WS_CONNECTED

    now_ms = manager._router_config.ws_stale_ms + 1000
    manager._heartbeat_symbol(symbol, now_ms, subscribed=True)
    assert manager._symbol_state[symbol]["ws_status"] == WS_LOST

    now_ms = manager._router_config.ws_stale_ms + 11_000
    manager._heartbeat_symbol(symbol, now_ms, subscribed=True)
    assert manager._symbol_state[symbol]["ws_status"] == WS_LOST
    assert manager._symbol_state[symbol]["http_fallback_enabled"] is True

    for tick in (16100, 16200, 16300):
        now_ms = tick
        manager._handle_ws_message({"s": symbol, "b": "1.0", "a": "1.1", "E": tick})
    now_ms = 16300 + WS_RECOVERY_HOLD_MS + 100
    manager._heartbeat_symbol(symbol, now_ms, subscribed=True)
    assert manager._symbol_state[symbol]["ws_status"] == WS_CONNECTED
    assert manager._symbol_state[symbol]["http_fallback_enabled"] is False

    now_ms = 16300 + manager._router_config.ws_stale_ms + 1000
    manager._heartbeat_symbol(symbol, now_ms, subscribed=True)
    assert manager._symbol_state[symbol]["http_fallback_enabled"] is True


def test_ws_health_requires_subscription_and_fresh_age() -> None:
    now_ms = 0

    def _now() -> int:
        return now_ms

    manager = PriceFeedManager(Config(), now_ms_fn=_now)
    symbol = "EURIUSDT"
    manager.register_symbol(symbol)

    health = manager.get_ws_health(symbol)
    assert health is not None
    assert health.state == "NO"

    manager._set_ws_state("CONNECTED", "test")
    manager._ws_thread._subscribed_symbols = {symbol.lower()}
    now_ms = 1000
    manager._handle_ws_message({"s": symbol, "b": "1.0", "a": "1.1", "E": now_ms})
    now_ms = manager._router_config.ws_stale_ms + 1001
    manager._heartbeat_symbol(symbol, now_ms, subscribed=True)
    health = manager.get_ws_health(symbol)
    assert health is not None
    assert health.state != "OK"


def test_ws_thread_enqueue_command_loop_closed() -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    thread = _BinanceBookTickerWsThread(
        ws_url="wss://example.test",
        on_message=lambda _: None,
        on_status=lambda *_: None,
        now_ms_fn=lambda: 0,
    )
    thread._loop = loop
    thread._command_queue = asyncio.Queue()
    thread._stopping = True
    thread._enqueue_command({"type": "sync_symbols"})


def test_ws_thread_stop_skips_commands_after_stop() -> None:
    thread = _BinanceBookTickerWsThread(
        ws_url="wss://example.test",
        on_message=lambda _: None,
        on_status=lambda *_: None,
        now_ms_fn=lambda: 0,
    )
    thread.stop()
    thread.set_symbols(["BTCUSDT"])


def test_manager_register_ignored_after_shutdown() -> None:
    manager = PriceFeedManager(Config())
    manager.shutdown()
    manager.register_symbol("BTCUSDT")
    manager.unregister_symbol("BTCUSDT")


def test_subscribe_refcounts_registry() -> None:
    manager = PriceFeedManager(Config())
    seen: list[PriceUpdate] = []

    def _cb(update: PriceUpdate) -> None:
        seen.append(update)

    def _cb_two(update: PriceUpdate) -> None:
        seen.append(update)

    manager.subscribe("BTCUSDT", _cb)
    assert manager._registry.count("BTCUSDT") == 1
    manager.subscribe("BTCUSDT", _cb_two)
    assert manager._registry.count("BTCUSDT") == 2
    manager.unsubscribe("BTCUSDT", _cb)
    assert manager._registry.count("BTCUSDT") == 1
    manager.unsubscribe("BTCUSDT", _cb_two)
    assert manager._registry.count("BTCUSDT") == 0
