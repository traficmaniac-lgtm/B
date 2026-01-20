from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class AiOperatorGridWindow(QMainWindow):
    def __init__(self, symbol: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._symbol = symbol

        self.setWindowTitle(f"AI Operator Grid — {symbol}")
        self.resize(1280, 860)

        central = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._build_market_strip())

        content = QHBoxLayout()
        content.addWidget(self._build_ai_control_panel(), stretch=3)
        content.addWidget(self._build_strategy_panel(), stretch=1)
        layout.addLayout(content)

        central.setLayout(layout)
        self.setCentralWidget(central)

    def _build_market_strip(self) -> QWidget:
        strip = QFrame()
        strip.setFrameShape(QFrame.StyledPanel)
        layout = QHBoxLayout()
        layout.setContentsMargins(8, 6, 8, 6)

        label = QLabel(
            f"[ {self._symbol} ] LAST: — | Spread: — | Vol: — | ATR: — | Latency: — | STATE: —"
        )
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(label)
        strip.setLayout(layout)
        return strip

    def _build_ai_control_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout()

        title = QLabel("AI Control Panel")
        title.setStyleSheet("font-weight: 600; font-size: 16px;")
        layout.addWidget(title)

        self._history = QPlainTextEdit()
        self._history.setReadOnly(True)
        self._history.setPlaceholderText("AI Operator log")
        self._history.appendPlainText("[AI] Watching… (skeleton)")
        layout.addWidget(self._history, stretch=1)

        input_row = QHBoxLayout()
        self._command_input = QLineEdit()
        self._command_input.setPlaceholderText("Type a command for AI Operator…")
        input_row.addWidget(self._command_input)

        send_button = QPushButton("Send")
        send_button.clicked.connect(self._handle_send)
        input_row.addWidget(send_button)
        layout.addLayout(input_row)

        panel.setLayout(layout)
        return panel

    def _build_strategy_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        panel.setFixedWidth(380)
        layout = QVBoxLayout()

        title = QLabel("Strategy (AI Autofill + Override)")
        title.setStyleSheet("font-weight: 600; font-size: 14px;")
        layout.addWidget(title)

        ai_control_row = QHBoxLayout()
        ai_control_label = QLabel("AI Control:")
        self._ai_control_mode = QComboBox()
        self._ai_control_mode.addItems(["AUTO", "LOCKED", "OVERRIDE"])
        ai_control_row.addWidget(ai_control_label)
        ai_control_row.addWidget(self._ai_control_mode)
        ai_control_row.addStretch()
        layout.addLayout(ai_control_row)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)

        self._risk_combo = QComboBox()
        self._risk_combo.addItems(["Low", "Med", "High"])
        form.addRow("Risk Level", self._risk_combo)

        self._activity_combo = QComboBox()
        self._activity_combo.addItems(["Low", "Med", "High"])
        form.addRow("Activity", self._activity_combo)

        self._bias_combo = QComboBox()
        self._bias_combo.addItems(["NEUTRAL", "BIASED_LONG", "BIASED_SHORT"])
        form.addRow("Bias", self._bias_combo)

        self._step_spin = self._make_double_spin()
        form.addRow("Step %", self._step_spin)

        self._range_spin = self._make_double_spin()
        form.addRow("Range ±%", self._range_spin)

        self._levels_spin = QSpinBox()
        self._levels_spin.setRange(1, 500)
        form.addRow("Levels", self._levels_spin)

        self._exposure_spin = self._make_double_spin()
        form.addRow("Max Exposure", self._exposure_spin)

        layout.addLayout(form)

        buttons_layout = QVBoxLayout()
        buttons_layout.setSpacing(8)
        for label in [
            "Start (AI Plan)",
            "Pause (AI)",
            "Emergency Stop (Cancel All)",
            "Rebuild Grid",
            "Ask AI",
        ]:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, name=label: self._log_ui_click(name))
            buttons_layout.addWidget(button)
        buttons_layout.addStretch()
        layout.addLayout(buttons_layout)

        panel.setLayout(layout)
        return panel

    def _make_double_spin(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(4)
        spin.setRange(0.0, 1000000.0)
        spin.setSingleStep(0.1)
        return spin

    def _handle_send(self) -> None:
        text = self._command_input.text().strip()
        if not text:
            return
        self._history.appendPlainText(f"[YOU] {text}")
        self._history.appendPlainText("[AI] (skeleton) Command received")
        self._command_input.clear()

    def _log_ui_click(self, label: str) -> None:
        self._history.appendPlainText(f"[UI] {label} clicked (skeleton)")
