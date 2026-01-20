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
from src.core.symbols import sanitize_symbol, validate_trade_symbol
from src.core.timeutil import utc_ms

WS_CONNECTED = "WS_CONNECTED"
WS_DEGRADED = "WS_DEGRADED"
WS_LOST = "WS_LOST"
WS_DISABLED_404 = "WS_DISABLED_404"
WS_HEALTH_OK = "OK"
WS_HEALTH_DEGRADED = "DEGRADED"
WS_HEALTH_DEAD = "DEAD"

WS_GRACE_MS = 4000
WS_DEGRADED_AFTER_MS = 5000
WS_LOST_AFTER_MS = 15000
WS_RECOVER_AFTER_N = 3
HTTP_DISABLE_GRACE_MS = 3000
INVALID_SYMBOL_LOG_MS = 60_000
SYMBOL_UPDATE_DEBOUNCE_MS = 400


def calculate_backoff(attempt: int) -> float:
    if attempt <= 1:
        return 1.0
    if attempt == 2:
        return 2.0
    if attempt == 3:
        return 5.0
    return 10.0


def resolve_health_state(status: str) -> str:
    if status == WS_CONNECTED:
        return WS_HEALTH_OK
    if status == WS_DEGRADED:
        return WS_HEALTH_DEGRADED
    return WS_HEALTH_DEAD


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
    ws_health_state: str


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

    def add(self, symbol: str) -> bool:
        cleaned = sanitize_symbol(symbol)
        if cleaned is None:
            return False
        with self._lock:
            self._counts[cleaned] = self._counts.get(cleaned, 0) + 1
            return self._counts[cleaned] == 1

    def remove(self, symbol: str) -> bool:
        cleaned = sanitize_symbol(symbol)
        if cleaned is None:
            return False
        with self._lock:
            if cleaned not in self._counts:
                return False
            remaining = self._counts[cleaned] - 1
            if remaining <= 0:
                self._counts.pop(cleaned, None)
                return True
            else:
                self._counts[cleaned] = remaining
                return False

    def active_symbols(self) -> list[str]:
        with self._lock:
            return list(self._counts.keys())


