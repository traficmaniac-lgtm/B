from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
    min_expected_profit_bps: float = 0.20
    slippage_bps: float = 0.30
    pad_bps: float = 0.50
    thin_edge_action: str = "HOLD"
    auto_shift_anchor: bool = True
    step_expand_factor: float = 1.5
    range_expand_factor: float = 1.25
    stale_policy: str = "CANCEL_REPLACE"
    allow_market_close: bool = True


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

        self._min_profit_input = self._make_bps_input(self._pilot_config.min_expected_profit_bps)
        self._slippage_input = self._make_bps_input(self._pilot_config.slippage_bps)
        self._pad_input = self._make_bps_input(self._pilot_config.pad_bps)

        self._thin_edge_action_combo = QComboBox()
        self._thin_edge_action_combo.addItems(["HOLD"])
        if self._pilot_config.thin_edge_action not in {"HOLD"}:
            self._thin_edge_action_combo.addItem(self._pilot_config.thin_edge_action)
        self._thin_edge_action_combo.setCurrentText(self._pilot_config.thin_edge_action)

        self._auto_shift_anchor_checkbox = QCheckBox("Сдвигать якорь автоматически")
        self._auto_shift_anchor_checkbox.setChecked(self._pilot_config.auto_shift_anchor)

        self._step_expand_input = self._make_factor_input(self._pilot_config.step_expand_factor)
        self._range_expand_input = self._make_factor_input(self._pilot_config.range_expand_factor)

        self._stale_policy_combo = QComboBox()
        self._stale_policy_combo.addItem("CANCEL_REPLACE")
        self._stale_policy_combo.addItem("RECENTER")
        self._stale_policy_combo.addItem("NONE")
        self._stale_policy_combo.setCurrentText(self._pilot_config.stale_policy)

        self._allow_market_checkbox = QCheckBox("Разрешить MARKET закрытие")
        self._allow_market_checkbox.setChecked(self._pilot_config.allow_market_close)

        form.addRow(QLabel("Min expected profit (bps)"), self._min_profit_input)
        form.addRow(QLabel("Slippage (bps)"), self._slippage_input)
        form.addRow(QLabel("Pad (bps)"), self._pad_input)
        form.addRow(QLabel("Thin edge action"), self._thin_edge_action_combo)
        form.addRow(self._auto_shift_anchor_checkbox)
        form.addRow(QLabel("Step expand factor"), self._step_expand_input)
        form.addRow(QLabel("Range expand factor"), self._range_expand_input)
        form.addRow(QLabel("Stale policy"), self._stale_policy_combo)
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
    def _make_factor_input(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(1.0, 10.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.05)
        spin.setValue(value)
        return spin

    def _handle_save(self) -> None:
        self._pilot_config.min_expected_profit_bps = self._min_profit_input.value()
        self._pilot_config.slippage_bps = self._slippage_input.value()
        self._pilot_config.pad_bps = self._pad_input.value()
        self._pilot_config.thin_edge_action = self._thin_edge_action_combo.currentText().strip()
        self._pilot_config.auto_shift_anchor = self._auto_shift_anchor_checkbox.isChecked()
        self._pilot_config.step_expand_factor = self._step_expand_input.value()
        self._pilot_config.range_expand_factor = self._range_expand_input.value()
        self._pilot_config.stale_policy = self._stale_policy_combo.currentText().strip()
        self._pilot_config.allow_market_close = self._allow_market_checkbox.isChecked()
        self._on_save(self._pilot_config)
        self.accept()
