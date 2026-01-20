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
    QSpinBox,
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

        self._binance_key_input = QLineEdit(self._app_state.binance_api_key)
        self._binance_key_input.setEchoMode(QLineEdit.Password)
        self._binance_key_hint = QLabel(self._key_present_label(self._app_state.binance_api_key))

        self._binance_secret_input = QLineEdit(self._app_state.binance_api_secret)
        self._binance_secret_input.setEchoMode(QLineEdit.Password)
        self._binance_secret_hint = QLabel(self._key_present_label(self._app_state.binance_api_secret))

        self._openai_key_input = QLineEdit(self._app_state.openai_api_key)
        self._openai_key_input.setEchoMode(QLineEdit.Password)
        self._openai_key_hint = QLabel(self._key_present_label(self._app_state.openai_api_key))

        self._default_period_combo = QComboBox()
        self._default_period_combo.addItems(
            ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "12h", "24h", "1d", "7d"]
        )
        self._default_period_combo.setCurrentText(self._app_state.default_period)

        self._default_quality_combo = QComboBox()
        self._default_quality_combo.addItems(["Standard", "Deep"])
        self._default_quality_combo.setCurrentText(self._app_state.default_quality)

        self._price_ttl_input = QSpinBox()
        self._price_ttl_input.setRange(500, 60000)
        self._price_ttl_input.setValue(self._app_state.price_ttl_ms)

        self._price_refresh_input = QSpinBox()
        self._price_refresh_input.setRange(200, 10000)
        self._price_refresh_input.setValue(self._app_state.price_refresh_ms)

        self._default_quote_combo = QComboBox()
        self._default_quote_combo.addItems(["USDT", "USDC", "FDUSD", "EUR"])
        self._default_quote_combo.setCurrentText(self._app_state.default_quote)

        self._allow_ai_more_data = QCheckBox("Allow AI to request more data")
        self._allow_ai_more_data.setChecked(self._app_state.allow_ai_more_data)

        self._show_logs_checkbox = QCheckBox("Show logs dock")
        self._show_logs_checkbox.setChecked(self._app_state.show_logs)
        self._show_logs_checkbox.toggled.connect(self._on_toggle_logs)

        form.addRow(QLabel("env"), self._env_combo)
        form.addRow(QLabel("log level"), self._log_level_combo)
        form.addRow(QLabel("config file path"), self._config_path_input)
        form.addRow(self._show_logs_checkbox)

        form.addRow(QLabel("Binance API key"), self._build_key_row(self._binance_key_input, self._binance_key_hint))
        form.addRow(
            QLabel("Binance API secret"),
            self._build_key_row(self._binance_secret_input, self._binance_secret_hint),
        )
        form.addRow(QLabel("OpenAI key"), self._build_key_row(self._openai_key_input, self._openai_key_hint))
        form.addRow(QLabel("Default period"), self._default_period_combo)
        form.addRow(QLabel("Default quality"), self._default_quality_combo)
        form.addRow(QLabel("Price TTL (ms)"), self._price_ttl_input)
        form.addRow(QLabel("Price refresh (ms)"), self._price_refresh_input)
        form.addRow(QLabel("Default quote"), self._default_quote_combo)
        form.addRow(self._allow_ai_more_data)
        return form

    def _handle_save(self) -> None:
        self._app_state.env = self._env_combo.currentText().upper()
        self._app_state.log_level = self._log_level_combo.currentText().upper()
        self._app_state.config_path = self._config_path_input.text().strip()
        self._app_state.show_logs = self._show_logs_checkbox.isChecked()
        self._app_state.binance_api_key = self._binance_key_input.text().strip()
        self._app_state.binance_api_secret = self._binance_secret_input.text().strip()
        self._app_state.openai_api_key = self._openai_key_input.text().strip()
        self._app_state.default_period = self._default_period_combo.currentText()
        self._app_state.default_quality = self._default_quality_combo.currentText()
        self._app_state.price_ttl_ms = self._price_ttl_input.value()
        self._app_state.price_refresh_ms = self._price_refresh_input.value()
        self._app_state.default_quote = self._default_quote_combo.currentText()
        self._app_state.allow_ai_more_data = self._allow_ai_more_data.isChecked()
        self._refresh_key_hints()
        api_key, api_secret = self._app_state.get_binance_keys()
        self._logger.info("binance keys present: %s", bool(api_key and api_secret))
        self._on_save(self._app_state)
        self._logger.info(
            "settings saved to %s",
            Path(self._app_state.user_config_path).as_posix() if self._app_state.user_config_path else "",
        )

    @staticmethod
    def _key_present_label(value: str) -> str:
        return "Key present: yes" if value else "Key present: no"

    def _build_key_row(self, input_field: QLineEdit, hint: QLabel) -> QWidget:
        layout = QHBoxLayout()
        layout.addWidget(input_field)
        layout.addWidget(hint)
        wrapper = QWidget()
        wrapper.setLayout(layout)
        return wrapper

    def _refresh_key_hints(self) -> None:
        self._binance_key_hint.setText(self._key_present_label(self._app_state.binance_api_key))
        self._binance_secret_hint.setText(self._key_present_label(self._app_state.binance_api_secret))
        self._openai_key_hint.setText(self._key_present_label(self._app_state.openai_api_key))
