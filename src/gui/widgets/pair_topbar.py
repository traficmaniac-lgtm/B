from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QWidget,
)


class PairTopBar(QWidget):
    def __init__(self, symbol: str, default_period: str, default_quality: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout()

        self.symbol_label = QLabel(symbol)
        self.symbol_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.symbol_label)

        self.price_label = QLabel("Price: --")
        self.price_label.setStyleSheet("color: #4b5563;")
        layout.addWidget(self.price_label)

        self.period_combo = QComboBox()
        self.period_combo.addItems(["1h", "4h", "12h", "24h", "7d"])
        self.period_combo.setCurrentText(default_period)
        layout.addWidget(QLabel("Period"))
        layout.addWidget(self.period_combo)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["Standard", "Deep"])
        self.quality_combo.setCurrentText(default_quality)
        layout.addWidget(QLabel("Quality"))
        layout.addWidget(self.quality_combo)

        self.prepare_button = QPushButton("Prepare Data")
        self.analyze_button = QPushButton("AI Analyze")
        self.apply_button = QPushButton("Apply Plan")
        self.confirm_button = QPushButton("Confirm Start")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")

        self.prepare_button.setToolTip("Step 1: Collect and normalize market data")
        self.analyze_button.setToolTip("Step 2: AI analysis only (no execution)")
        self.apply_button.setToolTip("Step 3: Fill strategy parameters (no trading)")
        self.confirm_button.setToolTip("Step 4: Manual confirmation & start")
        self.pause_button.setToolTip("Pause bot loop")
        self.stop_button.setToolTip("Stop bot loop (safe stop later)")

        layout.addWidget(self.prepare_button)
        self._add_separator(layout)
        layout.addWidget(self.analyze_button)
        self._add_separator(layout)
        layout.addWidget(self.apply_button)
        self._add_separator(layout)
        layout.addWidget(self.confirm_button)
        self._add_separator(layout, spacing=24)
        layout.addWidget(self.pause_button)
        layout.addWidget(self.stop_button)

        self.dry_run_toggle = QCheckBox("Dry-run")
        self.dry_run_toggle.setChecked(True)
        self.dry_run_toggle.setToolTip("Simulate execution (no real orders)")
        layout.addWidget(self.dry_run_toggle)

        self.state_label = QLabel("State: IDLE")
        state_font = self.state_label.font()
        state_font.setBold(True)
        self.state_label.setFont(state_font)
        layout.addWidget(self.state_label)
        layout.addStretch()

        self.setLayout(layout)

    def _add_separator(self, layout: QHBoxLayout, spacing: int = 12) -> None:
        layout.addSpacing(spacing)
        separator = QFrame()
        separator.setFrameShape(QFrame.VLine)
        separator.setFrameShadow(QFrame.Sunken)
        layout.addWidget(separator)
        layout.addSpacing(spacing)
