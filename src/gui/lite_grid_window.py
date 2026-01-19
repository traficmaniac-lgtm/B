from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
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
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger
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
        self._settings_state = GridSettingsState()

        self.setWindowTitle(f"Lite Grid Terminal — {self._symbol}")
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
        self._append_log("Lite Grid Terminal opened.")

    @property
    def symbol(self) -> str:
        return self._symbol

    def _build_header(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.setSpacing(12)

        self._symbol_label = QLabel(self._symbol)
        self._symbol_label.setStyleSheet("font-weight: 600; font-size: 16px;")

        self._last_price_label = QLabel("Last price: —")
        self._source_label = QLabel("Source: —")
        self._state_label = QLabel(f"State: {self._state}")
        self._state_label.setStyleSheet("font-weight: 600;")

        self._dry_run_toggle = QCheckBox("DRY-RUN")
        self._dry_run_toggle.setChecked(True)
        self._dry_run_toggle.toggled.connect(self._handle_dry_run_toggle)

        self._start_button = QPushButton("Start")
        self._pause_button = QPushButton("Pause")
        self._stop_button = QPushButton("Stop")
        self._start_button.clicked.connect(self._handle_start)
        self._pause_button.clicked.connect(self._handle_pause)
        self._stop_button.clicked.connect(self._handle_stop)

        layout.addWidget(self._symbol_label)
        layout.addWidget(self._last_price_label)
        layout.addWidget(self._source_label)
        layout.addStretch()
        layout.addWidget(self._start_button)
        layout.addWidget(self._pause_button)
        layout.addWidget(self._stop_button)
        layout.addWidget(self._dry_run_toggle)
        layout.addWidget(self._state_label)
        return layout

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
        group = QGroupBox("Market")
        layout = QVBoxLayout(group)
        layout.setSpacing(6)

        self._market_price = QLabel("Price: —")
        self._market_spread = QLabel("Spread: —")
        self._market_volatility = QLabel("Volatility: —")
        self._market_fee = QLabel("Fee/commission: —")
        self._feed_status = QLabel("Feed: —")
        self._feed_status.setStyleSheet("color: #6b7280;")

        layout.addWidget(self._market_price)
        layout.addWidget(self._market_spread)
        layout.addWidget(self._market_volatility)
        layout.addWidget(self._market_fee)
        layout.addSpacing(8)
        layout.addWidget(self._feed_status)
        layout.addStretch()
        return group

    def _build_grid_panel(self) -> QWidget:
        group = QGroupBox("Grid Settings")
        layout = QVBoxLayout(group)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self._budget_input = QDoubleSpinBox()
        self._budget_input.setRange(10.0, 1_000_000.0)
        self._budget_input.setDecimals(2)
        self._budget_input.setValue(self._settings_state.budget)
        self._budget_input.valueChanged.connect(lambda value: self._update_setting("budget", value))

        self._direction_combo = QComboBox()
        self._direction_combo.addItems(["Neutral", "Long-biased", "Short-biased"])
        self._direction_combo.currentTextChanged.connect(
            lambda value: self._update_setting("direction", value)
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
        self._range_mode_combo.addItems(["Auto", "Manual"])
        self._range_mode_combo.currentTextChanged.connect(self._handle_range_mode_change)

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
        self._stop_loss_toggle = QCheckBox("Enable")
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
        self._order_size_combo.addItems(["Equal"])
        self._order_size_combo.currentTextChanged.connect(
            lambda value: self._update_setting("order_size_mode", value)
        )

        form.addRow("Budget (USDT)", self._budget_input)
        form.addRow("Direction", self._direction_combo)
        form.addRow("Grid count", self._grid_count_input)
        form.addRow("Grid step %", self._grid_step_input)
        form.addRow("Range mode", self._range_mode_combo)
        form.addRow("Range low %", self._range_low_input)
        form.addRow("Range high %", self._range_high_input)
        form.addRow("Take profit %", self._take_profit_input)
        form.addRow("Stop loss %", stop_loss_row)
        form.addRow("Max active orders", self._max_orders_input)
        form.addRow("Order size mode", self._order_size_combo)

        layout.addLayout(form)

        actions = QHBoxLayout()
        actions.addStretch()
        self._reset_button = QPushButton("Reset defaults")
        self._reset_button.clicked.connect(self._reset_defaults)
        actions.addWidget(self._reset_button)
        layout.addLayout(actions)

        self._apply_range_mode(self._settings_state.range_mode)
        return group

    def _build_runtime_panel(self) -> QWidget:
        group = QGroupBox("Runtime")
        layout = QVBoxLayout(group)
        layout.setSpacing(6)

        self._balance_label = QLabel("Balance (quote/base): —")
        self._orders_label = QLabel("Open orders: —")
        self._budget_label = QLabel("Bot budget: —")
        self._used_label = QLabel("Used in orders: —")
        self._free_label = QLabel("Free in bot: —")
        self._locked_label = QLabel("Locked in orders: —")
        self._max_exposure_label = QLabel("Max exposure: —")
        self._pnl_unrealized_label = QLabel("Unrealized: —")
        self._pnl_realized_label = QLabel("Realized: —")
        self._pnl_total_label = QLabel("Total: —")

        layout.addWidget(self._balance_label)
        layout.addWidget(self._orders_label)
        layout.addWidget(self._budget_label)
        layout.addWidget(self._used_label)
        layout.addWidget(self._free_label)
        layout.addWidget(self._locked_label)
        layout.addWidget(self._max_exposure_label)
        layout.addWidget(self._pnl_unrealized_label)
        layout.addWidget(self._pnl_realized_label)
        layout.addWidget(self._pnl_total_label)

        self._orders_table = QTableWidget(0, 5, self)
        self._orders_table.setHorizontalHeaderLabels(["ID", "Side", "Price", "Qty", "Status"])
        self._orders_table.horizontalHeader().setStretchLastSection(True)
        self._orders_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._orders_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._orders_table.setMinimumHeight(160)
        self._orders_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._orders_table.customContextMenuRequested.connect(self._show_order_context_menu)

        layout.addWidget(self._orders_table)

        buttons = QHBoxLayout()
        self._cancel_selected_button = QPushButton("Cancel selected")
        self._cancel_all_button = QPushButton("Cancel all")
        self._refresh_button = QPushButton("Refresh")

        self._cancel_selected_button.clicked.connect(self._handle_cancel_selected)
        self._cancel_all_button.clicked.connect(self._handle_cancel_all)
        self._refresh_button.clicked.connect(self._handle_refresh)

        buttons.addWidget(self._cancel_selected_button)
        buttons.addWidget(self._cancel_all_button)
        buttons.addWidget(self._refresh_button)
        layout.addLayout(buttons)

        self._apply_pnl_style(self._pnl_unrealized_label, None)
        self._apply_pnl_style(self._pnl_realized_label, None)
        self._apply_pnl_style(self._pnl_total_label, None)
        self._refresh_orders_metrics()
        return group

    def _build_logs(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        trades_label = QLabel("Trades summary")
        trades_label.setStyleSheet("font-weight: 600;")
        trades_row = QHBoxLayout()
        self._trades_closed_label = QLabel("Closed: 0")
        self._trades_profitable_label = QLabel("Profitable: 0")
        self._trades_win_rate_label = QLabel("Win rate: —")
        self._trades_avg_profit_label = QLabel("Avg profit: —")
        trades_row.addWidget(self._trades_closed_label)
        trades_row.addWidget(self._trades_profitable_label)
        trades_row.addWidget(self._trades_win_rate_label)
        trades_row.addWidget(self._trades_avg_profit_label)
        trades_row.addStretch()

        label = QLabel("Logs")
        label.setStyleSheet("font-weight: 600;")
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(200)
        self._log_view.setFixedHeight(140)
        layout.addWidget(trades_label)
        layout.addLayout(trades_row)
        layout.addWidget(label)
        layout.addWidget(self._log_view)
        return frame

    def _emit_price_update(self, update: PriceUpdate) -> None:
        self._signals.price_update.emit(update)

    def _emit_status_update(self, status: str, message: str) -> None:
        self._signals.status_update.emit(status, message)

    def _apply_price_update(self, update: PriceUpdate) -> None:
        if update.last_price is not None:
            self._last_price_label.setText(f"Last price: {update.last_price:.8f}")
            self._market_price.setText(f"Price: {update.last_price:.8f}")
        else:
            self._last_price_label.setText("Last price: —")
            self._market_price.setText("Price: —")

        source = update.source
        latency = f"{update.latency_ms}ms" if update.latency_ms is not None else "—"
        age = f"{update.price_age_ms}ms" if update.price_age_ms is not None else "—"
        self._source_label.setText(f"Source: {source} | Latency {latency} | Age {age}")

        micro = update.microstructure
        if micro.spread_pct is not None:
            self._market_spread.setText(f"Spread: {micro.spread_pct:.4f}%")
        elif micro.spread_abs is not None:
            self._market_spread.setText(f"Spread: {micro.spread_abs:.8f}")
        else:
            self._market_spread.setText("Spread: —")

    def _apply_status_update(self, status: str, _: str) -> None:
        if status == WS_CONNECTED:
            self._feed_status.setText("Feed: WS ok")
            self._feed_status.setStyleSheet("color: #16a34a;")
            return
        if status == WS_DEGRADED:
            self._feed_status.setText("Feed: degraded")
            self._feed_status.setStyleSheet("color: #f59e0b;")
            return
        if status == WS_LOST:
            self._feed_status.setText("Feed: http fallback")
            self._feed_status.setStyleSheet("color: #dc2626;")
            return
        self._feed_status.setText("Feed: —")
        self._feed_status.setStyleSheet("color: #6b7280;")

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
        self._append_log(f"Dry-run {state}.")

    def _handle_start(self) -> None:
        self._append_log(f"Start pressed (dry-run={self._dry_run_toggle.isChecked()}).")
        self._change_state("RUNNING")

    def _handle_pause(self) -> None:
        self._append_log("Pause pressed.")
        self._change_state("PAUSED")

    def _handle_stop(self) -> None:
        self._append_log("Stop pressed.")
        self._change_state("IDLE")

    def _handle_cancel_selected(self) -> None:
        selected_rows = sorted({index.row() for index in self._orders_table.selectionModel().selectedRows()})
        if not selected_rows:
            self._append_log("Cancel selected: —")
            return
        if not self._dry_run_toggle.isChecked():
            self._append_log("Cancel selected: not implemented.")
            return
        for row in reversed(selected_rows):
            order_id = self._order_id_for_row(row)
            self._orders_table.removeRow(row)
            self._append_log(f"Cancel selected: {order_id}")
        self._refresh_orders_metrics()

    def _handle_cancel_all(self) -> None:
        count = self._orders_table.rowCount()
        if count == 0:
            self._append_log("Cancel all: 0")
            return
        if not self._dry_run_toggle.isChecked():
            self._append_log("Cancel all: not implemented.")
            return
        self._orders_table.setRowCount(0)
        self._append_log(f"Cancel all: {count}")
        self._refresh_orders_metrics()

    def _handle_refresh(self) -> None:
        self._append_log("Manual refresh requested (not implemented).")

    def _change_state(self, new_state: str) -> None:
        self._state = new_state
        self._state_label.setText(f"State: {self._state}")

    def _update_setting(self, key: str, value: Any) -> None:
        if hasattr(self._settings_state, key):
            setattr(self._settings_state, key, value)
        if key == "budget":
            self._refresh_orders_metrics()

    def _reset_defaults(self) -> None:
        defaults = GridSettingsState()
        self._settings_state = defaults
        self._budget_input.setValue(defaults.budget)
        self._direction_combo.setCurrentText(defaults.direction)
        self._grid_count_input.setValue(defaults.grid_count)
        self._grid_step_input.setValue(defaults.grid_step_pct)
        self._range_mode_combo.setCurrentText(defaults.range_mode)
        self._range_low_input.setValue(defaults.range_low_pct)
        self._range_high_input.setValue(defaults.range_high_pct)
        self._take_profit_input.setValue(defaults.take_profit_pct)
        self._stop_loss_toggle.setChecked(defaults.stop_loss_enabled)
        self._stop_loss_input.setValue(defaults.stop_loss_pct)
        self._max_orders_input.setValue(defaults.max_active_orders)
        self._order_size_combo.setCurrentText(defaults.order_size_mode)
        self._append_log("Settings reset to defaults.")

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
        self._orders_label.setText(f"Open orders: {self._orders_table.rowCount()}")
        self._budget_label.setText(f"Bot budget: {used:.2f} / {budget:.2f} USDT (used/total)")
        self._used_label.setText(f"Used in orders: {used:.2f} USDT")
        self._free_label.setText(f"Free in bot: {free:.2f} USDT")
        self._locked_label.setText(f"Locked in orders: {locked:.2f} USDT")
        self._max_exposure_label.setText("Max exposure: 100%")

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
            self._pnl_unrealized_label.setText("Unrealized: —")
            self._apply_pnl_style(self._pnl_unrealized_label, None)
        else:
            budget = float(self._budget_input.value())
            pct = (unrealized / budget * 100) if budget > 0 else None
            pct_text = f" ({pct:+.2f}%)" if pct is not None else ""
            self._pnl_unrealized_label.setText(f"Unrealized: {unrealized:+.2f} USDT{pct_text}")
            self._apply_pnl_style(self._pnl_unrealized_label, unrealized)

        if realized is None:
            self._pnl_realized_label.setText("Realized: —")
            self._apply_pnl_style(self._pnl_realized_label, None)
            total = None
        else:
            self._pnl_realized_label.setText(f"Realized: {realized:+.2f} USDT")
            self._apply_pnl_style(self._pnl_realized_label, realized)
            total = realized + (unrealized or 0.0)

        if total is None:
            self._pnl_total_label.setText("Total: —")
            self._apply_pnl_style(self._pnl_total_label, None)
        else:
            self._pnl_total_label.setText(f"Total: {total:+.2f} USDT")
            self._apply_pnl_style(self._pnl_total_label, total)

    def _show_order_context_menu(self, position: Any) -> None:
        row = self._orders_table.rowAt(position.y())
        if row < 0:
            return
        menu = QMenu(self)
        cancel_action = menu.addAction("Cancel")
        show_action = menu.addAction("Show in log")
        action = menu.exec(self._orders_table.viewport().mapToGlobal(position))
        if action == cancel_action:
            if not self._dry_run_toggle.isChecked():
                self._append_log("Cancel selected: not implemented.")
                return
            order_id = self._order_id_for_row(row)
            self._orders_table.removeRow(row)
            self._append_log(f"Cancel selected: {order_id}")
            self._refresh_orders_metrics()
        if action == show_action:
            details = self._format_order_details(row)
            self._append_log(f"Order context: show in log: {details}")

    def _format_order_details(self, row: int) -> str:
        values = []
        for column in range(self._orders_table.columnCount()):
            item = self._orders_table.item(row, column)
            values.append(item.text() if item else "—")
        return ", ".join(values)

    def _append_log(self, message: str) -> None:
        self._log_view.appendPlainText(message)
        self._logger.info("%s | %s", self._symbol, message)

    def closeEvent(self, event: object) -> None:  # noqa: N802
        self._price_feed_manager.unsubscribe(self._symbol, self._emit_price_update)
        self._price_feed_manager.unsubscribe_status(self._symbol, self._emit_status_update)
        self._price_feed_manager.unregister_symbol(self._symbol)
        self._append_log("Lite Grid Terminal closed.")
        super().closeEvent(event)

    def dump_settings(self) -> dict[str, Any]:
        return asdict(self._settings_state)
