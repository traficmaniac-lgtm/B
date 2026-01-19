from __future__ import annotations

import threading
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
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._logger = get_logger("services.prices")

    def start(self) -> None:
        self._stop_event.clear()
        self._ws_client.start()
        if self._fallback_enabled:
            self._logger.info("HTTP fallback disabled; using WS only.")

    def stop(self) -> None:
        self._stop_event.set()
        self._ws_client.stop()

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
            return None

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
        return result

    def _get_symbols_snapshot(self) -> list[str]:
        with self._lock:
            return list(self._symbols)
