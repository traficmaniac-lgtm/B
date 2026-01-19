from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Callable

import websockets

from src.core.logging import get_logger
from src.core.timeutil import backoff_delay, utc_ms


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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_message_ms: int | None = None

    @property
    def last_message_ms(self) -> int | None:
        return self._last_message_ms

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="binance-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_loop())
        finally:
            self._loop.close()

    async def _connect_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._notify_status("CONNECTED")
                await self._listen()
            except Exception as exc:  # noqa: BLE001
                await self._notify_status("ERROR", str(exc))
                self._logger.error("WS error: %s", exc)
            if self._stop_event.is_set():
                break
            attempt += 1
            await self._notify_status("RECONNECTING")
            delay = backoff_delay(attempt)
            await asyncio.sleep(delay)

    async def _listen(self) -> None:
        url = f"{self._ws_url}/!miniTicker@arr"
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as websocket:
            self._last_message_ms = utc_ms()
            async for message in websocket:
                if self._stop_event.is_set():
                    break
                self._last_message_ms = utc_ms()
                await self._handle_message(message)

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
