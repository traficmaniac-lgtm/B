from __future__ import annotations

from collections import deque
from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from math import ceil, floor
from time import perf_counter, sleep, time
from typing import Any, Callable
from uuid import uuid4

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

from src.binance.account_client import AccountStatus, BinanceAccountClient
from src.binance.http_client import BinanceHttpClient
from src.core.config import Config
from src.core.logging import get_logger
from src.gui.i18n import TEXT, tr
from src.gui.models.app_state import AppState
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
    grid_step_mode: str = "AUTO_ATR"
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
    log_append = Signal(str, str)
    api_error = Signal(str)


class TradeGate(Enum):
    TRADE_OK = "ok"
    TRADE_DISABLED_NO_KEYS = "no keys"
    TRADE_DISABLED_API_ERROR = "api error"
    TRADE_DISABLED_CANT_TRADE = "canTrade=false"
    TRADE_DISABLED_SYMBOL = "symbol not trading"
    TRADE_DISABLED_READONLY = "read-only"
    TRADE_DISABLED_NO_CONFIRM = "no live confirm"


@dataclass
class GridPlannedOrder:
    side: str
    price: float
    qty: float
    level_index: int


@dataclass
class GridPlanStats:
    min_notional_failed: int = 0


@dataclass
class TradeFill:
    side: str
    price: float
    qty: float
    quote_qty: float
    commission: float
    commission_asset: str
    time_ms: int
    order_id: str
    trade_id: str


@dataclass
class BaseLot:
    qty: float
    cost_per_unit: float
    trade_id: str


@dataclass
class BaseLot:
    qty: float
    cost_per_unit: float


