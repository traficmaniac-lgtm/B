from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QWidget

from src.gui.models.pair_mode import PAIR_MODE_TRADE_READY, PAIR_MODE_TRADING, PairMode
from src.gui.pair_action_dialog import PairActionDialog
from src.gui.trade_ready_mode_window import TradeReadyModeWindow
from src.services.price_feed_manager import PriceFeedManager


class PairModeManager:
    def __init__(
        self,
        open_trading_workspace: Callable[[str], None],
        market_state: object | None = None,
        exchange_name: str = "Binance",
        price_feed_manager: PriceFeedManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        self._open_trading_workspace = open_trading_workspace
        self._parent = parent
        self._market_state = market_state
        self._exchange_name = exchange_name
        self._price_feed_manager = price_feed_manager
        self._trade_ready_windows: list[TradeReadyModeWindow] = []

    def open_pair_dialog(
        self,
        symbol: str,
        last_price: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        dialog_parent = parent or self._parent
        dialog = PairActionDialog(symbol=symbol, parent=dialog_parent)
        if dialog.exec() != QDialog.Accepted:
            return
        if dialog.selected_mode is None:
            return
        self.open_pair_mode(symbol, dialog.selected_mode, last_price=last_price, parent=parent)

    def open_pair_mode(
        self,
        symbol: str,
        mode: PairMode,
        last_price: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        window_parent = parent or self._parent
        if mode == PAIR_MODE_TRADING:
            self._open_trading_workspace(symbol)
            return
        if mode == PAIR_MODE_TRADE_READY:
            window = TradeReadyModeWindow(
                symbol=symbol,
                exchange=self._exchange_name,
                last_price=last_price,
                market_state=self._market_state,
                price_feed_manager=self._price_feed_manager,
                parent=window_parent,
            )
            window.show()
            self._trade_ready_windows.append(window)
