from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Any, Callable

import websockets

from src.binance.http_client import BinanceHttpClient
from src.core.config import Config
from src.core.logging import get_logger
from src.core.timeutil import utc_ms

WS_CONNECTED = "WS_CONNECTED"
WS_DEGRADED = "WS_DEGRADED"
WS_LOST = "WS_LOST"
WS_OK = "WS_OK"
HTTP_FALLBACK = "HTTP_FALLBACK"
WS_DISABLED_404 = "WS_DISABLED_404"


def _now_ms() -> int:
    return utc_ms()


def calculate_backoff(attempt: int, base_s: float = 1.0, max_s: float = 30.0) -> float:
    exponent = max(0, min(attempt, 10))
    return min(base_s * (2**exponent), max_s)


def resolve_ws_status(age_ms: int, degraded_after_ms: int, lost_after_ms: int) -> str:
    if age_ms >= lost_after_ms:
        return WS_LOST
    if age_ms >= degraded_after_ms:
        return WS_DEGRADED
    return WS_CONNECTED


def estimate_spread(best_bid: float | None, best_ask: float | None) -> tuple[float | None, float | None, float | None]:
    if best_bid is None or best_ask is None:
        return None, None, None
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None, None, None
    spread_abs = best_ask - best_bid
    mid_price = (best_ask + best_bid) / 2
    if mid_price <= 0:
        return spread_abs, None, mid_price
    spread_pct = (spread_abs / mid_price) * 100
    return spread_abs, spread_pct, mid_price


@dataclass(frozen=True)
class MicrostructureSnapshot:
    symbol: str
    last_price: float | None
    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    spread_abs: float | None
    spread_pct: float | None
    tick_size: float | None
    step_size: float | None
    price_age_ms: int | None
    ws_latency_ms: int | None
    source: str
    ws_status: str


@dataclass(frozen=True)
class PriceUpdate:
    symbol: str
    last_price: float | None
    best_bid: float | None
    best_ask: float | None
    exchange_timestamp_ms: int | None
    local_timestamp_ms: int
    latency_ms: int | None
    source: str
    ws_status: str
    price_age_ms: int | None
    microstructure: MicrostructureSnapshot


class _SubscriberWorker:
    def __init__(self, callback: Callable[[Any], None], name: str) -> None:
        self._callback = callback
        self._queue: Queue[Any] = Queue(maxsize=250)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._logger = get_logger(f"services.price_feed.{name}")

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    def publish(self, item: Any) -> None:
        try:
            self._queue.put_nowait(item)
        except Full:
            self._logger.debug("Subscriber queue full; dropping update")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                self._callback(item)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("Subscriber callback failed: %s", exc)


class SymbolSubscriptionRegistry:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def add(self, symbol: str) -> None:
        cleaned = symbol.strip().upper()
        if not cleaned:
            return
        with self._lock:
            self._counts[cleaned] = self._counts.get(cleaned, 0) + 1

    def remove(self, symbol: str) -> None:
        cleaned = symbol.strip().upper()
        if not cleaned:
            return
        with self._lock:
            if cleaned not in self._counts:
                return
            remaining = self._counts[cleaned] - 1
            if remaining <= 0:
                self._counts.pop(cleaned, None)
            else:
                self._counts[cleaned] = remaining

    def active_symbols(self) -> list[str]:
        with self._lock:
            return list(self._counts.keys())