class GridEngine:
    def __init__(
        self,
        on_state_change: Callable[[str], None],
        on_log: Callable[[str, str], None],
    ) -> None:
        self.state = "IDLE"
        self.mode = "DRY_RUN"
        self._planned_orders: list[GridPlannedOrder] = []
        self._plan_stats = GridPlanStats()
        self._on_state_change = on_state_change
        self._on_log = on_log

    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def start(
        self,
        settings: GridSettingsState,
        last_price: float | None,
        rules: dict[str, float | None],
    ) -> list[GridPlannedOrder]:
        if last_price is None:
            raise ValueError("No price available.")
        if settings.budget <= 0:
            raise ValueError("Budget must be > 0.")
        if settings.grid_count < 2:
            raise ValueError("Grid count must be >= 2.")
        if settings.grid_step_pct <= 0:
            raise ValueError("Grid step must be > 0.")
        if settings.range_low_pct <= 0 or settings.range_high_pct <= 0:
            raise ValueError("Range must be > 0.")
        self._plan_stats = GridPlanStats()
        plan = self._build_plan(settings, last_price, rules)
        self._planned_orders = plan
        return plan

    def pause(self) -> None:
        if self.state not in {"RUNNING", "PLACING_GRID"}:
            return
        self._set_state("PAUSED")

    def stop(self, cancel_all: bool = True) -> None:
        if self.state == "IDLE":
            return
        self._set_state("STOPPING")
        self._planned_orders = []
        self._set_state("IDLE")

    def on_price(self, price: float) -> None:
        _ = price

    def sync_open_orders(self, orders: list[dict[str, Any]]) -> None:
        _ = orders

    def sync_balances(self, balances: dict[str, tuple[float, float]]) -> None:
        _ = balances

    def _set_state(self, state: str) -> None:
        self.state = state
        self._on_state_change(state)

    def _build_plan(
        self,
        settings: GridSettingsState,
        last_price: float,
        rules: dict[str, float | None],
    ) -> list[GridPlannedOrder]:
        tick = rules.get("tick")
        step = rules.get("step")
        min_notional = rules.get("min_notional")
        min_qty = rules.get("min_qty")
        max_qty = rules.get("max_qty")
        levels = int(settings.grid_count)
        budget = float(settings.budget)
        step_pct = float(settings.grid_step_pct)
        range_low = float(settings.range_low_pct)
        range_high = float(settings.range_high_pct)
        buys, sells = self._split_levels(levels, settings.direction)
        buy_budget, sell_budget = self._split_budget(budget, buys, sells, settings.direction)
        per_order_quote_buy = buy_budget / buys if buys else 0.0
        per_order_quote_sell = sell_budget / sells if sells else 0.0
        anchor_price = last_price
        buy_min = anchor_price * (1 - range_low / 100.0)
        sell_max = anchor_price * (1 + range_high / 100.0)
        buy_max = anchor_price
        sell_min = anchor_price

        buy_orders = self._build_side(
            side="BUY",
            count=buys,
            anchor_price=anchor_price,
            step_pct=step_pct,
            price_min=buy_min,
            price_max=buy_max,
            per_order_quote=per_order_quote_buy,
            tick=tick,
            step=step,
            min_notional=min_notional,
            min_qty=min_qty,
            max_qty=max_qty,
        )
        sell_orders = self._build_side(
            side="SELL",
            count=sells,
            anchor_price=anchor_price,
            step_pct=step_pct,
            price_min=sell_min,
            price_max=sell_max,
            per_order_quote=per_order_quote_sell,
            tick=tick,
            step=step,
            min_notional=min_notional,
            min_qty=min_qty,
            max_qty=max_qty,
        )
        plan = buy_orders + sell_orders
        return plan

    def _split_levels(self, total: int, direction: str) -> tuple[int, int]:
        if direction == "Long-biased":
            buy = int(ceil(total * 0.6))
            sell = max(total - buy, 0)
            return buy, sell
        elif direction == "Short-biased":
            buy = int(floor(total * 0.4))
            sell = max(total - buy, 0)
            return buy, sell
        per_side = max(total // 2, 1)
        return per_side, per_side

    def _build_side(
        self,
        side: str,
        count: int,
        anchor_price: float,
        step_pct: float,
        price_min: float,
        price_max: float,
        per_order_quote: float,
        tick: float | None,
        step: float | None,
        min_notional: float | None,
        min_qty: float | None,
        max_qty: float | None,
    ) -> list[GridPlannedOrder]:
        orders: list[GridPlannedOrder] = []
        use_tick_ladder = False
        last_price: float | None = None
        for idx in range(count):
            offset = step_pct * (idx + 1) / 100.0
            if side == "BUY":
                raw_price = anchor_price * (1 - offset)
                if raw_price < price_min:
                    break
                price = self.round_price_to_tick(raw_price, tick, mode="down")
                if price < price_min:
                    break
            else:
                raw_price = anchor_price * (1 + offset)
                if raw_price > price_max:
                    break
                price = self.round_price_to_tick(raw_price, tick, mode="up")
                if price > price_max:
                    break
            if price <= 0:
                continue
            if last_price is not None and price == last_price:
                use_tick_ladder = True
                break
            qty = per_order_quote / price if price > 0 else 0.0
            qty = self.quantize_qty(qty, step, mode="down")
            qty = self._adjust_qty_for_filters(
                side=side,
                price=price,
                qty=qty,
                step=step,
                min_notional=min_notional,
                min_qty=min_qty,
                max_qty=max_qty,
            )
            if qty <= 0:
                continue
            orders.append(GridPlannedOrder(side=side, price=price, qty=qty, level_index=idx + 1))
            last_price = price
        if use_tick_ladder and tick and tick > 0:
            self._on_log(
                "[PLAN] pct_step too small vs tick; switching to tick ladder",
                "INFO",
            )
            return self._build_tick_ladder(
                side=side,
                count=count,
                anchor_price=anchor_price,
                tick=tick,
                price_min=price_min,
                price_max=price_max,
                per_order_quote=per_order_quote,
                step=step,
                min_notional=min_notional,
                min_qty=min_qty,
                max_qty=max_qty,
            )
        return orders

    def _build_tick_ladder(
        self,
        side: str,
        count: int,
        anchor_price: float,
        tick: float,
        price_min: float,
        price_max: float,
        per_order_quote: float,
        step: float | None,
        min_notional: float | None,
        min_qty: float | None,
        max_qty: float | None,
    ) -> list[GridPlannedOrder]:
        orders: list[GridPlannedOrder] = []
        for idx in range(count):
            ladder_price = anchor_price + (idx + 1) * tick if side == "SELL" else anchor_price - (idx + 1) * tick
            if side == "BUY" and ladder_price < price_min:
                break
            if side == "SELL" and ladder_price > price_max:
                break
            price = self.round_price_to_tick(ladder_price, tick, mode="up" if side == "SELL" else "down")
            if side == "BUY" and price < price_min:
                break
            if side == "SELL" and price > price_max:
                break
            if price <= 0:
                continue
            qty = per_order_quote / price if price > 0 else 0.0
            qty = self.quantize_qty(qty, step, mode="down")
            qty = self._adjust_qty_for_filters(
                side=side,
                price=price,
                qty=qty,
                step=step,
                min_notional=min_notional,
                min_qty=min_qty,
                max_qty=max_qty,
            )
            if qty <= 0:
                continue
            orders.append(GridPlannedOrder(side=side, price=price, qty=qty, level_index=idx + 1))
        return orders

    def _quantize(self, value: float, step: float | None, round_up: bool) -> float:
        if step is None or step <= 0:
            return value
        if round_up:
            return ceil(value / step) * step
        return floor(value / step) * step

    def round_price_to_tick(self, price: float, tick: float | None, mode: str) -> float:
        if tick is None or tick <= 0:
            return price
        round_up = mode == "up"
        return self._quantize(price, tick, round_up=round_up)

    def quantize_price(self, price: float, tick: float | None) -> float:
        return self.round_price_to_tick(price, tick, mode="down")

    def quantize_qty(self, qty: float, step: float | None, mode: str) -> float:
        if step is None or step <= 0:
            return qty
        return self._quantize(qty, step, round_up=(mode == "up"))

    def _adjust_qty_for_filters(
        self,
        side: str,
        price: float,
        qty: float,
        step: float | None,
        min_notional: float | None,
        min_qty: float | None,
        max_qty: float | None,
    ) -> float:
        if price <= 0 or qty <= 0:
            return 0.0
        if max_qty is not None and qty > max_qty:
            qty = self.quantize_qty(max_qty, step, mode="down")
        if min_qty is not None and qty < min_qty:
            qty = self.quantize_qty(min_qty, step, mode="up")
            if max_qty is not None and qty > max_qty:
                self._log_skip(
                    "minQty",
                    side=side,
                    price=price,
                    qty=qty,
                    min_value=min_qty,
                )
                return 0.0
        qty = self.ensure_min_notional(
            side=side,
            price=price,
            qty=qty,
            step=step,
            min_notional=min_notional,
            min_qty=min_qty,
            max_qty=max_qty,
        )
        return qty

    def ensure_min_notional(
        self,
        side: str,
        price: float,
        qty: float,
        step: float | None,
        min_notional: float | None,
        min_qty: float | None,
        max_qty: float | None,
    ) -> float:
        if min_notional is None or price <= 0 or qty <= 0:
            return qty
        if price * qty >= min_notional:
            return qty
        required_qty = min_notional / price
        required_qty = self.quantize_qty(required_qty, step, mode="up")
        if min_qty is not None and required_qty < min_qty:
            required_qty = self.quantize_qty(min_qty, step, mode="up")
        if max_qty is not None and required_qty > max_qty:
            self._plan_stats.min_notional_failed += 1
            self._log_skip(
                "minNotional",
                side=side,
                price=price,
                qty=required_qty,
                min_value=min_notional,
            )
            return 0.0
        return required_qty

    def _split_budget(self, budget: float, buys: int, sells: int, direction: str) -> tuple[float, float]:
        if buys + sells <= 0:
            return 0.0, 0.0
        if direction == "Neutral":
            buy_budget = budget / 2
            sell_budget = budget - buy_budget
            return buy_budget, sell_budget
        buy_weight = buys / (buys + sells)
        buy_budget = budget * buy_weight
        sell_budget = budget - buy_budget
        return buy_budget, sell_budget

    def _log_skip(self, reason: str, side: str, price: float, qty: float, min_value: float) -> None:
        self._on_log(
            (
                f"SKIP {reason}"
                f" side={side} price={price:.8f} qty={qty:.8f} min={min_value:.8f}"
            ),
            "WARN",
        )

    def get_plan_stats(self) -> GridPlanStats:
        return self._plan_stats


class LiteGridWindow(QMainWindow):
    def __init__(
        self,
        symbol: str,
        config: Config,
        app_state: AppState,
        price_feed_manager: PriceFeedManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._logger = get_logger("gui.lite_grid")
        self._config = config
        self._app_state = app_state
        self._symbol = symbol.strip().upper()
        self._price_feed_manager = price_feed_manager
        self._signals = _LiteGridSignals()
        self._signals.price_update.connect(self._apply_price_update)
        self._signals.status_update.connect(self._apply_status_update)
        self._signals.log_append.connect(self._append_log)
        self._signals.api_error.connect(self._handle_live_api_error)
        self._state = "IDLE"
        self._engine_state = "WAITING"
        self._ws_status = ""
        self._settings_state = GridSettingsState()
        self._log_entries: list[tuple[str, str]] = []
        self._grid_engine = GridEngine(self._set_engine_state, self._append_log)
        self._manual_grid_step_pct = self._settings_state.grid_step_pct
        self._thread_pool = QThreadPool.globalInstance()
        self._account_client: BinanceAccountClient | None = None
        api_key, api_secret = self._app_state.get_binance_keys()
        self._has_api_keys = bool(api_key and api_secret)
        self._can_read_account = False
        self._last_account_status = ""
        self._last_account_trade_snapshot: tuple[bool, tuple[str, ...]] | None = None
        self._http_client = BinanceHttpClient(
            base_url=self._config.binance.base_url,
            timeout_s=self._config.http.timeout_s,
            retries=self._config.http.retries,
            backoff_base_s=self._config.http.backoff_base_s,
            backoff_max_s=self._config.http.backoff_max_s,
        )
        if self._has_api_keys:
            self._account_client = BinanceAccountClient(
                base_url=self._config.binance.base_url,
                api_key=api_key,
                api_secret=api_secret,
                recv_window=self._config.binance.recv_window,
                timeout_s=self._config.http.timeout_s,
                retries=self._config.http.retries,
                backoff_base_s=self._config.http.backoff_base_s,
                backoff_max_s=self._config.http.backoff_max_s,
            )
            self._sync_account_time()
        self._logger.info("binance keys present: %s", self._has_api_keys)
        self._balances: dict[str, tuple[float, float]] = {}
        self._open_orders: list[dict[str, Any]] = []
        self._open_orders_all: list[dict[str, Any]] = []
        self._bot_order_ids: set[str] = set()
        self._bot_order_keys: set[str] = set()
        self._fill_keys: set[str] = set()
        self._bot_session_id: str | None = None
        self._last_price: float | None = None
        self._price_history: list[float] = []
        self._account_can_trade = False
        self._account_permissions: list[str] = []
        self._account_api_error = False
        self._symbol_tradeable = False
        self._suppress_dry_run_event = False
        self._exchange_rules: dict[str, float | None] = {}
        self._trade_fees: tuple[float | None, float | None] = (None, None)
        self._fees_last_fetch_ts: float | None = None
        self._quote_asset = ""
        self._base_asset = ""
        self._balances_in_flight = False
        self._balances_loaded = False
        self._orders_in_flight = False
        self._rules_in_flight = False
        self._fees_in_flight = False
        self._trade_gate = TradeGate.TRADE_DISABLED_NO_KEYS if not self._has_api_keys else TradeGate.TRADE_DISABLED_READONLY
        self._rules_loaded = False
        self._live_mode_confirmed = False
        self._first_live_session = True
        self._live_settings: GridSettingsState | None = None
        self._open_orders_map: dict[str, dict[str, Any]] = {}
        self._fills: list[TradeFill] = []
        self._base_lots: deque[BaseLot] = deque()
        self._fills_in_flight = False
        self._seen_trade_ids: set[str] = set()
        self._realized_pnl = 0.0
        self._fees_total = 0.0
        self._closed_trades = 0
        self._win_trades = 0
        self._replacement_counter = 0
        self._balances_tick_count = 0
        self._orders_tick_count = 0
        self._orders_last_count: int | None = None
        self._balances_snapshot: tuple[float, float] | None = None

        self._balances_timer = QTimer(self)
        self._balances_timer.setInterval(10_000)
        self._balances_timer.timeout.connect(self._refresh_balances)
        self._orders_timer = QTimer(self)
        self._orders_timer.setInterval(3_000)
        self._orders_timer.timeout.connect(self._refresh_open_orders)
        self._fills_timer = QTimer(self)
        self._fills_timer.setInterval(2_500)
        self._fills_timer.timeout.connect(self._refresh_fills)

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
        self._apply_trade_gate()

        self._price_feed_manager.register_symbol(self._symbol)
        self._price_feed_manager.subscribe(self._symbol, self._emit_price_update)
        self._price_feed_manager.subscribe_status(self._symbol, self._emit_status_update)
        self._price_feed_manager.start()
        self._refresh_exchange_rules()
        if self._account_client:
            self._balances_timer.start(10_000)
            self._orders_timer.start(3_000)
            self._refresh_balances()
            self._refresh_open_orders()
            self._update_orders_timer_interval()
        else:
            self._set_account_status("no_keys")
            self._apply_trade_gate()
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

        self._grid_step_mode_combo = QComboBox()
        self._grid_step_mode_combo.addItem("AUTO ATR", "AUTO_ATR")
        self._grid_step_mode_combo.addItem("MANUAL", "MANUAL")
        self._grid_step_mode_combo.setCurrentIndex(
            self._grid_step_mode_combo.findData(self._settings_state.grid_step_mode)
        )
        self._grid_step_mode_combo.currentIndexChanged.connect(
            lambda _: self._handle_grid_step_mode_change(self._grid_step_mode_combo.currentData())
        )

        self._grid_step_input = QDoubleSpinBox()
        self._grid_step_input.setRange(0.000001, 10.0)
        self._grid_step_input.setDecimals(8)
        self._grid_step_input.setSingleStep(0.0001)
        self._grid_step_input.setValue(self._settings_state.grid_step_pct)
        self._grid_step_input.valueChanged.connect(
            lambda value: self._update_setting("grid_step_pct", value)
        )
        self._manual_override_button = QPushButton(tr("manual_override"))
        self._manual_override_button.setFixedHeight(24)
        self._manual_override_button.clicked.connect(self._handle_manual_override)
        grid_step_row = QHBoxLayout()
        grid_step_row.addWidget(self._grid_step_input)
        grid_step_row.addWidget(self._manual_override_button)

        self._range_mode_combo = QComboBox()
        self._range_mode_combo.addItem("Авто", "Auto")
        self._range_mode_combo.addItem("Ручной", "Manual")
        self._range_mode_combo.currentIndexChanged.connect(
            lambda _: self._handle_range_mode_change(self._range_mode_combo.currentData())
        )

        self._range_low_input = QDoubleSpinBox()
        self._range_low_input.setRange(0.000001, 10.0)
        self._range_low_input.setDecimals(8)
        self._range_low_input.setSingleStep(0.0001)
        self._range_low_input.setValue(self._settings_state.range_low_pct)
        self._range_low_input.valueChanged.connect(
            lambda value: self._update_setting("range_low_pct", value)
        )

        self._range_high_input = QDoubleSpinBox()
        self._range_high_input.setRange(0.000001, 10.0)
        self._range_high_input.setDecimals(8)
        self._range_high_input.setSingleStep(0.0001)
        self._range_high_input.setValue(self._settings_state.range_high_pct)
        self._range_high_input.valueChanged.connect(
            lambda value: self._update_setting("range_high_pct", value)
        )

        self._take_profit_input = QDoubleSpinBox()
        self._take_profit_input.setRange(0.000001, 50.0)
        self._take_profit_input.setDecimals(8)
        self._take_profit_input.setSingleStep(0.0001)
        self._take_profit_input.setValue(self._settings_state.take_profit_pct)
        self._take_profit_input.valueChanged.connect(
            lambda value: self._update_setting("take_profit_pct", value)
        )
        self._auto_values_label = QLabel(tr("auto_values_line", values="—"))
        self._auto_values_label.setStyleSheet("color: #6b7280; font-size: 10px;")

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
        form.addRow(tr("grid_step_mode"), self._grid_step_mode_combo)
        form.addRow(tr("grid_step"), grid_step_row)
        form.addRow(tr("range_mode"), self._range_mode_combo)
        form.addRow(tr("range_low"), self._range_low_input)
        form.addRow(tr("range_high"), self._range_high_input)
        form.addRow(tr("take_profit"), self._take_profit_input)
        form.addRow("", self._auto_values_label)
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
        self._apply_grid_step_mode(self._settings_state.grid_step_mode)
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
                quote="—",
                base="—",
                equity="—",
                quote_asset="—",
                base_asset="—",
            )
        )
        self._balance_quote_label.setFont(fixed_font)
        self._balance_bot_label = QLabel(
            tr("runtime_bot_line", used="—", free="—", locked="—")
        )
        self._balance_bot_label.setFont(fixed_font)

        account_status = (
            tr("account_status_checking") if self._has_api_keys else tr("account_status_no_keys")
        )
        account_style = (
            "color: #6b7280; font-size: 11px; font-weight: 600;"
            if self._has_api_keys
            else "color: #dc2626; font-size: 11px; font-weight: 600;"
        )
        self._account_status_label = QLabel(account_status)
        self._account_status_label.setStyleSheet(account_style)
        self._account_status_label.setFont(fixed_font)

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
            self._record_price(update.last_price)
            self._last_price_label.setText(tr("last_price", price=f"{update.last_price:.8f}"))
            self._market_price.setText(f"{tr('price')}: {update.last_price:.8f}")
            self._set_market_label_state(self._market_price, active=True)
            self._grid_engine.on_price(update.last_price)
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
        self._refresh_unrealized_pnl()

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
        if self._balances_loaded and self._quote_asset and self._base_asset:
            quote_total = self._asset_total(self._quote_asset)
            base_total = self._asset_total(self._base_asset)
            quote_text = f"{quote_total:.2f}"
            base_text = f"{base_total:.8f}"
            if self._last_price is None:
                equity_text = "—"
            else:
                equity = quote_total + (base_total * self._last_price)
                equity_text = f"{equity:.2f}"
            used = self._open_orders_value()
            locked = used
            free = max(quote_total - used, 0.0)
            used_text = f"{used:.2f}"
            free_text = f"{free:.2f}"
            locked_text = f"{locked:.2f}"
        else:
            quote_text = "—"
            base_text = "—"
            equity_text = "—"
            used_text = "—"
            free_text = "—"
            locked_text = "—"

        quote_asset = self._quote_asset or "—"
        base_asset = self._base_asset or "—"
        self._balance_quote_label.setText(
            tr(
                "runtime_account_line",
                quote=quote_text,
                base=base_text,
                equity=equity_text,
                quote_asset=quote_asset,
                base_asset=base_asset,
            )
        )
        self._balance_bot_label.setText(
            tr(
                "runtime_bot_line",
                used=used_text,
                free=free_text,
                locked=locked_text,
            )
        )

    def _refresh_unrealized_pnl(self) -> None:
        if not self._fills and not self._base_lots:
            return
        self._update_pnl(self._estimate_unrealized_pnl(), self._realized_pnl)

    def _asset_total(self, asset: str) -> float:
        free, locked = self._balances.get(asset, (0.0, 0.0))
        return free + locked

    def _open_orders_value(self) -> float:
        return sum(self._extract_order_value(row) for row in range(self._orders_table.rowCount()))

    def _handle_range_mode_change(self, value: str) -> None:
        self._update_setting("range_mode", value)
        self._apply_range_mode(value)

    def _handle_grid_step_mode_change(self, value: str) -> None:
        self._update_setting("grid_step_mode", value)
        self._apply_grid_step_mode(value)
        self._update_grid_preview()

    def _handle_manual_override(self) -> None:
        self._grid_step_mode_combo.setCurrentIndex(self._grid_step_mode_combo.findData("MANUAL"))

    def _apply_range_mode(self, value: str) -> None:
        manual = value == "Manual"
        self._range_low_input.setEnabled(True)
        self._range_high_input.setEnabled(True)
        self._range_low_input.setReadOnly(not manual and self._settings_state.grid_step_mode == "AUTO_ATR")
        self._range_high_input.setReadOnly(not manual and self._settings_state.grid_step_mode == "AUTO_ATR")

    def _apply_grid_step_mode(self, value: str) -> None:
        auto = value == "AUTO_ATR"
        self._grid_step_input.setEnabled(True)
        self._grid_step_input.setReadOnly(auto)
        self._take_profit_input.setReadOnly(auto)
        self._manual_override_button.setEnabled(auto)
        self._grid_step_mode_combo.setToolTip(tr("grid_step_mode_tip_auto") if auto else "")
        if auto:
            self._manual_grid_step_pct = self._settings_state.grid_step_pct
            self._range_mode_combo.setCurrentIndex(self._range_mode_combo.findData("Auto"))
            self._range_mode_combo.setEnabled(False)
            self._update_setting("range_mode", "Auto")
            self._apply_range_mode("Auto")
            auto_params = self._auto_grid_params_from_history()
            if auto_params:
                self._set_grid_step_input(auto_params["grid_step_pct"], update_setting=False)
            self._take_profit_input.blockSignals(True)
            if auto_params:
                self._take_profit_input.setValue(auto_params["take_profit_pct"])
            self._take_profit_input.blockSignals(False)
        else:
            self._manual_grid_step_pct = float(self._grid_step_input.value())
            self._range_mode_combo.setEnabled(True)
            self._take_profit_input.setReadOnly(False)
        self._update_auto_values_label(auto)

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
            self._live_mode_confirmed = True
        state = "enabled" if checked else "disabled"
        self._append_log(f"Dry-run {state}.", kind="INFO")
        self._append_log(
            f"Trade enabled state changed: live={str(not checked).lower()}.",
            kind="INFO",
        )
        if checked:
            self._live_mode_confirmed = False
        self._grid_engine.set_mode("DRY_RUN" if checked else "LIVE")
        self._apply_trade_gate()
        self._update_fills_timer()

    def _handle_start(self) -> None:
        dry_run = self._dry_run_toggle.isChecked()
        self._append_log(f"Start pressed (dry-run={dry_run}).", kind="ORDERS")
        self._append_log(
            f"account.canTrade={str(self._account_can_trade).lower()} permissions={self._account_permissions}",
            kind="INFO",
        )
        if not dry_run and self._trade_gate != TradeGate.TRADE_OK:
            reason = self._trade_gate_reason()
            self._append_log(f"Start blocked: TRADE DISABLED (reason={reason}).", kind="WARN")
            dry_run = True
            self._suppress_dry_run_event = True
            self._dry_run_toggle.setChecked(True)
            self._suppress_dry_run_event = False
        self._grid_engine.set_mode("DRY_RUN" if dry_run else "LIVE")
        if not dry_run and (not self._rules_loaded or not self._fees_last_fetch_ts):
            self._append_log("Start blocked: rules/fees not loaded.", kind="WARN")
            self._refresh_exchange_rules(force=True)
            self._refresh_trade_fees(force=True)
            self._change_state("IDLE")
            return
        try:
            anchor_price = self.get_anchor_price(self._symbol)
            if anchor_price is None:
                raise ValueError("No price available.")
            settings = self._resolve_start_settings()
            self._apply_auto_clamps(settings, anchor_price)
            if not dry_run and self._first_live_session:
                settings.grid_count = min(settings.grid_count, 4)
                settings.max_active_orders = min(settings.max_active_orders, 4)
            tick = self._exchange_rules.get("tick")
            step = self._exchange_rules.get("step")
            self._append_log(
                (
                    f"[START] mode={settings.grid_step_mode} range={settings.range_mode} "
                    f"step_pct={settings.grid_step_pct:.6f} tp_pct={settings.take_profit_pct:.6f} "
                    f"tick={tick} step={step}"
                ),
                kind="INFO",
            )
            self._last_price = anchor_price
            self._last_price_label.setText(tr("last_price", price=f"{anchor_price:.8f}"))
            self._market_price.setText(f"{tr('price')}: {anchor_price:.8f}")
            self._set_market_label_state(self._market_price, active=True)
            planned = self._grid_engine.start(settings, anchor_price, self._exchange_rules)
        except ValueError as exc:
            self._append_log(f"Start failed: {exc}", kind="WARN")
            self._change_state("IDLE")
            return
        planned = planned[: settings.max_active_orders]
        buy_count = sum(1 for order in planned if order.side == "BUY")
        sell_count = sum(1 for order in planned if order.side == "SELL")
        if not dry_run and (len(planned) < 2 or buy_count < 1 or sell_count < 1):
            self._append_log(
                f"Start blocked: insufficient orders after filters (buys={buy_count}, sells={sell_count}).",
                kind="WARN",
            )
            self._change_state("IDLE")
            return
        if not dry_run:
            self._append_log(
                (
                    "[LIVE] TEST: MANUAL step_pct=0.01 tp_pct=0.10 levels=2 budget=100 "
                    "(EURIUSDT/USDCUSDT) -> expect 4 orders; after FILLED SELL expect TP BUY + restore SELL; "
                    "no -2010; Realized PnL grows on sell->buy."
                ),
                kind="INFO",
            )
        self._append_log(
            f"grid plan: buys={buy_count} sells={sell_count}",
            kind="ORDERS",
        )
        if dry_run:
            self._change_state("RUNNING")
            self._render_sim_orders(planned)
            return
        plan_stats = self._grid_engine.get_plan_stats()
        min_notional_failed = plan_stats.min_notional_failed
        min_notional_ok = len(planned)
        max_exposure = sum(order.price * order.qty for order in planned)
        quote_asset = self._quote_asset or "USDT"
        first_live_warning = ""
        if self._first_live_session:
            first_live_warning = tr("grid_confirm_first_live_warning")
        confirm = QMessageBox.question(
            self,
            tr("grid_confirm_title"),
            (
                tr(
                    "grid_confirm_message",
                    symbol=self._symbol,
                    count=str(len(planned)),
                    exposure=f"{max_exposure:.2f}",
                    min_ok=str(min_notional_ok),
                    min_failed=str(min_notional_failed),
                    quote_asset=quote_asset,
                    warning=first_live_warning,
                )
                + f"\nBudget {settings.budget:.2f} {quote_asset}\nMode LIVE"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            self._append_log("Start cancelled by user.", kind="INFO")
            self._change_state("IDLE")
            return
        self._bot_session_id = uuid4().hex[:8]
        self._bot_order_ids.clear()
        self._bot_order_keys.clear()
        self._fill_keys.clear()
        self._open_orders_map = {}
        self._fills = []
        self._base_lots.clear()
        self._seen_trade_ids.clear()
        self._realized_pnl = 0.0
        self._fees_total = 0.0
        self._closed_trades = 0
        self._win_trades = 0
        self._replacement_counter = 0
        self._update_pnl(None, None)
        self._update_trade_summary()
        self._live_settings = settings
        self._first_live_session = False
        self._change_state("PLACING_GRID")
        self._place_live_orders(planned)

    def _handle_pause(self) -> None:
        self._append_log("Pause pressed.", kind="ORDERS")
        self._grid_engine.pause()
        self._change_state("PAUSED")

    def _handle_stop(self) -> None:
        self._append_log("Stop pressed.", kind="ORDERS")
        self._grid_engine.stop(cancel_all=True)
        self._change_state("IDLE")
        if self._dry_run_toggle.isChecked():
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle(tr("stop_confirm_title"))
        dialog.setText(tr("stop_confirm_message"))
        leave_button = dialog.addButton(tr("stop_leave_orders"), QMessageBox.RejectRole)
        cancel_button = dialog.addButton(tr("stop_cancel_orders"), QMessageBox.AcceptRole)
        dialog.exec()
        if dialog.clickedButton() == leave_button:
            self._append_log("Stop: left bot orders open.", kind="INFO")
            return
        if dialog.clickedButton() == cancel_button:
            self._cancel_bot_orders()

    def _handle_cancel_selected(self) -> None:
        selected_rows = sorted({index.row() for index in self._orders_table.selectionModel().selectedRows()})
        if not selected_rows:
            self._append_log("Cancel selected: —", kind="ORDERS")
            return
        if self._dry_run_toggle.isChecked():
            for row in reversed(selected_rows):
                order_id = self._order_id_for_row(row)
                self._orders_table.removeRow(row)
                self._append_log(f"Cancel selected: {order_id}", kind="ORDERS")
            self._refresh_orders_metrics()
            return
        if not self._account_client:
            self._append_log("Cancel selected: no account client.", kind="WARN")
            return
        order_ids = [self._order_id_for_row(row) for row in selected_rows]
        self._cancel_live_orders(order_ids)

    def _cancel_bot_orders(self) -> None:
        if not self._account_client:
            self._append_log("Cancel bot orders: no account client.", kind="WARN")
            return
        order_ids = [str(order.get("orderId", "")) for order in self._open_orders]
        order_ids = [order_id for order_id in order_ids if order_id and order_id != "—"]
        if not order_ids:
            self._append_log("Cancel bot orders: 0", kind="ORDERS")
            return
        self._cancel_live_orders(order_ids)

    def _handle_cancel_all(self) -> None:
        count = self._orders_table.rowCount()
        if count == 0:
            self._append_log("Cancel all: 0", kind="ORDERS")
            return
        if self._dry_run_toggle.isChecked():
            self._orders_table.setRowCount(0)
            self._append_log(f"Cancel all: {count}", kind="ORDERS")
            self._refresh_orders_metrics()
            return
        if not self._account_client:
            self._append_log("Cancel all: no account client.", kind="WARN")
            return
        self._cancel_bot_orders()

    def _handle_refresh(self) -> None:
        self._append_log("Manual refresh requested.", kind="INFO")
        self._refresh_balances(force=True)
        self._refresh_open_orders(force=True)
        self._refresh_exchange_rules(force=True)
        self._refresh_trade_fees(force=True)

    def _change_state(self, new_state: str) -> None:
        self._state = new_state
        self._state_badge.setText(f"{tr('state')}: {self._state}")
        self._engine_state = self._engine_state_from_status(new_state)
        self._engine_state_label.setText(f"{tr('engine')}: {self._engine_state}")
        self._apply_engine_state_style(self._engine_state)
        self._update_orders_timer_interval()
        self._update_fills_timer()

    def _update_setting(self, key: str, value: Any) -> None:
        if hasattr(self._settings_state, key):
            setattr(self._settings_state, key, value)
        if key == "grid_step_pct" and self._settings_state.grid_step_mode == "MANUAL":
            self._manual_grid_step_pct = float(value)
        if key == "budget":
            self._refresh_orders_metrics()
        self._update_grid_preview()

    def _set_grid_step_input(self, value: float, update_setting: bool) -> None:
        self._grid_step_input.blockSignals(True)
        self._grid_step_input.setValue(value)
        self._grid_step_input.blockSignals(False)
        if update_setting:
            self._update_setting("grid_step_pct", value)

    def _record_price(self, price: float) -> None:
        if price <= 0:
            return
        self._price_history.append(price)
        if len(self._price_history) > 500:
            self._price_history = self._price_history[-500:]
        if self._settings_state.grid_step_mode == "AUTO_ATR":
            auto_params = self._auto_grid_params_from_history()
            if auto_params:
                self._set_grid_step_input(auto_params["grid_step_pct"], update_setting=False)
            self._update_grid_preview()
            self._update_auto_values_label(True)

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(value, high))

    def _auto_grid_params_from_history(self) -> dict[str, float] | None:
        if len(self._price_history) < 2:
            return None
        prices = self._price_history[-300:]
        return self._compute_auto_grid_params(prices)

    def _auto_grid_params_from_http(self) -> dict[str, float] | None:
        try:
            klines = self._http_client.get_klines(self._symbol, interval="1h", limit=120)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"Auto ATR fallback failed: {exc}", kind="WARN")
            return None
        closes: list[float] = []
        for entry in klines:
            if not isinstance(entry, list) or len(entry) < 5:
                continue
            close_raw = entry[4]
            try:
                close = float(close_raw)
            except (TypeError, ValueError):
                continue
            if close > 0:
                closes.append(close)
        return self._compute_auto_grid_params(closes)

    def _compute_auto_grid_params(self, prices: list[float]) -> dict[str, float] | None:
        if len(prices) < 2:
            return None
        returns: list[float] = []
        for idx in range(1, len(prices)):
            prev = prices[idx - 1]
            if prev <= 0:
                continue
            returns.append(abs(prices[idx] / prev - 1))
        if not returns:
            return None
        avg_abs_return = sum(returns) / len(returns)
        grid_step_pct = self._clamp(avg_abs_return * 1.5 * 100, 0.05, 0.8)
        range_pct = self._clamp(grid_step_pct * self._settings_state.grid_count / 2, 0.5, 6.0)
        fee_candidates = [fee for fee in self._trade_fees if fee is not None]
        fee_pct = max(fee_candidates) * 100 if fee_candidates else 0.0
        tp_pct = max(grid_step_pct * 1.1, fee_pct * 2 + 0.01)
        return {
            "grid_step_pct": grid_step_pct,
            "range_pct": range_pct,
            "take_profit_pct": tp_pct,
        }

    def _update_auto_values_label(self, auto: bool) -> None:
        if not hasattr(self, "_auto_values_label"):
            return
        if not auto:
            self._auto_values_label.setText(tr("auto_values_line", values="—"))
            return
        auto_params = self._auto_grid_params_from_history()
        if not auto_params:
            self._auto_values_label.setText(tr("auto_values_line", values="—"))
            return
        values = (
            f"step {auto_params['grid_step_pct']:.2f}% | "
            f"range {auto_params['range_pct']:.2f}% | "
            f"tp {auto_params['take_profit_pct']:.2f}%"
        )
        self._auto_values_label.setText(tr("auto_values_line", values=values))

    def _resolve_start_settings(self) -> GridSettingsState:
        settings = GridSettingsState(**asdict(self._settings_state))
        if settings.grid_step_mode != "AUTO_ATR":
            return settings
        auto_params = self._auto_grid_params_from_history()
        if not auto_params:
            auto_params = self._auto_grid_params_from_http()
        if not auto_params:
            self._append_log("Auto ATR unavailable: fallback to manual grid values.", kind="WARN")
            return settings
        settings.grid_step_pct = auto_params["grid_step_pct"]
        settings.range_low_pct = auto_params["range_pct"]
        settings.range_high_pct = auto_params["range_pct"]
        settings.range_mode = "Auto"
        settings.take_profit_pct = auto_params["take_profit_pct"]
        return settings

    def get_anchor_price(self, symbol: str) -> float | None:
        snapshot = self._price_feed_manager.get_snapshot(symbol)
        ttl_ms = self._config.prices.ttl_ms
        if snapshot and snapshot.last_price is not None:
            age_ms = snapshot.price_age_ms or 0
            if snapshot.price_age_ms is None or age_ms <= ttl_ms:
                self._append_log(f"PRICE: WS age={age_ms}ms", kind="INFO")
                return snapshot.last_price
        try:
            book = self._http_client.get_book_ticker(symbol)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"PRICE: HTTP_BOOK failed ({exc})", kind="WARN")
            book = {}
        bid = self._coerce_float(str(book.get("bidPrice", ""))) if isinstance(book, dict) else None
        ask = self._coerce_float(str(book.get("askPrice", ""))) if isinstance(book, dict) else None
        if bid and ask:
            anchor = (bid + ask) / 2
            self._append_log("PRICE: HTTP_BOOK age=0ms", kind="INFO")
            return anchor
        try:
            price_raw = self._http_client.get_ticker_price(symbol)
            anchor = float(price_raw)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"PRICE: HTTP_LAST failed ({exc})", kind="WARN")
            return None
        self._append_log("PRICE: HTTP_LAST age=0ms", kind="INFO")
        return anchor

    def _apply_auto_clamps(self, settings: GridSettingsState, anchor_price: float) -> None:
        if settings.grid_step_mode != "AUTO_ATR":
            return
        tick = self._exchange_rules.get("tick")
        if tick and anchor_price > 0:
            min_step_pct = (tick / anchor_price) * 100
            if settings.grid_step_pct < min_step_pct:
                settings.grid_step_pct = min_step_pct
        if settings.take_profit_pct < settings.grid_step_pct:
            settings.take_profit_pct = settings.grid_step_pct

    def _reset_defaults(self) -> None:
        defaults = GridSettingsState()
        self._settings_state = defaults
        self._manual_grid_step_pct = defaults.grid_step_pct
        self._budget_input.setValue(defaults.budget)
        self._direction_combo.setCurrentIndex(
            self._direction_combo.findData(defaults.direction)
        )
        self._grid_count_input.setValue(defaults.grid_count)
        self._grid_step_mode_combo.setCurrentIndex(
            self._grid_step_mode_combo.findData(defaults.grid_step_mode)
        )
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
        self._apply_grid_step_mode(defaults.grid_step_mode)
        self._update_grid_preview()

    def _refresh_balances(self, force: bool = False) -> None:
        self._balances_tick_count += 1
        if self._balances_tick_count % 30 == 0:
            self._logger.debug("balances refresh tick")
        if not self._account_client:
            self._balances_loaded = False
            self._set_account_status("no_keys")
            self._apply_trade_gate()
            self._update_runtime_balances()
            return
        if self._balances_in_flight and not force:
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
        self._balances_loaded = True
        self._set_account_status("ready")
        self._account_api_error = False
        status = self._account_client.get_account_status(result) if self._account_client else AccountStatus(False, [], None)
        self._account_can_trade = status.can_trade
        self._account_permissions = status.permissions
        snapshot = (self._account_can_trade, tuple(self._account_permissions))
        if snapshot != self._last_account_trade_snapshot:
            self._append_log(
                f"account.canTrade={str(self._account_can_trade).lower()} permissions={self._account_permissions}",
                kind="INFO",
            )
            self._last_account_trade_snapshot = snapshot
        self._apply_trade_fees_from_account(result)
        self._apply_trade_gate()
        self._update_runtime_balances()
        self._grid_engine.sync_balances(self._balances)
        quote_asset = self._quote_asset or "—"
        base_asset = self._base_asset or "—"
        quote_total = self._asset_total(quote_asset)
        base_total = self._asset_total(base_asset)
        snapshot = (round(quote_total, 2), round(base_total, 8))
        if snapshot != self._balances_snapshot:
            self._append_log(
                f"balances updated: {quote_asset}={quote_total:.2f}, {base_asset}={base_total:.8f}",
                kind="INFO",
            )
            self._balances_snapshot = snapshot
        self._refresh_trade_fees()

    def _handle_account_error(self, message: str) -> None:
        self._balances_in_flight = False
        self._account_can_trade = False
        self._balances_loaded = False
        self._account_api_error = True
        self._account_permissions = []
        self._last_account_trade_snapshot = None
        self._set_account_status(self._infer_account_status(message))
        self._append_log(f"balances fetch failed: {message}", kind="WARN")
        self._auto_pause_on_api_error(message)
        self._auto_pause_on_exception(message)
        self._apply_trade_gate()
        self._update_runtime_balances()

    def _refresh_open_orders(self, force: bool = False) -> None:
        self._orders_tick_count += 1
        if self._orders_tick_count % 30 == 0:
            self._logger.debug("orders refresh tick")
        if not self._account_client:
            self._set_account_status("no_keys")
            self._open_orders_all = []
            self._open_orders = []
            self._open_orders_map = {}
            self._bot_order_keys = set()
            self._render_open_orders()
            self._apply_trade_gate()
            return
        if self._orders_in_flight and not force:
            return
        self._orders_in_flight = True
        worker = _Worker(lambda: self._account_client.get_open_orders(self._symbol))
        worker.signals.success.connect(self._handle_open_orders)
        worker.signals.error.connect(self._handle_open_orders_error)
        self._thread_pool.start(worker)

    def _refresh_fills(self) -> None:
        if self._dry_run_toggle.isChecked():
            return
        if self._state != "RUNNING":
            return
        if not self._account_client:
            return
        if self._fills_in_flight:
            return
        self._fills_in_flight = True
        worker = _Worker(lambda: self._account_client.get_my_trades(self._symbol, limit=50))
        worker.signals.success.connect(self._handle_fill_poll)
        worker.signals.error.connect(self._handle_fill_poll_error)
        self._thread_pool.start(worker)

    def _handle_fill_poll(self, result: object, latency_ms: int) -> None:
        self._fills_in_flight = False
        if not isinstance(result, list):
            self._handle_fill_poll_error("Unexpected trades response")
            return
        for trade in result:
            if not isinstance(trade, dict):
                continue
            trade_id = str(trade.get("id", ""))
            order_id = str(trade.get("orderId", ""))
            if not trade_id or trade_id in self._seen_trade_ids:
                continue
            if not order_id:
                continue
            if order_id and order_id not in self._bot_order_ids:
                continue
            self._seen_trade_ids.add(trade_id)
            is_buyer = trade.get("isBuyer")
            side = "BUY" if is_buyer else "SELL"
            order_stub = {"side": side, "orderId": order_id}
            fill = self._build_trade_fill(order_stub, trade)
            if fill:
                self._process_fill(fill)

    def _handle_fill_poll_error(self, message: str) -> None:
        self._fills_in_flight = False
        self._append_log(f"fills poll failed: {message}", kind="WARN")
        self._auto_pause_on_api_error(message)
        self._auto_pause_on_exception(message)

    def _update_orders_timer_interval(self) -> None:
        if not hasattr(self, "_orders_timer"):
            return
        slow_poll = not self._open_orders
        interval = 10_000 if slow_poll else 3_000
        if self._orders_timer.interval() != interval:
            self._orders_timer.setInterval(interval)

    def _update_fills_timer(self) -> None:
        if not hasattr(self, "_fills_timer"):
            return
        should_run = (
            self._account_client is not None
            and not self._dry_run_toggle.isChecked()
            and self._state == "RUNNING"
        )
        if should_run and not self._fills_timer.isActive():
            self._fills_timer.start()
        if not should_run and self._fills_timer.isActive():
            self._fills_timer.stop()

    def _handle_open_orders(self, result: object, latency_ms: int) -> None:
        self._orders_in_flight = False
        if not isinstance(result, list):
            self._handle_open_orders_error("Unexpected open orders response")
            return
        previous_map = dict(self._open_orders_map)
        self._open_orders_all = [item for item in result if isinstance(item, dict)]
        self._open_orders = self._filter_bot_orders(self._open_orders_all)
        self._open_orders_map = {
            str(order.get("orderId", "")): order
            for order in self._open_orders
            if str(order.get("orderId", ""))
        }
        self._bot_order_keys = {
            key
            for order in self._open_orders
            if (key := self._order_key_from_order(order)) is not None
        }
        for order in self._open_orders:
            order_id = str(order.get("orderId", ""))
            if order_id:
                self._bot_order_ids.add(order_id)
        self._render_open_orders()
        self._grid_engine.sync_open_orders(self._open_orders)
        missing = [
            order
            for order_id, order in previous_map.items()
            if order_id not in self._open_orders_map and self._is_bot_order(order)
        ]
        if missing and self._account_client and self._state in {"RUNNING", "WAITING_FILLS"}:
            self._queue_reconcile_missing_orders(missing[:3])
        count = len(self._open_orders)
        if self._orders_last_count is None or count != self._orders_last_count:
            self._append_log(
                f"open orders updated (n={count}).",
                kind="INFO",
            )
            self._orders_last_count = count
        self._update_orders_timer_interval()

    def _handle_open_orders_error(self, message: str) -> None:
        self._orders_in_flight = False
        if self._is_auth_error(message):
            self._account_api_error = True
        self._append_log(f"openOrders fetch failed: {message}", kind="WARN")
        self._auto_pause_on_api_error(message)
        self._auto_pause_on_exception(message)
        self._apply_trade_gate()

    def _filter_bot_orders(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for order in orders:
            if self._is_bot_order(order):
                filtered.append(order)
        return filtered

    def _is_bot_order(self, order: dict[str, Any]) -> bool:
        prefix = self._bot_order_prefix()
        client_order_id = str(order.get("clientOrderId", ""))
        return bool(prefix and client_order_id.startswith(prefix))

    def _bot_order_prefix(self) -> str:
        if not self._bot_session_id:
            return ""
        return f"BBOT_LITE_{self._symbol}_{self._bot_session_id}_"

    def _order_key(self, side: str, price: Decimal, qty: Decimal) -> str:
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        price_key = self._format_decimal(self.q_price(price, tick), tick)
        qty_key = self._format_decimal(self.q_qty(qty, step), step)
        return f"{self._symbol}|{side}|{price_key}|{qty_key}"

    def _order_key_from_order(self, order: dict[str, Any]) -> str | None:
        side = str(order.get("side", "")).upper()
        if not side:
            return None
        price = self._coerce_float(str(order.get("price", ""))) or 0.0
        qty = self._coerce_float(str(order.get("origQty", ""))) or 0.0
        if price <= 0 or qty <= 0:
            return None
        return self._order_key(side, self.as_decimal(price), self.as_decimal(qty))

    @staticmethod
    def _limit_client_order_id(client_id: str) -> str:
        return client_id[:36]

    def _make_client_order_id(self, role: str, idx: int, suffix: str = "") -> str:
        prefix = self._bot_order_prefix()
        return self._limit_client_order_id(f"{prefix}{role}_{idx}{suffix}")

    def _next_client_order_id(self, role: str, suffix: str = "") -> str:
        self._replacement_counter += 1
        return self._make_client_order_id(role, self._replacement_counter, suffix)

    def _queue_reconcile_missing_orders(self, missing: list[dict[str, Any]]) -> None:
        if not self._account_client:
            return

        def _reconcile() -> dict[str, Any]:
            trades = self._account_client.get_my_trades(self._symbol, limit=50)
            trades_by_order: dict[str, dict[str, Any]] = {}
            if isinstance(trades, list):
                for trade in trades:
                    if not isinstance(trade, dict):
                        continue
                    order_id = str(trade.get("orderId", ""))
                    if order_id:
                        trades_by_order[order_id] = trade
            results: list[dict[str, Any]] = []
            for order in missing:
                order_id = str(order.get("orderId", ""))
                client_order_id = str(order.get("clientOrderId", ""))
                if not order_id and not client_order_id:
                    continue
                if not order_id:
                    order_id = ""
                trade = trades_by_order.get(order_id)
                if trade:
                    results.append({"status": "FILLED", "order": order, "trade": trade})
                    continue
                if client_order_id:
                    order_info = self._account_client.get_order(
                        self._symbol,
                        orig_client_order_id=client_order_id,
                    )
                else:
                    order_info = self._account_client.get_order(self._symbol, order_id or None)
                status = str(order_info.get("status", "")).upper() if isinstance(order_info, dict) else "UNKNOWN"
                results.append({"status": status, "order": order, "trade": None})
            return {"results": results}

        worker = _Worker(_reconcile)
        worker.signals.success.connect(self._handle_reconcile_missing_orders)
        worker.signals.error.connect(self._handle_reconcile_error)
        self._thread_pool.start(worker)

    def _handle_reconcile_missing_orders(self, result: object, latency_ms: int) -> None:
        if not isinstance(result, dict):
            self._handle_reconcile_error("Unexpected reconcile response")
            return
        results = result.get("results", [])
        if not isinstance(results, list):
            return
        for entry in results:
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status", "")).upper()
            order = entry.get("order")
            trade = entry.get("trade")
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("orderId", ""))
            if status == "FILLED":
                fill = self._build_trade_fill(order, trade)
                if fill:
                    self._process_fill(fill)
                continue
            if status in {"CANCELED", "EXPIRED", "REJECTED"}:
                self._append_log(
                    f"[LIVE] order closed orderId={order_id} status={status}",
                    kind="ORDERS",
                )
                continue
            if order_id:
                self._append_log(
                    f"[LIVE] order disappeared orderId={order_id} status={status}",
                    kind="WARN",
                )

    def _handle_reconcile_error(self, message: str) -> None:
        self._append_log(f"Reconcile failed: {message}", kind="WARN")

    def _build_trade_fill(
        self,
        order: dict[str, Any],
        trade: dict[str, Any] | None,
    ) -> TradeFill | None:
        if trade:
            price = self._coerce_float(str(trade.get("price", ""))) or 0.0
            qty = self._coerce_float(str(trade.get("qty", ""))) or 0.0
            quote_qty = self._coerce_float(str(trade.get("quoteQty", ""))) or price * qty
            commission = self._coerce_float(str(trade.get("commission", ""))) or 0.0
            commission_asset = str(trade.get("commissionAsset", "")).upper()
            time_ms = int(trade.get("time", 0) or 0)
            trade_id = str(trade.get("id", "")) if trade.get("id") is not None else ""
        else:
            price = self._coerce_float(str(order.get("price", ""))) or 0.0
            qty = self._coerce_float(str(order.get("executedQty", ""))) or 0.0
            quote_qty = price * qty
            commission = 0.0
            commission_asset = ""
            time_ms = int(order.get("updateTime", 0) or order.get("time", 0) or 0)
            trade_id = ""
        if price <= 0 or qty <= 0:
            return None
        side = str(order.get("side", "")).upper()
        return TradeFill(
            side=side,
            price=price,
            qty=qty,
            quote_qty=quote_qty,
            commission=commission,
            commission_asset=commission_asset,
            time_ms=time_ms,
            order_id=str(order.get("orderId", "")),
            trade_id=trade_id,
        )

    def _process_fill(self, fill: TradeFill) -> None:
        fill_key = self._fill_key(fill)
        if fill_key in self._fill_keys:
            return
        self._fill_keys.add(fill_key)
        if fill.trade_id:
            self._seen_trade_ids.add(fill.trade_id)
        self._record_fill(fill)
        self._bot_order_keys.discard(
            self._order_key(fill.side, self.as_decimal(fill.price), self.as_decimal(fill.qty))
        )
        self._append_log(
            (
                f"[LIVE] FILLED orderId={fill.order_id} side={fill.side}"
                f" price={fill.price:.8f} qty={fill.qty:.8f}"
            ),
            kind="ORDERS",
        )
        self._place_replacement_order(fill)

    def _fill_key(self, fill: TradeFill) -> str:
        if fill.trade_id:
            return fill.trade_id
        base = f"{fill.side}:{fill.price:.8f}:{fill.qty:.8f}:{fill.time_ms}"
        return f"{fill.order_id}:{base}" if fill.order_id else base

    def _estimate_fee_usdt(self, fill: TradeFill) -> float:
        if fill.commission <= 0:
            return 0.0
        if fill.commission_asset == self._quote_asset:
            return fill.commission
        if fill.commission_asset == self._base_asset:
            return fill.commission * fill.price
        return 0.0

    def _record_fill(self, fill: TradeFill) -> None:
        self._fills.append(fill)
        realized_delta = 0.0
        if fill.side == "BUY":
            self._base_lots.append(BaseLot(qty=fill.qty, cost_per_unit=fill.price))
        else:
            remaining = fill.qty
            while remaining > 0 and self._base_lots:
                lot = self._base_lots[0]
                take_qty = min(lot.qty, remaining)
                realized_delta += take_qty * (fill.price - lot.cost_per_unit)
                lot.qty -= take_qty
                remaining -= take_qty
                if lot.qty <= 0:
                    self._base_lots.popleft()
            self._closed_trades += 1
            if realized_delta > 0:
                self._win_trades += 1
        fee_usdt = self._estimate_fee_usdt(fill)
        if fee_usdt:
            self._fees_total += fee_usdt
            realized_delta -= fee_usdt
        self._realized_pnl += realized_delta
        self._update_pnl(self._estimate_unrealized_pnl(), self._realized_pnl)
        self._update_trade_summary()

    def _estimate_unrealized_pnl(self) -> float | None:
        if self._last_price is None:
            return None
        if not self._base_lots:
            return 0.0
        total_qty = sum(lot.qty for lot in self._base_lots)
        if total_qty <= 0:
            return 0.0
        total_cost = sum(lot.qty * lot.cost_per_unit for lot in self._base_lots)
        avg_cost = total_cost / total_qty
        return (self._last_price - avg_cost) * total_qty

    def _update_trade_summary(self) -> None:
        if self._closed_trades > 0:
            win_pct = self._win_trades / self._closed_trades * 100
            avg = self._realized_pnl / self._closed_trades
            win_text = f"{win_pct:.0f}%"
            avg_text = f"{avg:.2f}"
        else:
            win_text = "—"
            avg_text = "—"
        self._trades_summary_label.setText(
            tr(
                "trades_summary",
                closed=str(self._closed_trades),
                win=win_text,
                avg=avg_text,
                realized=f"{self._realized_pnl:.2f}",
                fees=f"{self._fees_total:.2f}",
            )
        )

    def _place_replacement_order(self, fill: TradeFill) -> None:
        if not self._account_client or not self._bot_session_id:
            return
        if self._state not in {"RUNNING", "WAITING_FILLS"}:
            return
        settings = self._live_settings or self._settings_state
        tp_pct = settings.take_profit_pct or settings.grid_step_pct
        if tp_pct <= 0:
            return
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        tp_side = "SELL" if fill.side == "BUY" else "BUY"
        tp_dec = self.as_decimal(tp_pct) / Decimal("100")
        fill_price = self.as_decimal(fill.price)
        fill_qty = self.as_decimal(fill.qty)
        raw_tp_price = fill_price * (Decimal("1") + tp_dec) if tp_side == "SELL" else fill_price * (Decimal("1") - tp_dec)
        tp_price = self.q_price(raw_tp_price, tick)
        tp_qty = self.q_qty(fill_qty, step)
        if tp_price <= 0 or tp_qty <= 0:
            self._append_log(
                f"[LIVE] TP skipped: invalid side={tp_side} price={tp_price} qty={tp_qty}",
                kind="WARN",
            )
            return
        existing_tp = self.find_matching_order(tp_side, tp_price, tp_qty, tolerance_ticks=0)
        existing_qty = Decimal("0")
        existing_order_id = ""
        if existing_tp:
            existing_order_id = str(existing_tp.get("orderId", ""))
            existing_qty = self.q_qty(
                self.as_decimal(self._coerce_float(str(existing_tp.get("origQty", ""))) or 0.0),
                step,
            )
            merged_qty = self.q_qty(existing_qty + tp_qty, step)
            self._append_log(
                (
                    "[LIVE] MERGE TP "
                    f"price={self.fmt_price(tp_price, tick)} old_qty={self.fmt_qty(existing_qty, step)} "
                    f"add={self.fmt_qty(tp_qty, step)} new={self.fmt_qty(merged_qty, step)}"
                ),
                kind="ORDERS",
            )
        else:
            merged_qty = tp_qty

        step_pct = settings.grid_step_pct or 0.0
        reference_price = self._last_price or fill.price
        step_abs = self.q_price(
            self.as_decimal(reference_price) * self.as_decimal(step_pct) / Decimal("100"),
            tick,
        )
        restore_side = fill.side
        if restore_side == "BUY":
            restore_price = self.q_price(fill_price - step_abs, tick)
        else:
            restore_price = self.q_price(fill_price + step_abs, tick)
        restore_qty = self.q_qty(fill_qty, step)

        tp_client_order_id = self._next_client_order_id("TP", suffix="M" if existing_tp else "")
        restore_client_order_id = self._next_client_order_id("GRID") if restore_price > 0 else ""

        def _place() -> dict[str, Any]:
            results: dict[str, Any] = {"tp": None, "restore": None, "errors": []}
            if existing_tp and existing_order_id:
                try:
                    self._account_client.cancel_order(self._symbol, existing_order_id)
                except Exception as exc:  # noqa: BLE001
                    results["errors"].append(self._format_cancel_exception(exc, existing_order_id))
                self._sleep_ms(75)
            tp_response, tp_error = self._place_limit(
                tp_side,
                tp_price,
                merged_qty,
                tp_client_order_id,
                reason="tp_merge" if existing_tp else "tp",
            )
            if tp_response:
                results["tp"] = tp_response
            if tp_error:
                results["errors"].append(tp_error)
            self._sleep_ms(75)
            if restore_price > 0 and restore_qty > 0 and restore_client_order_id:
                restore_response, restore_error = self._place_limit(
                    restore_side,
                    restore_price,
                    restore_qty,
                    restore_client_order_id,
                    reason="restore",
                )
                if restore_response:
                    results["restore"] = restore_response
                if restore_error:
                    results["errors"].append(restore_error)
            return results

        worker = _Worker(_place)
        worker.signals.success.connect(self._handle_replacement_order)
        worker.signals.error.connect(self._handle_live_order_error)
        self._thread_pool.start(worker)

    def _handle_replacement_order(self, result: object, latency_ms: int) -> None:
        if not isinstance(result, dict):
            return
        errors = result.get("errors", [])
        if isinstance(errors, list):
            for message in errors:
                if not message:
                    continue
                self._append_log(str(message), kind="ERROR")
                self._auto_pause_on_api_error(str(message))
                self._auto_pause_on_exception(str(message))
        for key, label in (("tp", "TP"), ("restore", "RESTORE")):
            entry = result.get(key)
            if not isinstance(entry, dict):
                continue
            order_id = str(entry.get("orderId", ""))
            if order_id:
                self._bot_order_ids.add(order_id)
            side = str(entry.get("side", "")).upper()
            price = self._coerce_float(str(entry.get("price", ""))) or 0.0
            qty = self._coerce_float(str(entry.get("origQty", ""))) or 0.0
            if side in {"BUY", "SELL"} and price > 0 and qty > 0:
                self._bot_order_keys.add(
                    self._order_key(side, self.as_decimal(price), self.as_decimal(qty))
                )
            self._append_log(f"[LIVE] PLACE {label} orderId={order_id}", kind="ORDERS")
        self._refresh_open_orders(force=True)

    def _place_limit(
        self,
        side: str,
        price: Decimal,
        qty: Decimal,
        client_id: str,
        reason: str,
        *,
        ignore_order_id: str | None = None,
        ignore_keys: set[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not self._account_client:
            return None, "[LIVE] place skipped: no account client"
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        min_notional = self._rule_decimal(self._exchange_rules.get("min_notional"))
        min_qty = self._rule_decimal(self._exchange_rules.get("min_qty"))
        max_qty = self._rule_decimal(self._exchange_rules.get("max_qty"))
        price = self.q_price(price, tick)
        qty = self.q_qty(qty, step)
        if min_qty is not None and qty < min_qty:
            self._signals.log_append.emit(
                (
                    "[LIVE] place skipped: minQty "
                    f"side={side} price={self.fmt_price(price, tick)} qty={self.fmt_qty(qty, step)} "
                    f"minQty={self.fmt_qty(min_qty, step)}"
                ),
                "WARN",
            )
            return None, None
        if max_qty is not None and qty > max_qty:
            qty = self.q_qty(max_qty, step)
        notional = price * qty
        if min_notional is not None and price > 0 and notional < min_notional:
            target_qty = self.ceil_to_step(min_notional / price, step)
            qty = target_qty
            if max_qty is not None and qty > max_qty:
                qty = self.q_qty(max_qty, step)
            notional = price * qty
            if (min_qty is not None and qty < min_qty) or notional < min_notional:
                self._signals.log_append.emit(
                    (
                        "[LIVE] place skipped: minNotional"
                        f" side={side} price={self.fmt_price(price, tick)} qty={self.fmt_qty(qty, step)} "
                        f"notional={self.fmt_price(notional, None)} minNotional={self.fmt_price(min_notional, None)}"
                    ),
                    "WARN",
                )
                return None, None
        if price <= 0 or qty <= 0:
            self._signals.log_append.emit(
                (
                    "[LIVE] place skipped: invalid "
                    f"side={side} price={self.fmt_price(price, tick)} qty={self.fmt_qty(qty, step)}"
                ),
                "WARN",
            )
            return None, None
        key = self._order_key(side, price, qty)
        if self._has_duplicate_order(
            side,
            price,
            qty,
            tolerance_ticks=0,
            ignore_order_id=ignore_order_id,
            ignore_keys=ignore_keys,
        ):
            self._signals.log_append.emit(
                (
                    f"[LIVE] SKIP duplicate key {key} "
                    f"side={side} price={self.fmt_price(price, tick)} qty={self.fmt_qty(qty, step)}"
                ),
                "WARN",
            )
            return None, None
        log_message = (
            f"[LIVE] place {reason} side={side} price={self.fmt_price(price, tick)} qty={self.fmt_qty(qty, step)} "
            f"notional={self.fmt_price(notional, None)} tick={self._format_rule(tick)} "
            f"step={self._format_rule(step)} minNotional={self._format_rule(min_notional)}"
        )
        self._signals.log_append.emit(log_message, "ORDERS")
        try:
            response = self._account_client.place_limit_order(
                symbol=self._symbol,
                side=side,
                price=self.fmt_price(price, tick),
                quantity=self.fmt_qty(qty, step),
                time_in_force="GTC",
                new_client_order_id=client_id,
            )
        except Exception as exc:  # noqa: BLE001
            status, code, message, response_body = self._parse_binance_exception(exc)
            if code == -2010:
                self._signals.log_append.emit(
                    (
                        "[LIVE] duplicate order skipped "
                        f"status={status} code={code} msg={message} response={response_body}"
                    ),
                    "WARN",
                )
                return None, None
            message = self._format_binance_exception(
                exc,
                context=f"place {reason}",
                side=side,
                price=price,
                qty=qty,
                notional=notional,
            )
            return None, message
        return response, None

    @staticmethod
    def _decimal_to_str(value: Decimal) -> str:
        return format(value, "f")

    @staticmethod
    def _decimal_places(value: Decimal) -> int:
        if value == 0:
            return 0
        exponent = value.normalize().as_tuple().exponent
        return max(-exponent, 0)

    def _format_decimal(self, value: Decimal, step: Decimal | None) -> str:
        if step is None or step <= 0:
            return format(value, "f")
        decimals = self._decimal_places(step)
        quant = Decimal("1").scaleb(-decimals)
        return format(value.quantize(quant), "f")

    def q_price(self, price: Decimal, tick: Decimal | None) -> Decimal:
        if tick is None or tick <= 0:
            return price
        return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick

    def q_qty(self, qty: Decimal, step: Decimal | None) -> Decimal:
        if step is None or step <= 0:
            return qty
        return (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step

    def fmt_price(self, price: Decimal, tick: Decimal | None) -> str:
        return self._format_decimal(price, tick)

    def fmt_qty(self, qty: Decimal, step: Decimal | None) -> str:
        return self._format_decimal(qty, step)

    @staticmethod
    def _rule_decimal(value: float | None) -> Decimal | None:
        if value is None:
            return None
        return LiteGridWindow.as_decimal(value)

    @staticmethod
    def as_decimal(value: float | str | Decimal) -> Decimal:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @staticmethod
    def floor_to_step(value: Decimal, step: Decimal | None) -> Decimal:
        if step is None or step <= 0:
            return value
        return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step

    @staticmethod
    def floor_to_tick(price: Decimal, tick: Decimal | None) -> Decimal:
        if tick is None or tick <= 0:
            return price
        return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick

    @staticmethod
    def ceil_to_step(value: Decimal, step: Decimal | None) -> Decimal:
        if step is None or step <= 0:
            return value
        return (value / step).to_integral_value(rounding=ROUND_CEILING) * step

    @staticmethod
    def _format_rule(value: Decimal | None) -> str:
        if value is None:
            return "—"
        return format(value, "f")

    def _format_binance_exception(
        self,
        exc: Exception,
        context: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        notional: Decimal,
    ) -> str:
        status, code, message, response_body = self._parse_binance_exception(exc)
        return (
            f"[LIVE] {context} failed status={status} code={code} msg={message} response={response_body} "
            f"side={side} price={price} qty={qty} notional={notional}"
        )

    @staticmethod
    def _parse_binance_exception(exc: Exception) -> tuple[int | None, int | None, str, str | dict[str, Any] | None]:
        message = str(exc)
        status = None
        code = None
        response_body: str | dict[str, Any] | None = None
        response = getattr(exc, "response", None)
        if response is not None:
            status = response.status_code
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                payload = None
            if isinstance(payload, dict):
                code = payload.get("code")
                msg = payload.get("msg")
                if msg:
                    message = str(msg)
                response_body = payload
            else:
                response_body = response.text
        return status, code, message, response_body

    def _format_cancel_exception(self, exc: Exception, order_id: str) -> str:
        status, code, message, response_body = self._parse_binance_exception(exc)
        return (
            "[LIVE] cancel failed "
            f"orderId={order_id} status={status} code={code} msg={message} response={response_body}"
        )

    def find_matching_order(
        self,
        side: str,
        price: Decimal,
        qty: Decimal,
        tolerance_ticks: int = 0,
    ) -> dict[str, Any] | None:
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        target_price = self.q_price(price, tick)
        target_qty = self.q_qty(qty, step)
        tolerance = (tick or Decimal("0")) * Decimal(tolerance_ticks)
        fallback_match: dict[str, Any] | None = None
        for order in self._open_orders:
            if str(order.get("side", "")).upper() != side:
                continue
            order_price = self._coerce_float(str(order.get("price", ""))) or 0.0
            order_qty = self._coerce_float(str(order.get("origQty", ""))) or 0.0
            order_price_dec = self.q_price(self.as_decimal(order_price), tick)
            order_qty_dec = self.q_qty(self.as_decimal(order_qty), step)
            if tolerance > 0:
                if abs(order_price_dec - target_price) > tolerance:
                    continue
            elif order_price_dec != target_price:
                continue
            if order_qty_dec == target_qty:
                return order
            if fallback_match is None:
                fallback_match = order
        return fallback_match

    def _has_duplicate_order(
        self,
        side: str,
        price: Decimal,
        qty: Decimal,
        tolerance_ticks: int = 0,
        ignore_order_id: str | None = None,
        ignore_keys: set[str] | None = None,
    ) -> bool:
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        target_price = self.q_price(price, tick)
        target_qty = self.q_qty(qty, step)
        key = self._order_key(side, target_price, target_qty)
        if key in self._bot_order_keys and (ignore_keys is None or key not in ignore_keys):
            return True
        tolerance = (tick or Decimal("0")) * Decimal(tolerance_ticks)
        qty_tolerance = step or Decimal("0.00000001")
        for order in self._open_orders:
            if ignore_order_id and str(order.get("orderId", "")) == ignore_order_id:
                continue
            if str(order.get("side", "")).upper() != side:
                continue
            order_price = self._coerce_float(str(order.get("price", ""))) or 0.0
            order_qty = self._coerce_float(str(order.get("origQty", ""))) or 0.0
            order_price_dec = self.q_price(self.as_decimal(order_price), tick)
            order_qty_dec = self.q_qty(self.as_decimal(order_qty), step)
            if tolerance > 0:
                if abs(order_price_dec - target_price) > tolerance:
                    continue
            elif order_price_dec != target_price:
                continue
            if abs(order_qty_dec - target_qty) <= qty_tolerance:
                return True
        return False

    def _refresh_exchange_rules(self, force: bool = False) -> None:
        if self._rules_in_flight:
            return
        if self._rules_loaded and not force:
            return
        self._rules_in_flight = True
        worker = _Worker(lambda: self._http_client.get_exchange_info_symbol(self._symbol))
        worker.signals.success.connect(self._handle_exchange_info)
        worker.signals.error.connect(self._handle_exchange_error)
        self._thread_pool.start(worker)

    def _sync_account_time(self) -> None:
        if not self._account_client:
            return
        worker = _Worker(self._account_client.sync_time_offset)
        worker.signals.success.connect(self._handle_time_sync)
        worker.signals.error.connect(self._handle_time_sync_error)
        self._thread_pool.start(worker)

    def _handle_time_sync(self, result: object, latency_ms: int) -> None:
        if not isinstance(result, dict):
            return
        offset = result.get("offset_ms")
        if isinstance(offset, int):
            self._append_log(f"[LIVE] time sync offset={offset}ms ({latency_ms}ms)", kind="INFO")

    def _handle_time_sync_error(self, message: str) -> None:
        self._append_log(f"[LIVE] time sync failed: {message}", kind="WARN")

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
        min_qty = self._extract_filter_value(filters, "LOT_SIZE", "minQty")
        max_qty = self._extract_filter_value(filters, "LOT_SIZE", "maxQty")
        min_notional = self._extract_filter_value(filters, "MIN_NOTIONAL", "minNotional")
        if min_notional is None:
            min_notional = self._extract_filter_value(filters, "NOTIONAL", "minNotional")
        self._exchange_rules = {
            "tick": tick_size,
            "step": step_size,
            "min_notional": min_notional,
            "min_qty": min_qty,
            "max_qty": max_qty,
        }
        self._rules_loaded = True
        self._apply_trade_gate()
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
        self._auto_pause_on_api_error(message)
        self._auto_pause_on_exception(message)
        self._apply_trade_gate()
        self._update_rules_label()

    def _refresh_trade_fees(self, force: bool = False) -> None:
        if not self._account_client or self._fees_in_flight:
            return
        now = time()
        if not force and self._fees_last_fetch_ts and now - self._fees_last_fetch_ts < 1800:
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
        self._fees_last_fetch_ts = time()
        self._update_rules_label()
        self._append_log(f"Trade fees loaded ({latency_ms}ms).", kind="INFO")

    def _handle_trade_fees_error(self, message: str) -> None:
        self._fees_in_flight = False
        self._append_log(f"Trade fees error: {message}", kind="ERROR")
        self._auto_pause_on_api_error(message)
        self._auto_pause_on_exception(message)
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
        self._fees_last_fetch_ts = time()
        self._update_rules_label()

    def _apply_trade_gate(self) -> None:
        gate = self._determine_trade_gate()
        if gate != self._trade_gate:
            self._append_log(
                f"trade_gate: {self._trade_gate.value} -> {gate.value}",
                kind="INFO",
            )
            self._trade_gate = gate
        if gate in {
            TradeGate.TRADE_DISABLED_NO_KEYS,
            TradeGate.TRADE_DISABLED_API_ERROR,
            TradeGate.TRADE_DISABLED_CANT_TRADE,
            TradeGate.TRADE_DISABLED_SYMBOL,
        }:
            self._suppress_dry_run_event = True
            self._dry_run_toggle.setChecked(True)
            self._suppress_dry_run_event = False
            self._dry_run_toggle.setEnabled(False)
        else:
            self._dry_run_toggle.setEnabled(True)
        self._apply_trade_status_label()
        self._update_grid_preview()
        self._update_fills_timer()

    def _apply_trade_status_label(self) -> None:
        if self._trade_gate != TradeGate.TRADE_OK:
            reason = self._trade_gate_reason()
            self._trade_status_label.setText(f"{tr('trade_status_disabled')} ({reason})")
            self._trade_status_label.setStyleSheet(
                "color: #dc2626; font-size: 11px; font-weight: 600;"
            )
            self._trade_status_label.setToolTip(tr("trade_disabled_tooltip", reason=reason))
            self._set_cancel_buttons_enabled(self._dry_run_toggle.isChecked())
        else:
            self._trade_status_label.setText(tr("trade_status_enabled"))
            self._trade_status_label.setStyleSheet(
                "color: #16a34a; font-size: 11px; font-weight: 600;"
            )
            self._trade_status_label.setToolTip("")
            self._set_cancel_buttons_enabled(True)

    def _set_cancel_buttons_enabled(self, enabled: bool) -> None:
        if hasattr(self, "_cancel_selected_button"):
            self._cancel_selected_button.setEnabled(enabled)
        if hasattr(self, "_cancel_all_button"):
            self._cancel_all_button.setEnabled(enabled)

    def _can_trade(self) -> bool:
        if not self._account_client:
            return False
        if not self._can_read_account:
            return False
        if not self._symbol_tradeable:
            return False
        return self._account_can_trade

    def _determine_trade_gate(self) -> TradeGate:
        if not self._has_api_keys:
            return TradeGate.TRADE_DISABLED_NO_KEYS
        if self._account_api_error or not self._can_read_account:
            return TradeGate.TRADE_DISABLED_API_ERROR
        if self._dry_run_toggle.isChecked():
            return TradeGate.TRADE_DISABLED_READONLY
        if not self._live_mode_confirmed:
            return TradeGate.TRADE_DISABLED_NO_CONFIRM
        if not self._symbol_tradeable:
            return TradeGate.TRADE_DISABLED_SYMBOL
        if not self._account_can_trade:
            return TradeGate.TRADE_DISABLED_CANT_TRADE
        return TradeGate.TRADE_OK

    def _trade_gate_reason(self) -> str:
        mapping = {
            TradeGate.TRADE_DISABLED_NO_KEYS: "no keys",
            TradeGate.TRADE_DISABLED_API_ERROR: "api error",
            TradeGate.TRADE_DISABLED_CANT_TRADE: "canTrade=false",
            TradeGate.TRADE_DISABLED_SYMBOL: "symbol not trading",
            TradeGate.TRADE_DISABLED_READONLY: tr("trade_disabled_reason_spot"),
            TradeGate.TRADE_DISABLED_NO_CONFIRM: tr("trade_disabled_reason_confirm"),
        }
        return mapping.get(self._trade_gate, "unknown")

    @staticmethod
    def _is_auth_error(message: str) -> bool:
        message_lower = message.lower()
        return (
            "401" in message_lower
            or "unauthorized" in message_lower
            or "403" in message_lower
            or "forbidden" in message_lower
        )

    def _infer_account_status(self, message: str) -> str:
        message_lower = message.lower()
        if "401" in message_lower or "unauthorized" in message_lower:
            return "no_permission"
        if "403" in message_lower or "forbidden" in message_lower:
            return "no_permission"
        return "error"

    def _auto_pause_on_api_error(self, message: str) -> None:
        if self._dry_run_toggle.isChecked():
            return
        if self._state not in {"RUNNING", "WAITING_FILLS"}:
            return
        if not self._should_autopause_on_error(message):
            return
        self._append_log(f"Auto-paused due to API error: {message}", kind="WARN")
        self._grid_engine.pause()
        self._change_state("PAUSED")

    def _auto_pause_on_exception(self, message: str) -> None:
        if self._dry_run_toggle.isChecked():
            return
        if self._state not in {"RUNNING", "WAITING_FILLS"}:
            return
        if not self._should_autopause_on_error(message):
            return
        self._append_log(f"Auto-paused due to exception: {message}", kind="WARN")
        self._grid_engine.pause()
        self._change_state("PAUSED")

    def _handle_live_api_error(self, message: str) -> None:
        self._auto_pause_on_api_error(message)
        self._auto_pause_on_exception(message)

    def _should_autopause_on_error(self, message: str) -> bool:
        return self._is_rate_limit_or_server_error(message) or self._is_time_sync_error(message)

    @staticmethod
    def _is_rate_limit_or_server_error(message: str) -> bool:
        text = message.lower()
        codes = ("429", "418", "500", "501", "502", "503", "504", "505", "-1003")
        return (
            any(code in text for code in codes)
            or "retryable status 5" in text
            or "5xx" in text
            or "too many requests" in text
        )

    @staticmethod
    def _is_time_sync_error(message: str) -> bool:
        text = message.lower()
        return "-1021" in text or "timestamp" in text or "recvwindow" in text

    def _set_account_status(self, status: str) -> None:
        if status == "ready":
            self._can_read_account = True
            text = tr("account_status_ready")
            style = "color: #16a34a; font-size: 11px; font-weight: 600;"
        elif status == "no_keys":
            self._can_read_account = False
            text = tr("account_status_no_keys")
            style = "color: #dc2626; font-size: 11px; font-weight: 600;"
        elif status == "no_permission":
            self._can_read_account = False
            text = tr("account_status_no_permission")
            style = "color: #dc2626; font-size: 11px; font-weight: 600;"
        else:
            self._can_read_account = False
            text = tr("account_status_error")
            style = "color: #f97316; font-size: 11px; font-weight: 600;"

        self._account_status_label.setText(text)
        self._account_status_label.setStyleSheet(style)
        if status != self._last_account_status:
            self._append_log(text, kind="WARN" if status != "ready" else "INFO")
            self._last_account_status = status

    def _update_rules_label(self) -> None:
        tick = self._exchange_rules.get("tick")
        step = self._exchange_rules.get("step")
        min_notional = self._exchange_rules.get("min_notional")
        min_qty = self._exchange_rules.get("min_qty")
        max_qty = self._exchange_rules.get("max_qty")
        maker, taker = self._trade_fees
        has_rules = any(value is not None for value in (tick, step, min_notional, min_qty, max_qty, maker, taker))
        if not has_rules:
            self._rules_label.setText(tr("rules_line", rules="—"))
            self._market_fee.setText(f"{tr('fee')}: —")
            self._set_market_label_state(self._market_fee, active=False)
            return
        tick_text = f"{tick:.8f}" if tick is not None else "—"
        step_text = f"{step:.8f}" if step is not None else "—"
        min_text = f"{min_notional:.4f}" if min_notional is not None else "—"
        min_qty_text = f"{min_qty:.8f}" if min_qty is not None else "—"
        max_qty_text = f"{max_qty:.8f}" if max_qty is not None else "—"
        maker_text = f"{(maker or 0.0) * 100:.2f}%" if maker is not None else "—"
        taker_text = f"{(taker or 0.0) * 100:.2f}%" if taker is not None else "—"
        rules = (
            f"tick {tick_text} | step {step_text}"
            f" | minQty {min_qty_text} | maxQty {max_qty_text}"
            f" | minNotional {min_text} | maker/taker {maker_text}/{taker_text}"
        )
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

    def _render_sim_orders(self, planned: list[GridPlannedOrder]) -> None:
        self._orders_table.setRowCount(len(planned))
        now_ms = int(time() * 1000)
        for row, order in enumerate(planned):
            order_id = f"SIM-{row + 1}"
            age_text = self._format_age(now_ms, now_ms)
            self._set_order_cell(row, 0, "", align=Qt.AlignLeft, user_role=order_id)
            self._set_order_cell(row, 1, order.side, align=Qt.AlignLeft, user_role=order_id)
            self._set_order_cell(row, 2, f"{order.price:.8f}", align=Qt.AlignRight)
            self._set_order_cell(row, 3, f"{order.qty:.8f}", align=Qt.AlignRight)
            self._set_order_cell(row, 4, "SIM", align=Qt.AlignRight)
            self._set_order_cell(row, 5, age_text, align=Qt.AlignRight)
            self._set_order_row_tooltip(row)
        self._refresh_orders_metrics()

    def _place_live_orders(self, planned: list[GridPlannedOrder]) -> None:
        if not self._account_client:
            self._append_log("Live order placement skipped: no account client.", kind="WARN")
            return
        if not self._bot_session_id:
            self._bot_session_id = uuid4().hex[:8]
        batch_size = 5

        def _place() -> dict[str, Any]:
            results: list[dict[str, Any]] = []
            errors: list[str] = []
            last_open_orders: list[dict[str, Any]] | None = None
            for idx, order in enumerate(planned, start=1):
                client_order_id = self._make_client_order_id("GRID", idx)
                try:
                    price = self.as_decimal(order.price)
                    qty = self.as_decimal(order.qty)
                    response, error = self._place_limit(
                        order.side,
                        price,
                        qty,
                        client_order_id,
                        reason="grid",
                    )
                    if response:
                        results.append(response)
                    if error:
                        errors.append(error)
                except Exception as exc:  # noqa: BLE001
                    errors.append(self._format_binance_error(exc, order))
                self._sleep_ms(200)
                if idx % batch_size == 0:
                    self._sleep_ms(200)
                    try:
                        last_open_orders = self._account_client.get_open_orders(self._symbol)
                    except Exception:
                        last_open_orders = None
            return {"results": results, "errors": errors, "open_orders": last_open_orders}

        worker = _Worker(_place)
        worker.signals.success.connect(self._handle_live_order_placement)
        worker.signals.error.connect(self._handle_live_order_error)
        self._thread_pool.start(worker)

    def _handle_live_order_placement(self, result: object, latency_ms: int) -> None:
        if not isinstance(result, dict):
            self._handle_live_order_error("Unexpected live order response")
            return
        results = result.get("results", [])
        errors = result.get("errors", [])
        if isinstance(results, list):
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                order_id = str(entry.get("orderId", ""))
                if order_id:
                    self._bot_order_ids.add(order_id)
                side = str(entry.get("side", "—")).upper()
                price = str(entry.get("price", "—"))
                qty = str(entry.get("origQty", "—"))
                if side in {"BUY", "SELL"} and price and qty:
                    parsed_price = self._coerce_float(price) or 0.0
                    parsed_qty = self._coerce_float(qty) or 0.0
                    if parsed_price > 0 and parsed_qty > 0:
                        self._bot_order_keys.add(
                            self._order_key(
                                side,
                                self.as_decimal(parsed_price),
                                self.as_decimal(parsed_qty),
                            )
                        )
                self._append_log(
                    f"[LIVE] place orderId={order_id} side={side} price={price} qty={qty}",
                    kind="ORDERS",
                )
        if isinstance(errors, list):
            for message in errors:
                self._append_log(message, kind="ERROR")
                self._auto_pause_on_api_error(message)
                self._auto_pause_on_exception(message)
        self._append_log(f"placed: n={len(results)}", kind="ORDERS")
        open_orders = result.get("open_orders")
        if isinstance(open_orders, list):
            self._handle_open_orders(open_orders, latency_ms)
            self._append_log(f"[LIVE] OPEN_ORDERS n={len(self._open_orders)}", kind="ORDERS")
        else:
            self._refresh_open_orders(force=True)
        self._change_state("RUNNING")

    def _handle_live_order_error(self, message: str) -> None:
        self._append_log(f"[LIVE] order error: {message}", kind="WARN")
        if self._state == "PLACING_GRID":
            self._change_state("RUNNING")
        self._auto_pause_on_api_error(message)
        self._auto_pause_on_exception(message)

    def _cancel_live_orders(self, order_ids: list[str]) -> None:
        if not self._account_client:
            self._append_log("Cancel selected: no account client.", kind="WARN")
            return
        filtered_ids = [order_id for order_id in order_ids if order_id and order_id != "—"]
        if not filtered_ids:
            self._append_log("Cancel selected: no valid order IDs.", kind="WARN")
            return

        def _cancel() -> list[dict[str, Any]]:
            responses: list[dict[str, Any]] = []
            batch_size = 4
            for idx, order_id in enumerate(filtered_ids, start=1):
                attempts = 3
                for attempt in range(attempts):
                    try:
                        response = self._account_client.cancel_order(self._symbol, order_id)
                        responses.append(response)
                        break
                    except Exception as exc:  # noqa: BLE001
                        status, code, message, response_body = self._parse_binance_exception(exc)
                        lower_message = str(message).lower()
                        if code == -2011 or "unknown order" in lower_message:
                            self._signals.log_append.emit(
                                (
                                    "[LIVE] cancel skipped: unknown order "
                                    f"orderId={order_id} status={status} code={code} msg={message} response={response_body}"
                                ),
                                "WARN",
                            )
                            break
                        if not self._is_rate_limit_or_server_error(message) or attempt == attempts - 1:
                            error_text = (
                                "[LIVE] cancel failed "
                                f"orderId={order_id} status={status} code={code} msg={message} response={response_body}"
                            )
                            self._signals.log_append.emit(error_text, "ERROR")
                            self._signals.api_error.emit(error_text)
                            break
                        self._sleep_ms(300)
                if idx % batch_size == 0:
                    self._sleep_ms(250)
            return responses

        worker = _Worker(_cancel)
        worker.signals.success.connect(self._handle_cancel_selected_result)
        worker.signals.error.connect(self._handle_cancel_error)
        self._thread_pool.start(worker)

    def _handle_cancel_selected_result(self, result: object, latency_ms: int) -> None:
        if not isinstance(result, list):
            self._handle_cancel_error("Unexpected cancel response")
            return
        for entry in result:
            order_id = str(entry.get("orderId", "—")) if isinstance(entry, dict) else "—"
            self._append_log(f"[LIVE] cancel orderId={order_id}", kind="ORDERS")
        self._append_log(f"Cancel selected: {len(result)}", kind="ORDERS")
        self._refresh_open_orders(force=True)

    def _cancel_all_live_orders(self) -> None:
        self._cancel_bot_orders()

    def _handle_cancel_all_result(self, result: object, latency_ms: int) -> None:
        if not isinstance(result, list):
            self._handle_cancel_error("Unexpected cancel all response")
            return
        for entry in result:
            order_id = str(entry.get("orderId", "—")) if isinstance(entry, dict) else "—"
            self._append_log(f"[LIVE] cancel orderId={order_id}", kind="ORDERS")
        self._append_log(f"Cancel all: {len(result)}", kind="ORDERS")
        self._refresh_open_orders(force=True)

    def _handle_cancel_error(self, message: str) -> None:
        self._append_log(f"[LIVE] cancel failed: {message}", kind="WARN")
        self._auto_pause_on_api_error(message)
        self._auto_pause_on_exception(message)

    def _format_binance_error(self, exc: Exception, order: GridPlannedOrder) -> str:
        message = str(exc)
        code = None
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                payload = None
            if isinstance(payload, dict):
                code = payload.get("code")
                message = str(payload.get("msg") or message)
        filter_name = self._extract_filter_failure(message)
        price = order.price
        qty = order.qty
        notional = price * qty
        min_notional = self._exchange_rules.get("min_notional")
        if filter_name:
            min_note = f" min={min_notional:.8f}" if min_notional is not None else ""
            code_note = f" code={code}" if code is not None else ""
            return (
                f"order rejected:{code_note} FILTER_FAILURE {filter_name} "
                f"price={price:.8f} qty={qty:.8f} notional={notional:.8f}{min_note}"
            )
        if code is not None:
            return f"order rejected: code={code} msg={message}"
        return f"order rejected: {message}"

    @staticmethod
    def _extract_filter_failure(message: str) -> str | None:
        text = message.strip()
        lower = text.lower()
        if "filter failure" not in lower:
            return None
        parts = text.split(":", 1)
        if len(parts) == 2:
            return parts[1].strip().upper()
        return "UNKNOWN"

    @staticmethod
    def _sleep_ms(duration_ms: int) -> None:
        if duration_ms <= 0:
            return
        sleep(duration_ms / 1000)

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
            "IDLE": "#6b7280",
            "PLACING_GRID": "#2563eb",
            "RUNNING": "#16a34a",
            "WAITING_FILLS": "#16a34a",
            "PAUSED": "#d97706",
            "STOPPING": "#f97316",
            "ERROR": "#dc2626",
        }
        color = color_map.get(state, "#6b7280")
        self._engine_state_label.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 600;"
        )

    def _set_engine_state(self, state: str) -> None:
        self._engine_state = state
        self._engine_state_label.setText(f"{tr('engine')}: {self._engine_state}")
        self._apply_engine_state_style(self._engine_state)

    def _update_pnl(self, unrealized: float | None, realized: float | None) -> None:
        if unrealized is None and realized is None:
            self._pnl_label.setText(tr("pnl_no_fills"))
            self._apply_pnl_style(self._pnl_label, None)
            return

        unreal_text = "—" if unrealized is None else f"{unrealized:.2f}"
        real_text = "—" if realized is None else f"{realized:.2f}"
        fees_text = f"{self._fees_total:.2f}"
        if realized is None and unrealized is None:
            self._apply_pnl_style(self._pnl_label, None)
        else:
            total = (realized or 0.0) + (unrealized or 0.0)
            self._apply_pnl_style(self._pnl_label, total)

        self._pnl_label.setText(
            tr(
                "pnl_line",
                unreal=unreal_text,
                real=real_text,
                fees=fees_text,
            )
        )

    def _show_order_context_menu(self, position: Any) -> None:
        row = self._orders_table.rowAt(position.y())
        if row < 0:
            return
        menu = QMenu(self)
        cancel_action = menu.addAction(tr("context_cancel"))
        cancel_action.setEnabled(self._dry_run_toggle.isChecked() or self._trade_gate == TradeGate.TRADE_OK)
        copy_action = menu.addAction(tr("context_copy_id"))
        show_action = menu.addAction(tr("context_show_logs"))
        action = menu.exec(self._orders_table.viewport().mapToGlobal(position))
        if action == cancel_action:
            order_id = self._order_id_for_row(row)
            if self._dry_run_toggle.isChecked():
                self._orders_table.removeRow(row)
                self._append_log(f"Cancel selected: {order_id}", kind="ORDERS")
                self._refresh_orders_metrics()
            else:
                self._cancel_live_orders([order_id])
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
            "IDLE": "IDLE",
            "RUNNING": "RUNNING",
            "WAITING_FILLS": "WAITING_FILLS",
            "PAUSED": "PAUSED",
            "STOPPING": "STOPPING",
            "PLACING_GRID": "PLACING_GRID",
            "ERROR": "ERROR",
        }
        return mapping.get(state, state)

    def _update_grid_preview(self) -> None:
        levels = int(self._grid_count_input.value())
        step = float(self._grid_step_input.value())
        range_low = float(self._range_low_input.value())
        range_high = float(self._range_high_input.value())
        if self._settings_state.grid_step_mode == "AUTO_ATR":
            auto_params = self._auto_grid_params_from_history()
            if auto_params:
                step = auto_params["grid_step_pct"]
                range_low = auto_params["range_pct"]
                range_high = auto_params["range_pct"]
                self._set_grid_step_input(step, update_setting=False)
        range_pct = max(range_low, range_high)
        budget = float(self._budget_input.value())
        min_order = budget / levels if levels > 0 else 0.0
        quote_ccy = self._quote_asset or "—"
        self._grid_preview_label.setText(
            tr(
                "grid_preview",
                levels=str(levels),
                step=f"{step:.2f}",
                range=f"{range_pct:.2f}",
                orders=str(levels),
                min_order=f"{min_order:.2f}",
                quote_ccy=quote_ccy,
            )
        )
        self._update_auto_values_label(self._settings_state.grid_step_mode == "AUTO_ATR")

    def closeEvent(self, event: object) -> None:  # noqa: N802
        self._balances_timer.stop()
        self._orders_timer.stop()
        self._fills_timer.stop()
        self._price_feed_manager.unsubscribe(self._symbol, self._emit_price_update)
        self._price_feed_manager.unsubscribe_status(self._symbol, self._emit_status_update)
        self._price_feed_manager.unregister_symbol(self._symbol)
        self._append_log("Lite Grid Terminal closed.", kind="INFO")
        super().closeEvent(event)

    def dump_settings(self) -> dict[str, Any]:
        return asdict(self._settings_state)
