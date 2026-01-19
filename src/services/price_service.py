from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from src.binance.http_client import BinanceHttpClient
from src.binance.ws_client import BinanceWsClient
from src.core.logging import get_logger
from src.core.timeutil import utc_ms


@dataclass(frozen=True)
class PriceSnapshot:
    price: float
    source: str
    age_ms: int


class PriceService:
    def __init__(
        self,
        http_client: BinanceHttpClient,
        ws_client: BinanceWsClient,
        ttl_ms: int,
        refresh_interval_ms: int,
        fallback_enabled: bool = True,
    ) -> None:
        self._http_client = http_client
        self._ws_client = ws_client
        self._ttl_ms = ttl_ms
        self._refresh_interval_ms = refresh_interval_ms
        self._fallback_enabled = fallback_enabled
        self._symbols: list[str] = []
        self._ws_prices: dict[str, tuple[float, int]] = {}
        self._http_prices: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._http_thread: threading.Thread | None = None
        self._logger = get_logger("services.prices")

    def start(self) -> None:
        self._stop_event.clear()
        self._ws_client.start()
        if self._http_thread and self._http_thread.is_alive():
            return
        self._http_thread = threading.Thread(target=self._http_loop, name="binance-http", daemon=True)
        self._http_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._ws_client.stop()
        if self._http_thread:
            self._http_thread.join(timeout=2.0)

    def set_symbols(self, symbols: Iterable[str]) -> None:
        with self._lock:
            self._symbols = [symbol for symbol in symbols if symbol]

    def on_ws_tick(self, symbol: str, price: float, timestamp_ms: int) -> None:
        with self._lock:
            self._ws_prices[symbol] = (price, timestamp_ms)

    def get_price(self, symbol: str) -> PriceSnapshot | None:
        now_ms = utc_ms()
        with self._lock:
            ws_entry = self._ws_prices.get(symbol)
            if ws_entry is not None:
                price, ts_ms = ws_entry
                age = now_ms - ts_ms
                if age <= self._ttl_ms:
                    return PriceSnapshot(price=price, source="WS", age_ms=max(age, 0))
            http_entry = self._http_prices.get(symbol)
            if http_entry is None:
                return None
            price, ts_ms = http_entry
            age = now_ms - ts_ms
            return PriceSnapshot(price=price, source="HTTP", age_ms=max(age, 0))

    def snapshot(self, symbols: Iterable[str]) -> dict[str, PriceSnapshot]:
        now_ms = utc_ms()
        result: dict[str, PriceSnapshot] = {}
        with self._lock:
            for symbol in symbols:
                ws_entry = self._ws_prices.get(symbol)
                if ws_entry is not None:
                    price, ts_ms = ws_entry
                    age = now_ms - ts_ms
                    if age <= self._ttl_ms:
                        result[symbol] = PriceSnapshot(price=price, source="WS", age_ms=max(age, 0))
                        continue
                http_entry = self._http_prices.get(symbol)
                if http_entry is None:
                    continue
                price, ts_ms = http_entry
                age = now_ms - ts_ms
                result[symbol] = PriceSnapshot(price=price, source="HTTP", age_ms=max(age, 0))
        return result

    def _http_loop(self) -> None:
        last_request_ms = 0
        while not self._stop_event.is_set():
            now_ms = utc_ms()
            if now_ms - last_request_ms < self._refresh_interval_ms:
                time.sleep(self._refresh_interval_ms / 1000)
                continue
            symbols = self._get_symbols_snapshot()
            if not symbols or not self._fallback_enabled:
                time.sleep(self._refresh_interval_ms / 1000)
                continue
            if not self._should_refresh_http(symbols, now_ms):
                time.sleep(self._refresh_interval_ms / 1000)
                continue
            try:
                prices = self._http_client.get_ticker_price()
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("HTTP fallback failed: %s", exc)
                time.sleep(self._refresh_interval_ms / 1000)
                continue
            if not isinstance(prices, dict):
                time.sleep(self._refresh_interval_ms / 1000)
                continue
            with self._lock:
                for symbol, price_str in prices.items():
                    try:
                        price_value = float(price_str)
                    except ValueError:
                        continue
                    self._http_prices[symbol] = (price_value, now_ms)
            last_request_ms = now_ms
            time.sleep(self._refresh_interval_ms / 1000)

    def _get_symbols_snapshot(self) -> list[str]:
        with self._lock:
            return list(self._symbols)

    def _should_refresh_http(self, symbols: list[str], now_ms: int) -> bool:
        with self._lock:
            for symbol in symbols:
                ws_entry = self._ws_prices.get(symbol)
                if ws_entry is None:
                    return True
                _, ts_ms = ws_entry
                if now_ms - ts_ms > self._ttl_ms:
                    return True
        return False
