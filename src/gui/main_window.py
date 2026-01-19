from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QMessageBox, QStatusBar, QTabWidget

from src.core.config import Config
from src.core.logging import get_logger
from src.gui.models.app_state import AppState
from src.gui.widgets.bot_tab import BotTab
from src.gui.widgets.dashboard_tab import DashboardTab
from src.gui.widgets.log_dock import LogDock
from src.gui.widgets.markets_tab import MarketsTab
from src.gui.widgets.settings_tab import SettingsTab


class MainWindow(QMainWindow):
    def __init__(self, config: Config, app_state: AppState) -> None:
        super().__init__()
        self._config = config
        self._app_state = app_state
        self._logger = get_logger("gui.main_window")

        self.setWindowTitle("BBOT — Desktop Terminal")
        self.resize(1200, 800)

        self._tabs = QTabWidget()
        self._tabs.addTab(DashboardTab(), "Dashboard")
        self._tabs.addTab(MarketsTab(), "Markets")
        self._tabs.addTab(BotTab(), "Bot")
        self._tabs.addTab(
            SettingsTab(
                app_state,
                on_save=self._handle_settings_save,
                on_toggle_logs=self._toggle_logs_dock,
            ),
            "Settings",
        )
        self.setCentralWidget(self._tabs)

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

        self._logger.info("main window initialized")

    @property
    def log_handler(self) -> logging.Handler:
        return self._log_dock.handler

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

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
