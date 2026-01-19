from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QWidget

from src.gui.advanced_ai_mode_window import AdvancedAIModeWindow
from src.gui.models.pair_mode import PAIR_MODE_ADVANCED, PAIR_MODE_TRADING, PairMode
from src.gui.pair_action_dialog import PairActionDialog


class PairModeManager:
    def __init__(
        self,
        open_trading_workspace: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        self._open_trading_workspace = open_trading_workspace
        self._parent = parent
        self._advanced_windows: list[AdvancedAIModeWindow] = []

    def open_pair_dialog(self, symbol: str, parent: QWidget | None = None) -> None:
        dialog_parent = parent or self._parent
        dialog = PairActionDialog(symbol=symbol, parent=dialog_parent)
        if dialog.exec() != QDialog.Accepted:
            return
        if dialog.selected_mode is None:
            return
        self.open_pair_mode(symbol, dialog.selected_mode, parent=parent)

    def open_pair_mode(self, symbol: str, mode: PairMode, parent: QWidget | None = None) -> None:
        window_parent = parent or self._parent
        if mode == PAIR_MODE_TRADING:
            self._open_trading_workspace(symbol)
            return
        if mode == PAIR_MODE_ADVANCED:
            window = AdvancedAIModeWindow(symbol=symbol, parent=window_parent)
            window.show()
            self._advanced_windows.append(window)
