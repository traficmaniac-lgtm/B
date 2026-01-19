from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Any, Callable

import websockets

from src.core.logging import get_logger
from src.core.timeutil import backoff_delay, utc_ms


@dataclass(frozen=True)
class PriceTick:
    symbol: str
    price: float
    exchange_timestamp_ms: int
    local_timestamp_ms: int
    latency_ms: int


class _SubscriberWorker:
    def __init__(self, callback: Callable[[Any], None], name: str) -> None:
        self._callback = callback
        self._queue: Queue[Any] = Queue(maxsize=250)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._logger = get_logger(f"services.price_feed.{name}")

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    def publish(self, item: Any) -> None:
        try:
            self._queue.put_nowait(item)
        except Full:
            self._logger.debug("Subscriber queue full; dropping tick")

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


class PriceFeedService:
    def __init__(
        self,
        symbol: str,
        ws_url: str = "wss://stream.binance.com:9443",
        max_retries: int = 8,
        heartbeat_interval_s: float = 5.0,
        degraded_after_ms: int = 4000,
        lost_after_ms: int = 12000,
    ) -> None:
        self._symbol = symbol.replace("/", "").lower()
        self._ws_url = ws_url.rstrip("/")
        self._max_retries = max_retries
        self._heartbeat_interval_s = heartbeat_interval_s
        self._degraded_after_ms = degraded_after_ms
        self._lost_after_ms = lost_after_ms
        self._logger = get_logger(f"services.price_feed.{self._symbol}")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._tick_subscribers: dict[Callable[[PriceTick], None], _SubscriberWorker] = {}
        self._status_subscribers: dict[Callable[[str, str], None], _SubscriberWorker] = {}
        self._status = "LOST"
        self._last_price: float | None = None
        self._last_exchange_ts: int | None = None
        self._last_latency_ms: int | None = None
        self._last_update_ms: int | None = None

    @property
    def last_price(self) -> float | None:
        with self._lock:
            return self._last_price

    @property
    def last_update_time(self) -> int | None:
        with self._lock:
            return self._last_exchange_ts

    @property
    def latency_ms(self) -> int | None:
        with self._lock:
            return self._last_latency_ms

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def subscribe(self, callback: Callable[[PriceTick], None]) -> None:
        with self._lock:
            if callback in self._tick_subscribers:
                return
            worker = _SubscriberWorker(callback, name=f"tick-{id(callback)}")
            self._tick_subscribers[callback] = worker
            worker.start()

    def unsubscribe(self, callback: Callable[[PriceTick], None]) -> None:
        with self._lock:
            worker = self._tick_subscribers.pop(callback, None)
        if worker:
            worker.stop()

    def subscribe_status(self, callback: Callable[[str, str], None]) -> None:
        with self._lock:
            if callback in self._status_subscribers:
                return

            def _wrapped(item: tuple[str, str]) -> None:
                status, details = item
                callback(status, details)

            worker = _SubscriberWorker(_wrapped, name=f"status-{id(callback)}")
            self._status_subscribers[callback] = worker
            worker.start()

    def unsubscribe_status(self, callback: Callable[[str, str], None]) -> None:
        with self._lock:
            worker = self._status_subscribers.pop(callback, None)
        if worker:
            worker.stop()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name=f"price-feed-{self._symbol}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        with self._lock:
            subscribers = list(self._tick_subscribers.values()) + list(self._status_subscribers.values())
            self._tick_subscribers.clear()
            self._status_subscribers.clear()
        for worker in subscribers:
            worker.stop()

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
            if attempt >= self._max_retries:
                await self._set_status("LOST", "max retries reached")
                self._logger.warning("Max retries reached; giving up")
                break
            try:
                await self._listen()
            except Exception as exc:  # noqa: BLE001
                await self._set_status("DEGRADED", f"error: {exc}")
                self._logger.error("WS error: %s", exc)
            if self._stop_event.is_set():
                break
            attempt += 1
            delay = backoff_delay(attempt, base_s=1.0, max_s=30.0)
            self._logger.info("RECONNECT backoff=%.1fs", delay)
            await asyncio.sleep(delay)

    async def _listen(self) -> None:
        stream = f"{self._symbol}@trade"
        url = f"{self._ws_url}/ws/{stream}"
        self._logger.info("WS CONNECT %s", url)
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as websocket:
            await self._set_status("CONNECTED", "connected")
            self._logger.info("WS connected")
            heartbeat_task = asyncio.create_task(self._heartbeat_monitor(websocket))
            try:
                async for message in websocket:
                    if self._stop_event.is_set():
                        break
                    await self._handle_message(message)
            finally:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
                await self._set_status("LOST", "disconnected")

    async def _handle_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        price_raw = payload.get("p")
        symbol = payload.get("s")
        exchange_ts = payload.get("T") or payload.get("E")
        if not isinstance(symbol, str) or not isinstance(price_raw, str):
            return
        if not isinstance(exchange_ts, int):
            return
        try:
            price = float(price_raw)
        except ValueError:
            return
        local_ts = utc_ms()
        latency_ms = max(local_ts - exchange_ts, 0)
        tick = PriceTick(
            symbol=symbol,
            price=price,
            exchange_timestamp_ms=exchange_ts,
            local_timestamp_ms=local_ts,
            latency_ms=latency_ms,
        )
        with self._lock:
            self._last_price = price
            self._last_exchange_ts = exchange_ts
            self._last_latency_ms = latency_ms
            self._last_update_ms = local_ts
        await self._set_status("CONNECTED", "tick")
        self._publish_tick(tick)

    async def _heartbeat_monitor(self, websocket: websockets.WebSocketClientProtocol) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(self._heartbeat_interval_s)
            now_ms = utc_ms()
            with self._lock:
                last_update = self._last_update_ms
            if last_update is None:
                continue
            delta = now_ms - last_update
            if delta >= self._lost_after_ms:
                self._logger.warning("Heartbeat lost (%sms)", delta)
                await self._set_status("LOST", f"no data for {delta}ms")
                await websocket.close()
                break
            if delta >= self._degraded_after_ms:
                await self._set_status("DEGRADED", f"no data for {delta}ms")

    def _publish_tick(self, tick: PriceTick) -> None:
        with self._lock:
            subscribers = list(self._tick_subscribers.values())
        for worker in subscribers:
            worker.publish(tick)

    async def _set_status(self, status: str, details: str) -> None:
        notify = False
        with self._lock:
            if self._status != status:
                self._status = status
                notify = True
            subscribers = list(self._status_subscribers.values())
        if notify:
            for worker in subscribers:
                worker.publish((status, details))
