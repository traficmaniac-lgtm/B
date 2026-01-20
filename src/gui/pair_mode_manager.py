from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QWidget

from src.core.logging import get_logger
from src.gui.models.pair_mode import (
    PAIR_MODE_AI_OPERATOR_GRID,
    PAIR_MODE_TRADE_READY,
    PAIR_MODE_TRADING,
    PairMode,
)
from src.gui.ai_operator_grid_window import AiOperatorGridWindow
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
        self._logger = get_logger("gui.pair_mode_manager")
        self._trade_ready_windows: list[TradeReadyModeWindow] = []
        self._ai_operator_grid_windows: list[AiOperatorGridWindow] = []

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
        self._logger.info(
            "[MODE] dialog accepted symbol=%s selected=%s",
            symbol,
            dialog.selected_mode.name,
        )
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
            self._logger.info("[MODE] open window=LiteGridWindow symbol=%s", symbol)
            self._open_trading_workspace(symbol)
            return
        if mode == PAIR_MODE_TRADE_READY:
            self._logger.info("[MODE] open window=TradeReadyModeWindow symbol=%s", symbol)
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
            return
        if mode == PAIR_MODE_AI_OPERATOR_GRID:
            self._logger.info("[MODE] open window=AiOperatorGridWindow symbol=%s", symbol)
            window = AiOperatorGridWindow(symbol=symbol, parent=window_parent)
            window.show()
            self._ai_operator_grid_windows.append(window)
