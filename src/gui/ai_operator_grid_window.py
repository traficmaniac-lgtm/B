from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
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

from src.gui.i18n import TEXT, tr


class AiOperatorGridWindow(QMainWindow):
    def __init__(self, symbol: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._symbol = symbol

        self.setWindowTitle(f"AI Operator Grid — {symbol}")
        self.resize(1050, 720)
        self._state = "READY"
        self._engine_state = "—"

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

        self._last_price_label = QLabel(tr("last_price", price="—"))
        self._last_price_label.setStyleSheet("font-weight: 600;")

        self._start_button = QPushButton(tr("start"))
        self._pause_button = QPushButton(tr("pause"))
        self._stop_button = QPushButton(tr("stop"))
        self._start_button.clicked.connect(lambda: self._log_ui_click("Start"))
        self._pause_button.clicked.connect(lambda: self._log_ui_click("Pause"))
        self._stop_button.clicked.connect(lambda: self._log_ui_click("Stop"))

        self._dry_run_toggle = QPushButton(tr("dry_run"))
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

        self._state_badge = QLabel(f"{tr('state')}: {self._state}")
        self._state_badge.setStyleSheet(
            f"{badge_base} background: #111827; color: white;"
        )

        row_top.addWidget(self._symbol_label)
        row_top.addWidget(self._last_price_label)
        row_top.addStretch()
        row_top.addWidget(self._start_button)
        row_top.addWidget(self._pause_button)
        row_top.addWidget(self._stop_button)
        row_top.addWidget(self._dry_run_toggle)
        row_top.addWidget(self._state_badge)

        self._feed_indicator = QLabel(
            f"{tr('source_http')} ✓ | {tr('source_ws')} — | {tr('source_clock')} —"
        )
        self._feed_indicator.setStyleSheet("color: #6b7280; font-size: 11px;")

        self._age_label = QLabel(tr("age", age="—"))
        self._age_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        self._latency_label = QLabel(tr("latency", latency="—"))
        self._latency_label.setStyleSheet("color: #6b7280; font-size: 11px;")

        row_bottom.addWidget(self._feed_indicator)
        row_bottom.addWidget(self._age_label)
        row_bottom.addWidget(self._latency_label)
        row_bottom.addStretch()
        self._trade_status_label = QLabel(tr("trade_status_disabled"))
        self._trade_status_label.setStyleSheet(
            "color: #dc2626; font-size: 11px; font-weight: 600;"
        )
        self._engine_state_label = QLabel(f"{tr('engine')}: {self._engine_state}")
        self._engine_state_label.setStyleSheet(
            "color: #6b7280; font-size: 11px; font-weight: 600;"
        )
        row_bottom.addWidget(self._trade_status_label)
        row_bottom.addWidget(self._engine_state_label)

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
        self._history.appendPlainText("[AI] Waiting…")
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
        group = QGroupBox(tr("grid_settings"))
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)

        control_frame = QFrame()
        control_layout = QHBoxLayout(control_frame)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_label = QLabel("AI Control")
        self._ai_control_mode = QComboBox()
        self._ai_control_mode.addItems(["AUTO", "LOCKED", "OVERRIDE"])
        control_layout.addWidget(control_label)
        control_layout.addWidget(self._ai_control_mode)
        control_layout.addStretch()

        layout.addWidget(control_frame)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setVerticalSpacing(4)

        self._budget_input = QDoubleSpinBox()
        self._budget_input.setRange(10.0, 1_000_000.0)
        self._budget_input.setDecimals(2)
        self._budget_input.setValue(100.0)
        form.addRow(tr("budget"), self._budget_input)

        self._direction_combo = QComboBox()
        self._direction_combo.addItem("Нейтрально", "Neutral")
        self._direction_combo.addItem("Преимущественно Long", "Long-biased")
        self._direction_combo.addItem("Преимущественно Short", "Short-biased")
        form.addRow(tr("direction"), self._direction_combo)

        self._grid_count_input = QSpinBox()
        self._grid_count_input.setRange(2, 200)
        self._grid_count_input.setValue(10)
        form.addRow(tr("grid_count"), self._grid_count_input)

        self._grid_step_mode_combo = QComboBox()
        self._grid_step_mode_combo.addItem("AUTO ATR", "AUTO_ATR")
        self._grid_step_mode_combo.addItem("MANUAL", "MANUAL")
        form.addRow(tr("grid_step_mode"), self._grid_step_mode_combo)

        self._grid_step_input = QDoubleSpinBox()
        self._grid_step_input.setRange(0.000001, 10.0)
        self._grid_step_input.setDecimals(8)
        self._grid_step_input.setSingleStep(0.0001)
        self._grid_step_input.setValue(0.5)
        self._manual_override_button = QPushButton(tr("manual_override"))
        self._manual_override_button.setFixedHeight(24)
        grid_step_row = QHBoxLayout()
        grid_step_row.addWidget(self._grid_step_input)
        grid_step_row.addWidget(self._manual_override_button)
        form.addRow(tr("grid_step"), grid_step_row)

        self._range_mode_combo = QComboBox()
        self._range_mode_combo.addItem("Авто", "Auto")
        self._range_mode_combo.addItem("Ручной", "Manual")
        form.addRow(tr("range_mode"), self._range_mode_combo)

        self._range_low_input = QDoubleSpinBox()
        self._range_low_input.setRange(0.000001, 10.0)
        self._range_low_input.setDecimals(8)
        self._range_low_input.setSingleStep(0.0001)
        self._range_low_input.setValue(1.0)
        form.addRow(tr("range_low"), self._range_low_input)

        self._range_high_input = QDoubleSpinBox()
        self._range_high_input.setRange(0.000001, 10.0)
        self._range_high_input.setDecimals(8)
        self._range_high_input.setSingleStep(0.0001)
        self._range_high_input.setValue(1.0)
        form.addRow(tr("range_high"), self._range_high_input)

        self._take_profit_input = QDoubleSpinBox()
        self._take_profit_input.setRange(0.000001, 50.0)
        self._take_profit_input.setDecimals(8)
        self._take_profit_input.setSingleStep(0.0001)
        self._take_profit_input.setValue(1.0)
        form.addRow(tr("take_profit"), self._take_profit_input)

        self._auto_values_label = QLabel(tr("auto_values_line", values="—"))
        self._auto_values_label.setStyleSheet("color: #6b7280; font-size: 10px;")
        form.addRow("", self._auto_values_label)

        stop_loss_row = QHBoxLayout()
        self._stop_loss_toggle = QCheckBox(tr("enable"))
        self._stop_loss_input = QDoubleSpinBox()
        self._stop_loss_input.setRange(0.1, 100.0)
        self._stop_loss_input.setDecimals(2)
        self._stop_loss_input.setValue(1.0)
        self._stop_loss_input.setEnabled(False)
        stop_loss_row.addWidget(self._stop_loss_toggle)
        stop_loss_row.addWidget(self._stop_loss_input)
        form.addRow(tr("stop_loss"), stop_loss_row)

        self._max_orders_input = QSpinBox()
        self._max_orders_input.setRange(1, 200)
        self._max_orders_input.setValue(10)
        form.addRow(tr("max_active_orders"), self._max_orders_input)

        self._order_size_combo = QComboBox()
        self._order_size_combo.addItem("Равный", "Equal")
        form.addRow(tr("order_size_mode"), self._order_size_combo)

        layout.addLayout(form)

        actions = QHBoxLayout()
        actions.addStretch()
        self._reset_button = QPushButton(tr("reset_defaults"))
        self._reset_button.clicked.connect(self._handle_reset_defaults)
        actions.addWidget(self._reset_button)
        layout.addLayout(actions)

        self._grid_preview_label = QLabel("—")
        self._grid_preview_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        layout.addWidget(self._grid_preview_label)
        return group

    def _build_runtime_panel(self) -> QWidget:
        group = QGroupBox(tr("runtime"))
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        fixed_font = QFont()
        fixed_font.setStyleHint(QFont.Monospace)
        fixed_font.setFixedPitch(True)

        self._account_status_label = QLabel(tr("account_status_ready"))
        self._account_status_label.setFont(fixed_font)
        self._account_status_label.setStyleSheet(
            "color: #16a34a; font-size: 11px; font-weight: 600;"
        )

        self._balance_quote_label = QLabel(
            tr(
                "runtime_account_line",
                quote="—",
                base="—",
                equity="—",
                quote_asset="—",
                base_asset="—",
            )
        )
        self._balance_quote_label.setFont(fixed_font)

        self._balance_bot_label = QLabel(tr("runtime_bot_line", used="—", free="—", locked="—"))
        self._balance_bot_label.setFont(fixed_font)

        self._pnl_label = QLabel(tr("pnl_no_fills"))
        self._pnl_label.setFont(fixed_font)
        self._pnl_label.setTextFormat(Qt.RichText)

        self._orders_count_label = QLabel(tr("orders_count", count="0"))
        self._orders_count_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        self._orders_count_label.setFont(fixed_font)

        layout.addWidget(self._account_status_label)
        layout.addWidget(self._balance_quote_label)
        layout.addWidget(self._balance_bot_label)
        layout.addWidget(self._pnl_label)
        layout.addWidget(self._orders_count_label)

        self._orders_table = QTableWidget(0, 6, self)
        self._orders_table.setHorizontalHeaderLabels(TEXT["orders_columns"])
        self._orders_table.setColumnHidden(0, True)
        self._orders_table.horizontalHeader().setStretchLastSection(True)
        self._orders_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._orders_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._orders_table.verticalHeader().setDefaultSectionSize(22)
        self._orders_table.setMinimumHeight(160)
        layout.addWidget(self._orders_table)

        buttons = QHBoxLayout()
        self._cancel_selected_button = QPushButton(tr("cancel_selected"))
        self._cancel_all_button = QPushButton(tr("cancel_all"))
        self._refresh_button = QPushButton(tr("refresh"))
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

    def _handle_send(self) -> None:
        self._log_ui_click("Send")
        text = self._command_input.text().strip()
        if not text:
            return
        self._history.appendPlainText(f"[YOU] {text}")
        self._history.appendPlainText("[AI] (stub) received")
        self._command_input.clear()

    def _handle_reset_defaults(self) -> None:
        self._budget_input.setValue(100.0)
        self._direction_combo.setCurrentIndex(0)
        self._grid_count_input.setValue(10)
        self._grid_step_mode_combo.setCurrentIndex(0)
        self._grid_step_input.setValue(0.5)
        self._range_mode_combo.setCurrentIndex(0)
        self._range_low_input.setValue(1.0)
        self._range_high_input.setValue(1.0)
        self._take_profit_input.setValue(1.0)
        self._max_orders_input.setValue(10)
        self._stop_loss_toggle.setChecked(False)
        self._stop_loss_input.setValue(1.0)
        self._order_size_combo.setCurrentIndex(0)
        self._log_ui_click("Reset defaults")

    def _log_ui_click(self, label: str) -> None:
        self._history.appendPlainText(f"[UI] {label} clicked (stub)")
