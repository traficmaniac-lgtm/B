from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QWidget

from src.core.config import Config
from src.core.logging import get_logger
from src.gui.lite_all_strategy_algo_pilot_window import LiteAllStrategyAlgoPilotWindow
from src.gui.lite_all_strategy_nc_micro_window import NcMicroMainWindow
from src.gui.lite_all_strategy_nc_pilot_window import NcPilotMainWindow
from src.gui.lite_all_strategy_terminal_window import LiteAllStrategyTerminalWindow
from src.gui.models.pair_mode import (
    PAIR_MODE_ALGO_PILOT,
    PAIR_MODE_LITE,
    PAIR_MODE_LITE_ALL_STRATEGY,
    PAIR_MODE_NC_MICRO,
    PAIR_MODE_NC_PILOT,
    PairMode,
)
from src.gui.models.app_state import AppState
from src.gui.pair_action_dialog import PairActionDialog
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
        self._lite_all_strategy_windows: list[LiteAllStrategyTerminalWindow] = []
        self._lite_all_strategy_algo_pilot_windows: list[LiteAllStrategyAlgoPilotWindow] = []
        self._nc_micro_main_window: NcMicroMainWindow | None = None
        self._nc_pilot_main_window: NcPilotMainWindow | None = None

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
        if mode == PAIR_MODE_LITE_ALL_STRATEGY:
            self._logger.info("[MODE] open window=LiteAllStrategyTerminalWindow symbol=%s", symbol)
            if self._config is None or self._app_state is None or self._price_feed_manager is None:
                self._logger.error("Lite All Strategy Terminal unavailable: missing runtime dependencies.")
                return
            window = LiteAllStrategyTerminalWindow(
                symbol=symbol,
                config=self._config,
                app_state=self._app_state,
                price_feed_manager=self._price_feed_manager,
                parent=window_parent,
            )
            window.show()
            self._lite_all_strategy_windows.append(window)
            return
        if mode == PAIR_MODE_ALGO_PILOT:
            self._logger.info("[MODE] open window=LiteAllStrategyAlgoPilotWindow symbol=%s", symbol)
            if self._config is None or self._app_state is None or self._price_feed_manager is None:
                self._logger.error("Lite All Strategy Algo Pilot unavailable: missing runtime dependencies.")
                return
            window = LiteAllStrategyAlgoPilotWindow(
                symbol=symbol,
                config=self._config,
                app_state=self._app_state,
                price_feed_manager=self._price_feed_manager,
                parent=window_parent,
            )
            window.show()
            self._lite_all_strategy_algo_pilot_windows.append(window)
            return
        if mode == PAIR_MODE_NC_MICRO:
            self._logger.info("[MODE] open window=NC_MICRO symbol=%s", symbol)
            if self._config is None or self._app_state is None or self._price_feed_manager is None:
                self._logger.error("Lite All Strategy NC Micro unavailable: missing runtime dependencies.")
                return
            if self._nc_micro_main_window is None:
                self._nc_micro_main_window = NcMicroMainWindow(
                    config=self._config,
                    app_state=self._app_state,
                    price_feed_manager=self._price_feed_manager,
                    parent=window_parent,
                )
                self._nc_micro_main_window.destroyed.connect(self._reset_nc_micro_window)
            self._nc_micro_main_window.add_or_activate_symbol(symbol)
            self._nc_micro_main_window.show()
            self._nc_micro_main_window.raise_()
            self._nc_micro_main_window.activateWindow()
            return
        if mode == PAIR_MODE_NC_PILOT:
            self._logger.info("[MODE] open window=NC_PILOT symbol=%s", symbol)
            if self._config is None or self._app_state is None or self._price_feed_manager is None:
                self._logger.error("Lite All Strategy NC Pilot unavailable: missing runtime dependencies.")
                return
            if self._nc_pilot_main_window is None:
                self._nc_pilot_main_window = NcPilotMainWindow(
                    config=self._config,
                    app_state=self._app_state,
                    price_feed_manager=self._price_feed_manager,
                    parent=window_parent,
                )
                self._nc_pilot_main_window.destroyed.connect(self._reset_nc_pilot_window)
            self._nc_pilot_main_window.add_or_activate_symbol(symbol)
            self._nc_pilot_main_window.show()
            self._nc_pilot_main_window.raise_()
            self._nc_pilot_main_window.activateWindow()
            return
        self._logger.warning("[MODE] unsupported mode=%s symbol=%s", mode.name, symbol)

    def _reset_nc_micro_window(self, *_: object) -> None:
        self._nc_micro_main_window = None

    def _reset_nc_pilot_window(self, *_: object) -> None:
        self._nc_pilot_main_window = None
