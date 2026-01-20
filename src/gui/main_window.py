from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QToolBar,
)

from src.core.config import Config
from src.core.logging import get_logger
from src.gui.models.app_state import AppState
from src.gui.lite_grid_window import LiteGridWindow
from src.gui.overview_tab import OverviewTab
from src.gui.settings_dialog import SettingsDialog
from src.gui.widgets.log_dock import LogDock
from src.services.price_feed_manager import PriceFeedManager


class MainWindow(QMainWindow):
    def __init__(self, config: Config, app_state: AppState) -> None:
        super().__init__()
        self._config = config
        self._app_state = app_state
        self._logger = get_logger("gui.main_window")

        self.setWindowTitle("BBOT — Desktop Terminal")
        self.resize(1200, 800)

        self._price_feed_manager = PriceFeedManager.get_instance(self._config)
        self._overview_tab = OverviewTab(
            self._config,
            app_state=self._app_state,
            on_open_pair=self.open_lite_grid_window,
            price_feed_manager=self._price_feed_manager,
        )
        self.setCentralWidget(self._overview_tab)
        self._lite_grid_windows: list[LiteGridWindow] = []

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

    def open_lite_grid_window(self, symbol: str) -> None:
        window = LiteGridWindow(
            symbol=symbol,
            config=self._config,
            app_state=self._app_state,
            price_feed_manager=self._price_feed_manager,
            parent=self,
        )
        window.show()
        self._lite_grid_windows.append(window)
        window.destroyed.connect(lambda _: self._remove_lite_window(window))

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
        self._overview_tab.refresh_account_status()

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

    def _remove_lite_window(self, window: LiteGridWindow) -> None:
        if window in self._lite_grid_windows:
            self._lite_grid_windows.remove(window)

    def closeEvent(self, event: object) -> None:  # noqa: N802
        self._overview_tab.shutdown()
        for window in list(self._lite_grid_windows):
            window.close()
        self._price_feed_manager.shutdown()
        super().closeEvent(event)
