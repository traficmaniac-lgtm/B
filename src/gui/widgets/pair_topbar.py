from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class PairTopBar(QWidget):
    def __init__(self, symbol: str, default_period: str, default_quality: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.symbol_label = QLabel(symbol)
        self.symbol_label.setStyleSheet("font-weight: bold;")
        self._set_header_policy(self.symbol_label)

        self.price_label = QLabel("Price: --")
        self.price_label.setStyleSheet("color: #4b5563;")
        self._set_header_policy(self.price_label)

        self.period_combo = QComboBox()
        self.period_combo.addItems(["1h", "4h", "12h", "24h", "7d"])
        self.period_combo.setCurrentText(default_period)
        self._set_header_policy(self.period_combo)
        period_label = QLabel("Period")
        self._set_header_policy(period_label)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["Standard", "Deep"])
        self.quality_combo.setCurrentText(default_quality)
        self._set_header_policy(self.quality_combo)
        quality_label = QLabel("Quality")
        self._set_header_policy(quality_label)

        self.prepare_button = QPushButton("Prepare Data")
        self.analyze_button = QPushButton("AI Analyze")
        self.apply_button = QPushButton("Apply Plan")
        self.confirm_button = QPushButton("Confirm Start")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")
        for button in (
            self.prepare_button,
            self.analyze_button,
            self.apply_button,
            self.confirm_button,
            self.pause_button,
            self.stop_button,
        ):
            self._set_header_policy(button)

        self.prepare_button.setToolTip("Step 1: Collect and normalize market data")
        self.analyze_button.setToolTip("Step 2: AI analysis only (no execution)")
        self.apply_button.setToolTip("Step 3: Fill strategy parameters (no trading)")
        self.confirm_button.setToolTip("Step 4: Manual confirmation & start")
        self.pause_button.setToolTip("Pause bot loop")
        self.stop_button.setToolTip("Stop bot loop (safe stop later)")

        self.dry_run_toggle = QCheckBox("Dry-run")
        self.dry_run_toggle.setChecked(True)
        self.dry_run_toggle.setToolTip("Simulate execution (no real orders)")
        self._set_header_policy(self.dry_run_toggle)

        self.state_label = QLabel("State: IDLE")
        state_font = self.state_label.font()
        state_font.setBold(True)
        self.state_label.setFont(state_font)
        self._set_header_policy(self.state_label)

        row_top = QHBoxLayout()
        row_top.setContentsMargins(0, 0, 0, 0)

        left_controls = QHBoxLayout()
        left_controls.addWidget(self.symbol_label)
        left_controls.addWidget(self.price_label)
        left_controls.addSpacing(12)
        left_controls.addWidget(period_label)
        left_controls.addWidget(self.period_combo)
        left_controls.addSpacing(8)
        left_controls.addWidget(quality_label)
        left_controls.addWidget(self.quality_combo)
        left_controls_widget = QWidget()
        left_controls_widget.setLayout(left_controls)

        right_controls = QHBoxLayout()
        right_controls.addWidget(self.dry_run_toggle)
        right_controls.addSpacing(12)
        right_controls.addWidget(self.state_label)
        right_controls_widget = QWidget()
        right_controls_widget.setLayout(right_controls)

        row_top.addWidget(left_controls_widget)
        row_top.addStretch()
        row_top.addWidget(right_controls_widget)

        row_bottom = QHBoxLayout()
        row_bottom.addStretch()
        row_bottom.addWidget(self.prepare_button)
        self._add_separator(row_bottom)
        row_bottom.addWidget(self.analyze_button)
        self._add_separator(row_bottom)
        row_bottom.addWidget(self.apply_button)
        self._add_separator(row_bottom)
        row_bottom.addWidget(self.confirm_button)
        self._add_separator(row_bottom, spacing=24)
        row_bottom.addWidget(self.pause_button)
        row_bottom.addWidget(self.stop_button)
        row_bottom.addStretch()

        layout.addLayout(row_top)
        layout.addLayout(row_bottom)

        self.setLayout(layout)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _add_separator(self, layout: QHBoxLayout, spacing: int = 12) -> None:
        layout.addSpacing(spacing)
        separator = QFrame()
        separator.setFrameShape(QFrame.VLine)
        separator.setFrameShadow(QFrame.Sunken)
        layout.addWidget(separator)
        layout.addSpacing(spacing)

    def _set_header_policy(self, widget: QWidget) -> None:
        widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
