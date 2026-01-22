from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QWidget

from src.core.config import Config
from src.core.logging import get_logger
from src.gui.models.pair_mode import (
    PAIR_MODE_LITE,
    PAIR_MODE_AI_OPERATOR_GRID,
    PAIR_MODE_AI_FULL_STRATEG_V2,
    PAIR_MODE_TRADE_READY,
    PAIR_MODE_TRADING,
    PairMode,
)
from src.gui.ai_operator_grid_window import AiOperatorGridWindow
from src.gui.modes.ai_full_strateg_v2.controller import AiFullStrategV2Controller
from src.gui.models.app_state import AppState
from src.gui.pair_action_dialog import PairActionDialog
from src.gui.trade_ready_mode_window import TradeReadyModeWindow
from src.services.price_feed_manager import PriceFeedManager


class PairModeManager:
    def __init__(
        self,
        open_trading_workspace: Callable[[str], None],
        config: Config | None = None,
        app_state: AppState | None = None,
        market_state: object | None = None,
        exchange_name: str = "Binance",
        price_feed_manager: PriceFeedManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        self._open_trading_workspace = open_trading_workspace
        self._config = config
        self._app_state = app_state
        self._parent = parent
        self._market_state = market_state
        self._exchange_name = exchange_name
        self._price_feed_manager = price_feed_manager
        self._logger = get_logger("gui.pair_mode_manager")
        self._trade_ready_windows: list[TradeReadyModeWindow] = []
        self._ai_operator_grid_windows: list[AiOperatorGridWindow] = []
        self._ai_full_strateg_v2_windows: list[QWidget] = []

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
            "[MODE] selected=%s symbol=%s",
            dialog.selected_mode.name,
            symbol,
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
        if mode == PAIR_MODE_LITE:
            self._logger.info("[MODE] open window=LiteGridWindow symbol=%s", symbol)
            self._open_trading_workspace(symbol)
            return
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
            if self._config is None or self._app_state is None or self._price_feed_manager is None:
                self._logger.error("AI Operator Grid unavailable: missing runtime dependencies.")
                return
            window = AiOperatorGridWindow(
                symbol=symbol,
                config=self._config,
                app_state=self._app_state,
                price_feed_manager=self._price_feed_manager,
                parent=window_parent,
            )
            window.show()
            self._ai_operator_grid_windows.append(window)
            return
        if mode == PAIR_MODE_AI_FULL_STRATEG_V2:
            self._logger.info("[MODE] open window=AiFullStrategV2Window symbol=%s", symbol)
            controller = AiFullStrategV2Controller(
                config=self._config,
                app_state=self._app_state,
                price_feed_manager=self._price_feed_manager,
                parent=window_parent,
            )
            window = controller.open(symbol)
            if window is not None:
                self._ai_full_strateg_v2_windows.append(window)
