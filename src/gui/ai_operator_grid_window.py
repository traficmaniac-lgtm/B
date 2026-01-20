from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)


class AiOperatorGridWindow(QMainWindow):
    def __init__(self, symbol: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._symbol = symbol

        self.setWindowTitle(f"AI Operator Grid — {symbol}")
        self.resize(1050, 720)

        central = QWidget(self)
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(10, 10, 10, 10)
        outer_layout.setSpacing(8)

        outer_layout.addLayout(self._build_header())
        outer_layout.addWidget(self._build_body())

        self.setCentralWidget(central)

    def _build_header(self) -> QVBoxLayout:
        wrapper = QVBoxLayout()
        wrapper.setSpacing(4)

        row_top = QHBoxLayout()
        row_top.setSpacing(8)
        row_bottom = QHBoxLayout()
        row_bottom.setSpacing(8)

        self._symbol_label = QLabel(self._symbol)
        self._symbol_label.setStyleSheet("font-weight: 600; font-size: 16px;")

        self._last_price_label = QLabel("LAST —")
        self._last_price_label.setStyleSheet("font-weight: 600;")

        self._start_button = QPushButton("Start")
        self._pause_button = QPushButton("Pause")
        self._stop_button = QPushButton("Stop")
        self._start_button.clicked.connect(lambda: self._log_ui_click("Start"))
        self._pause_button.clicked.connect(lambda: self._log_ui_click("Pause"))
        self._stop_button.clicked.connect(lambda: self._log_ui_click("Stop"))

        self._dry_run_toggle = QPushButton("DRY-RUN")
        self._dry_run_toggle.setCheckable(True)
        self._dry_run_toggle.setChecked(True)
        self._dry_run_toggle.clicked.connect(lambda: self._log_ui_click("DRY-RUN"))
        badge_base = (
            "padding: 3px 8px; border-radius: 10px; border: 1px solid #d1d5db; "
            "font-weight: 600; min-height: 20px;"
        )
        self._dry_run_toggle.setStyleSheet(
            "QPushButton {"
            f"{badge_base}"
            "background: #f3f4f6;}"
            "QPushButton:checked {background: #16a34a; color: white; border-color: #16a34a;}"
            "QPushButton:disabled {background: #e5e7eb; color: #9ca3af;}"
        )

        row_top.addWidget(self._symbol_label)
        row_top.addWidget(self._last_price_label)
        row_top.addStretch()
        row_top.addWidget(self._start_button)
        row_top.addWidget(self._pause_button)
        row_top.addWidget(self._stop_button)
        row_top.addWidget(self._dry_run_toggle)

        self._feed_indicator = QLabel("HTTP — | WS — | CLOCK —")
        self._feed_indicator.setStyleSheet("color: #6b7280; font-size: 11px;")

        self._age_label = QLabel("AGE —")
        self._age_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        self._latency_label = QLabel("LAT —")
        self._latency_label.setStyleSheet("color: #6b7280; font-size: 11px;")

        row_bottom.addWidget(self._feed_indicator)
        row_bottom.addWidget(self._age_label)
        row_bottom.addWidget(self._latency_label)
        row_bottom.addStretch()

        wrapper.addLayout(row_top)
        wrapper.addLayout(row_bottom)
        return wrapper

    def _build_body(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_ai_panel())
        splitter.addWidget(self._build_grid_panel())
        splitter.addWidget(self._build_runtime_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 1)
        return splitter

    def _build_ai_panel(self) -> QWidget:
        group = QGroupBox("AI Operator")
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        top_actions = QHBoxLayout()
        for label in ["AI Analyze", "Apply Plan"]:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, name=label: self._log_ui_click(name))
            top_actions.addWidget(button)
        top_actions.addStretch()
        layout.addLayout(top_actions)

        self._history = QPlainTextEdit()
        self._history.setReadOnly(True)
        self._history.appendPlainText("[AI] Watching…")
        layout.addWidget(self._history, stretch=1)

        input_row = QHBoxLayout()
        self._command_input = QLineEdit()
        self._command_input.setPlaceholderText("Type a message for AI Operator…")
        self._command_input.returnPressed.connect(self._handle_send)
        input_row.addWidget(self._command_input)

        send_button = QPushButton("Send")
        send_button.clicked.connect(self._handle_send)
        input_row.addWidget(send_button)
        layout.addLayout(input_row)

        bottom_actions = QHBoxLayout()
        for label in ["Approve", "Pause (AI)"]:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, name=label: self._log_ui_click(name))
            bottom_actions.addWidget(button)
        bottom_actions.addStretch()
        layout.addLayout(bottom_actions)

        return group

    def _build_grid_panel(self) -> QWidget:
        group = QGroupBox("Grid Parameters")
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        layout.setSpacing(6)

        control_frame = QFrame()
        control_layout = QHBoxLayout(control_frame)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_label = QLabel("AI Control")
        control_label.setStyleSheet("font-weight: 600;")
        self._ai_control_mode = QComboBox()
        self._ai_control_mode.addItems(["AUTO", "LOCKED", "OVERRIDE"])
        control_layout.addWidget(control_label)
        control_layout.addWidget(self._ai_control_mode)
        control_layout.addStretch()

        self._reset_button = QPushButton("Reset defaults")
        self._reset_button.clicked.connect(self._handle_reset_defaults)
        self._ai_fill_button = QPushButton("AI fill")
        self._ai_fill_button.clicked.connect(self._handle_ai_fill)
        control_layout.addWidget(self._reset_button)
        control_layout.addWidget(self._ai_fill_button)
        layout.addWidget(control_frame)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setVerticalSpacing(4)

        self._budget_input = QDoubleSpinBox()
        self._budget_input.setRange(10.0, 1_000_000.0)
        self._budget_input.setDecimals(2)
        self._budget_input.setValue(100.0)
        form.addRow("Budget (USDT)", self._budget_input)

        self._direction_combo = QComboBox()
        self._direction_combo.addItems(["Neutral", "Long", "Short"])
        form.addRow("Direction/Bias", self._direction_combo)

        self._levels_input = QSpinBox()
        self._levels_input.setRange(2, 500)
        self._levels_input.setValue(10)
        form.addRow("Levels", self._levels_input)

        self._step_input = self._make_double_spin()
        form.addRow("Step %", self._step_input)

        range_row = QHBoxLayout()
        self._range_down_input = self._make_double_spin()
        self._range_up_input = self._make_double_spin()
        range_row.addWidget(QLabel("Down"))
        range_row.addWidget(self._range_down_input)
        range_row.addWidget(QLabel("Up"))
        range_row.addWidget(self._range_up_input)
        form.addRow("Range down/up", range_row)

        self._take_profit_input = self._make_double_spin()
        form.addRow("Take-profit %", self._take_profit_input)

        self._max_orders_input = QSpinBox()
        self._max_orders_input.setRange(1, 200)
        self._max_orders_input.setValue(10)
        form.addRow("Max active orders", self._max_orders_input)

        self._max_exposure_input = self._make_double_spin()
        form.addRow("Max exposure", self._max_exposure_input)

        layout.addLayout(form)
        layout.addStretch()
        return group

    def _build_runtime_panel(self) -> QWidget:
        group = QGroupBox("Runtime")
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        fixed_font = QFont()
        fixed_font.setStyleHint(QFont.Monospace)
        fixed_font.setFixedPitch(True)

        account_status = QLabel("Account READY")
        account_status.setFont(fixed_font)
        account_status.setStyleSheet("color: #16a34a; font-size: 11px; font-weight: 600;")

        balances = QLabel("Quote — | Base — | Equity —")
        balances.setFont(fixed_font)

        balance_state = QLabel("Used — | Free — | Locked —")
        balance_state.setFont(fixed_font)

        pnl_label = QLabel("PnL —")
        pnl_label.setFont(fixed_font)

        layout.addWidget(account_status)
        layout.addWidget(balances)
        layout.addWidget(balance_state)
        layout.addWidget(pnl_label)

        self._orders_table = QTableWidget(0, 6, self)
        self._orders_table.setHorizontalHeaderLabels(
            ["ID", "Side", "Price", "Qty", "Status", "Time"]
        )
        self._orders_table.setColumnHidden(0, True)
        self._orders_table.horizontalHeader().setStretchLastSection(True)
        self._orders_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._orders_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._orders_table.verticalHeader().setDefaultSectionSize(22)
        self._orders_table.setMinimumHeight(160)
        layout.addWidget(self._orders_table)

        buttons = QHBoxLayout()
        self._cancel_selected_button = QPushButton("Cancel selected")
        self._cancel_all_button = QPushButton("Cancel all")
        self._refresh_button = QPushButton("Refresh")
        for button in (self._cancel_selected_button, self._cancel_all_button, self._refresh_button):
            button.setFixedHeight(28)
        self._cancel_selected_button.clicked.connect(
            lambda: self._log_ui_click("Cancel selected")
        )
        self._cancel_all_button.clicked.connect(lambda: self._log_ui_click("Cancel all"))
        self._refresh_button.clicked.connect(lambda: self._log_ui_click("Refresh"))
        buttons.addWidget(self._cancel_selected_button)
        buttons.addWidget(self._cancel_all_button)
        buttons.addWidget(self._refresh_button)
        layout.addLayout(buttons)
        return group

    @staticmethod
    def _make_double_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(4)
        spin.setRange(0.0, 1_000_000.0)
        spin.setSingleStep(0.1)
        return spin

    def _handle_send(self) -> None:
        text = self._command_input.text().strip()
        if not text:
            return
        self._history.appendPlainText(f"[YOU] {text}")
        self._history.appendPlainText("[AI] (stub) received")
        self._command_input.clear()

    def _handle_ai_fill(self) -> None:
        self._history.appendPlainText("[AI] (stub) filled")

    def _handle_reset_defaults(self) -> None:
        self._budget_input.setValue(100.0)
        self._direction_combo.setCurrentIndex(0)
        self._levels_input.setValue(10)
        self._step_input.setValue(0.5)
        self._range_down_input.setValue(1.0)
        self._range_up_input.setValue(1.0)
        self._take_profit_input.setValue(1.0)
        self._max_orders_input.setValue(10)
        self._max_exposure_input.setValue(0.0)
        self._log_ui_click("Reset defaults")

    def _log_ui_click(self, label: str) -> None:
        self._history.appendPlainText(f"[UI] {label} clicked (stub)")