class _BinanceBookTickerWsThread:
    def __init__(
        self,
        ws_url: str,
        on_message: Callable[[dict[str, Any]], None],
        on_status: Callable[[str, str], None],
    ) -> None:
        ws_url = ws_url.rstrip("/")
        if ws_url.endswith("/ws"):
            ws_url = ws_url[: -len("/ws")]
        self._ws_url = ws_url
        self._on_message = on_message
        self._on_status = on_status
        self._logger = get_logger("services.price_feed.ws")
        self._thread = threading.Thread(target=self._run_loop, name="price-feed-ws", daemon=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = threading.Event()
        self._symbols_lock = threading.Lock()
        self._symbols: set[str] = set()
        self._symbols_updated = threading.Event()
        self._websocket: websockets.WebSocketClientProtocol | None = None
        self._disabled_until_ms = 0
        self._logged_url = False

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=5.0)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("WS shutdown error: %s", exc)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def set_symbols(self, symbols: list[str]) -> None:
        cleaned = {symbol.strip().lower() for symbol in symbols if symbol.strip()}
        with self._symbols_lock:
            if cleaned == self._symbols:
                return
            self._symbols = cleaned
        self._symbols_updated.set()

    def set_disabled_until(self, disabled_until_ms: int) -> None:
        self._disabled_until_ms = disabled_until_ms

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.create_task(self._connect_loop())
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    async def _connect_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            await self._wait_if_disabled()
            symbols = self._get_symbols()
            if not symbols:
                await asyncio.sleep(1.0)
                continue
            url = self._build_url(symbols)
            if url is None:
                await asyncio.sleep(1.0)
                continue
            try:
                if not self._logged_url:
                    self._logger.debug("WS URL = %s", url)
                    self._logged_url = True
                self._logger.debug("WS подключение (%s символов)", len(symbols))
                self._on_status("CONNECTED", "WS подключен")
                await self._listen(url)
                attempt = 0
            except websockets.exceptions.InvalidStatusCode as exc:
                if exc.status_code == 404:
                    self._on_status(WS_DISABLED_404, "HTTP 404")
                    await self._wait_if_disabled()
                    continue
                self._on_status("ERROR", f"ошибка WS: {exc}")
                self._logger.error("WS error: %s", exc)
            except Exception as exc:  # noqa: BLE001
                self._on_status("ERROR", f"ошибка WS: {exc}")
                self._logger.error("WS error: %s", exc)
            if self._stop_event.is_set():
                break
            attempt += 1
            delay = calculate_backoff(attempt, base_s=1.0, max_s=30.0)
            self._logger.debug("WS переподключение через %.1fs", delay)
            self._on_status("RECONNECTING", f"backoff={delay:.1f}s")
            await asyncio.sleep(delay)

    async def _listen(self, url: str) -> None:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as websocket:
            self._websocket = websocket
            self._symbols_updated.clear()
            while not self._stop_event.is_set():
                if self._symbols_updated.is_set():
                    self._symbols_updated.clear()
                    self._logger.info("WS обновляет список символов")
                    break
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                self._handle_message(message)
        self._websocket = None

    def _handle_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return
        self._on_message(data)

    async def _shutdown(self) -> None:
        if self._websocket is not None:
            await self._websocket.close()

    def _build_url(self, symbols: list[str]) -> str | None:
        streams = [f"{symbol}@bookTicker" for symbol in symbols if symbol]
        if not streams:
            return None
        if len(streams) == 1:
            return f"{self._ws_url}/ws/{streams[0]}"
        joined = "/".join(streams)
        return f"{self._ws_url}/stream?streams={joined}"

    def _get_symbols(self) -> list[str]:
        with self._symbols_lock:
            return list(self._symbols)

    async def _wait_if_disabled(self) -> None:
        while not self._stop_event.is_set():
            now_ms = _now_ms()
            if now_ms >= self._disabled_until_ms:
                return
            sleep_s = max((self._disabled_until_ms - now_ms) / 1000.0, 0.5)
            await asyncio.sleep(min(sleep_s, 5.0))


