from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger
from src.gui.models.app_state import AppState


class SettingsTab(QWidget):
    def __init__(
        self,
        app_state: AppState,
        on_save: Callable[[AppState], None],
        on_toggle_logs: Callable[[bool], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._logger = get_logger("gui.settings")
        self._app_state = app_state
        self._on_save = on_save
        self._on_toggle_logs = on_toggle_logs

        layout = QVBoxLayout()
        layout.addLayout(self._build_form())

        button_row = QHBoxLayout()
        self._save_button = QPushButton("Save")
        self._save_button.clicked.connect(self._handle_save)
        button_row.addWidget(self._save_button)
        button_row.addStretch()

        layout.addLayout(button_row)
        layout.addStretch()
        self.setLayout(layout)

    def _build_form(self) -> QFormLayout:
        form = QFormLayout()

        self._env_combo = QComboBox()
        self._env_combo.addItems(["DEV", "PROD"])
        self._env_combo.setCurrentText(self._app_state.env)

        self._log_level_combo = QComboBox()
        self._log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self._log_level_combo.setCurrentText(self._app_state.log_level)

        self._config_path_input = QLineEdit(self._app_state.config_path)

        self._show_logs_checkbox = QCheckBox("Show logs dock")
        self._show_logs_checkbox.setChecked(self._app_state.show_logs)
        self._show_logs_checkbox.toggled.connect(self._on_toggle_logs)

        form.addRow(QLabel("env"), self._env_combo)
        form.addRow(QLabel("log level"), self._log_level_combo)
        form.addRow(QLabel("config file path"), self._config_path_input)
        form.addRow(self._show_logs_checkbox)
        return form

    def _handle_save(self) -> None:
        self._app_state.env = self._env_combo.currentText().upper()
        self._app_state.log_level = self._log_level_combo.currentText().upper()
        self._app_state.config_path = self._config_path_input.text().strip()
        self._app_state.show_logs = self._show_logs_checkbox.isChecked()
        self._on_save(self._app_state)
        self._logger.info(
            "settings saved to %s",
            Path(self._app_state.user_config_path).as_posix() if self._app_state.user_config_path else "",
        )
