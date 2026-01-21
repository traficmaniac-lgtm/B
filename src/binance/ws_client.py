from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Callable, Coroutine

import websockets

from src.core.logging import get_logger
from src.core.timeutil import backoff_delay, utc_ms


class WsManager:
    def __init__(self, logger: Any) -> None:
        self._logger = logger
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._main_coro_factory: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._shutdown_lock = threading.Lock()
        self._is_stopping = False
        self._is_closed = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    @property
    def is_stopping(self) -> bool:
        return self._is_stopping

    @property
    def is_closed(self) -> bool:
        return self._is_closed

    def start(self, main_coro_factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._main_coro_factory = main_coro_factory
        self._is_stopping = False
        self._is_closed = False
        self._thread = threading.Thread(target=self._run_loop, name="binance-ws", daemon=True)
        self._thread.start()

    def stop(self, shutdown_coro: Callable[[], Coroutine[Any, Any, None]] | None = None) -> None:
        with self._shutdown_lock:
            self._is_stopping = True
            if self._loop and self._loop.is_running():
                if not self._loop.is_closed():
                    if shutdown_coro is not None:
                        future = asyncio.run_coroutine_threadsafe(shutdown_coro(), self._loop)
                        future.result(timeout=5.0)
                    try:
                        self._loop.call_soon_threadsafe(self._loop.stop)
                    except RuntimeError:
                        self._logger.info("[WS] skip enqueue: loop closed")
                else:
                    self._logger.info("[WS] skip enqueue: loop closed")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._is_closed = True

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._logger.info("START_LOOP")
            if self._main_coro_factory is not None:
                self._loop.create_task(self._main_coro_factory())
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()
            self._is_closed = True
            self._logger.info("STOP_LOOP")


class BinanceWsClient:
    def __init__(
        self,
        ws_url: str,
        on_tick: Callable[[str, float, int], None],
        on_status: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self._ws_url = ws_url.rstrip("/")
        self._on_tick = on_tick
        self._on_status = on_status
        self._logger = get_logger("binance.ws")
        self._manager = WsManager(self._logger)
        self._stop_event = threading.Event()
        self._last_message_ms: int | None = None
        self._websocket: websockets.WebSocketClientProtocol | None = None

    @property
    def last_message_ms(self) -> int | None:
        return self._last_message_ms

    def start(self) -> None:
        self._stop_event.clear()
        self._manager.start(self._connect_loop)

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._manager.stop(self._shutdown)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("WS shutdown error: %s", exc)

    async def _connect_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                self._logger.info("CONNECT")
                await self._notify_status("CONNECTED")
                await self._listen()
            except Exception as exc:  # noqa: BLE001
                await self._notify_status("ERROR", str(exc))
                self._logger.error("WS error: %s", exc)
            if self._stop_event.is_set():
                break
            attempt += 1
            delay = backoff_delay(attempt, base_s=1.0, max_s=30.0)
            self._logger.info("RECONNECT backoff=%.1fs", delay)
            await self._notify_status("RECONNECTING", f"backoff={delay:.1f}s")
            await asyncio.sleep(delay)
        self._logger.info("DISCONNECT")

    async def _listen(self) -> None:
        url = f"{self._ws_url}/!miniTicker@arr"
        self._logger.info("SUBSCRIBE")
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as websocket:
                self._websocket = websocket
                self._last_message_ms = utc_ms()
                async for message in websocket:
                    if self._stop_event.is_set():
                        break
                    self._last_message_ms = utc_ms()
                    await self._handle_message(message)
        finally:
            self._logger.info("DISCONNECT")
            self._websocket = None

    async def _handle_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, list):
            return
        now_ms = utc_ms()
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = item.get("s")
            price = item.get("c")
            if isinstance(symbol, str) and isinstance(price, str):
                try:
                    price_value = float(price)
                except ValueError:
                    continue
                self._on_tick(symbol, price_value, now_ms)

    async def _notify_status(self, status: str, details: str | None = None) -> None:
        if self._on_status is None:
            return
        message = status if details is None else f"{status}: {details}"
        try:
            result = self._on_status(status, message)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("WS status callback failed: %s", exc)

    async def _shutdown(self) -> None:
        loop = self._manager.loop
        if loop is None:
            return
        current = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks(loop) if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._websocket is not None:
            try:
                await self._websocket.close()
            except Exception as exc:  # noqa: BLE001
                self._logger.debug("WS close error: %s", exc)
