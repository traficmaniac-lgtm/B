from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger
from src.gui.i18n import TEXT, tr
from src.services.price_feed_manager import PriceFeedManager, PriceUpdate, WS_CONNECTED, WS_DEGRADED, WS_LOST


@dataclass
class GridSettingsState:
    budget: float = 100.0
    direction: str = "Neutral"
    grid_count: int = 10
    grid_step_pct: float = 0.5
    range_mode: str = "Auto"
    range_low_pct: float = 1.0
    range_high_pct: float = 1.0
    take_profit_pct: float = 1.0
    stop_loss_enabled: bool = False
    stop_loss_pct: float = 2.0
    max_active_orders: int = 10
    order_size_mode: str = "Equal"


class _LiteGridSignals(QObject):
    price_update = Signal(object)
    status_update = Signal(str, str)


class LiteGridWindow(QMainWindow):
    def __init__(
        self,
        symbol: str,
        price_feed_manager: PriceFeedManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._logger = get_logger("gui.lite_grid")
        self._symbol = symbol.strip().upper()
        self._price_feed_manager = price_feed_manager
        self._signals = _LiteGridSignals()
        self._signals.price_update.connect(self._apply_price_update)
        self._signals.status_update.connect(self._apply_status_update)
        self._state = "IDLE"
        self._engine_state = "WAITING"
        self._ws_status = ""
        self._settings_state = GridSettingsState()
        self._log_entries: list[tuple[str, str]] = []

        self.setWindowTitle(tr("window_title", symbol=self._symbol))
        self.resize(1050, 720)

        central = QWidget(self)
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(12, 12, 12, 12)
        outer_layout.setSpacing(10)

        outer_layout.addLayout(self._build_header())
        outer_layout.addWidget(self._build_body())
        outer_layout.addWidget(self._build_logs())

        self.setCentralWidget(central)
        self._handle_dry_run_toggle(self._dry_run_toggle.isChecked())

        self._price_feed_manager.register_symbol(self._symbol)
        self._price_feed_manager.subscribe(self._symbol, self._emit_price_update)
        self._price_feed_manager.subscribe_status(self._symbol, self._emit_status_update)
        self._price_feed_manager.start()
        self._append_log("Lite Grid Terminal opened.", kind="INFO")

    @property
    def symbol(self) -> str:
        return self._symbol

    def _build_header(self) -> QVBoxLayout:
        wrapper = QVBoxLayout()
        wrapper.setSpacing(4)

        row_top = QHBoxLayout()
        row_top.setSpacing(10)
        row_bottom = QHBoxLayout()
        row_bottom.setSpacing(8)

        self._symbol_label = QLabel(self._symbol)
        self._symbol_label.setStyleSheet("font-weight: 600; font-size: 16px;")

        self._last_price_label = QLabel(tr("last_price", price="—"))
        self._last_price_label.setStyleSheet("font-weight: 600;")

        self._start_button = QPushButton(tr("start"))
        self._pause_button = QPushButton(tr("pause"))
        self._stop_button = QPushButton(tr("stop"))
        self._start_button.clicked.connect(self._handle_start)
        self._pause_button.clicked.connect(self._handle_pause)
        self._stop_button.clicked.connect(self._handle_stop)

        self._dry_run_toggle = QPushButton(tr("dry_run"))
        self._dry_run_toggle.setCheckable(True)
        self._dry_run_toggle.setChecked(True)
        self._dry_run_toggle.toggled.connect(self._handle_dry_run_toggle)
        self._dry_run_toggle.setStyleSheet(
            "QPushButton {"
            "padding: 4px 8px; border-radius: 10px; border: 1px solid #d1d5db;"
            "background: #f3f4f6; font-weight: 600;}"
            "QPushButton:checked {background: #16a34a; color: white; border-color: #16a34a;}"
        )

        self._state_badge = QLabel(f"{tr('state')}: {self._state}")
        self._state_badge.setStyleSheet(
            "padding: 4px 8px; border-radius: 10px; background: #1f2937; color: white; font-weight: 600;"
        )

        row_top.addWidget(self._symbol_label)
        row_top.addWidget(self._last_price_label)
        row_top.addStretch()
        row_top.addWidget(self._start_button)
        row_top.addWidget(self._pause_button)
        row_top.addWidget(self._stop_button)
        row_top.addWidget(self._dry_run_toggle)
        row_top.addWidget(self._state_badge)

        self._feed_indicator = QLabel("HTTP ✓ | WS — | CLOCK —")
        self._feed_indicator.setStyleSheet("color: #6b7280; font-size: 11px;")

        self._age_label = QLabel(tr("age", age="—"))
        self._age_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        self._latency_label = QLabel(tr("latency", latency="—"))
        self._latency_label.setStyleSheet("color: #6b7280; font-size: 11px;")

        self._engine_state_label = QLabel(f"{tr('engine')}: {self._engine_state}")
        self._engine_state_label.setStyleSheet("color: #374151; font-size: 11px; font-weight: 600;")

        row_bottom.addWidget(self._feed_indicator)
        row_bottom.addWidget(self._age_label)
        row_bottom.addWidget(self._latency_label)
        row_bottom.addStretch()
        row_bottom.addWidget(self._engine_state_label)

        wrapper.addLayout(row_top)
        wrapper.addLayout(row_bottom)
        return wrapper

    def _build_body(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_market_panel())
        splitter.addWidget(self._build_grid_panel())
        splitter.addWidget(self._build_runtime_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 1)
        return splitter

    def _build_market_panel(self) -> QWidget:
        group = QGroupBox(tr("market"))
        layout = QVBoxLayout(group)
        layout.setSpacing(6)

        self._market_price = QLabel(f"{tr('price')}: —")
        self._market_spread = QLabel(f"{tr('spread')}: —")
        self._market_volatility = QLabel(f"{tr('volatility')}: —")
        self._market_fee = QLabel(f"{tr('fee')}: —")

        layout.addWidget(self._market_price)
        layout.addWidget(self._market_spread)
        layout.addWidget(self._market_volatility)
        layout.addWidget(self._market_fee)
        layout.addStretch()
        return group

    def _build_grid_panel(self) -> QWidget:
        group = QGroupBox(tr("grid_settings"))
        layout = QVBoxLayout(group)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self._budget_input = QDoubleSpinBox()
        self._budget_input.setRange(10.0, 1_000_000.0)
        self._budget_input.setDecimals(2)
        self._budget_input.setValue(self._settings_state.budget)
        self._budget_input.valueChanged.connect(lambda value: self._update_setting("budget", value))

        self._direction_combo = QComboBox()
        self._direction_combo.addItem("Нейтрально", "Neutral")
        self._direction_combo.addItem("Преимущественно Long", "Long-biased")
        self._direction_combo.addItem("Преимущественно Short", "Short-biased")
        self._direction_combo.currentIndexChanged.connect(
            lambda _: self._update_setting("direction", self._direction_combo.currentData())
        )

        self._grid_count_input = QSpinBox()
        self._grid_count_input.setRange(2, 200)
        self._grid_count_input.setValue(self._settings_state.grid_count)
        self._grid_count_input.valueChanged.connect(lambda value: self._update_setting("grid_count", value))

        self._grid_step_input = QDoubleSpinBox()
        self._grid_step_input.setRange(0.05, 25.0)
        self._grid_step_input.setDecimals(2)
        self._grid_step_input.setValue(self._settings_state.grid_step_pct)
        self._grid_step_input.valueChanged.connect(
            lambda value: self._update_setting("grid_step_pct", value)
        )

        self._range_mode_combo = QComboBox()
        self._range_mode_combo.addItem("Авто", "Auto")
        self._range_mode_combo.addItem("Ручной", "Manual")
        self._range_mode_combo.currentIndexChanged.connect(
            lambda _: self._handle_range_mode_change(self._range_mode_combo.currentData())
        )

        self._range_low_input = QDoubleSpinBox()
        self._range_low_input.setRange(0.1, 50.0)
        self._range_low_input.setDecimals(2)
        self._range_low_input.setValue(self._settings_state.range_low_pct)
        self._range_low_input.valueChanged.connect(
            lambda value: self._update_setting("range_low_pct", value)
        )

        self._range_high_input = QDoubleSpinBox()
        self._range_high_input.setRange(0.1, 50.0)
        self._range_high_input.setDecimals(2)
        self._range_high_input.setValue(self._settings_state.range_high_pct)
        self._range_high_input.valueChanged.connect(
            lambda value: self._update_setting("range_high_pct", value)
        )

        self._take_profit_input = QDoubleSpinBox()
        self._take_profit_input.setRange(0.1, 100.0)
        self._take_profit_input.setDecimals(2)
        self._take_profit_input.setValue(self._settings_state.take_profit_pct)
        self._take_profit_input.valueChanged.connect(
            lambda value: self._update_setting("take_profit_pct", value)
        )

        stop_loss_row = QHBoxLayout()
        self._stop_loss_toggle = QCheckBox(tr("enable"))
        self._stop_loss_toggle.toggled.connect(self._handle_stop_loss_toggle)
        self._stop_loss_input = QDoubleSpinBox()
        self._stop_loss_input.setRange(0.1, 100.0)
        self._stop_loss_input.setDecimals(2)
        self._stop_loss_input.setValue(self._settings_state.stop_loss_pct)
        self._stop_loss_input.setEnabled(False)
        self._stop_loss_input.valueChanged.connect(
            lambda value: self._update_setting("stop_loss_pct", value)
        )
        stop_loss_row.addWidget(self._stop_loss_toggle)
        stop_loss_row.addWidget(self._stop_loss_input)

        self._max_orders_input = QSpinBox()
        self._max_orders_input.setRange(1, 200)
        self._max_orders_input.setValue(self._settings_state.max_active_orders)
        self._max_orders_input.valueChanged.connect(
            lambda value: self._update_setting("max_active_orders", value)
        )

        self._order_size_combo = QComboBox()
        self._order_size_combo.addItem("Равный", "Equal")
        self._order_size_combo.currentIndexChanged.connect(
            lambda _: self._update_setting("order_size_mode", self._order_size_combo.currentData())
        )

        form.addRow(tr("budget"), self._budget_input)
        form.addRow(tr("direction"), self._direction_combo)
        form.addRow(tr("grid_count"), self._grid_count_input)
        form.addRow(tr("grid_step"), self._grid_step_input)
        form.addRow(tr("range_mode"), self._range_mode_combo)
        form.addRow(tr("range_low"), self._range_low_input)
        form.addRow(tr("range_high"), self._range_high_input)
        form.addRow(tr("take_profit"), self._take_profit_input)
        form.addRow(tr("stop_loss"), stop_loss_row)
        form.addRow(tr("max_active_orders"), self._max_orders_input)
        form.addRow(tr("order_size_mode"), self._order_size_combo)

        layout.addLayout(form)

        actions = QHBoxLayout()
        actions.addStretch()
        self._reset_button = QPushButton(tr("reset_defaults"))
        self._reset_button.clicked.connect(self._reset_defaults)
        actions.addWidget(self._reset_button)
        layout.addLayout(actions)

        self._grid_preview_label = QLabel("—")
        self._grid_preview_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        layout.addWidget(self._grid_preview_label)

        self._apply_range_mode(self._settings_state.range_mode)
        self._update_grid_preview()
        return group

    def _build_runtime_panel(self) -> QWidget:
        group = QGroupBox(tr("runtime"))
        layout = QVBoxLayout(group)
        layout.setSpacing(6)

        fixed_font = QFont()
        fixed_font.setStyleHint(QFont.Monospace)
        fixed_font.setFixedPitch(True)

        self._balance_quote_label = QLabel(
            tr(
                "balance_quote_base",
                quote="0.00",
                quote_ccy="USDT",
                base="0.0000",
                base_ccy=self._symbol,
            )
        )
        self._balance_quote_label.setFont(fixed_font)
        self._balance_equity_label = QLabel(
            tr("equity_line", equity="0.00", free="0.00", locked="0.00")
        )
        self._balance_equity_label.setFont(fixed_font)

        self._budget_line = QLabel(tr("bot_budget_line", budget="0.00", used="0.00", free="0.00"))
        self._budget_line.setFont(fixed_font)
        self._budget_progress = QProgressBar()
        self._budget_progress.setRange(0, 100)
        self._budget_progress.setValue(0)
        self._budget_progress.setTextVisible(False)
        self._budget_progress.setFixedHeight(7)

        self._pnl_label = QLabel(tr("pnl_line", unreal="0.00", real="0.00", total="0.00"))
        self._pnl_label.setFont(fixed_font)

        self._orders_count_label = QLabel(tr("orders_count", count="0"))
        self._orders_count_label.setStyleSheet("color: #6b7280; font-size: 11px;")

        layout.addWidget(self._balance_quote_label)
        layout.addWidget(self._balance_equity_label)
        layout.addWidget(self._budget_line)
        layout.addWidget(self._budget_progress)
        layout.addWidget(self._pnl_label)
        layout.addSpacing(6)
        layout.addWidget(self._orders_count_label)

        self._orders_table = QTableWidget(0, 7, self)
        self._orders_table.setHorizontalHeaderLabels(TEXT["orders_columns"])
        self._orders_table.setColumnHidden(0, True)
        self._orders_table.horizontalHeader().setStretchLastSection(True)
        self._orders_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._orders_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._orders_table.setMinimumHeight(160)
        self._orders_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._orders_table.customContextMenuRequested.connect(self._show_order_context_menu)

        layout.addWidget(self._orders_table)

        buttons = QHBoxLayout()
        self._cancel_selected_button = QPushButton(tr("cancel_selected"))
        self._cancel_all_button = QPushButton(tr("cancel_all"))
        self._refresh_button = QPushButton(tr("refresh"))

        self._cancel_selected_button.clicked.connect(self._handle_cancel_selected)
        self._cancel_all_button.clicked.connect(self._handle_cancel_all)
        self._refresh_button.clicked.connect(self._handle_refresh)

        buttons.addWidget(self._cancel_selected_button)
        buttons.addWidget(self._cancel_all_button)
        buttons.addWidget(self._refresh_button)
        layout.addLayout(buttons)

        self._apply_pnl_style(self._pnl_label, None)
        self._refresh_orders_metrics()
        return group

    def _build_logs(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        self._trades_summary_label = QLabel(
            tr(
                "trades_summary",
                closed="0",
                win="—",
                avg="—",
                realized="0.00",
                fees="0.00",
            )
        )
        self._trades_summary_label.setStyleSheet("font-weight: 600;")

        filter_row = QHBoxLayout()
        filter_label = QLabel(tr("logs"))
        filter_label.setStyleSheet("font-weight: 600;")
        self._log_filter = QComboBox()
        self._log_filter.addItems(
            [tr("log_filter_all"), tr("log_filter_orders"), tr("log_filter_errors")]
        )
        self._log_filter.currentTextChanged.connect(self._apply_log_filter)
        filter_row.addWidget(filter_label)
        filter_row.addStretch()
        filter_row.addWidget(self._log_filter)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(200)
        self._log_view.setFixedHeight(140)
        layout.addWidget(self._trades_summary_label)
        layout.addLayout(filter_row)
        layout.addWidget(self._log_view)
        return frame

    def _emit_price_update(self, update: PriceUpdate) -> None:
        self._signals.price_update.emit(update)

    def _emit_status_update(self, status: str, message: str) -> None:
        self._signals.status_update.emit(status, message)

    def _apply_price_update(self, update: PriceUpdate) -> None:
        if update.last_price is not None:
            self._last_price_label.setText(tr("last_price", price=f"{update.last_price:.8f}"))
            self._market_price.setText(f"{tr('price')}: {update.last_price:.8f}")
        else:
            self._last_price_label.setText(tr("last_price", price="—"))
            self._market_price.setText(f"{tr('price')}: —")

        latency = f"{update.latency_ms}ms" if update.latency_ms is not None else "—"
        age = f"{update.price_age_ms}ms" if update.price_age_ms is not None else "—"
        self._age_label.setText(tr("age", age=age))
        self._latency_label.setText(tr("latency", latency=latency))
        clock_status = "✓" if update.price_age_ms is not None else "—"
        self._feed_indicator.setText(f"HTTP ✓ | WS {self._ws_indicator_symbol()} | CLOCK {clock_status}")

        micro = update.microstructure
        if micro.spread_pct is not None:
            self._market_spread.setText(f"{tr('spread')}: {micro.spread_pct:.4f}%")
        elif micro.spread_abs is not None:
            self._market_spread.setText(f"{tr('spread')}: {micro.spread_abs:.8f}")
        else:
            self._market_spread.setText(f"{tr('spread')}: —")

    def _apply_status_update(self, status: str, _: str) -> None:
        if status == WS_CONNECTED:
            self._ws_status = WS_CONNECTED
            self._feed_indicator.setToolTip("")
        elif status == WS_DEGRADED:
            self._ws_status = WS_DEGRADED
            self._feed_indicator.setToolTip(tr("ws_degraded_tooltip"))
        elif status == WS_LOST:
            self._ws_status = WS_LOST
            self._feed_indicator.setToolTip(tr("ws_degraded_tooltip"))
        else:
            self._ws_status = ""
            self._feed_indicator.setToolTip("")
        self._feed_indicator.setText(f"HTTP ✓ | WS {self._ws_indicator_symbol()} | CLOCK —")

    def _handle_range_mode_change(self, value: str) -> None:
        self._update_setting("range_mode", value)
        self._apply_range_mode(value)

    def _apply_range_mode(self, value: str) -> None:
        manual = value == "Manual"
        self._range_low_input.setEnabled(manual)
        self._range_high_input.setEnabled(manual)

    def _handle_stop_loss_toggle(self, enabled: bool) -> None:
        self._stop_loss_input.setEnabled(enabled)
        self._update_setting("stop_loss_enabled", enabled)

    def _handle_dry_run_toggle(self, checked: bool) -> None:
        state = "enabled" if checked else "disabled"
        self._append_log(f"Dry-run {state}.", kind="INFO")

    def _handle_start(self) -> None:
        self._append_log(f"Start pressed (dry-run={self._dry_run_toggle.isChecked()}).", kind="ORDERS")
        self._change_state("RUNNING")

    def _handle_pause(self) -> None:
        self._append_log("Pause pressed.", kind="ORDERS")
        self._change_state("PAUSED")

    def _handle_stop(self) -> None:
        self._append_log("Stop pressed.", kind="ORDERS")
        self._change_state("IDLE")

    def _handle_cancel_selected(self) -> None:
        selected_rows = sorted({index.row() for index in self._orders_table.selectionModel().selectedRows()})
        if not selected_rows:
            self._append_log("Cancel selected: —", kind="ORDERS")
            return
        if not self._dry_run_toggle.isChecked():
            self._append_log("Cancel selected: not implemented.", kind="ORDERS")
            return
        for row in reversed(selected_rows):
            order_id = self._order_id_for_row(row)
            self._orders_table.removeRow(row)
            self._append_log(f"Cancel selected: {order_id}", kind="ORDERS")
        self._refresh_orders_metrics()

    def _handle_cancel_all(self) -> None:
        count = self._orders_table.rowCount()
        if count == 0:
            self._append_log("Cancel all: 0", kind="ORDERS")
            return
        if not self._dry_run_toggle.isChecked():
            self._append_log("Cancel all: not implemented.", kind="ORDERS")
            return
        self._orders_table.setRowCount(0)
        self._append_log(f"Cancel all: {count}", kind="ORDERS")
        self._refresh_orders_metrics()

    def _handle_refresh(self) -> None:
        self._append_log("Manual refresh requested (not implemented).", kind="INFO")

    def _change_state(self, new_state: str) -> None:
        self._state = new_state
        self._state_badge.setText(f"{tr('state')}: {self._state}")
        self._engine_state = self._engine_state_from_status(new_state)
        self._engine_state_label.setText(f"{tr('engine')}: {self._engine_state}")

    def _update_setting(self, key: str, value: Any) -> None:
        if hasattr(self._settings_state, key):
            setattr(self._settings_state, key, value)
        if key == "budget":
            self._refresh_orders_metrics()
        self._update_grid_preview()

    def _reset_defaults(self) -> None:
        defaults = GridSettingsState()
        self._settings_state = defaults
        self._budget_input.setValue(defaults.budget)
        self._direction_combo.setCurrentIndex(
            self._direction_combo.findData(defaults.direction)
        )
        self._grid_count_input.setValue(defaults.grid_count)
        self._grid_step_input.setValue(defaults.grid_step_pct)
        self._range_mode_combo.setCurrentIndex(
            self._range_mode_combo.findData(defaults.range_mode)
        )
        self._range_low_input.setValue(defaults.range_low_pct)
        self._range_high_input.setValue(defaults.range_high_pct)
        self._take_profit_input.setValue(defaults.take_profit_pct)
        self._stop_loss_toggle.setChecked(defaults.stop_loss_enabled)
        self._stop_loss_input.setValue(defaults.stop_loss_pct)
        self._max_orders_input.setValue(defaults.max_active_orders)
        self._order_size_combo.setCurrentIndex(
            self._order_size_combo.findData(defaults.order_size_mode)
        )
        self._append_log("Settings reset to defaults.", kind="INFO")
        self._update_grid_preview()

    def _order_id_for_row(self, row: int) -> str:
        item = self._orders_table.item(row, 0)
        return item.text() if item and item.text() else "—"

    def _extract_order_value(self, row: int) -> float:
        price_item = self._orders_table.item(row, 2)
        qty_item = self._orders_table.item(row, 3)
        price = self._coerce_float(price_item.text() if price_item else "")
        qty = self._coerce_float(qty_item.text() if qty_item else "")
        if price is None or qty is None:
            return 0.0
        return price * qty

    @staticmethod
    def _coerce_float(value: str) -> float | None:
        cleaned = value.replace(",", "").strip()
        if not cleaned or cleaned == "—":
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _refresh_orders_metrics(self) -> None:
        budget = float(self._budget_input.value())
        locked = sum(self._extract_order_value(row) for row in range(self._orders_table.rowCount()))
        used = locked
        free = max(budget - used, 0.0)
        self._orders_count_label.setText(tr("orders_count", count=str(self._orders_table.rowCount())))
        self._budget_line.setText(
            tr("bot_budget_line", budget=f"{budget:.2f}", used=f"{used:.2f}", free=f"{free:.2f}")
        )
        usage_pct = int((used / budget) * 100) if budget > 0 else 0
        self._budget_progress.setValue(min(max(usage_pct, 0), 100))
        self._balance_quote_label.setText(
            tr(
                "balance_quote_base",
                quote=f"{budget:.2f}",
                quote_ccy="USDT",
                base="0.0000",
                base_ccy=self._symbol,
            )
        )
        self._balance_equity_label.setText(
            tr("equity_line", equity=f"{budget:.2f}", free=f"{free:.2f}", locked=f"{locked:.2f}")
        )
        for row in range(self._orders_table.rowCount()):
            self._set_order_row_tooltip(row)

    def _apply_pnl_style(self, label: QLabel, value: float | None) -> None:
        if value is None:
            label.setStyleSheet("color: #6b7280;")
            return
        if value > 0:
            label.setStyleSheet("color: #16a34a;")
        elif value < 0:
            label.setStyleSheet("color: #dc2626;")
        else:
            label.setStyleSheet("color: #6b7280;")

    def _update_pnl(self, unrealized: float | None, realized: float | None) -> None:
        if unrealized is None:
            unreal_text = "—"
        else:
            unreal_text = f"{unrealized:.2f}"

        if realized is None:
            real_text = "—"
            total = None
        else:
            real_text = f"{realized:.2f}"
            total = realized + (unrealized or 0.0)

        if total is None:
            total_text = "—"
            self._apply_pnl_style(self._pnl_label, None)
        else:
            total_text = f"{total:.2f}"
            self._apply_pnl_style(self._pnl_label, total)

        self._pnl_label.setText(tr("pnl_line", unreal=unreal_text, real=real_text, total=total_text))

    def _show_order_context_menu(self, position: Any) -> None:
        row = self._orders_table.rowAt(position.y())
        if row < 0:
            return
        menu = QMenu(self)
        cancel_action = menu.addAction(tr("context_cancel"))
        copy_action = menu.addAction(tr("context_copy_id"))
        show_action = menu.addAction(tr("context_show_details"))
        action = menu.exec(self._orders_table.viewport().mapToGlobal(position))
        if action == cancel_action:
            if not self._dry_run_toggle.isChecked():
                self._append_log("Cancel selected: not implemented.", kind="ORDERS")
                return
            order_id = self._order_id_for_row(row)
            self._orders_table.removeRow(row)
            self._append_log(f"Cancel selected: {order_id}", kind="ORDERS")
            self._refresh_orders_metrics()
        if action == copy_action:
            order_id = self._order_id_for_row(row)
            QApplication.clipboard().setText(order_id)
            self._append_log(f"Copy ID: {order_id}", kind="ORDERS")
        if action == show_action:
            details = self._format_order_details(row)
            self._append_log(f"Order context: {details}", kind="ORDERS")

    def _set_order_row_tooltip(self, row: int) -> None:
        order_id = self._order_id_for_row(row)
        tooltip = f"ID: {order_id}"
        for column in range(self._orders_table.columnCount()):
            item = self._orders_table.item(row, column)
            if item:
                item.setToolTip(tooltip)

    def _format_order_details(self, row: int) -> str:
        values = []
        for column in range(self._orders_table.columnCount()):
            header = self._orders_table.horizontalHeaderItem(column)
            header_text = header.text() if header else str(column)
            item = self._orders_table.item(row, column)
            values.append(f"{header_text}={item.text() if item else '—'}")
        return "; ".join(values)

    def _append_log(self, message: str, kind: str = "INFO") -> None:
        entry = (kind.upper(), message)
        self._log_entries.append(entry)
        self._apply_log_filter()
        self._logger.info("%s | %s | %s", self._symbol, entry[0], entry[1])

    def _apply_log_filter(self) -> None:
        selected = self._log_filter.currentText() if hasattr(self, "_log_filter") else tr("log_filter_all")
        if selected == tr("log_filter_orders"):
            allowed = {"ORDERS"}
        elif selected == tr("log_filter_errors"):
            allowed = {"WARN", "ERR", "ERROR"}
        else:
            allowed = None

        lines = []
        for kind, message in self._log_entries:
            if allowed is None or kind in allowed:
                lines.append(f"[{kind}] {message}")
        self._log_view.setPlainText("\n".join(lines))

    def _ws_indicator_symbol(self) -> str:
        if self._ws_status == WS_CONNECTED:
            return "✓"
        if self._ws_status == WS_DEGRADED:
            return "!"
        if self._ws_status == WS_LOST:
            return "×"
        return "—"

    @staticmethod
    def _engine_state_from_status(state: str) -> str:
        mapping = {
            "IDLE": "WAITING",
            "RUNNING": "PLACING_GRID",
            "PAUSED": "WAITING_FILLS",
        }
        return mapping.get(state, state)

    def _update_grid_preview(self) -> None:
        levels = int(self._grid_count_input.value())
        step = float(self._grid_step_input.value())
        range_low = float(self._range_low_input.value())
        range_high = float(self._range_high_input.value())
        range_pct = max(range_low, range_high)
        budget = float(self._budget_input.value())
        min_order = budget / levels if levels > 0 else 0.0
        self._grid_preview_label.setText(
            tr(
                "grid_preview",
                levels=str(levels),
                step=f"{step:.2f}",
                range=f"{range_pct:.2f}",
                orders=str(levels),
                min_order=f"{min_order:.2f}",
                quote_ccy="USDT",
            )
        )

    def closeEvent(self, event: object) -> None:  # noqa: N802
        self._price_feed_manager.unsubscribe(self._symbol, self._emit_price_update)
        self._price_feed_manager.unsubscribe_status(self._symbol, self._emit_status_update)
        self._price_feed_manager.unregister_symbol(self._symbol)
        self._append_log("Lite Grid Terminal closed.", kind="INFO")
        super().closeEvent(event)

    def dump_settings(self) -> dict[str, Any]:
        return asdict(self._settings_state)
