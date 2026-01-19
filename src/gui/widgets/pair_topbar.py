from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)


class PairTopBar(QWidget):
    def __init__(self, symbol: str, default_period: str, default_quality: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout()

        self.symbol_label = QLabel(symbol)
        self.symbol_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.symbol_label)

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
        self.confirm_button = QPushButton("Confirm & Start")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")

        layout.addWidget(self.prepare_button)
        layout.addWidget(self.analyze_button)
        layout.addWidget(self.apply_button)
        layout.addWidget(self.confirm_button)
        layout.addWidget(self.pause_button)
        layout.addWidget(self.stop_button)

        self.dry_run_toggle = QCheckBox("Dry-run")
        self.dry_run_toggle.setChecked(True)
        layout.addWidget(self.dry_run_toggle)

        self.state_label = QLabel("State: IDLE")
        layout.addWidget(self.state_label)
        layout.addStretch()

        self.setLayout(layout)
