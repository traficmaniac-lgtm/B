from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Any, Callable, Literal

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
WS_HEALTH_DEAD = "LOST"
WS_HEALTH_NO = "NO"

WS_GRACE_MS = 4000
WS_DEGRADED_AFTER_MS = 3000
WS_LOST_AFTER_MS = 10000
WS_OK_AFTER_MS = 2000
WS_STABLE_MS = 2000
WS_LOST_MS = 3000
SWITCH_COOLDOWN_MS = 5000
UNSUBSCRIBE_GRACE_MS = 2500
WS_STATE_CONNECTED = "CONNECTED"
WS_STATE_CONNECTING = "CONNECTING"
WS_STATE_DEGRADED = "DEGRADED"
WS_STATE_LOST = "LOST"
WS_STATE_STOPPING = "STOPPING"
WS_STATE_STOPPED = "STOPPED"
HTTP_DISABLE_GRACE_MS = 3000
INVALID_SYMBOL_LOG_MS = 60_000
SYMBOL_UPDATE_DEBOUNCE_MS = 2500
SELFTEST_TIMEOUT_S = 3.0
SELFTEST_SYMBOLS = ("BTCUSDT", "USDCUSDT", "EURIUSDT")

PriceSource = Literal["WS", "HTTP"]


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
    source: PriceSource
    ws_status: str
    ws_health_state: str


@dataclass(frozen=True)
class SymbolWsHealth:
    symbol: str
    subscribed: bool
    last_update_ts: int | None
    age_ms: int | None
    state: str


@dataclass
class SymbolDataRouter:
    symbol: str
    ws_active: bool = False
    ws_last_tick_ts: int | None = None
    http_last_tick_ts: int | None = None
    active_source: PriceSource = "HTTP"
    last_switch_ms: int = 0

    def update_ws_tick(self, now_ms: int) -> None:
        self.ws_last_tick_ts = now_ms

    def update_http_tick(self, now_ms: int) -> None:
        self.http_last_tick_ts = now_ms

    def evaluate(
        self,
        now_ms: int,
        *,
        subscribed: bool,
        ws_stable_ms: int,
        ws_lost_ms: int,
        switch_cooldown_ms: int,
    ) -> tuple[bool, bool]:
        ws_age_ms = (
            max(now_ms - self.ws_last_tick_ts, 0)
            if subscribed and self.ws_last_tick_ts is not None
            else None
        )
        ws_active = bool(ws_age_ms is not None and ws_age_ms < ws_lost_ms)
        ws_recent = bool(ws_age_ms is not None and ws_age_ms < ws_stable_ms)
        ws_active_changed = ws_active != self.ws_active
        self.ws_active = ws_active
        source_changed = False
        desired_source: PriceSource | None = None
        if ws_recent:
            desired_source = "WS"
        elif not ws_active:
            desired_source = "HTTP"
        if desired_source and desired_source != self.active_source:
            if desired_source == "WS":
                if now_ms - self.last_switch_ms >= switch_cooldown_ms:
                    self.active_source = "WS"
                    self.last_switch_ms = now_ms
                    source_changed = True
            else:
                self.active_source = "HTTP"
                self.last_switch_ms = now_ms
                source_changed = True
        return ws_active_changed, source_changed


