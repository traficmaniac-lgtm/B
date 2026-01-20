from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger
from src.gui.models.app_state import AppState


class SettingsDialog(QDialog):
    def __init__(
        self,
        app_state: AppState,
        on_save: Callable[[AppState], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._logger = get_logger("gui.settings")
        self._app_state = app_state
        self._on_save = on_save

        self.setWindowTitle("Settings")
        self.setModal(True)

        layout = QVBoxLayout()
        layout.addLayout(self._build_form())

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._handle_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def _build_form(self) -> QFormLayout:
        form = QFormLayout()

        self._binance_key_input = QLineEdit(self._app_state.binance_api_key)
        self._binance_key_input.setEchoMode(QLineEdit.Password)

        self._binance_secret_input = QLineEdit(self._app_state.binance_api_secret)
        self._binance_secret_input.setEchoMode(QLineEdit.Password)

        self._openai_key_input = QLineEdit(self._app_state.openai_api_key)
        self._openai_key_input.setEchoMode(QLineEdit.Password)

        self._openai_model_combo = QComboBox()
        self._openai_model_combo.addItems(["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"])
        self._openai_model_combo.setCurrentText(self._app_state.openai_model)

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

        self._zero_fee_symbols_input = QLineEdit(", ".join(self._app_state.zero_fee_symbols))
        self._zero_fee_symbols_input.setPlaceholderText("USDTUSDC, EURIUSDT")

        form.addRow(QLabel("Binance API key"), self._binance_key_input)
        form.addRow(QLabel("Binance API secret"), self._binance_secret_input)
        form.addRow(QLabel("OpenAI API key"), self._openai_key_input)
        form.addRow(QLabel("OpenAI model"), self._openai_model_combo)
        form.addRow(QLabel("Default period"), self._default_period_combo)
        form.addRow(QLabel("Default quality"), self._default_quality_combo)
        form.addRow(QLabel("Price TTL (ms)"), self._price_ttl_input)
        form.addRow(QLabel("Price refresh (ms)"), self._price_refresh_input)
        form.addRow(QLabel("Default quote"), self._default_quote_combo)
        form.addRow(QLabel("Zero-fee symbols"), self._zero_fee_symbols_input)
        form.addRow(self._allow_ai_more_data)
        return form

    def _handle_save(self) -> None:
        previous_openai_key = self._app_state.openai_api_key
        previous_model = self._app_state.openai_model
        self._app_state.binance_api_key = self._binance_key_input.text().strip()
        self._app_state.binance_api_secret = self._binance_secret_input.text().strip()
        self._app_state.openai_api_key = self._openai_key_input.text().strip()
        self._app_state.openai_model = self._openai_model_combo.currentText().strip()
        self._app_state.default_period = self._default_period_combo.currentText()
        self._app_state.default_quality = self._default_quality_combo.currentText()
        self._app_state.price_ttl_ms = self._price_ttl_input.value()
        self._app_state.price_refresh_ms = self._price_refresh_input.value()
        self._app_state.default_quote = self._default_quote_combo.currentText()
        self._app_state.allow_ai_more_data = self._allow_ai_more_data.isChecked()
        self._app_state.zero_fee_symbols = [
            item.strip().upper()
            for item in self._zero_fee_symbols_input.text().replace("\n", ",").split(",")
            if item.strip()
        ]
        api_key, api_secret = self._app_state.get_binance_keys()
        self._logger.info("binance keys present: %s", bool(api_key and api_secret))
        if (
            previous_openai_key != self._app_state.openai_api_key
            or previous_model != self._app_state.openai_model
        ):
            self._app_state.ai_connected = False
            self._app_state.ai_checked = False
        self._on_save(self._app_state)
        config_path = self._app_state.user_config_path or Path("config.user.yaml")
        self._logger.info("[SETTINGS] saved to %s", config_path.as_posix())
        self.accept()
