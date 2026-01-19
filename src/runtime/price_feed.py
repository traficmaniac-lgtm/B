from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from src.core.logging import get_logger


class PriceFeed:
    def __init__(
        self,
        symbol: str,
        interval_sec: float = 1.5,
        on_price: Callable[[float, datetime, float], None] | None = None,
    ) -> None:
        self._symbol = symbol.replace("/", "").upper()
        self._interval_sec = interval_sec
        self._on_price = on_price
        self._logger = get_logger(f"runtime.price_feed.{symbol.lower()}")
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.last_price: float | None = None
        self.timestamp: datetime | None = None
        self.latency: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name=f"price-feed-{self._symbol}", daemon=True)
        self._thread.start()
        self._logger.info("Price feed started", extra={"symbol": self._symbol})

    def stop(self) -> None:
        if not self._thread:
            return
        self._stop_event.set()
        self._thread.join(timeout=2)
        self._logger.info("Price feed stopped", extra={"symbol": self._symbol})

    def on_price(self, price: float, timestamp: datetime, latency: float) -> None:
        if self._on_price:
            self._on_price(price, timestamp, latency)

    def _run(self) -> None:
        url = "https://api.binance.com/api/v3/ticker/price"
        with httpx.Client(timeout=4.0) as client:
            while not self._stop_event.is_set():
                start = time.perf_counter()
                try:
                    response = client.get(url, params={"symbol": self._symbol})
                    response.raise_for_status()
                    payload = response.json()
                    price = float(payload.get("price"))
                except Exception as exc:  # noqa: BLE001 - surface in logs
                    self._logger.warning("Price feed error", extra={"symbol": self._symbol, "error": str(exc)})
                    time.sleep(self._interval_sec)
                    continue

                latency = time.perf_counter() - start
                timestamp = datetime.now(timezone.utc)
                self.last_price = price
                self.timestamp = timestamp
                self.latency = latency
                self.on_price(price, timestamp, latency)

                sleep_for = max(self._interval_sec - latency, 0.1)
                if self._stop_event.wait(timeout=sleep_for):
                    break