class _BinanceBookTickerWsThread:
    def __init__(
        self,
        ws_url: str,
        on_message: Callable[[dict[str, Any]], None],
        on_status: Callable[[str, str], None],
        now_ms_fn: Callable[[], int],
        debounce_ms: int = 700,
        min_reconnect_interval_ms: int = 5000,
        heartbeat_interval_s: float = 10.0,
        heartbeat_timeout_s: float = 5.0,
        dead_after_ms: int = WS_LOST_AFTER_MS,
    ) -> None:
        ws_url = ws_url.rstrip("/")
        if ws_url.endswith("/ws"):
            ws_url = ws_url[: -len("/ws")]
        self._ws_url = ws_url
        self._on_message = on_message
        self._on_status = on_status
        self._now_ms = now_ms_fn
        self._logger = get_logger("services.price_feed.ws")
        self._thread = threading.Thread(target=self._run_loop, name="price-feed-ws", daemon=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = threading.Event()
        self._symbols_lock = threading.Lock()
        self._symbols: set[str] = set()
        self._symbols_updated = threading.Event()
        self._symbols_updated_ms: int | None = None
        self._websocket: websockets.WebSocketClientProtocol | None = None
        self._disabled_until_ms = 0
        self._logged_url = False
        self._debounce_ms = debounce_ms
        self._min_reconnect_interval_ms = min_reconnect_interval_ms
        self._heartbeat_interval_s = heartbeat_interval_s
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._dead_after_ms = dead_after_ms
        self._last_connect_ms = 0
        self._generation = 0
        self._last_message_ms: int | None = None
        self._closing = False
        self._close_lock = threading.Lock()

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
                self._logger.warning("WS shutdown error", exc_info=True)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def set_symbols(self, symbols: list[str]) -> None:
        cleaned = {symbol.strip().lower() for symbol in symbols if symbol.strip()}
        with self._symbols_lock:
            if cleaned == self._symbols:
                return
            self._symbols = cleaned
            self._symbols_updated_ms = self._now_ms()
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
            await self._wait_for_debounce()
            await self._wait_for_reconnect_window()
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
                self._generation += 1
                self._last_connect_ms = self._now_ms()
                await self._listen(url, self._generation)
                attempt = 0
            except websockets.exceptions.InvalidStatusCode as exc:
                if exc.status_code == 404:
                    self._on_status(WS_DISABLED_404, "HTTP 404")
                    await self._wait_if_disabled()
                    continue
                self._on_status("ERROR", f"ошибка WS: {exc}")
            except Exception as exc:  # noqa: BLE001
                self._on_status("ERROR", f"ошибка WS: {exc}")
            if self._stop_event.is_set():
                break
            attempt += 1
            delay = calculate_backoff(attempt)
            self._logger.debug("WS переподключение через %.1fs", delay)
            self._on_status("RECONNECTING", f"backoff={delay:.1f}s")
            await asyncio.sleep(delay)

    async def _listen(self, url: str, generation: int) -> None:
        async with websockets.connect(url, ping_interval=None, ping_timeout=None) as websocket:
            self._websocket = websocket
            with self._close_lock:
                self._closing = False
            self._symbols_updated.clear()
            self._last_message_ms = self._now_ms()
            next_ping_ms = self._last_message_ms + int(self._heartbeat_interval_s * 1000)
            while not self._stop_event.is_set():
                if generation != self._generation:
                    break
                if self._symbols_updated.is_set():
                    if await self._should_reconnect_for_update():
                        self._symbols_updated.clear()
                        self._logger.debug("WS обновляет список символов")
                        break
                now_ms = self._now_ms()
                if self._last_message_ms is not None and now_ms - self._last_message_ms > self._dead_after_ms:
                    self._on_status("ERROR", f"heartbeat: no messages > {self._dead_after_ms}ms")
                    break
                if now_ms >= next_ping_ms:
                    try:
                        pong_waiter = websocket.ping()
                        await asyncio.wait_for(pong_waiter, timeout=self._heartbeat_timeout_s)
                    except Exception as exc:  # noqa: BLE001
                        self._on_status("ERROR", f"heartbeat: ping timeout {exc}")
                        break
                    next_ping_ms = now_ms + int(self._heartbeat_interval_s * 1000)
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                self._last_message_ms = self._now_ms()
                self._handle_message(message)
        self._websocket = None

    def _handle_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if isinstance(data, dict):
            self._on_message(data)
            return
        self._on_message(payload)

    async def _shutdown(self) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        with self._close_lock:
            if self._closing:
                return
            self._closing = True
        if websocket.closed:
            self._websocket = None
            return
        await websocket.close()
        self._websocket = None

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

    async def _wait_for_debounce(self) -> None:
        while not self._stop_event.is_set():
            updated_ms = self._symbols_updated_ms
            if updated_ms is None:
                return
            now_ms = self._now_ms()
            elapsed = now_ms - updated_ms
            if elapsed >= self._debounce_ms:
                return
            await asyncio.sleep(min((self._debounce_ms - elapsed) / 1000.0, 0.5))

    async def _wait_for_reconnect_window(self) -> None:
        while not self._stop_event.is_set():
            now_ms = self._now_ms()
            elapsed = now_ms - self._last_connect_ms
            if elapsed >= self._min_reconnect_interval_ms:
                return
            await asyncio.sleep(min((self._min_reconnect_interval_ms - elapsed) / 1000.0, 0.5))

    async def _should_reconnect_for_update(self) -> bool:
        updated_ms = self._symbols_updated_ms
        if updated_ms is None:
            return True
        now_ms = self._now_ms()
        if now_ms - updated_ms < self._debounce_ms:
            return False
        if now_ms - self._last_connect_ms < self._min_reconnect_interval_ms:
            return False
        return True

    async def _wait_if_disabled(self) -> None:
        while not self._stop_event.is_set():
            now_ms = self._now_ms()
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
        degraded_after_ms: int = WS_DEGRADED_AFTER_MS,
        lost_after_ms: int = WS_LOST_AFTER_MS,
        recover_after_n: int = WS_RECOVER_AFTER_N,
        http_disable_grace_ms: int = HTTP_DISABLE_GRACE_MS,
        now_ms_fn: Callable[[], int] = utc_ms,
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
        self._recover_after_n = recover_after_n
        self._http_disable_grace_ms = http_disable_grace_ms
        self._now_ms = now_ms_fn
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
        self._log_limiter: dict[tuple[str, str], int] = {}
        self._invalid_symbols_logged: dict[str, int] = {}
        self._exchange_symbols: set[str] = set()
        self._symbols_update_timer: threading.Timer | None = None
        self._symbols_update_lock = threading.Lock()
        self._ws_thread = _BinanceBookTickerWsThread(
            ws_url=config.binance.ws_url.rstrip("/"),
            on_message=self._handle_ws_message,
            on_status=self._handle_ws_status,
            now_ms_fn=self._now_ms,
            heartbeat_interval_s=max(self._heartbeat_interval_s * 2, 4.0),
            heartbeat_timeout_s=5.0,
            dead_after_ms=self._lost_after_ms,
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
        with self._symbols_update_lock:
            if self._symbols_update_timer and self._symbols_update_timer.is_alive():
                self._symbols_update_timer.cancel()
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
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return
        is_new = self._registry.add(cleaned)
        if is_new:
            self._logger.info("subscribe %s", cleaned)
            now_ms = self._now_ms()
            with self._lock:
                state = self._symbol_state.setdefault(cleaned, {})
                state.update(
                    {
                        "subscribed_ms": now_ms,
                        "ws_grace_until_ms": now_ms + WS_GRACE_MS,
                        "ws_status": WS_CONNECTED,
                        "ws_health_state": resolve_health_state(WS_CONNECTED),
                        "ws_recover_streak": 0,
                        "last_ws_msg_ms": None,
                        "last_ws_price_ms": None,
                        "http_fallback_enabled": False,
                        "http_fallback_reason": None,
                        "http_disable_grace_until_ms": None,
                    }
                )
        self._schedule_symbol_update()
        self._ensure_exchange_info_loaded()

    def unregister_symbol(self, symbol: str) -> None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return
        removed = self._registry.remove(cleaned)
        if removed:
            self._logger.info("unsubscribe %s", cleaned)
        self._schedule_symbol_update()

    def get_ws_overall_status(self) -> str:
        now_ms = self._now_ms()
        if now_ms < self._ws_disabled_until_ms:
            return WS_LOST
        active_symbols = self._registry.active_symbols()
        if not active_symbols:
            return WS_LOST
        best_status = WS_LOST
        with self._lock:
            states = {symbol: self._symbol_state.get(symbol, {}) for symbol in active_symbols}
        for symbol in active_symbols:
            state = states.get(symbol, {})
            last_ws_msg = state.get("last_ws_msg_ms")
            subscribed_ms = state.get("subscribed_ms", now_ms)
            ws_grace_until = state.get("ws_grace_until_ms")
            if isinstance(last_ws_msg, int):
                age_ms = max(now_ms - last_ws_msg, 0)
            else:
                age_ms = max(now_ms - subscribed_ms, 0)
            if ws_grace_until is not None and now_ms < ws_grace_until:
                age_ms = 0
            status = resolve_ws_status(age_ms, self._degraded_after_ms, self._lost_after_ms)
            if status == WS_CONNECTED:
                return WS_CONNECTED
            if status == WS_DEGRADED:
                best_status = WS_DEGRADED
        return best_status

    def _schedule_symbol_update(self) -> None:
        def _flush() -> None:
            symbols = self._registry.active_symbols()
            self._ws_thread.set_symbols(symbols)

        with self._symbols_update_lock:
            if self._symbols_update_timer and self._symbols_update_timer.is_alive():
                self._symbols_update_timer.cancel()
            self._symbols_update_timer = threading.Timer(SYMBOL_UPDATE_DEBOUNCE_MS / 1000.0, _flush)
            self._symbols_update_timer.daemon = True
            self._symbols_update_timer.start()

    def subscribe(self, symbol: str, callback: Callable[[PriceUpdate], None]) -> None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return
        with self._lock:
            symbol_workers = self._tick_subscribers.setdefault(cleaned, {})
            if callback in symbol_workers:
                return
            worker = _SubscriberWorker(callback, name=f"tick-{cleaned}-{id(callback)}")
            symbol_workers[callback] = worker
            worker.start()

    def unsubscribe(self, symbol: str, callback: Callable[[PriceUpdate], None]) -> None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return
        with self._lock:
            symbol_workers = self._tick_subscribers.get(cleaned, {})
            worker = symbol_workers.pop(callback, None)
            if not symbol_workers:
                self._tick_subscribers.pop(cleaned, None)
        if worker:
            worker.stop()

    def subscribe_status(self, symbol: str, callback: Callable[[str, str], None]) -> None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
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
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return
        with self._lock:
            symbol_workers = self._status_subscribers.get(cleaned, {})
            worker = symbol_workers.pop(callback, None)
            if not symbol_workers:
                self._status_subscribers.pop(cleaned, None)
        if worker:
            worker.stop()

    def get_snapshot(self, symbol: str) -> MicrostructureSnapshot | None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return None
        with self._lock:
            state = self._symbol_state.get(cleaned)
        if not state:
            return None
        return self._build_snapshot(cleaned, state)

    def _log_rate_limited(self, symbol: str, kind: str, level: str, message: str) -> None:
        now_ms = self._now_ms()
        key = (symbol, kind)
        last_ms = self._log_limiter.get(key, 0)
        if now_ms - last_ms < 10_000:
            return
        self._log_limiter[key] = now_ms
        getattr(self._logger, level)(message)

    def _sanitize_symbol(self, symbol: object) -> str | None:
        cleaned = sanitize_symbol(symbol)
        if cleaned is None:
            if self._should_log_invalid_symbol(symbol):
                self._log_invalid_symbol(symbol)
            return None
        if self._exchange_info_loaded and self._exchange_symbols:
            if not validate_trade_symbol(cleaned, self._exchange_symbols):
                self._log_invalid_symbol(cleaned)
                return None
        return cleaned

    def _log_invalid_symbol(self, symbol: object) -> None:
        symbol_text = str(symbol)
        now_ms = self._now_ms()
        last_ms = self._invalid_symbols_logged.get(symbol_text, 0)
        if now_ms - last_ms < INVALID_SYMBOL_LOG_MS:
            return
        self._invalid_symbols_logged[symbol_text] = now_ms
        self._logger.warning("invalid symbol dropped: %s", symbol_text)

    @staticmethod
    def _should_log_invalid_symbol(symbol: object) -> bool:
        if not isinstance(symbol, str):
            return False
        return len(symbol.strip()) >= 5

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
        now_ms = self._now_ms()
        if status == WS_DISABLED_404:
            self._ws_disabled_until_ms = now_ms + 300_000
            self._ws_thread.set_disabled_until(self._ws_disabled_until_ms)
            self._log_rate_limited("*", "ws_disabled_404", "warning", "WS отключен из-за 404 на 5 минут")
            symbols_to_notify: list[str] = []
            with self._lock:
                for symbol, symbol_state in self._symbol_state.items():
                    symbol_state["ws_status"] = WS_LOST
                    symbol_state["ws_health_state"] = resolve_health_state(WS_LOST)
                    symbol_state["http_fallback_enabled"] = True
                    symbol_state["http_fallback_reason"] = "LOST"
                    symbol_state["http_disable_grace_until_ms"] = None
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
        symbol = self._sanitize_symbol(data.get("s"))
        if symbol is None:
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
        now_ms = self._now_ms()
        latency_ms = max(now_ms - event_ts, 0) if event_ts else None
        spread_abs, spread_pct, mid_price = estimate_spread(best_bid, best_ask)
        last_price = mid_price
        with self._lock:
            state = self._symbol_state.setdefault(symbol, {})
            last_ws_msg = state.get("last_ws_msg_ms")
            if isinstance(last_ws_msg, int) and now_ms - last_ws_msg <= self._degraded_after_ms:
                state["ws_recover_streak"] = state.get("ws_recover_streak", 0) + 1
            else:
                state["ws_recover_streak"] = 1
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
                    "last_ws_msg_ms": now_ms,
                    "last_ws_price_ms": now_ms,
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
            now_ms = self._now_ms()
            for symbol in active_symbols:
                self._heartbeat_symbol(symbol, now_ms)
                self._maybe_poll_http(symbol, now_ms)
            time.sleep(self._heartbeat_interval_s)
        self._logger.info("Price feed монитор остановлен")

    def _heartbeat_symbol(self, symbol: str, now_ms: int) -> None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return
        with self._lock:
            state = self._symbol_state.setdefault(cleaned, {})
            last_ws_msg = state.get("last_ws_msg_ms")
            current_status = state.get("ws_status", WS_LOST)
            ws_recover_streak = state.get("ws_recover_streak", 0)
            http_fallback_enabled = state.get("http_fallback_enabled", False)
            http_fallback_reason = state.get("http_fallback_reason")
            http_disable_grace_until = state.get("http_disable_grace_until_ms")
            subscribed_ms = state.get("subscribed_ms", now_ms)
            ws_grace_until = state.get("ws_grace_until_ms")
        ws_disabled = now_ms < self._ws_disabled_until_ms
        if ws_disabled:
            desired_status = WS_LOST
        else:
            if last_ws_msg is None:
                age_ms = max(now_ms - subscribed_ms, 0)
                if ws_grace_until is not None and now_ms < ws_grace_until:
                    desired_status = WS_CONNECTED
                else:
                    desired_status = resolve_ws_status(age_ms, self._degraded_after_ms, self._lost_after_ms)
            else:
                age_ms = max(now_ms - last_ws_msg, 0)
                desired_status = resolve_ws_status(age_ms, self._degraded_after_ms, self._lost_after_ms)
        if desired_status == WS_CONNECTED and current_status != WS_CONNECTED:
            if ws_recover_streak < self._recover_after_n:
                desired_status = current_status
        if desired_status != current_status:
            with self._lock:
                state = self._symbol_state.setdefault(cleaned, {})
                state["ws_status"] = desired_status
                state["ws_health_state"] = resolve_health_state(desired_status)
                state["ws_ok"] = desired_status == WS_CONNECTED
                state["ws_degraded"] = desired_status == WS_DEGRADED
                state["ws_lost"] = desired_status == WS_LOST
                if desired_status != WS_CONNECTED:
                    state["ws_recover_streak"] = 0
            if desired_status == WS_DEGRADED:
                self._log_rate_limited(cleaned, "ws_state", "info", f"WS деградирован для {cleaned}")
            elif desired_status == WS_LOST:
                self._log_rate_limited(cleaned, "ws_state", "warning", f"WS потерян для {cleaned}")
            elif desired_status == WS_CONNECTED:
                last_ws_age_ms = max(now_ms - last_ws_msg, 0) if isinstance(last_ws_msg, int) else None
                if current_status in {WS_DEGRADED, WS_LOST} and last_ws_age_ms is not None:
                    message = (
                        f"WS восстановлен для {cleaned} "
                        f"(last_ws_age_ms={last_ws_age_ms}, "
                        f"restore_confirm_count={ws_recover_streak})"
                    )
                    self._log_rate_limited(cleaned, "ws_state", "info", message)
            self._publish_status(cleaned, desired_status)

        if ws_disabled or desired_status == WS_LOST:
            if not http_fallback_enabled:
                with self._lock:
                    state = self._symbol_state.setdefault(cleaned, {})
                    state["http_fallback_enabled"] = True
                    state["http_fallback_reason"] = "LOST"
                    state["http_disable_grace_until_ms"] = None
                self._logger.debug("HTTP фолбэк включен для %s", cleaned)
        elif desired_status == WS_DEGRADED:
            if not http_fallback_enabled or http_fallback_reason != "STALE":
                with self._lock:
                    state = self._symbol_state.setdefault(cleaned, {})
                    state["http_fallback_enabled"] = True
                    state["http_fallback_reason"] = "STALE"
                    state["http_disable_grace_until_ms"] = None
                self._logger.debug("HTTP фолбэк (stale) включен для %s", cleaned)
        elif desired_status == WS_CONNECTED:
            if http_fallback_enabled:
                if http_disable_grace_until is None:
                    with self._lock:
                        state = self._symbol_state.setdefault(cleaned, {})
                        state["http_disable_grace_until_ms"] = now_ms + self._http_disable_grace_ms
                        http_disable_grace_until = state["http_disable_grace_until_ms"]
                if http_disable_grace_until is not None and now_ms >= http_disable_grace_until:
                    with self._lock:
                        state = self._symbol_state.setdefault(cleaned, {})
                        state["http_fallback_enabled"] = False
                        state["http_fallback_reason"] = None
                        state["http_disable_grace_until_ms"] = None
                    self._logger.debug("HTTP фолбэк выключен для %s", cleaned)

    def _maybe_poll_http(self, symbol: str, now_ms: int) -> None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return
        with self._lock:
            state = self._symbol_state.setdefault(cleaned, {})
            http_fallback_enabled = state.get("http_fallback_enabled", False)
            http_fallback_reason = state.get("http_fallback_reason")
            last_poll = state.get("http_poll_ms", 0)
            current_source = state.get("source", "WS")
        if not http_fallback_enabled:
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
            self._logger.debug("HTTP фолбэк ошибка для %s: %s", cleaned, exc)
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
                    "source": current_source if http_fallback_reason == "STALE" else "HTTP",
                    "best_bid": None,
                    "best_ask": None,
                    "spread_abs": None,
                    "spread_pct": None,
                    "mid_price": None,
                    "http_poll_ms": now_ms,
                    "last_http_price_ms": now_ms,
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
            local_timestamp_ms=state.get("updated_ms") or self._now_ms(),
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
        ws_health_state = state.get("ws_health_state", resolve_health_state(ws_status))
        price_age_ms = None
        if isinstance(updated_ms, int):
            price_age_ms = max(self._now_ms() - updated_ms, 0)
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
            ws_health_state=ws_health_state,
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
            symbol = sanitize_symbol(item.get("symbol", ""))
            if symbol is None:
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
            self._exchange_symbols = set(mapping.keys())
        with self._exchange_info_lock:
            self._exchange_info_loaded = True
            self._exchange_info_loading = False
        self._logger.info("Exchange info загружен (%s symbols)", len(mapping))
