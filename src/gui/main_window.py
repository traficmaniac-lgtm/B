from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabBar,
    QTabWidget,
    QToolBar,
)

from src.core.config import Config
from src.core.logging import get_logger
from src.gui.models.app_state import AppState
from src.gui.overview_tab import OverviewTab
from src.gui.pair_workspace_tab import PairWorkspaceTab
from src.gui.settings_dialog import SettingsDialog
from src.gui.widgets.log_dock import LogDock


class MainWindow(QMainWindow):
    def __init__(self, config: Config, app_state: AppState) -> None:
        super().__init__()
        self._config = config
        self._app_state = app_state
        self._logger = get_logger("gui.main_window")

        self.setWindowTitle("BBOT — Desktop Terminal")
        self.resize(1200, 800)

        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._overview_tab = OverviewTab(self._config, on_open_pair=self.open_pair_tab)
        self._tabs.addTab(self._overview_tab, "Overview")
        self._tabs.tabBar().setTabButton(0, QTabBar.RightSide, None)
        self._tabs.tabBar().setTabButton(0, QTabBar.LeftSide, None)
        self._tabs.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self._tabs)

        self._pair_tabs: dict[str, PairWorkspaceTab] = {}

        self._log_dock = LogDock(self)
        self._log_dock.handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        self.addDockWidget(Qt.RightDockWidgetArea, self._log_dock)
        self._log_dock.setVisible(self._app_state.show_logs)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._ready_label = self._status_bar.addWidget(self._build_status_left())
        self._status_right_label = self._build_status_right()
        self._status_bar.addPermanentWidget(self._status_right_label)

        self._build_menu()
        self._build_toolbar()

        self._logger.info("main window initialized")

    @property
    def log_handler(self) -> logging.Handler:
        return self._log_dock.handler

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def open_pair_tab(self, symbol: str) -> None:
        if symbol in self._pair_tabs:
            self._tabs.setCurrentWidget(self._pair_tabs[symbol])
            return
        tab = PairWorkspaceTab(symbol=symbol, app_state=self._app_state)
        self._pair_tabs[symbol] = tab
        index = self._tabs.addTab(tab, f"Bot: {symbol}")
        self._tabs.setCurrentIndex(index)

    def _build_status_left(self) -> QLabel:
        return QLabel("Ready")

    def _build_status_right(self) -> QLabel:
        env_label = self._app_state.env.lower()
        return QLabel(f"env: {env_label} | core: loaded")

    def _toggle_logs_dock(self, visible: bool) -> None:
        self._log_dock.setVisible(visible)
        self._app_state.show_logs = visible

    def _handle_settings_save(self, app_state: AppState) -> None:
        if app_state.user_config_path is None:
            return
        app_state.save(Path(app_state.user_config_path))
        root_logger = logging.getLogger()
        root_logger.setLevel(app_state.log_level)
        self.statusBar().showMessage("Settings saved", 3000)
        self._status_right_label.setText(f"env: {app_state.env.lower()} | core: loaded")

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("File")
        settings_action = menu.addAction("Settings")
        settings_action.triggered.connect(self._open_settings_dialog)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setObjectName("MainToolbar")
        self.addToolBar(toolbar)
        settings_action = toolbar.addAction("Settings")
        settings_action.triggered.connect(self._open_settings_dialog)

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self._app_state, on_save=self._handle_settings_save, parent=self)
        dialog.exec()

    def _close_tab(self, index: int) -> None:
        if index == 0:
            return
        widget = self._tabs.widget(index)
        if isinstance(widget, PairWorkspaceTab):
            symbol = widget.symbol
            widget.shutdown()
            self._pair_tabs.pop(symbol, None)
        self._tabs.removeTab(index)