class PriceFeedManager:
    _instance: PriceFeedManager | None = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        config: Config,
        http_client: BinanceHttpClient | None = None,
        heartbeat_interval_s: float = 2.0,
        degraded_after_ms: int = 5000,
        lost_after_ms: int = 8000,
    ) -> None:
        self._config = config
        self._logger = get_logger("services.price_feed.manager")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self._http_client = http_client or BinanceHttpClient(
            base_url=config.binance.base_url,
            timeout_s=config.http.timeout_s,
            retries=config.http.retries,
            backoff_base_s=config.http.backoff_base_s,
            backoff_max_s=config.http.backoff_max_s,
        )
        self._heartbeat_interval_s = heartbeat_interval_s
        self._degraded_after_ms = degraded_after_ms
        self._lost_after_ms = lost_after_ms
        self._registry = SymbolSubscriptionRegistry()
        self._lock = threading.Lock()
        self._symbol_state: dict[str, dict[str, Any]] = {}
        self._tick_subscribers: dict[str, dict[Callable[[PriceUpdate], None], _SubscriberWorker]] = {}
        self._status_subscribers: dict[str, dict[Callable[[str, str], None], _SubscriberWorker]] = {}
        self._stop_event = threading.Event()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, name="price-feed-monitor", daemon=True)
        self._exchange_info: dict[str, tuple[float | None, float | None]] = {}
        self._exchange_info_loaded = False
        self._exchange_info_loading = False
        self._exchange_info_lock = threading.Lock()
        self._ws_disabled_until_ms = 0
        self._switch_cooldown_until_ms = 0
        self._log_limiter: dict[tuple[str, str], int] = {}
        self._ws_thread = _BinanceBookTickerWsThread(
            ws_url=config.binance.ws_url.rstrip("/"),
            on_message=self._handle_ws_message,
            on_status=self._handle_ws_status,
        )

    @classmethod
    def get_instance(cls, config: Config) -> PriceFeedManager:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(config)
            return cls._instance

    def start(self) -> None:
        if self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._ws_thread.start()
        self._monitor_thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        self._ws_thread.stop()
        if self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5.0)
        with self._lock:
            tick_workers = [worker for sub in self._tick_subscribers.values() for worker in sub.values()]
            status_workers = [worker for sub in self._status_subscribers.values() for worker in sub.values()]
            self._tick_subscribers.clear()
            self._status_subscribers.clear()
        for worker in tick_workers + status_workers:
            worker.stop()

    def register_symbol(self, symbol: str) -> None:
        self._registry.add(symbol)
        self._ws_thread.set_symbols(self._registry.active_symbols())
        self._ensure_exchange_info_loaded()

    def unregister_symbol(self, symbol: str) -> None:
        self._registry.remove(symbol)
        self._ws_thread.set_symbols(self._registry.active_symbols())

    def subscribe(self, symbol: str, callback: Callable[[PriceUpdate], None]) -> None:
        cleaned = symbol.strip().upper()
        if not cleaned:
            return
        with self._lock:
            symbol_workers = self._tick_subscribers.setdefault(cleaned, {})
            if callback in symbol_workers:
                return
            worker = _SubscriberWorker(callback, name=f"tick-{cleaned}-{id(callback)}")
            symbol_workers[callback] = worker
            worker.start()

    def unsubscribe(self, symbol: str, callback: Callable[[PriceUpdate], None]) -> None:
        cleaned = symbol.strip().upper()
        if not cleaned:
            return
        with self._lock:
            symbol_workers = self._tick_subscribers.get(cleaned, {})
            worker = symbol_workers.pop(callback, None)
            if not symbol_workers:
                self._tick_subscribers.pop(cleaned, None)
        if worker:
            worker.stop()

    def subscribe_status(self, symbol: str, callback: Callable[[str, str], None]) -> None:
        cleaned = symbol.strip().upper()
        if not cleaned:
            return
        with self._lock:
            symbol_workers = self._status_subscribers.setdefault(cleaned, {})
            if callback in symbol_workers:
                return

            def _wrapped(item: tuple[str, str]) -> None:
                status, details = item
                callback(status, details)

            worker = _SubscriberWorker(_wrapped, name=f"status-{cleaned}-{id(callback)}")
            symbol_workers[callback] = worker
            worker.start()

    def unsubscribe_status(self, symbol: str, callback: Callable[[str, str], None]) -> None:
        cleaned = symbol.strip().upper()
        if not cleaned:
            return
        with self._lock:
            symbol_workers = self._status_subscribers.get(cleaned, {})
            worker = symbol_workers.pop(callback, None)
            if not symbol_workers:
                self._status_subscribers.pop(cleaned, None)
        if worker:
            worker.stop()

    def get_snapshot(self, symbol: str) -> MicrostructureSnapshot | None:
        cleaned = symbol.strip().upper()
        if not cleaned:
            return None
        with self._lock:
            state = self._symbol_state.get(cleaned)
        if not state:
            return None
        return self._build_snapshot(cleaned, state)

    def _log_rate_limited(self, symbol: str, kind: str, level: str, message: str) -> None:
        now_ms = _now_ms()
        key = (symbol, kind)
        last_ms = self._log_limiter.get(key, 0)
        if now_ms - last_ms < 10_000:
            return
        self._log_limiter[key] = now_ms
        getattr(self._logger, level)(message)

    def _can_switch(self, now_ms: int) -> bool:
        return now_ms >= self._switch_cooldown_until_ms

    def _set_switch_cooldown(self, now_ms: int) -> None:
        self._switch_cooldown_until_ms = now_ms + 15_000

    def _should_restore_ws(self, state: dict[str, Any], now_ms: int) -> bool:
        streak = state.get("ws_streak", 0)
        recovery_start = state.get("ws_recovery_start_ms")
        if streak >= 3:
            return True
        if isinstance(recovery_start, int) and now_ms - recovery_start >= 2000:
            return True
        return False

    def _get_poll_interval_ms(self, symbol: str) -> int:
        with self._lock:
            tick_count = len(self._tick_subscribers.get(symbol, {}))
            status_count = len(self._status_subscribers.get(symbol, {}))
        if tick_count > 0:
            return 1000
        if status_count > 0:
            return 4000
        return 0

    def _handle_ws_status(self, status: str, details: str) -> None:
        now_ms = _now_ms()
        if status == WS_DISABLED_404:
            self._ws_disabled_until_ms = now_ms + 300_000
            self._ws_thread.set_disabled_until(self._ws_disabled_until_ms)
            self._log_rate_limited("*", "ws_disabled_404", "warning", "WS отключен из-за 404 на 5 минут")
            symbols_to_notify: list[str] = []
            with self._lock:
                for symbol, symbol_state in self._symbol_state.items():
                    symbol_state["feed_state"] = WS_DISABLED_404
                    symbol_state["ws_status"] = WS_LOST
                    symbols_to_notify.append(symbol)
            for symbol in symbols_to_notify:
                self._publish_status(symbol, WS_LOST)
            return
        if status == "CONNECTED":
            self._log_rate_limited("*", "ws_connected", "info", "WS подключен")
            return
        if status == "RECONNECTING":
            self._log_rate_limited("*", "ws_reconnect", "warning", f"WS переподключение: {details}")
            return
        if status == "ERROR":
            self._log_rate_limited("*", "ws_error", "error", f"WS ошибка: {details}")

    def _handle_ws_message(self, data: dict[str, Any]) -> None:
        symbol = data.get("s")
        if not isinstance(symbol, str):
            return
        best_bid_raw = data.get("b")
        best_ask_raw = data.get("a")
        event_ts = data.get("E")
        try:
            best_bid = float(best_bid_raw) if isinstance(best_bid_raw, str) else None
            best_ask = float(best_ask_raw) if isinstance(best_ask_raw, str) else None
        except ValueError:
            return
        if not isinstance(event_ts, int):
            event_ts = None
        symbol = symbol.upper()
        now_ms = _now_ms()
        latency_ms = max(now_ms - event_ts, 0) if event_ts else None
        spread_abs, spread_pct, mid_price = estimate_spread(best_bid, best_ask)
        last_price = mid_price
        with self._lock:
            state = self._symbol_state.setdefault(symbol, {})
            last_update = state.get("updated_ms")
            if state.get("ws_status") != WS_CONNECTED:
                if isinstance(last_update, int) and now_ms - last_update <= 2000:
                    state["ws_streak"] = state.get("ws_streak", 0) + 1
                else:
                    state["ws_streak"] = 1
                if state.get("ws_recovery_start_ms") is None:
                    state["ws_recovery_start_ms"] = now_ms
            else:
                state["ws_streak"] = 0
                state["ws_recovery_start_ms"] = None
            state.update(
                {
                    "last_price": last_price,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "event_ts": event_ts,
                    "updated_ms": now_ms,
                    "latency_ms": latency_ms,
                    "source": "WS",
                    "spread_abs": spread_abs,
                    "spread_pct": spread_pct,
                    "mid_price": mid_price,
                }
            )
        self._publish_update(symbol)

    def _monitor_loop(self) -> None:
        self._logger.info("Price feed монитор запущен")
        while not self._stop_event.is_set():
            active_symbols = self._registry.active_symbols()
            if not active_symbols:
                time.sleep(self._heartbeat_interval_s)
                continue
            now_ms = utc_ms()
            for symbol in active_symbols:
                self._heartbeat_symbol(symbol, now_ms)
                self._maybe_poll_http(symbol, now_ms)
            time.sleep(self._heartbeat_interval_s)
        self._logger.info("Price feed монитор остановлен")

    def _heartbeat_symbol(self, symbol: str, now_ms: int) -> None:
        cleaned = symbol.strip().upper()
        if not cleaned:
            return
        with self._lock:
            state = self._symbol_state.get(cleaned)
            last_update = state.get("updated_ms") if state else None
            current_status = state.get("ws_status") if state else WS_LOST
            feed_state = state.get("feed_state", WS_OK) if state else WS_OK
        if last_update is None:
            new_status = WS_LOST
        else:
            age_ms = max(now_ms - last_update, 0)
            new_status = resolve_ws_status(age_ms, self._degraded_after_ms, self._lost_after_ms)
        if new_status == WS_CONNECTED and current_status != WS_CONNECTED:
            with self._lock:
                state = self._symbol_state.setdefault(cleaned, {})
                if not self._should_restore_ws(state, now_ms):
                    new_status = current_status
        if new_status != current_status:
            with self._lock:
                state = self._symbol_state.setdefault(cleaned, {})
                state["ws_status"] = new_status
                if new_status != WS_CONNECTED:
                    state["ws_streak"] = 0
                    state["ws_recovery_start_ms"] = None
            if new_status == WS_DEGRADED:
                self._log_rate_limited(cleaned, "ws_degraded", "warning", f"WS деградирован для {cleaned}")
            elif new_status == WS_LOST:
                self._log_rate_limited(cleaned, "ws_lost", "warning", f"WS потерян для {cleaned}")
            elif new_status == WS_CONNECTED:
                self._log_rate_limited(cleaned, "ws_restored", "info", f"WS восстановлен для {cleaned}")
            self._publish_status(cleaned, new_status)

        desired_feed_state = feed_state
        if now_ms < self._ws_disabled_until_ms:
            desired_feed_state = WS_DISABLED_404
        elif new_status == WS_LOST:
            desired_feed_state = HTTP_FALLBACK
        elif new_status == WS_DEGRADED:
            desired_feed_state = WS_DEGRADED
        else:
            desired_feed_state = WS_OK

        if desired_feed_state != feed_state:
            allow_switch = True
            if desired_feed_state != WS_DISABLED_404 and {feed_state, desired_feed_state} <= {WS_OK, HTTP_FALLBACK}:
                allow_switch = self._can_switch(now_ms)
            if allow_switch:
                with self._lock:
                    state = self._symbol_state.setdefault(cleaned, {})
                    state["feed_state"] = desired_feed_state
                if {feed_state, desired_feed_state} <= {WS_OK, HTTP_FALLBACK}:
                    self._set_switch_cooldown(now_ms)
                if desired_feed_state == HTTP_FALLBACK:
                    self._log_rate_limited(cleaned, "http_fallback_on", "warning", f"HTTP фолбэк включен для {cleaned}")
                elif desired_feed_state == WS_OK:
                    self._log_rate_limited(cleaned, "http_fallback_off", "info", f"HTTP фолбэк выключен для {cleaned}")
                elif desired_feed_state == WS_DISABLED_404:
                    self._log_rate_limited(cleaned, "ws_disabled", "warning", f"WS отключен (404) для {cleaned}")

    def _maybe_poll_http(self, symbol: str, now_ms: int) -> None:
        cleaned = symbol.strip().upper()
        if not cleaned:
            return
        with self._lock:
            state = self._symbol_state.setdefault(cleaned, {})
            feed_state = state.get("feed_state", WS_OK)
            last_poll = state.get("http_poll_ms", 0)
        if feed_state not in {HTTP_FALLBACK, WS_DISABLED_404}:
            return
        poll_interval_ms = self._get_poll_interval_ms(cleaned)
        if poll_interval_ms <= 0:
            return
        if now_ms - last_poll < poll_interval_ms:
            return
        try:
            price_raw = self._http_client.get_ticker_price(cleaned)
            price = float(price_raw)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("HTTP фолбэк ошибка для %s: %s", cleaned, exc)
            with self._lock:
                state["http_poll_ms"] = now_ms
            return
        with self._lock:
            state.update(
                {
                    "last_price": price,
                    "event_ts": None,
                    "updated_ms": now_ms,
                    "latency_ms": None,
                    "source": "HTTP",
                    "best_bid": None,
                    "best_ask": None,
                    "spread_abs": None,
                    "spread_pct": None,
                    "mid_price": None,
                    "http_poll_ms": now_ms,
                }
            )
        self._publish_update(cleaned)

    def _publish_update(self, symbol: str) -> None:
        update = self._build_update(symbol)
        if update is None:
            return
        with self._lock:
            subscribers = list(self._tick_subscribers.get(symbol, {}).values())
        for worker in subscribers:
            worker.publish(update)

    def _publish_status(self, symbol: str, status: str) -> None:
        with self._lock:
            subscribers = list(self._status_subscribers.get(symbol, {}).values())
        for worker in subscribers:
            worker.publish((status, status))

    def _build_update(self, symbol: str) -> PriceUpdate | None:
        with self._lock:
            state = self._symbol_state.get(symbol)
        if not state:
            return None
        snapshot = self._build_snapshot(symbol, state)
        return PriceUpdate(
            symbol=symbol,
            last_price=snapshot.last_price,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            exchange_timestamp_ms=state.get("event_ts"),
            local_timestamp_ms=state.get("updated_ms") or utc_ms(),
            latency_ms=snapshot.ws_latency_ms,
            source=snapshot.source,
            ws_status=snapshot.ws_status,
            price_age_ms=snapshot.price_age_ms,
            microstructure=snapshot,
        )

    def _build_snapshot(self, symbol: str, state: dict[str, Any]) -> MicrostructureSnapshot:
        last_price = state.get("last_price")
        best_bid = state.get("best_bid")
        best_ask = state.get("best_ask")
        tick_size, step_size = self._exchange_info.get(symbol, (None, None))
        spread_abs = state.get("spread_abs")
        spread_pct = state.get("spread_pct")
        mid_price = state.get("mid_price")
        updated_ms = state.get("updated_ms")
        ws_latency_ms = state.get("latency_ms")
        source = state.get("source", "WS")
        ws_status = state.get("ws_status", WS_LOST)
        price_age_ms = None
        if isinstance(updated_ms, int):
            price_age_ms = max(utc_ms() - updated_ms, 0)
        return MicrostructureSnapshot(
            symbol=symbol,
            last_price=last_price,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid_price,
            spread_abs=spread_abs,
            spread_pct=spread_pct,
            tick_size=tick_size,
            step_size=step_size,
            price_age_ms=price_age_ms,
            ws_latency_ms=ws_latency_ms,
            source=source,
            ws_status=ws_status,
        )

    def _ensure_exchange_info_loaded(self) -> None:
        with self._exchange_info_lock:
            if self._exchange_info_loaded or self._exchange_info_loading:
                return
            self._exchange_info_loading = True
        thread = threading.Thread(target=self._load_exchange_info, name="price-feed-exchange-info", daemon=True)
        thread.start()

    def _load_exchange_info(self) -> None:
        try:
            data = self._http_client.get_exchange_info()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Не удалось загрузить exchange info: %s", exc)
            with self._exchange_info_lock:
                self._exchange_info_loading = False
            return
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        mapping: dict[str, tuple[float | None, float | None]] = {}
        for item in symbols:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            filters = item.get("filters", [])
            tick_size = None
            step_size = None
            if isinstance(filters, list):
                for entry in filters:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("filterType") == "PRICE_FILTER":
                        tick_raw = entry.get("tickSize")
                        try:
                            tick_size = float(tick_raw)
                        except (TypeError, ValueError):
                            tick_size = None
                    if entry.get("filterType") == "LOT_SIZE":
                        step_raw = entry.get("stepSize")
                        try:
                            step_size = float(step_raw)
                        except (TypeError, ValueError):
                            step_size = None
            mapping[symbol] = (tick_size, step_size)
        with self._lock:
            self._exchange_info = mapping
        with self._exchange_info_lock:
            self._exchange_info_loaded = True
            self._exchange_info_loading = False
        self._logger.info("Exchange info загружен (%s symbols)", len(mapping))
