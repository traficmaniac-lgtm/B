from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)


@dataclass
class PilotConfig:
    min_profit_bps: float = 0.05
    max_step_pct: float = 0.06
    max_range_pct: float = 0.40
    hold_timeout_sec: int = 30
    stale_policy: str = "CANCEL_REPLACE"
    allow_market_close: bool = True
    allow_guard_autofix: bool = False
    trade_allowed_families: set[str] = field(default_factory=lambda: {"2LEG"})
    trade_min_profit_bps: float = 8.0
    trade_min_life_s: float = 3.0


class PilotSettingsDialog(QDialog):
    def __init__(
        self,
        pilot_config: PilotConfig,
        on_save: Callable[[PilotConfig], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._pilot_config = pilot_config
        self._on_save = on_save

        self.setWindowTitle("Настройки пилота")
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

        self._min_profit_input = self._make_bps_input(self._pilot_config.min_profit_bps)
        self._max_step_input = self._make_pct_input(self._pilot_config.max_step_pct)
        self._max_range_input = self._make_pct_input(self._pilot_config.max_range_pct)
        self._hold_timeout_input = self._make_seconds_input(self._pilot_config.hold_timeout_sec)
        self._allow_market_checkbox = QCheckBox("Разрешить MARKET закрытие")
        self._allow_market_checkbox.setChecked(self._pilot_config.allow_market_close)

        form.addRow(QLabel("Min profit (bps)"), self._min_profit_input)
        form.addRow(QLabel("Max step (%)"), self._max_step_input)
        form.addRow(QLabel("Max range (%)"), self._max_range_input)
        form.addRow(QLabel("Hold timeout (sec)"), self._hold_timeout_input)
        form.addRow(self._allow_market_checkbox)
        return form

    @staticmethod
    def _make_bps_input(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 1000.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.05)
        spin.setValue(value)
        return spin

    @staticmethod
    def _make_pct_input(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 10.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.05)
        spin.setValue(value)
        return spin

    @staticmethod
    def _make_seconds_input(value: int) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 3600.0)
        spin.setDecimals(0)
        spin.setSingleStep(5.0)
        spin.setValue(value)
        return spin

    def _handle_save(self) -> None:
        self._pilot_config.min_profit_bps = self._min_profit_input.value()
        self._pilot_config.max_step_pct = self._max_step_input.value()
        self._pilot_config.max_range_pct = self._max_range_input.value()
        self._pilot_config.hold_timeout_sec = int(self._hold_timeout_input.value())
        self._pilot_config.allow_market_close = self._allow_market_checkbox.isChecked()
        self._on_save(self._pilot_config)
        self.accept()
