from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from src.binance.http_client import BinanceHttpClient
from src.binance.ws_client import BinanceWsClient
from src.core.config import Config
from src.core.logging import get_logger
from src.gui.models.app_state import AppState
from src.services.price_service import PriceService


class PriceHub(QObject):
    price_updated = Signal(str, float, str, int)

    def __init__(self, config: Config, app_state: AppState, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._logger = get_logger("services.price_hub")
        self._http_client = BinanceHttpClient(
            base_url=config.binance.base_url,
            timeout_s=config.http.timeout_s,
            retries=config.http.retries,
            backoff_base_s=config.http.backoff_base_s,
            backoff_max_s=config.http.backoff_max_s,
        )
        self._ws_client = BinanceWsClient(
            ws_url=config.binance.ws_url,
            on_tick=self._handle_ws_tick,
            on_status=self._handle_ws_status,
        )
        self._price_service = PriceService(
            http_client=self._http_client,
            ws_client=self._ws_client,
            ttl_ms=app_state.price_ttl_ms,
            refresh_interval_ms=app_state.price_refresh_ms,
            fallback_enabled=config.prices.fallback_enabled,
        )
        self._symbols: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(app_state.price_refresh_ms)
        self._timer.timeout.connect(self._emit_snapshot)

    def register_symbol(self, symbol: str) -> None:
        cleaned = symbol.strip()
        if not cleaned:
            return
        if cleaned in self._symbols:
            return
        self._symbols.add(cleaned)
        self._price_service.set_symbols(self._symbols)
        if not self._timer.isActive():
            self._price_service.start()
            self._timer.start()

    def unregister_symbol(self, symbol: str) -> None:
        cleaned = symbol.strip()
        if cleaned in self._symbols:
            self._symbols.remove(cleaned)
        self._price_service.set_symbols(self._symbols)
        if not self._symbols:
            self._timer.stop()
            self._price_service.stop()

    def shutdown(self) -> None:
        self._timer.stop()
        self._price_service.stop()

    def _emit_snapshot(self) -> None:
        if not self._symbols:
            return
        snapshot = self._price_service.snapshot(self._symbols)
        for symbol, price in snapshot.items():
            self.price_updated.emit(symbol, price.price, price.source, price.age_ms)

    def _handle_ws_tick(self, symbol: str, price: float, timestamp_ms: int) -> None:
        self._price_service.on_ws_tick(symbol, price, timestamp_ms)

    def _handle_ws_status(self, status: str, message: str | None) -> None:
        if status == "ERROR":
            self._logger.warning("WS status error: %s", message or status)

    def fetch_ticker_24h(self, symbol: str) -> dict[str, object]:
        return self._http_client.get_ticker_24h(symbol)