@dataclass(frozen=True)
class PriceUpdate:
    symbol: str
    last_price: float | None
    best_bid: float | None
    best_ask: float | None
    exchange_timestamp_ms: int | None
    local_timestamp_ms: int
    latency_ms: int | None
    source: PriceSource
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
        self._connect_task: asyncio.Task | None = None
        self._command_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._pending_commands: list[dict[str, Any]] = []
        self._stop_event = threading.Event()
        self._symbols_lock = threading.Lock()
        self._symbols: set[str] = set()
        self._subscribed_symbols: set[str] = set()
        self._websocket: websockets.WebSocketClientProtocol | None = None
        self._disabled_until_ms = 0
        self._logged_url = False
        self._heartbeat_interval_s = heartbeat_interval_s
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._dead_after_ms = dead_after_ms
        self._last_connect_ms = 0
        self._generation = 0
        self._last_message_ms: int | None = None
        self._closing = False
        self._close_lock = threading.Lock()
        self._reconnect_in_progress = False
        self._reconnect_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._state = WS_STATE_STOPPED
        self._stopping = False
        self._has_received_message = False

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread.start()

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._set_state(WS_STATE_STOPPING, "stopping")
        self._stop_event.set()
        if self._loop and self._loop.is_running() and not self._loop.is_closed():
            try:
                if self._connect_task:
                    self._loop.call_soon_threadsafe(self._connect_task.cancel)
                asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=5.0)
            except TimeoutError:
                self._logger.warning("WS shutdown timeout; continuing stop.")
            except Exception:  # noqa: BLE001
                self._logger.warning("WS shutdown error", exc_info=True)
            finally:
                if not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._set_state(WS_STATE_STOPPED, "stopped")

    def set_symbols(self, symbols: list[str]) -> None:
        if self._stopping:
            return
        cleaned = {symbol.strip().lower() for symbol in symbols if symbol.strip()}
        with self._symbols_lock:
            if cleaned == self._symbols:
                return
            self._symbols = cleaned
        self._enqueue_command({"type": "sync_symbols"})

    def _enqueue_command(self, command: dict[str, Any]) -> None:
        if self._stopping:
            return
        if self._loop and self._command_queue and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._command_queue.put_nowait, command)
            except RuntimeError:
                if self._stopping:
                    return
                self._logger.debug("WS loop closed; dropping command")
                return
        else:
            if not self._stopping:
                self._pending_commands.append(command)

    def is_running(self) -> bool:
        return bool(self._loop and self._loop.is_running() and not self._stopping)

    def get_subscribed_symbols(self) -> set[str]:
        with self._symbols_lock:
            return set(self._subscribed_symbols)

    def set_disabled_until(self, disabled_until_ms: int) -> None:
        self._disabled_until_ms = disabled_until_ms

    def _set_state(self, new_state: str, details: str = "") -> None:
        with self._state_lock:
            if new_state == self._state:
                return
            self._state = new_state
        self._on_status(new_state, details)

    def _get_state(self) -> str:
        with self._state_lock:
            return self._state

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._command_queue = asyncio.Queue()
        for command in self._pending_commands:
            self._command_queue.put_nowait(command)
        self._pending_commands.clear()
        try:
            self._connect_task = self._loop.create_task(self._connect_loop())
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
                await asyncio.sleep(0.5)
                continue
            if self._get_state() == WS_STATE_STOPPED:
                self._set_state(WS_STATE_LOST, "ready")
            if self._get_state() not in {WS_STATE_LOST, WS_STATE_CONNECTING}:
                await asyncio.sleep(0.1)
                continue
            if attempt > 0:
                delay = calculate_backoff(attempt)
                self._set_state(WS_STATE_CONNECTING, f"backoff={delay:.1f}s")
                await asyncio.sleep(delay)
            self._set_state(WS_STATE_CONNECTING, "connecting")
            url = self._build_url()
            if self._reconnect_in_progress:
                await asyncio.sleep(0.1)
                continue
            self._reconnect_in_progress = True
            async with self._reconnect_lock:
                if not self._logged_url:
                    self._logger.debug("WS URL = %s", url)
                    self._logged_url = True
                self._logger.debug("WS подключение (%s символов)", len(symbols))
                self._generation += 1
                self._last_connect_ms = self._now_ms()
                try:
                    await self._listen(url, self._generation)
                    attempt = 0
                except websockets.exceptions.InvalidStatusCode as exc:
                    if exc.status_code == 404:
                        self._on_status(WS_DISABLED_404, "HTTP 404")
                        self._set_state(WS_STATE_LOST, "HTTP 404")
                        await self._wait_if_disabled()
                        continue
                    self._set_state(WS_STATE_LOST, f"handshake error: {exc}")
                except Exception as exc:  # noqa: BLE001
                    self._set_state(WS_STATE_LOST, f"ws error: {exc}")
                finally:
                    self._reconnect_in_progress = False
            if self._stop_event.is_set():
                break
            attempt += 1

    async def _listen(self, url: str, generation: int) -> None:
        async with websockets.connect(url, ping_interval=None, ping_timeout=None) as websocket:
            self._websocket = websocket
            with self._close_lock:
                self._closing = False
            self._subscribed_symbols = set()
            self._last_message_ms = self._now_ms()
            self._has_received_message = False
            self._set_state(WS_STATE_CONNECTING, "WS подключен, ждем данные")
            await self._sync_subscriptions(websocket, force=True)
            next_ping_ms = self._last_message_ms + int(self._heartbeat_interval_s * 1000)
            while not self._stop_event.is_set():
                if generation != self._generation:
                    break
                await self._drain_commands(websocket)
                now_ms = self._now_ms()
                if self._last_message_ms is not None and now_ms - self._last_message_ms > self._dead_after_ms:
                    raise RuntimeError(f"heartbeat: no messages > {self._dead_after_ms}ms")
                if now_ms >= next_ping_ms:
                    try:
                        pong_waiter = websocket.ping()
                        await asyncio.wait_for(pong_waiter, timeout=self._heartbeat_timeout_s)
                    except Exception as exc:  # noqa: BLE001
                        raise RuntimeError(f"heartbeat: ping timeout {exc}") from exc
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
        if not self._has_received_message:
            self._has_received_message = True
            self._set_state(WS_STATE_CONNECTED, "first message")
        data = payload.get("data")
        if isinstance(data, dict):
            self._on_message(data)
            return
        self._on_message(payload)

    async def _drain_commands(self, websocket: websockets.WebSocketClientProtocol) -> None:
        if not self._command_queue:
            return
        while True:
            try:
                command = self._command_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if command.get("type") == "sync_symbols":
                await self._sync_subscriptions(websocket, force=False)

    async def _sync_subscriptions(
        self,
        websocket: websockets.WebSocketClientProtocol,
        *,
        force: bool,
    ) -> None:
        desired = self._get_symbols()
        if force:
            to_add = desired
            to_remove: set[str] = set()
        else:
            to_add = desired - self._subscribed_symbols
            to_remove = self._subscribed_symbols - desired
        if to_remove:
            await self._send_subscription(websocket, "UNSUBSCRIBE", to_remove)
            self._subscribed_symbols -= to_remove
        if to_add:
            await self._send_subscription(websocket, "SUBSCRIBE", to_add)
            self._subscribed_symbols |= to_add

    async def _send_subscription(
        self,
        websocket: websockets.WebSocketClientProtocol,
        action: str,
        symbols: set[str],
    ) -> None:
        if not symbols:
            return
        params = [f"{symbol}@bookTicker" for symbol in sorted(symbols)]
        payload = {"method": action, "params": params, "id": int(self._now_ms())}
        await websocket.send(json.dumps(payload))

    async def _shutdown(self) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        with self._close_lock:
            if self._closing:
                return
            self._closing = True
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            self._logger.warning("WS shutdown close error", exc_info=True)
        self._websocket = None

    def _build_url(self) -> str:
        return f"{self._ws_url}/ws"

    def _get_symbols(self) -> set[str]:
        with self._symbols_lock:
            return set(self._symbols)

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
        self._http_disable_grace_ms = http_disable_grace_ms
        self._now_ms = now_ms_fn
        self._ws_ok_after_ms = WS_OK_AFTER_MS
        self._ws_state_lock = threading.Lock()
        self._ws_state = WS_STATE_STOPPED
        self._ws_state_details = ""
        self._registry = SymbolSubscriptionRegistry()
        self._lock = threading.Lock()
        self._ws_tick_condition = threading.Condition(self._lock)
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
        self._self_test_lock = threading.Lock()
        self._self_test_started = False
        self._self_test_completed = False
        self._symbol_capabilities: dict[str, dict[str, bool]] = {}
        self._recent_unsubscribe_ms: dict[str, int] = {}
        self._ws_thread = _BinanceBookTickerWsThread(
            ws_url=config.binance.ws_url.rstrip("/"),
            on_message=self._handle_ws_message,
            on_status=self._handle_ws_status,
            now_ms_fn=self._now_ms,
            heartbeat_interval_s=max(self._heartbeat_interval_s * 2, 4.0),
            heartbeat_timeout_s=5.0,
            dead_after_ms=self._lost_after_ms,
        )
        self._stopping = False
        self._shutting_down = False

    @classmethod
    def get_instance(cls, config: Config) -> PriceFeedManager:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(config)
            return cls._instance

    def _set_ws_state(self, new_state: str, details: str) -> None:
        with self._ws_state_lock:
            old_state = self._ws_state
            if new_state == old_state:
                return
            self._ws_state = new_state
            self._ws_state_details = details
        suffix = f" ({details})" if details else ""
        self._logger.info("WS state: %s -> %s%s", old_state, new_state, suffix)

    def _get_ws_state(self) -> str:
        with self._ws_state_lock:
            return self._ws_state

    def start(self) -> None:
        if self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._ws_thread.start()
        self._monitor_thread.start()
        self._start_selftest_once()

    def _is_ws_capable(self, symbol: str) -> bool:
        with self._lock:
            return self._symbol_capabilities.get(symbol, {}).get("ws", True)

    def _set_symbol_capability(self, symbol: str, *, ws_available: bool) -> None:
        with self._lock:
            self._symbol_capabilities.setdefault(symbol, {})["ws"] = ws_available
            state = self._symbol_state.get(symbol)
            if state is None:
                return
            state["ws_capable"] = ws_available
            if not ws_available:
                router = state.get("router")
                if not isinstance(router, SymbolDataRouter):
                    router = SymbolDataRouter(symbol=symbol)
                    state["router"] = router
                router.ws_active = False
                router.active_source = "HTTP"
                state["active_source"] = router.active_source
                state["ws_status"] = WS_LOST
                state["ws_health_state"] = WS_HEALTH_NO
                state["http_fallback_enabled"] = True
                state["http_fallback_reason"] = "NO_WS"

    def _start_selftest_once(self) -> None:
        if self._shutting_down:
            return
        with self._self_test_lock:
            if self._self_test_started or self._self_test_completed:
                return
            self._self_test_started = True
        thread = threading.Thread(target=self._run_startup_selftest, name="price-feed-selftest", daemon=True)
        thread.start()

    def _run_startup_selftest(self) -> None:
        try:
            symbols = []
            for symbol in SELFTEST_SYMBOLS:
                cleaned = sanitize_symbol(symbol)
                if cleaned is not None:
                    symbols.append(cleaned)
            if not symbols:
                return
            ws_symbols = [symbol for symbol in symbols if self._is_ws_capable(symbol)]
            http_latencies: dict[str, int | None] = {}
            for symbol in symbols:
                latency_ms: int | None = None
                start = time.perf_counter()
                try:
                    self._http_client.get_ticker_price(symbol)
                    latency_ms = int((time.perf_counter() - start) * 1000)
                except Exception as exc:  # noqa: BLE001
                    self._logger.warning("SELFTEST HTTP error for %s: %s", symbol, exc)
                http_latencies[symbol] = latency_ms
            now_ms = self._now_ms()
            self._logger.info("[TRANSPORT-TEST]")
            for symbol in symbols:
                registered_before = symbol in self._registry.active_symbols()
                subscribed_for_test = False
                if symbol in ws_symbols:
                    self.register_symbol(symbol)
                    subscribed_for_test = True
                    last_seen = self._wait_for_ws_tick(symbol, timeout_ms=2000)
                else:
                    last_seen = None
                if subscribed_for_test and not registered_before:
                    self.unregister_symbol(symbol)
                ws_ok = last_seen is not None
                ws_age_ms = max(now_ms - last_seen, 0) if isinstance(last_seen, int) else None
                self._set_symbol_capability(symbol, ws_available=ws_ok)
                http_latency_ms = http_latencies.get(symbol)
                final_source = self.active_source(symbol)
                if ws_ok and ws_age_ms is not None:
                    line = (
                        f"PRICE: {symbol} WS=OK age={ws_age_ms}ms   "
                        f"HTTP={http_latency_ms}ms   SOURCE={final_source}"
                    )
                else:
                    line = (
                        f"PRICE: {symbol} WS=NONE          "
                        f"HTTP={http_latency_ms}ms   SOURCE={final_source}"
                    )
                self._logger.info("[TRANSPORT-TEST] %s", line)
            self._schedule_symbol_update()
        finally:
            with self._self_test_lock:
                self._self_test_completed = True

    def _wait_for_ws_tick(self, symbol: str, *, timeout_ms: int) -> int | None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return None
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        with self._ws_tick_condition:
            initial_tick = self._get_last_ws_tick(cleaned)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._ws_tick_condition.wait(timeout=remaining)
                latest_tick = self._get_last_ws_tick(cleaned)
                if latest_tick is not None and latest_tick != initial_tick:
                    return latest_tick

    def shutdown(self) -> None:
        self._stopping = True
        self._shutting_down = True
        self._stop_event.set()
        with self._symbols_update_lock:
            if self._symbols_update_timer and self._symbols_update_timer.is_alive():
                self._symbols_update_timer.cancel()
                self._symbols_update_timer = None
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
            initial_status = WS_CONNECTED if self._get_ws_state() == WS_STATE_CONNECTED else WS_LOST
            with self._lock:
                state = self._symbol_state.setdefault(cleaned, {})
                router = state.get("router")
                if not isinstance(router, SymbolDataRouter):
                    router = SymbolDataRouter(symbol=cleaned)
                ws_capable = self._symbol_capabilities.get(cleaned, {}).get("ws", True)
                state["ws_capable"] = ws_capable
                if not ws_capable:
                    router.ws_active = False
                    router.active_source = "HTTP"
                    router.last_switch_ms = now_ms
                    state["http_fallback_enabled"] = True
                    state["http_fallback_reason"] = "NO_WS"
                state.update(
                    {
                        "subscribed_ms": now_ms,
                        "ws_grace_until_ms": now_ms + WS_GRACE_MS,
                        "ws_status": initial_status if ws_capable else WS_LOST,
                        "ws_health_state": resolve_health_state(initial_status) if ws_capable else WS_HEALTH_NO,
                        "ws_recover_streak": 0,
                        "last_ws_msg_ms": None,
                        "last_ws_price_ms": None,
                        "http_fallback_enabled": state.get("http_fallback_enabled", False),
                        "http_fallback_reason": state.get("http_fallback_reason"),
                        "http_disable_grace_until_ms": None,
                        "router": router,
                        "ws_symbol_active": False,
                        "ws_symbol_age_ms": None,
                        "active_source": router.active_source,
                    }
                )
            with self._lock:
                self._recent_unsubscribe_ms.pop(cleaned, None)
        self._schedule_symbol_update()
        self._ensure_exchange_info_loaded()

    def unregister_symbol(self, symbol: str) -> None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return
        removed = self._registry.remove(cleaned)
        if removed:
            self._logger.info("unsubscribe %s", cleaned)
            with self._lock:
                self._recent_unsubscribe_ms[cleaned] = self._now_ms()
        self._schedule_symbol_update()

    def get_ws_overall_status(self) -> str:
        now_ms = self._now_ms()
        if now_ms < self._ws_disabled_until_ms:
            return WS_LOST
        if self._get_ws_state() in {WS_STATE_LOST, WS_STATE_CONNECTING, WS_STATE_STOPPED, WS_STATE_STOPPING}:
            return WS_LOST
        active_symbols = [
            symbol for symbol in self._registry.active_symbols() if self._is_ws_capable(symbol)
        ]
        if not active_symbols:
            return WS_LOST
        best_status = WS_LOST
        with self._lock:
            states = {symbol: self._symbol_state.get(symbol, {}) for symbol in active_symbols}
        for symbol in active_symbols:
            state = states.get(symbol, {})
            status = state.get("ws_status", WS_LOST)
            if status == WS_CONNECTED:
                return WS_CONNECTED
            if status == WS_DEGRADED:
                best_status = WS_DEGRADED
        return best_status

    def _schedule_symbol_update(self) -> None:
        def _flush() -> None:
            if self._stopping or self._shutting_down:
                return
            if not self._ws_thread or not self._ws_thread.is_running():
                return
            symbols = self._get_ws_subscription_symbols()
            self._ws_thread.set_symbols(symbols)

        with self._symbols_update_lock:
            if self._symbols_update_timer and self._symbols_update_timer.is_alive():
                self._symbols_update_timer.cancel()
            if self._stopping or self._shutting_down:
                return
            self._symbols_update_timer = threading.Timer(SYMBOL_UPDATE_DEBOUNCE_MS / 1000.0, _flush)
            self._symbols_update_timer.daemon = True
            self._symbols_update_timer.start()

    def _get_ws_subscription_symbols(self) -> list[str]:
        now_ms = self._now_ms()
        active_symbols = self._registry.active_symbols()
        with self._lock:
            ws_capable = {
                symbol
                for symbol in active_symbols
                if self._symbol_capabilities.get(symbol, {}).get("ws", True)
            }
            for symbol, last_unsub_ms in list(self._recent_unsubscribe_ms.items()):
                if now_ms - last_unsub_ms >= UNSUBSCRIBE_GRACE_MS:
                    self._recent_unsubscribe_ms.pop(symbol, None)
                    continue
                if self._symbol_capabilities.get(symbol, {}).get("ws", True):
                    ws_capable.add(symbol)
        return list(ws_capable)

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

    def get_ws_health(self, symbol: str) -> SymbolWsHealth | None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return None
        if not self._is_ws_capable(cleaned):
            return SymbolWsHealth(
                symbol=cleaned,
                subscribed=False,
                last_update_ts=None,
                age_ms=None,
                state=WS_HEALTH_NO,
            )
        now_ms = self._now_ms()
        subscribed_symbols = self._ws_thread.get_subscribed_symbols() if self._ws_thread else set()
        subscribed = cleaned.lower() in subscribed_symbols
        with self._lock:
            state = self._symbol_state.get(cleaned, {})
            router = state.get("router")
        if not isinstance(router, SymbolDataRouter):
            return SymbolWsHealth(
                symbol=cleaned,
                subscribed=subscribed,
                last_update_ts=None,
                age_ms=None,
                state=WS_HEALTH_NO if not subscribed else WS_HEALTH_DEAD,
            )
        if not subscribed:
            return SymbolWsHealth(
                symbol=cleaned,
                subscribed=False,
                last_update_ts=None,
                age_ms=None,
                state=WS_HEALTH_NO,
            )
        age_ms = max(now_ms - router.ws_last_tick_ts, 0) if router.ws_last_tick_ts is not None else None
        last_update_ts = router.ws_last_tick_ts
        health_state = WS_HEALTH_OK if router.ws_active else WS_HEALTH_DEAD
        return SymbolWsHealth(
            symbol=cleaned,
            subscribed=subscribed,
            last_update_ts=last_update_ts,
            age_ms=age_ms,
            state=health_state,
        )

    def active_source(self, symbol: str) -> PriceSource:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return "HTTP"
        with self._lock:
            state = self._symbol_state.get(cleaned, {})
            router = state.get("router")
            if isinstance(router, SymbolDataRouter):
                return router.active_source
        return "HTTP"

    def get_price(self, symbol: str) -> tuple[float | None, PriceSource, int | None]:
        snapshot = self.get_snapshot(symbol)
        if snapshot is None:
            return None, "HTTP", None
        age_ms = None
        with self._lock:
            state = self._symbol_state.get(snapshot.symbol, {})
            router = state.get("router")
            if isinstance(router, SymbolDataRouter):
                if snapshot.source == "WS" and router.ws_last_tick_ts is not None:
                    age_ms = max(self._now_ms() - router.ws_last_tick_ts, 0)
                elif snapshot.source == "HTTP" and router.http_last_tick_ts is not None:
                    age_ms = max(self._now_ms() - router.http_last_tick_ts, 0)
        return snapshot.last_price, snapshot.source, age_ms

    def get_source_ages(self, symbol: str) -> tuple[int | None, int | None]:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return None, None
        now_ms = self._now_ms()
        with self._lock:
            state = self._symbol_state.get(cleaned, {})
            router = state.get("router")
            if not isinstance(router, SymbolDataRouter):
                return None, None
            ws_age_ms = max(now_ms - router.ws_last_tick_ts, 0) if router.ws_last_tick_ts else None
            http_age_ms = max(now_ms - router.http_last_tick_ts, 0) if router.http_last_tick_ts else None
            return ws_age_ms, http_age_ms

    def _get_last_ws_tick(self, symbol: str) -> int | None:
        state = self._symbol_state.get(symbol, {})
        router = state.get("router")
        if isinstance(router, SymbolDataRouter):
            return router.ws_last_tick_ts
        return None

    def _log_rate_limited(self, symbol: str, kind: str, level: str, message: str) -> None:
        now_ms = self._now_ms()
        key = (symbol, kind)
        last_ms = self._log_limiter.get(key, 0)
        if now_ms - last_ms < 10_000:
            return
        self._log_limiter[key] = now_ms
        getattr(self._logger, level)(message)

    def _resubscribe_active_symbols(self) -> None:
        if not self._ws_thread or not self._ws_thread.is_running():
            return
        symbols = self._get_ws_subscription_symbols()
        if symbols:
            self._ws_thread.set_symbols(symbols)

    def _update_router_state(
        self,
        symbol: str,
        now_ms: int,
        *,
        subscribed: bool,
        ws_allowed: bool,
        ws_tick: bool = False,
        http_tick: bool = False,
    ) -> tuple[SymbolDataRouter, bool, bool, int | None]:
        with self._lock:
            state = self._symbol_state.setdefault(symbol, {})
            router = state.get("router")
            if not isinstance(router, SymbolDataRouter):
                router = SymbolDataRouter(symbol=symbol)
                state["router"] = router
            if not ws_allowed:
                router.ws_active = False
                router.active_source = "HTTP"
                state["ws_symbol_active"] = False
                state["ws_symbol_age_ms"] = None
                state["active_source"] = router.active_source
                state["ws_status"] = WS_LOST
                state["ws_health_state"] = WS_HEALTH_NO
                subscribed_ms = state.get("subscribed_ms")
                return router, False, False, subscribed_ms
            if ws_tick:
                router.update_ws_tick(now_ms)
                state["last_ws_msg_ms"] = now_ms
                self._ws_tick_condition.notify_all()
            if http_tick:
                router.update_http_tick(now_ms)
                state["last_http_price_ms"] = now_ms
            ws_active_changed, source_changed = router.evaluate(
                now_ms,
                subscribed=subscribed,
                ws_stable_ms=WS_STABLE_MS,
                ws_lost_ms=WS_LOST_MS,
                switch_cooldown_ms=SWITCH_COOLDOWN_MS,
            )
            ws_age_ms = max(now_ms - router.ws_last_tick_ts, 0) if router.ws_last_tick_ts is not None else None
            state["ws_symbol_active"] = router.ws_active
            state["ws_symbol_age_ms"] = ws_age_ms
            state["active_source"] = router.active_source
            ws_status = WS_CONNECTED if router.ws_active else WS_LOST
            state["ws_status"] = ws_status
            state["ws_health_state"] = resolve_health_state(ws_status)
            subscribed_ms = state.get("subscribed_ms")
        return router, ws_active_changed, source_changed, subscribed_ms

    def _log_router_events(
        self,
        symbol: str,
        router: SymbolDataRouter,
        *,
        ws_active_changed: bool,
        source_changed: bool,
        now_ms: int,
        subscribed_ms: int | None,
        ws_allowed: bool,
    ) -> None:
        if not ws_allowed:
            return
        if ws_active_changed and router.ws_active:
            age_ms = max(now_ms - router.ws_last_tick_ts, 0) if router.ws_last_tick_ts is not None else None
            if isinstance(age_ms, int):
                self._log_rate_limited(
                    symbol,
                    "router_active",
                    "info",
                    f"[ROUTER] {symbol} WS active (last_tick={age_ms}ms)",
                )
        if source_changed and router.active_source == "HTTP":
            if router.ws_last_tick_ts is not None:
                age_ms = max(now_ms - router.ws_last_tick_ts, 0)
            elif isinstance(subscribed_ms, int):
                age_ms = max(now_ms - subscribed_ms, 0)
            else:
                age_ms = None
            age_text = f"{age_ms}ms" if isinstance(age_ms, int) else "unknown"
            self._log_rate_limited(
                symbol,
                "router_fallback",
                "warning",
                f"[ROUTER] {symbol} fallback to HTTP (no WS ticks {age_text})",
            )
        if source_changed and router.active_source == "WS":
            self._log_rate_limited(
                symbol,
                "router_recovered",
                "info",
                f"[ROUTER] {symbol} WS recovered, switching source",
            )

    def _resolve_price_source(self, symbol: str, state: dict[str, Any]) -> PriceSource:
        router = state.get("router")
        if isinstance(router, SymbolDataRouter):
            return router.active_source
        return "HTTP"

    def _log_price_source(self, symbol: str, source: PriceSource) -> None:
        self._log_rate_limited(symbol, "price_source", "info", f"Price source {symbol} = {source}")

    def _set_http_fallback(
        self,
        symbol: str,
        *,
        enabled: bool,
        reason: str | None,
    ) -> None:
        with self._lock:
            state = self._symbol_state.setdefault(symbol, {})
            current_enabled = state.get("http_fallback_enabled", False)
            current_reason = state.get("http_fallback_reason")
            if enabled:
                state["http_fallback_enabled"] = True
                state["http_fallback_reason"] = reason
                state["http_disable_grace_until_ms"] = None
            else:
                state["http_fallback_enabled"] = False
                state["http_fallback_reason"] = None
                state["http_disable_grace_until_ms"] = None
        if enabled and (not current_enabled or current_reason != reason):
            self._log_rate_limited(symbol, "http_fallback", "info", f"HTTP fallback for {symbol}: {reason}")
        if not enabled and current_enabled:
            self._log_rate_limited(symbol, "http_fallback", "info", f"HTTP fallback cleared for {symbol}")

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
            self._set_ws_state(WS_STATE_LOST, "HTTP 404")
            symbols_to_notify: list[str] = []
            with self._lock:
                for symbol, symbol_state in self._symbol_state.items():
                    symbol_state["ws_status"] = WS_LOST
                    symbol_state["ws_health_state"] = resolve_health_state(WS_LOST)
                    symbols_to_notify.append(symbol)
            for symbol in symbols_to_notify:
                if not self._is_ws_capable(symbol):
                    self._set_http_fallback(symbol, enabled=True, reason="NO_WS")
                    continue
                self._set_http_fallback(symbol, enabled=True, reason="LOST")
                self._publish_status(symbol, WS_LOST)
            return
        if status in {
            WS_STATE_CONNECTED,
            WS_STATE_CONNECTING,
            WS_STATE_LOST,
            WS_STATE_STOPPED,
            WS_STATE_STOPPING,
        }:
            self._set_ws_state(status, details)
            if status == WS_STATE_CONNECTED:
                with self._lock:
                    for symbol_state in self._symbol_state.values():
                        symbol_state["ws_grace_until_ms"] = now_ms + WS_GRACE_MS
                self._resubscribe_active_symbols()
            if status in {WS_STATE_LOST, WS_STATE_CONNECTING, WS_STATE_STOPPED, WS_STATE_STOPPING}:
                symbols_to_notify = []
                with self._lock:
                    for symbol, symbol_state in self._symbol_state.items():
                        if not self._symbol_capabilities.get(symbol, {}).get("ws", True):
                            continue
                        symbol_state["ws_status"] = WS_LOST
                        symbol_state["ws_health_state"] = resolve_health_state(WS_LOST)
                        symbol_state["ws_recover_streak"] = 0
                        symbols_to_notify.append(symbol)
                for symbol in symbols_to_notify:
                    self._publish_status(symbol, WS_LOST)
            return
        self._log_rate_limited("*", "ws_error", "error", f"WS ошибка: {details}")

    def _handle_ws_message(self, data: dict[str, Any]) -> None:
        symbol = self._sanitize_symbol(data.get("s"))
        if symbol is None:
            return
        if not self._is_ws_capable(symbol):
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
            if isinstance(last_ws_msg, int) and now_ms - last_ws_msg <= self._ws_ok_after_ms:
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
        router, ws_active_changed, source_changed, subscribed_ms = self._update_router_state(
            symbol,
            now_ms,
            subscribed=True,
            ws_allowed=True,
            ws_tick=True,
        )
        self._log_router_events(
            symbol,
            router,
            ws_active_changed=ws_active_changed,
            source_changed=source_changed,
            now_ms=now_ms,
            subscribed_ms=subscribed_ms,
            ws_allowed=True,
        )
        if source_changed and router.active_source == "WS":
            self._resubscribe_active_symbols()
        self._publish_update(symbol)

    def _monitor_loop(self) -> None:
        self._logger.info("Price feed монитор запущен")
        while not self._stop_event.is_set():
            active_symbols = self._registry.active_symbols()
            if not active_symbols:
                time.sleep(self._heartbeat_interval_s)
                continue
            now_ms = self._now_ms()
            subscribed_symbols = self._ws_thread.get_subscribed_symbols() if self._ws_thread else set()
            for symbol in active_symbols:
                subscribed = symbol.lower() in subscribed_symbols
                self._heartbeat_symbol(symbol, now_ms, subscribed=subscribed)
                self._maybe_poll_http(symbol, now_ms, subscribed=subscribed)
            self._update_ws_overall_state(active_symbols)
            time.sleep(self._heartbeat_interval_s)
        self._logger.info("Price feed монитор остановлен")

    def _update_ws_overall_state(self, active_symbols: list[str]) -> None:
        ws_state = self._get_ws_state()
        if ws_state in {WS_STATE_CONNECTING, WS_STATE_STOPPING, WS_STATE_STOPPED}:
            return
        active_ws_symbols = [symbol for symbol in active_symbols if self._is_ws_capable(symbol)]
        if not active_ws_symbols:
            self._set_ws_state(WS_STATE_LOST, "no active symbols")
            return
        with self._lock:
            statuses = [
                self._symbol_state.get(symbol, {}).get("ws_status", WS_LOST) for symbol in active_ws_symbols
            ]
        if any(status == WS_DEGRADED for status in statuses):
            self._set_ws_state(WS_STATE_DEGRADED, "symbol degraded")
            return
        if any(status == WS_CONNECTED for status in statuses):
            self._set_ws_state(WS_STATE_CONNECTED, "symbols connected")
            return
        self._set_ws_state(WS_STATE_LOST, "symbols lost")

    def _heartbeat_symbol(self, symbol: str, now_ms: int, *, subscribed: bool) -> None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return
        ws_allowed = self._is_ws_capable(cleaned)
        with self._lock:
            current_status = self._symbol_state.setdefault(cleaned, {}).get("ws_status", WS_LOST)
        router, ws_active_changed, source_changed, subscribed_ms = self._update_router_state(
            cleaned,
            now_ms,
            subscribed=subscribed,
            ws_allowed=ws_allowed,
        )
        desired_status = WS_CONNECTED if router.ws_active else WS_LOST
        if ws_allowed and desired_status != current_status:
            if desired_status == WS_LOST:
                self._log_rate_limited(cleaned, "ws_state", "warning", f"WS потерян для {cleaned}")
            elif desired_status == WS_CONNECTED:
                self._log_rate_limited(cleaned, "ws_state", "info", f"WS восстановлен для {cleaned}")
            self._publish_status(cleaned, desired_status)
        self._log_router_events(
            cleaned,
            router,
            ws_active_changed=ws_active_changed,
            source_changed=source_changed,
            now_ms=now_ms,
            subscribed_ms=subscribed_ms,
            ws_allowed=ws_allowed,
        )
        if router.active_source == "HTTP":
            self._set_http_fallback(cleaned, enabled=True, reason="NO_WS" if not ws_allowed else "ROUTER")
        else:
            self._set_http_fallback(cleaned, enabled=False, reason=None)
        if source_changed and router.active_source == "WS":
            self._resubscribe_active_symbols()

    def _maybe_poll_http(self, symbol: str, now_ms: int, *, subscribed: bool) -> None:
        cleaned = self._sanitize_symbol(symbol)
        if cleaned is None:
            return
        ws_allowed = self._is_ws_capable(cleaned)
        with self._lock:
            state = self._symbol_state.setdefault(cleaned, {})
            http_fallback_enabled = state.get("http_fallback_enabled", False)
            last_poll = state.get("http_poll_ms", 0)
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
                    "source": "HTTP",
                    "best_bid": None,
                    "best_ask": None,
                    "spread_abs": None,
                    "spread_pct": None,
                    "mid_price": None,
                    "http_poll_ms": now_ms,
                    "last_http_price_ms": now_ms,
                }
            )
        router, ws_active_changed, source_changed, subscribed_ms = self._update_router_state(
            cleaned,
            now_ms,
            subscribed=subscribed,
            ws_allowed=ws_allowed,
            http_tick=True,
        )
        self._log_router_events(
            cleaned,
            router,
            ws_active_changed=ws_active_changed,
            source_changed=source_changed,
            now_ms=now_ms,
            subscribed_ms=subscribed_ms,
            ws_allowed=ws_allowed,
        )
        self._publish_update(cleaned)

    def _publish_update(self, symbol: str) -> None:
        update = self._build_update(symbol)
        if update is None:
            return
        self._log_price_source(symbol, update.source)
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
        source = self._resolve_price_source(symbol, state)
        router = state.get("router")
        if source == "HTTP" and isinstance(router, SymbolDataRouter):
            if router.http_last_tick_ts is None:
                last_price = None
                best_bid = None
                best_ask = None
                spread_abs = None
                spread_pct = None
                mid_price = None
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
