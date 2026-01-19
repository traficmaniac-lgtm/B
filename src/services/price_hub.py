from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from src.core.config import Config
from src.core.logging import get_logger
from src.gui.models.app_state import AppState
from src.binance.http_client import BinanceHttpClient
from src.services.price_feed_manager import PriceFeedManager, PriceUpdate


class PriceHub(QObject):
    price_updated = Signal(str, float, str, int)

    def __init__(
        self,
        config: Config,
        app_state: AppState,
        price_feed_manager: PriceFeedManager | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._logger = get_logger("services.price_hub")
        self._http_client = BinanceHttpClient(
            base_url=config.binance.base_url,
            timeout_s=config.http.timeout_s,
            retries=config.http.retries,
            backoff_base_s=config.http.backoff_base_s,
            backoff_max_s=config.http.backoff_max_s,
        )
        self._price_manager = price_feed_manager or PriceFeedManager.get_instance(config)
        self._symbols: set[str] = set()
        self._latest: dict[str, PriceUpdate] = {}
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
        self._price_manager.register_symbol(cleaned)
        self._price_manager.subscribe(cleaned, self._handle_price_update)
        if not self._timer.isActive():
            self._price_manager.start()
            self._timer.start()

    def unregister_symbol(self, symbol: str) -> None:
        cleaned = symbol.strip()
        if cleaned in self._symbols:
            self._symbols.remove(cleaned)
            self._price_manager.unsubscribe(cleaned, self._handle_price_update)
            self._price_manager.unregister_symbol(cleaned)
        if not self._symbols:
            self._timer.stop()

    def shutdown(self) -> None:
        self._timer.stop()
        for symbol in list(self._symbols):
            self._price_manager.unsubscribe(symbol, self._handle_price_update)
            self._price_manager.unregister_symbol(symbol)
        self._symbols.clear()

    def _emit_snapshot(self) -> None:
        if not self._symbols:
            return
        for symbol in self._symbols:
            update = self._latest.get(symbol)
            if update and update.last_price is not None and update.price_age_ms is not None:
                self.price_updated.emit(
                    symbol,
                    update.last_price,
                    update.source,
                    update.price_age_ms,
                )

    def _handle_price_update(self, update: PriceUpdate) -> None:
        self._latest[update.symbol] = update

    def fetch_ticker_24h(self, symbol: str) -> dict[str, object]:
        return self._http_client.get_ticker_24h(symbol)
