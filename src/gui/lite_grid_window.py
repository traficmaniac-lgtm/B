from __future__ import annotations

from dataclasses import asdict, dataclass
from time import perf_counter, time
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
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
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.binance.account_client import BinanceAccountClient
from src.binance.http_client import BinanceHttpClient
from src.core.config import Config
from src.core.logging import get_logger
from src.gui.i18n import TEXT, tr
from src.services.price_feed_manager import PriceFeedManager, PriceUpdate, WS_CONNECTED, WS_DEGRADED, WS_LOST


class _WorkerSignals(QObject):
    success = Signal(object, int)
    error = Signal(str)


class _Worker(QRunnable):
    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self.signals = _WorkerSignals()
        self._fn = fn

    def run(self) -> None:
        start = perf_counter()
        try:
            result = self._fn()
        except Exception as exc:
            self.signals.error.emit(str(exc))
            return
        latency_ms = int((perf_counter() - start) * 1000)
        self.signals.success.emit(result, latency_ms)


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
        config: Config,
        price_feed_manager: PriceFeedManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._logger = get_logger("gui.lite_grid")
        self._config = config
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
        self._thread_pool = QThreadPool.globalInstance()
        self._account_client: BinanceAccountClient | None = None
        self._http_client = BinanceHttpClient(
            base_url=self._config.binance.base_url,
            timeout_s=self._config.http.timeout_s,
            retries=self._config.http.retries,
            backoff_base_s=self._config.http.backoff_base_s,
            backoff_max_s=self._config.http.backoff_max_s,
        )
        if self._config.binance.api_key and self._config.binance.api_secret:
            self._account_client = BinanceAccountClient(
                base_url=self._config.binance.base_url,
                api_key=self._config.binance.api_key,
                api_secret=self._config.binance.api_secret,
                recv_window=self._config.binance.recv_window,
                timeout_s=self._config.http.timeout_s,
                retries=self._config.http.retries,
                backoff_base_s=self._config.http.backoff_base_s,
                backoff_max_s=self._config.http.backoff_max_s,
            )
        self._balances: dict[str, tuple[float, float]] = {}
        self._open_orders: list[dict[str, Any]] = []
        self._last_price: float | None = None
        self._account_can_trade = False
        self._symbol_tradeable = False
        self._trade_disabled_reason = ""
        self._last_trade_disabled_reason = ""
        self._suppress_dry_run_event = False
        self._exchange_rules: dict[str, float | None] = {}
        self._trade_fees: tuple[float | None, float | None] = (None, None)
        self._quote_asset, self._base_asset = self._infer_assets_from_symbol(self._symbol)
        self._balances_in_flight = False
        self._orders_in_flight = False
        self._rules_in_flight = False
        self._fees_in_flight = False

        self._balances_timer = QTimer(self)
        self._balances_timer.setInterval(10_000)
        self._balances_timer.timeout.connect(self._refresh_balances)
        self._orders_timer = QTimer(self)
        self._orders_timer.setInterval(3_000)
        self._orders_timer.timeout.connect(self._refresh_open_orders)

        self.setWindowTitle(tr("window_title", symbol=self._symbol))
        self.resize(1050, 720)

        central = QWidget(self)
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(10, 10, 10, 10)
        outer_layout.setSpacing(8)

        outer_layout.addLayout(self._build_header())
        outer_layout.addWidget(self._build_body())
        outer_layout.addWidget(self._build_logs())

        self.setCentralWidget(central)
        self._apply_trade_status_label()

        self._price_feed_manager.register_symbol(self._symbol)
        self._price_feed_manager.subscribe(self._symbol, self._emit_price_update)
        self._price_feed_manager.subscribe_status(self._symbol, self._emit_status_update)
        self._price_feed_manager.start()
        self._refresh_exchange_rules()
        if self._account_client:
            self._balances_timer.start()
            self._orders_timer.start()
            self._refresh_balances()
            self._refresh_open_orders()
        else:
            self._trade_disabled_reason = "no keys"
            self._apply_trading_permissions()
        self._append_log("Lite Grid Terminal opened.", kind="INFO")

    @property
    def symbol(self) -> str:
        return self._symbol

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
        self._start_button.clicked.connect(self._handle_start)
        self._pause_button.clicked.connect(self._handle_pause)
        self._stop_button.clicked.connect(self._handle_stop)

        self._dry_run_toggle = QPushButton(tr("dry_run"))
        self._dry_run_toggle.setCheckable(True)
        self._dry_run_toggle.setChecked(True)
        self._dry_run_toggle.toggled.connect(self._handle_dry_run_toggle)
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

        self._feed_indicator = QLabel("HTTP ✓ | WS — | CLOCK —")
        self._feed_indicator.setStyleSheet("color: #6b7280; font-size: 11px;")

        self._age_label = QLabel(tr("age", age="—"))
        self._age_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        self._latency_label = QLabel(tr("latency", latency="—"))
        self._latency_label.setStyleSheet("color: #6b7280; font-size: 11px;")

        self._engine_state_label = QLabel(f"{tr('engine')}: {self._engine_state}")
        self._apply_engine_state_style(self._engine_state)
        self._trade_status_label = QLabel(tr("trade_status_disabled"))
        self._trade_status_label.setStyleSheet("color: #dc2626; font-size: 11px; font-weight: 600;")

        row_bottom.addWidget(self._feed_indicator)
        row_bottom.addWidget(self._age_label)
        row_bottom.addWidget(self._latency_label)
        row_bottom.addStretch()
        row_bottom.addWidget(self._trade_status_label)
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
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        self._market_price = QLabel(f"{tr('price')}: —")
        self._market_spread = QLabel(f"{tr('spread')}: —")
        self._market_volatility = QLabel(f"{tr('volatility')}: —")
        self._market_fee = QLabel(f"{tr('fee')}: —")
        self._rules_label = QLabel(tr("rules_line", rules="—"))

        self._set_market_label_state(self._market_price, active=False)
        self._set_market_label_state(self._market_spread, active=False)
        self._set_market_label_state(self._market_volatility, active=False)
        self._set_market_label_state(self._market_fee, active=False)
        self._rules_label.setStyleSheet("color: #6b7280; font-size: 10px;")

        layout.addWidget(self._market_price)
        layout.addWidget(self._market_spread)
        layout.addWidget(self._market_volatility)
        layout.addWidget(self._market_fee)
        layout.addWidget(self._rules_label)
        layout.addStretch()
        return group

    def _build_grid_panel(self) -> QWidget:
        group = QGroupBox(tr("grid_settings"))
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setVerticalSpacing(4)

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
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        fixed_font = QFont()
        fixed_font.setStyleHint(QFont.Monospace)
        fixed_font.setFixedPitch(True)

        self._balance_quote_label = QLabel(
            tr(
                "runtime_account_line",
                quote="0.00",
                base="0.0000",
                equity="0.00",
            )
        )
        self._balance_quote_label.setFont(fixed_font)
        self._balance_bot_label = QLabel(
            tr("runtime_bot_line", used="0.00", free="0.00", locked="0.00")
        )
        self._balance_bot_label.setFont(fixed_font)

        self._pnl_label = QLabel(tr("pnl_line", unreal="0.00", real="0.00", total="+0.00"))
        self._pnl_label.setFont(fixed_font)
        self._pnl_label.setTextFormat(Qt.RichText)

        self._orders_count_label = QLabel(tr("orders_count", count="0"))
        self._orders_count_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        self._orders_count_label.setFont(fixed_font)

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
        self._orders_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._orders_table.customContextMenuRequested.connect(self._show_order_context_menu)

        layout.addWidget(self._orders_table)

        buttons = QHBoxLayout()
        self._cancel_selected_button = QPushButton(tr("cancel_selected"))
        self._cancel_all_button = QPushButton(tr("cancel_all"))
        self._refresh_button = QPushButton(tr("refresh"))
        for button in (self._cancel_selected_button, self._cancel_all_button, self._refresh_button):
            button.setFixedHeight(28)

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
        fixed_font = QFont()
        fixed_font.setStyleHint(QFont.Monospace)
        fixed_font.setFixedPitch(True)
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
        self._trades_summary_label.setFont(fixed_font)

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
            self._last_price = update.last_price
            self._last_price_label.setText(tr("last_price", price=f"{update.last_price:.8f}"))
            self._market_price.setText(f"{tr('price')}: {update.last_price:.8f}")
            self._set_market_label_state(self._market_price, active=True)
        else:
            self._last_price = None
            self._last_price_label.setText(tr("last_price", price="—"))
            self._market_price.setText(f"{tr('price')}: —")
            self._set_market_label_state(self._market_price, active=False)

        latency = f"{update.latency_ms}ms" if update.latency_ms is not None else "—"
        age = f"{update.price_age_ms}ms" if update.price_age_ms is not None else "—"
        self._age_label.setText(tr("age", age=age))
        self._latency_label.setText(tr("latency", latency=latency))
        clock_status = "✓" if update.price_age_ms is not None else "—"
        self._feed_indicator.setText(f"HTTP ✓ | WS {self._ws_indicator_symbol()} | CLOCK {clock_status}")

        micro = update.microstructure
        if micro.spread_pct is not None:
            self._market_spread.setText(f"{tr('spread')}: {micro.spread_pct:.4f}%")
            self._set_market_label_state(self._market_spread, active=True)
        elif micro.spread_abs is not None:
            self._market_spread.setText(f"{tr('spread')}: {micro.spread_abs:.8f}")
            self._set_market_label_state(self._market_spread, active=True)
        else:
            self._market_spread.setText(f"{tr('spread')}: —")
            self._set_market_label_state(self._market_spread, active=False)
        self._update_runtime_balances()

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

    @staticmethod
    def _set_market_label_state(label: QLabel, active: bool) -> None:
        if active:
            label.setStyleSheet("color: #111827; font-size: 11px;")
        else:
            label.setStyleSheet("color: #9ca3af; font-size: 10px;")

    def _update_runtime_balances(self) -> None:
        quote_total = self._asset_total(self._quote_asset)
        base_total = self._asset_total(self._base_asset)
        equity = quote_total + (base_total * self._last_price if self._last_price else 0.0)
        self._balance_quote_label.setText(
            tr(
                "runtime_account_line",
                quote=f"{quote_total:.2f}",
                base=f"{base_total:.8f}",
                equity=f"{equity:.2f}",
            )
        )
        used = self._open_orders_value()
        locked = used
        free = max(quote_total - used, 0.0)
        self._balance_bot_label.setText(
            tr(
                "runtime_bot_line",
                used=f"{used:.2f}",
                free=f"{free:.2f}",
                locked=f"{locked:.2f}",
            )
        )

    def _asset_total(self, asset: str) -> float:
        free, locked = self._balances.get(asset, (0.0, 0.0))
        return free + locked

    def _open_orders_value(self) -> float:
        return sum(self._extract_order_value(row) for row in range(self._orders_table.rowCount()))

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
        if self._suppress_dry_run_event:
            return
        if not checked and not self._can_trade():
            self._suppress_dry_run_event = True
            self._dry_run_toggle.setChecked(True)
            self._suppress_dry_run_event = False
            return
        if not checked:
            confirm = QMessageBox.question(
                self,
                tr("trade_confirm_title"),
                tr("trade_confirm_message"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                self._suppress_dry_run_event = True
                self._dry_run_toggle.setChecked(True)
                self._suppress_dry_run_event = False
                return
        state = "enabled" if checked else "disabled"
        self._append_log(f"Dry-run {state}.", kind="INFO")
        self._append_log(
            f"Trade enabled state changed: live={str(not checked).lower()}.",
            kind="INFO",
        )
        self._apply_trade_status_label()

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
        self._append_log("Manual refresh requested.", kind="INFO")
        self._refresh_balances()
        self._refresh_open_orders()

    def _change_state(self, new_state: str) -> None:
        self._state = new_state
        self._state_badge.setText(f"{tr('state')}: {self._state}")
        self._engine_state = self._engine_state_from_status(new_state)
        self._engine_state_label.setText(f"{tr('engine')}: {self._engine_state}")
        self._apply_engine_state_style(self._engine_state)

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

    def _refresh_balances(self) -> None:
        if not self._account_client or self._balances_in_flight:
            return
        self._balances_in_flight = True
        worker = _Worker(self._account_client.get_account_info)
        worker.signals.success.connect(self._handle_account_info)
        worker.signals.error.connect(self._handle_account_error)
        self._thread_pool.start(worker)

    def _handle_account_info(self, result: object, latency_ms: int) -> None:
        self._balances_in_flight = False
        if not isinstance(result, dict):
            self._handle_account_error("Unexpected account response")
            return
        balances_raw = result.get("balances", [])
        balances: dict[str, tuple[float, float]] = {}
        if isinstance(balances_raw, list):
            for entry in balances_raw:
                if not isinstance(entry, dict):
                    continue
                asset = str(entry.get("asset", "")).upper()
                if not asset:
                    continue
                free = self._coerce_float(str(entry.get("free", ""))) or 0.0
                locked = self._coerce_float(str(entry.get("locked", ""))) or 0.0
                balances[asset] = (free, locked)
        self._balances = balances
        can_trade = result.get("canTrade")
        permissions = result.get("permissions")
        self._account_can_trade = True
        if can_trade is not None:
            self._account_can_trade = bool(can_trade)
        if isinstance(permissions, list) and permissions:
            self._account_can_trade = self._account_can_trade and "SPOT" in permissions
            if "SPOT" not in permissions:
                self._trade_disabled_reason = "no permissions"
        elif isinstance(permissions, list) and not permissions:
            self._account_can_trade = False
            self._trade_disabled_reason = "no permissions"
        if not self._account_can_trade and not self._trade_disabled_reason:
            self._trade_disabled_reason = "canTrade=false"
        if self._account_can_trade:
            self._trade_disabled_reason = ""
        self._apply_trade_fees_from_account(result)
        self._apply_trading_permissions()
        self._update_runtime_balances()
        quote_total = self._asset_total(self._quote_asset)
        base_total = self._asset_total(self._base_asset)
        self._append_log(
            f"balances updated: {self._quote_asset}={quote_total:.2f}, "
            f"{self._base_asset}={base_total:.8f}",
            kind="INFO",
        )
        self._refresh_trade_fees()

    def _handle_account_error(self, message: str) -> None:
        self._balances_in_flight = False
        self._account_can_trade = False
        self._trade_disabled_reason = self._infer_trade_error_reason(message)
        self._append_log(f"Balances update error: {message}", kind="ERROR")
        self._apply_trading_permissions()

    def _refresh_open_orders(self) -> None:
        if not self._account_client or self._orders_in_flight:
            return
        self._orders_in_flight = True
        worker = _Worker(lambda: self._account_client.get_open_orders(self._symbol))
        worker.signals.success.connect(self._handle_open_orders)
        worker.signals.error.connect(self._handle_open_orders_error)
        self._thread_pool.start(worker)

    def _handle_open_orders(self, result: object, latency_ms: int) -> None:
        self._orders_in_flight = False
        if not isinstance(result, list):
            self._handle_open_orders_error("Unexpected open orders response")
            return
        self._open_orders = [item for item in result if isinstance(item, dict)]
        self._render_open_orders()
        self._append_log(
            f"open orders updated (n={len(self._open_orders)}).",
            kind="INFO",
        )

    def _handle_open_orders_error(self, message: str) -> None:
        self._orders_in_flight = False
        self._trade_disabled_reason = self._infer_trade_error_reason(message)
        self._append_log(f"Open orders update error: {message}", kind="ERROR")
        self._apply_trading_permissions()

    def _refresh_exchange_rules(self) -> None:
        if self._rules_in_flight:
            return
        self._rules_in_flight = True
        worker = _Worker(lambda: self._http_client.get_exchange_info_symbol(self._symbol))
        worker.signals.success.connect(self._handle_exchange_info)
        worker.signals.error.connect(self._handle_exchange_error)
        self._thread_pool.start(worker)

    def _handle_exchange_info(self, result: object, latency_ms: int) -> None:
        self._rules_in_flight = False
        if not isinstance(result, dict):
            self._handle_exchange_error("Unexpected exchange info response")
            return
        symbols = result.get("symbols", []) if isinstance(result.get("symbols"), list) else []
        info = symbols[0] if symbols else {}
        if not isinstance(info, dict):
            self._handle_exchange_error("Unexpected exchange info payload")
            return
        self._base_asset = str(info.get("baseAsset", self._base_asset)).upper()
        self._quote_asset = str(info.get("quoteAsset", self._quote_asset)).upper()
        self._symbol_tradeable = str(info.get("status", "")).upper() == "TRADING"
        filters = info.get("filters", [])
        tick_size = self._extract_filter_value(filters, "PRICE_FILTER", "tickSize")
        step_size = self._extract_filter_value(filters, "LOT_SIZE", "stepSize")
        min_notional = self._extract_filter_value(filters, "MIN_NOTIONAL", "minNotional")
        if min_notional is None:
            min_notional = self._extract_filter_value(filters, "NOTIONAL", "minNotional")
        self._exchange_rules = {
            "tick": tick_size,
            "step": step_size,
            "min_notional": min_notional,
        }
        self._apply_trading_permissions()
        self._update_rules_label()
        self._update_grid_preview()
        self._update_runtime_balances()
        self._append_log(
            f"Exchange rules loaded ({latency_ms}ms). base={self._base_asset}, quote={self._quote_asset}",
            kind="INFO",
        )

    def _handle_exchange_error(self, message: str) -> None:
        self._rules_in_flight = False
        self._symbol_tradeable = False
        self._append_log(f"Exchange rules error: {message}", kind="ERROR")
        self._update_rules_label()

    def _refresh_trade_fees(self) -> None:
        if not self._account_client or self._fees_in_flight:
            return
        self._fees_in_flight = True
        worker = _Worker(lambda: self._account_client.get_trade_fees(self._symbol))
        worker.signals.success.connect(self._handle_trade_fees)
        worker.signals.error.connect(self._handle_trade_fees_error)
        self._thread_pool.start(worker)

    def _handle_trade_fees(self, result: object, latency_ms: int) -> None:
        self._fees_in_flight = False
        if not isinstance(result, list):
            self._handle_trade_fees_error("Unexpected trade fee response")
            return
        entry = result[0] if result else {}
        if isinstance(entry, dict):
            maker = self._coerce_float(str(entry.get("makerCommission", "")))
            taker = self._coerce_float(str(entry.get("takerCommission", "")))
            self._trade_fees = (maker, taker)
        self._update_rules_label()
        self._append_log(f"Trade fees loaded ({latency_ms}ms).", kind="INFO")

    def _handle_trade_fees_error(self, message: str) -> None:
        self._fees_in_flight = False
        self._append_log(f"Trade fees error: {message}", kind="ERROR")
        self._update_rules_label()

    def _apply_trade_fees_from_account(self, account: dict[str, Any]) -> None:
        maker_raw = account.get("makerCommission")
        taker_raw = account.get("takerCommission")
        if maker_raw is None or taker_raw is None:
            return
        maker = self._coerce_float(str(maker_raw))
        taker = self._coerce_float(str(taker_raw))
        if maker is None or taker is None:
            return
        self._trade_fees = (maker / 10_000, taker / 10_000)
        self._update_rules_label()

    def _apply_trading_permissions(self) -> None:
        can_trade = self._can_trade()
        if not can_trade:
            if not self._trade_disabled_reason:
                self._trade_disabled_reason = "read-only"
            if self._trade_disabled_reason != self._last_trade_disabled_reason:
                self._append_log(
                    f"cannot trade: reason={self._trade_disabled_reason}",
                    kind="WARN",
                )
                self._last_trade_disabled_reason = self._trade_disabled_reason
            self._suppress_dry_run_event = True
            self._dry_run_toggle.setChecked(True)
            self._suppress_dry_run_event = False
            self._dry_run_toggle.setEnabled(False)
            self._dry_run_toggle.setToolTip(tr("trade_disabled_tooltip"))
        else:
            self._trade_disabled_reason = ""
            self._last_trade_disabled_reason = ""
            self._dry_run_toggle.setEnabled(True)
            self._dry_run_toggle.setToolTip("")
        self._apply_trade_status_label()
        self._update_grid_preview()

    def _apply_trade_status_label(self) -> None:
        if not self._can_trade():
            self._trade_status_label.setText(tr("trade_status_disabled"))
            self._trade_status_label.setStyleSheet(
                "color: #dc2626; font-size: 11px; font-weight: 600;"
            )
            self._set_cancel_buttons_enabled(True)
        elif self._dry_run_toggle.isChecked():
            self._trade_status_label.setText(tr("trade_status_dry_run"))
            self._trade_status_label.setStyleSheet(
                "color: #6b7280; font-size: 11px; font-weight: 600;"
            )
            self._set_cancel_buttons_enabled(True)
        else:
            self._trade_status_label.setText(tr("trade_status_live"))
            self._trade_status_label.setStyleSheet(
                "color: #16a34a; font-size: 11px; font-weight: 600;"
            )
            self._set_cancel_buttons_enabled(False)

    def _set_cancel_buttons_enabled(self, enabled: bool) -> None:
        if hasattr(self, "_cancel_selected_button"):
            self._cancel_selected_button.setEnabled(enabled)
        if hasattr(self, "_cancel_all_button"):
            self._cancel_all_button.setEnabled(enabled)

    def _can_trade(self) -> bool:
        if not self._account_client:
            return False
        if not self._symbol_tradeable:
            return False
        return self._account_can_trade

    @staticmethod
    def _infer_trade_error_reason(message: str) -> str:
        message_lower = message.lower()
        if "401" in message_lower or "unauthorized" in message_lower:
            return "invalid keys"
        if "403" in message_lower or "forbidden" in message_lower:
            return "no permissions"
        return "read-only"

    def _update_rules_label(self) -> None:
        tick = self._exchange_rules.get("tick")
        step = self._exchange_rules.get("step")
        min_notional = self._exchange_rules.get("min_notional")
        maker, taker = self._trade_fees
        has_rules = any(value is not None for value in (tick, step, min_notional, maker, taker))
        if not has_rules:
            self._rules_label.setText(tr("rules_line", rules="—"))
            self._market_fee.setText(f"{tr('fee')}: —")
            self._set_market_label_state(self._market_fee, active=False)
            return
        tick_text = f"{tick:.8f}" if tick is not None else "—"
        step_text = f"{step:.8f}" if step is not None else "—"
        min_text = f"{min_notional:.4f}" if min_notional is not None else "—"
        maker_text = f"{(maker or 0.0) * 100:.2f}%" if maker is not None else "—"
        taker_text = f"{(taker or 0.0) * 100:.2f}%" if taker is not None else "—"
        rules = f"tick {tick_text} | step {step_text} | minNotional {min_text} | maker/taker {maker_text}/{taker_text}"
        self._rules_label.setText(tr("rules_line", rules=rules))
        self._market_fee.setText(f"{tr('fee')}: {maker_text}/{taker_text}")
        self._set_market_label_state(self._market_fee, active=maker is not None or taker is not None)

    @staticmethod
    def _extract_filter_value(filters: object, filter_type: str, key: str) -> float | None:
        if not isinstance(filters, list):
            return None
        for entry in filters:
            if not isinstance(entry, dict):
                continue
            if entry.get("filterType") == filter_type:
                return LiteGridWindow._coerce_float(str(entry.get(key, "")))
        return None

    def _order_id_for_row(self, row: int) -> str:
        item = self._orders_table.item(row, 0)
        if item:
            data = item.data(Qt.UserRole)
            if data:
                return str(data)
            if item.text():
                return item.text()
        side_item = self._orders_table.item(row, 1)
        if side_item:
            data = side_item.data(Qt.UserRole)
            if data:
                return str(data)
        return "—"

    def _render_open_orders(self) -> None:
        self._orders_table.setRowCount(len(self._open_orders))
        now_ms = int(time() * 1000)
        for row, order in enumerate(self._open_orders):
            order_id = str(order.get("orderId", "—"))
            side = str(order.get("side", "—"))
            price_raw = order.get("price", "—")
            qty_raw = order.get("origQty", "—")
            filled_raw = order.get("executedQty", "—")
            time_ms = order.get("time")
            age_text = self._format_age(time_ms, now_ms)
            self._set_order_cell(row, 0, "", align=Qt.AlignLeft, user_role=order_id)
            self._set_order_cell(row, 1, side, align=Qt.AlignLeft, user_role=order_id)
            self._set_order_cell(row, 2, str(price_raw), align=Qt.AlignRight)
            self._set_order_cell(row, 3, str(qty_raw), align=Qt.AlignRight)
            self._set_order_cell(row, 4, str(filled_raw), align=Qt.AlignRight)
            self._set_order_cell(row, 5, age_text, align=Qt.AlignRight)
            self._set_order_row_tooltip(row)
        self._refresh_orders_metrics()

    def _set_order_cell(
        self,
        row: int,
        column: int,
        text: str,
        align: Qt.AlignmentFlag,
        user_role: str | None = None,
    ) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(int(align | Qt.AlignVCenter))
        if user_role is not None:
            item.setData(Qt.UserRole, user_role)
        self._orders_table.setItem(row, column, item)

    def _remove_orders_by_side(self, side: str) -> int:
        removed = 0
        for row in reversed(range(self._orders_table.rowCount())):
            side_item = self._orders_table.item(row, 1)
            side_text = side_item.text().upper() if side_item else ""
            if side_text == side.upper():
                self._orders_table.removeRow(row)
                removed += 1
        return removed

    def _format_age(self, time_ms: object, now_ms: int) -> str:
        if not isinstance(time_ms, (int, float)):
            return "—"
        age_ms = max(now_ms - int(time_ms), 0)
        seconds = age_ms // 1000
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

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
        self._orders_count_label.setText(tr("orders_count", count=str(self._orders_table.rowCount())))
        self._update_runtime_balances()
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

    def _apply_engine_state_style(self, state: str) -> None:
        color_map = {
            "WAITING": "#6b7280",
            "PLACING_GRID": "#2563eb",
            "WAITING_FILLS": "#d97706",
            "REBALANCING": "#7c3aed",
            "PAUSED_BY_RISK": "#f97316",
            "ERROR": "#dc2626",
        }
        color = color_map.get(state, "#6b7280")
        self._engine_state_label.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 600;"
        )

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
            total_text = f"{total:+.2f}"
            self._apply_pnl_style(self._pnl_label, total)

        self._pnl_label.setText(tr("pnl_line", unreal=unreal_text, real=real_text, total=total_text))

    def _show_order_context_menu(self, position: Any) -> None:
        row = self._orders_table.rowAt(position.y())
        if row < 0:
            return
        menu = QMenu(self)
        cancel_action = menu.addAction(tr("context_cancel"))
        cancel_action.setEnabled(self._dry_run_toggle.isChecked())
        copy_action = menu.addAction(tr("context_copy_id"))
        show_action = menu.addAction(tr("context_show_logs"))
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
            self._append_log(f"Copy id: {order_id}", kind="ORDERS")
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
            if header_text.upper() == "ID":
                values.append(f"{header_text}={self._order_id_for_row(row)}")
            else:
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
            "REBALANCING": "REBALANCING",
            "PAUSED_BY_RISK": "PAUSED_BY_RISK",
            "ERROR": "ERROR",
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
                quote_ccy=self._quote_asset,
            )
        )

    @staticmethod
    def _infer_assets_from_symbol(symbol: str) -> tuple[str, str]:
        candidates = ["USDT", "USDC", "BUSD", "BTC", "ETH", "BNB"]
        for quote in candidates:
            if symbol.endswith(quote):
                base = symbol[: -len(quote)]
                return quote, base or symbol
        return "USDT", symbol

    def closeEvent(self, event: object) -> None:  # noqa: N802
        self._balances_timer.stop()
        self._orders_timer.stop()
        self._price_feed_manager.unsubscribe(self._symbol, self._emit_price_update)
        self._price_feed_manager.unsubscribe_status(self._symbol, self._emit_status_update)
        self._price_feed_manager.unregister_symbol(self._symbol)
        self._append_log("Lite Grid Terminal closed.", kind="INFO")
        super().closeEvent(event)

    def dump_settings(self) -> dict[str, Any]:
        return asdict(self._settings_state)
