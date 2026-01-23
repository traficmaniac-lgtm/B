from __future__ import annotations

import atexit
import faulthandler
import os
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from math import ceil, floor
from time import monotonic, perf_counter, sleep, time as time_fn
from typing import Any, Callable
from uuid import uuid4

from PySide6.QtCore import QObject, QEventLoop, QRunnable, Qt, QThreadPool, QTimer, Signal
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
    QHeaderView,
    QGridLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.ai.operator_math import compute_break_even_tp_pct, compute_fee_total_pct, evaluate_tp_profitability
from src.ai.operator_profiles import get_profile_preset
from src.binance.account_client import AccountStatus, BinanceAccountClient
from src.binance.http_client import BinanceHttpClient
from src.core.config import Config
from src.core.logging import get_logger
from src.gui.i18n import TEXT, tr
from src.gui.lite_grid_math import FillAccumulator, build_action_key, compute_order_qty
from src.gui.models.app_state import AppState
from src.services.data_cache import DataCache
from src.services.price_feed_manager import (
    PriceFeedManager,
    PriceUpdate,
    WS_CONNECTED,
    WS_DEGRADED,
    WS_LOST,
    WS_STALE_MS,
)

_NC_MICRO_CRASH_HANDLES: list[object] = []


def _install_nc_micro_crash_catcher(logger: Any, symbol: str) -> tuple[str, object]:
    base_dir = os.getcwd()
    crash_dir = os.path.join(base_dir, "logs", "crash")
    os.makedirs(crash_dir, exist_ok=True)
    safe_symbol = symbol.replace("/", "_")
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    crash_log_path = os.path.join(crash_dir, f"NC_MICRO_{safe_symbol}_{timestamp}.log")
    crash_file = open(crash_log_path, "a", buffering=1, encoding="utf-8")
    _NC_MICRO_CRASH_HANDLES.append(crash_file)
    faulthandler.enable(crash_file, all_threads=True)
    faulthandler.dump_traceback_later(30, repeat=True, file=crash_file)

    def _write_line(message: str) -> None:
        try:
            crash_file.write(f"{message}\n")
            crash_file.flush()
        except Exception:
            return

    pid = os.getpid()
    _write_line(f"=== START pid={pid} ts={datetime.now().isoformat()} ===")

    def _on_clean_exit() -> None:
        _write_line(f"=== CLEAN EXIT pid={pid} ts={datetime.now().isoformat()} ===")

    atexit.register(_on_clean_exit)

    def _write_traceback(prefix: str, exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        try:
            crash_file.write(f"\n[{prefix}] {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            crash_file.writelines(traceback.format_exception(exc_type, exc, tb))
            crash_file.flush()
        except Exception:
            return

    def _sys_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        _write_traceback("SYS", exc_type, exc, tb)
        try:
            logger.exception("[NC_MICRO][CRASH] unhandled exception", exc_info=(exc_type, exc, tb))
        except Exception:
            return

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        _write_traceback(f"THREAD:{args.thread.name}", args.exc_type, args.exc_value, args.exc_traceback)
        try:
            logger.exception(
                "[NC_MICRO][CRASH] unhandled thread exception",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        except Exception:
            return

    sys.excepthook = _sys_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook
    return crash_log_path, crash_file


class _WorkerSignals(QObject):
    success = Signal(object, int)
    error = Signal(str)


class _Worker(QRunnable):
    def __init__(self, fn: Callable[[], object], should_emit: Callable[[], bool] | None = None) -> None:
        super().__init__()
        self.signals = _WorkerSignals()
        self._fn = fn
        self._should_emit = should_emit

    def run(self) -> None:
        start = perf_counter()
        try:
            result = self._fn()
        except Exception as exc:
            if self._should_emit and not self._should_emit():
                return
            try:
                self.signals.error.emit(str(exc))
            except RuntimeError:
                return
            return
        latency_ms = int((perf_counter() - start) * 1000)
        if self._should_emit and not self._should_emit():
            return
        try:
            self.signals.success.emit(result, latency_ms)
        except RuntimeError:
            return


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
    balances_refresh = Signal(bool)


class TradeGate(Enum):
    TRADE_OK = "ok"
    TRADE_DISABLED_NO_KEYS = "no keys"
    TRADE_DISABLED_API_ERROR = "api error"
    TRADE_DISABLED_CANT_TRADE = "canTrade=false"
    TRADE_DISABLED_SYMBOL = "symbol not trading"
    TRADE_DISABLED_READONLY = "read-only"
    TRADE_DISABLED_NO_CONFIRM = "no live confirm"


class TradeGateState(Enum):
    READ_ONLY_API_ERROR = "read-only api error"
    READ_ONLY_NO_LIVE_CONFIRM = "read-only no live confirm"
    OK = "ok"


class PilotState(Enum):
    OFF = "OFF"
    NORMAL = "NORMAL"
    RECENTERING = "RECENTERING"
    RECOVERY = "RECOVERY"
    PAUSED_BY_RISK = "PAUSED_BY_RISK"


class PilotAction(Enum):
    TOGGLE = "TOGGLE"
    RECENTER = "RECENTER"
    RECOVERY = "RECOVERY"
    FLATTEN_BE = "FLATTEN_BE"
    FLAG_STALE = "FLAG_STALE"
    CANCEL_REPLACE_STALE = "CANCEL_REPLACE_STALE"


class StalePolicy(Enum):
    NONE = "NONE"
    RECENTER = "RECENTER"
    CANCEL_REPLACE_STALE = "CANCEL_REPLACE_STALE"


RECENTER_THRESHOLD_PCT = 0.7
STALE_DRIFT_PCT_DEFAULT = 0.20
STALE_DRIFT_PCT_STABLE = 0.05
STALE_BUFFER_MIN_PCT = 0.10
STALE_WARMUP_SEC = 30
STALE_ORDER_LOG_DEDUP_SEC = 10
STALE_LOG_DEDUP_SEC = 600
STALE_AUTO_ACTION_COOLDOWN_SEC = 300
AUTO_EXEC_ACTION_COOLDOWN_SEC = 30
STALE_SNAPSHOT_MAX_AGE_SEC = 5
STALE_COOLDOWN_SEC = 120
STALE_ACTION_COOLDOWN_SEC = 120
STALE_AUTO_ACTION_PAUSE_SEC = 300
TP_MIN_PROFIT_COOLDOWN_SEC = 30
PROFIT_GUARD_DEFAULT_MAKER_FEE_PCT = 0.10
PROFIT_GUARD_DEFAULT_TAKER_FEE_PCT = 0.10
PROFIT_GUARD_SLIPPAGE_BPS = 1.5
PROFIT_GUARD_EXTRA_BUFFER_PCT = 0.02
PROFIT_GUARD_BUFFER_PCT = 0.03
PROFIT_GUARD_MIN_FLOOR_PCT = 0.10
PROFIT_GUARD_EXIT_BUFFER_PCT = 0.02
PROFIT_GUARD_THIN_SPREAD_FACTOR = 0.25
PROFIT_GUARD_LOW_VOL_FACTOR = 0.5
PROFIT_GUARD_LOW_VOL_FLOOR_PCT = 0.05
PROFIT_GUARD_MIN_VOL_PCT = 0.05
VOL_CONFIRM_TICKS = 3
VOL_CONFIRM_TIME_MS = 1000
KPI_BOOK_REQUEST_INTERVAL_MS = 1000
KPI_DEBOUNCE_SEC = 5
KPI_VOL_SAMPLE_MAXLEN = 60
KPI_VOL_MIN_SAMPLES = 10
KPI_VOL_WINDOW_SEC = 60
KPI_VOL_TTL_SEC = 120
KPI_STATE_OK = "OK"
KPI_STATE_UNKNOWN = "UNKNOWN"
KPI_STATE_INVALID = "INVALID"
BIDASK_POLL_MS = 700
BIDASK_FAIL_SOFT_MS = 1500
BIDASK_STALE_MS = 4000
PRICE_STALE_MS = 4000
SPREAD_DISPLAY_EPS_PCT = 0.0001
REGISTRY_GC_INTERVAL_MS = 60_000
REGISTRY_GC_TTL_SEC = 600
SPREAD_ZERO_OK_SYMBOLS = {
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
}


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


@dataclass
class PositionState:
    position_qty: float
    avg_entry_price: float | None
    realized_pnl_quote: float
    fees_paid_quote: float
    break_even_price: float | None


@dataclass
class OrderInfo:
    order_id: str
    side: str
    price: float
    qty: float
    created_ts: float
    last_seen_ts: float


@dataclass
class StaleOrderCandidate:
    info: OrderInfo
    order: dict[str, Any]
    age_s: int
    reason: str
    dist_pct: float | None
    range_limit_pct: float | None
    excess_pct: float | None


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

    def build_side_plan(
        self,
        settings: GridSettingsState,
        last_price: float,
        rules: dict[str, float | None],
        side: str,
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
        if side == "SELL":
            count = sells
            per_order_quote = sell_budget / sells if sells else 0.0
            price_min = last_price
            price_max = last_price * (1 + range_high / 100.0)
        else:
            count = buys
            per_order_quote = buy_budget / buys if buys else 0.0
            price_min = last_price * (1 - range_low / 100.0)
            price_max = last_price
        return self._build_side(
            side=side,
            count=count,
            anchor_price=last_price,
            step_pct=step_pct,
            price_min=price_min,
            price_max=price_max,
            per_order_quote=per_order_quote,
            tick=tick,
            step=step,
            min_notional=min_notional,
            min_qty=min_qty,
            max_qty=max_qty,
        )

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


class LiteAllStrategyNcMicroWindow(QMainWindow):
    def __init__(
        self,
        symbol: str,
        config: Config,
        app_state: AppState,
        price_feed_manager: PriceFeedManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._logger = get_logger("gui.lite_all_strategy_nc_micro")
        self._config = config
        self._app_state = app_state
        self._symbol = symbol.strip().upper()
        self._price_feed_manager = price_feed_manager
        self._signals = _LiteGridSignals()
        self._signals.price_update.connect(self._apply_price_update)
        self._signals.status_update.connect(self._apply_status_update)
        self._signals.log_append.connect(self._append_log)
        self._signals.api_error.connect(self._handle_live_api_error)
        self._log_entries: list[tuple[str, str]] = []
        self._crash_log_path, self._crash_file = _install_nc_micro_crash_catcher(
            self._logger, self._symbol
        )
        self._crash_notified = False
        self._append_log(
            f"[NC_MICRO] crash catcher installed path={self._crash_log_path}",
            kind="INFO",
        )
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._handle_about_to_quit)
        self._state = "IDLE"
        self._engine_state = "WAITING"
        self._ws_status = ""
        self._closing = False
        self._bootstrap_mode = False
        self._bootstrap_sell_enabled = False
        self._sell_side_enabled = False
        self._active_tp_ids: set[str] = set()
        self._active_restore_ids: set[str] = set()
        self._pending_tp_ids: set[str] = set()
        self._pending_restore_ids: set[str] = set()
        self._settings_state = GridSettingsState()
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
        self._http_cache = DataCache()
        self._http_cache_ttls = {
            "book_ticker": 2.0,
            "klines_1h": 60.0,
            "exchange_info_symbol": 3600.0,
        }
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
        self._bot_client_ids: set[str] = set()
        self._bot_order_keys: set[str] = set()
        self._fill_keys: set[str] = set()
        self._fill_accumulator = FillAccumulator()
        self._active_action_keys: set[str] = set()
        self._active_order_keys: set[str] = set()
        self._recent_order_keys: dict[str, float] = {}
        self._order_id_to_registry_key: dict[str, str] = {}
        self._order_id_to_level_index: dict[str, int] = {}
        self._closed_order_ids: set[str] = set()
        self._closed_order_statuses: set[tuple[str, str]] = set()
        self._recent_key_ttl_s = 8.0
        self._recent_key_insufficient_ttl_s = 2.0
        self._bot_session_id: str | None = None
        self._last_price: float | None = None
        self._last_price_ts: float | None = None
        self._price_history: list[float] = []
        self._account_can_trade = False
        self._account_permissions: list[str] = []
        self._account_api_error = False
        self._symbol_tradeable = False
        self._suppress_dry_run_event = False
        self._dry_run_enabled = True
        self._exchange_rules: dict[str, float | None] = {}
        self._trade_fees: tuple[float | None, float | None] = (None, None)
        self._fees_last_fetch_ts: float | None = None
        self._quote_asset = ""
        self._base_asset = ""
        self._balances_in_flight = False
        self._balances_loaded = False
        self._balance_ready_ts_monotonic_ms: int | None = None
        self._orders_in_flight = False
        self._rules_in_flight = False
        self._fees_in_flight = False
        self._trade_gate = TradeGate.TRADE_DISABLED_NO_KEYS if not self._has_api_keys else TradeGate.TRADE_DISABLED_READONLY
        self._trade_gate_state = TradeGateState.READ_ONLY_API_ERROR
        self._engine_ready_state = False
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
        self._position_fees_paid_quote = 0.0
        self._closed_trades = 0
        self._win_trades = 0
        self._replacement_counter = 0
        self._balances_tick_count = 0
        self._orders_tick_count = 0
        self._orders_last_count: int | None = None
        self._balances_snapshot: tuple[float, float] | None = None
        self._sell_retry_limit = 5
        self._start_in_progress = False
        self._start_in_progress_logged = False
        self._start_token = 0
        self._last_preflight_hash: str | None = None
        self._last_preflight_blocked = False
        self._start_locked_until_change = False
        self._start_locked_logged = False
        self._tp_fix_target: float | None = None
        self._auto_fix_tp_enabled = True
        self._stop_in_progress = False
        self._pilot_state = PilotState.OFF
        self._pilot_auto_actions_enabled = True
        self._pilot_confirm_dry_run = False
        self._pilot_allow_market = False
        self._pilot_action_in_progress = False
        self._current_action: str | None = None
        self._pilot_pending_action: PilotAction | None = None
        self._pilot_pending_plan: list[GridPlannedOrder] | None = None
        self._pilot_warning_override: str | None = None
        self._pilot_warning_override_until: float | None = None
        self._pilot_anchor_price: float | None = None
        self._pilot_pending_anchor: float | None = None
        self._pilot_recenter_expected_min: int | None = None
        self._pilot_stale_active = False
        self._pilot_stale_last_log_ts: float | None = None
        self._pilot_stale_last_log_order_id: str | None = None
        self._pilot_stale_last_log_total: int | None = None
        self._pilot_stale_last_action_ts: float | None = None
        self._pilot_snapshot_stale_active = False
        self._pilot_snapshot_stale_last_log_ts: float | None = None
        self._pilot_stale_policy = StalePolicy.NONE
        self._pilot_pending_cancel_ids: set[str] = set()
        self._pilot_stale_seen_ids: dict[str, float] = {}
        self._pilot_stale_handled: dict[str, float] = {}
        self._pilot_stale_skip_log_ts: dict[str, float] = {}
        self._pilot_stale_action_ts: dict[str, float] = {}
        self._pilot_stale_order_log_ts: dict[tuple[str, str], float] = {}
        self._pilot_stale_check_log_ts: dict[tuple[str, str], float] = {}
        self._pilot_auto_exec_cooldown_ts: dict[tuple[str, str], float] = {}
        self._pilot_stale_warmup_until: float | None = None
        self._pilot_stale_warmup_last_log_ts: float | None = None
        self._pilot_auto_actions_paused_until: float | None = None
        self._tp_min_profit_action_ts: dict[str, float] = {}
        self._last_price_update: PriceUpdate | None = None
        self._feed_ok = False
        self._feed_last_ok: bool | None = None
        self._feed_last_source: str | None = None
        self._kpi_has_data = False
        self._kpi_last_source: str | None = None
        self._kpi_valid = False
        self._kpi_invalid_reason: str | None = None
        self._kpi_state = KPI_STATE_UNKNOWN
        self._kpi_last_state: str | None = None
        self._kpi_vol_state: str | None = None
        self._kpi_zero_vol_ticks = 0
        self._kpi_zero_vol_start_ts: float | None = None
        self._kpi_missing_bidask_since_ts: float | None = None
        self._kpi_last_book_request_ts: float | None = None
        self._kpi_missing_bidask_active = False
        self._bidask_last: tuple[float, float] | None = None
        self._bidask_ts: float | None = None
        self._bidask_src: str | None = None
        self._bidask_last_request_ts: float | None = None
        self._bidask_ready_logged = False
        self._kpi_vol_samples: deque[tuple[float, float]] = deque(maxlen=KPI_VOL_SAMPLE_MAXLEN)
        self._kpi_last_good_vol: float | None = None
        self._kpi_last_good_ts: float | None = None
        self._kpi_last_state_ts: float | None = None
        self._kpi_last_reason: str | None = None
        self._kpi_last_good: dict[str, Any] | None = None
        self._open_orders_snapshot_ts: float | None = None
        self._order_info_map: dict[str, OrderInfo] = {}
        self._registry_key_last_seen_ts: dict[str, float] = {}
        self._registry_gc_last_ts: float | None = None
        self._position_state = PositionState(
            position_qty=0.0,
            avg_entry_price=None,
            realized_pnl_quote=0.0,
            fees_paid_quote=0.0,
            break_even_price=None,
        )

        self._balances_timer = QTimer(self)
        self._balances_timer.setInterval(10_000)
        self._balances_timer.timeout.connect(self._refresh_balances)
        self._orders_timer = QTimer(self)
        self._orders_timer.setInterval(3_000)
        self._orders_timer.timeout.connect(self._refresh_open_orders)
        self._fills_timer = QTimer(self)
        self._fills_timer.setInterval(2_500)
        self._fills_timer.timeout.connect(self._refresh_fills)
        self._pilot_ui_timer = QTimer(self)
        self._pilot_ui_timer.setInterval(750)
        self._pilot_ui_timer.timeout.connect(self._update_pilot_panel)
        self._market_kpi_timer = QTimer(self)
        self._market_kpi_timer.setInterval(500)
        self._market_kpi_timer.timeout.connect(self.update_market_kpis)
        self._registry_gc_timer = QTimer(self)
        self._registry_gc_timer.setInterval(REGISTRY_GC_INTERVAL_MS)
        self._registry_gc_timer.timeout.connect(self._gc_registry_keys)

        self.setWindowTitle(f"Lite All Strategy Terminal — NC MICRO — {self._symbol}")
        self.resize(1050, 720)

        central = QWidget(self)
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(10, 10, 10, 10)
        outer_layout.setSpacing(8)

        outer_layout.addLayout(self._build_header())
        outer_layout.addWidget(self._build_body())

        self.setCentralWidget(central)
        self._apply_trade_gate()
        self._pilot_ui_timer.start()
        self._market_kpi_timer.start()
        self._registry_gc_timer.start()

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
        self._append_log(
            f"[NC_MICRO] opened. version=NC MICRO v1.0.5 symbol={self._symbol}",
            kind="INFO",
        )

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
        for label in (self._feed_indicator, self._age_label, self._latency_label):
            label.setFixedHeight(18)
            label.setMinimumWidth(150)
            label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self._engine_state_label = QLabel(f"{tr('engine')}: {self._engine_state}")
        self._apply_engine_state_style(self._engine_state)
        self._trade_status_label = QLabel(tr("trade_status_disabled"))
        self._trade_status_label.setStyleSheet("color: #dc2626; font-size: 11px; font-weight: 600;")
        for label in (self._engine_state_label, self._trade_status_label):
            label.setFixedHeight(18)
            label.setMinimumWidth(160)
            label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

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
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 1)
        return splitter

    def _build_right_panel(self) -> QSplitter:
        self._right_splitter = QSplitter(Qt.Vertical)
        self._right_splitter.addWidget(self._build_runtime_panel())
        self._right_splitter.addWidget(self._build_logs())
        self._right_splitter.setStretchFactor(0, 3)
        self._right_splitter.setStretchFactor(1, 1)
        self._right_splitter.setCollapsible(0, False)
        self._right_splitter.setCollapsible(1, True)
        self._right_splitter.setSizes([720, 240])
        return self._right_splitter

    def _build_market_panel(self) -> QWidget:
        group = QGroupBox(tr("market"))
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        summary_frame = QFrame(group)
        summary_frame.setStyleSheet("QFrame { border: 1px solid #e5e7eb; border-radius: 6px; }")
        summary_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        summary_frame.setMinimumHeight(110)
        summary_layout = QGridLayout(summary_frame)
        summary_layout.setHorizontalSpacing(8)
        summary_layout.setVerticalSpacing(2)
        summary_layout.setContentsMargins(6, 6, 6, 6)

        def make_summary_key(text: str) -> QLabel:
            label = QLabel(text)
            label.setStyleSheet("color: #d1d5db; font-size: 10px;")
            label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            return label

        self._market_price = QLabel("—")
        self._market_spread = QLabel("—")
        self._market_volatility = QLabel("—")
        self._market_fee = QLabel("—")
        self._market_source = QLabel("—")
        self._rules_label = QLabel(tr("rules_line", rules="—"))
        self._rules_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._rules_label.setToolTip(tr("rules_line", rules="—"))
        self._rules_label.setWordWrap(False)

        self._set_market_label_state(self._market_price, active=False)
        self._set_market_label_state(self._market_spread, active=False)
        self._set_market_label_state(self._market_volatility, active=False)
        self._set_market_label_state(self._market_fee, active=False)
        self._set_market_label_state(self._market_source, active=False)
        self._rules_label.setStyleSheet("color: #d1d5db; font-size: 10px;")

        summary_rows = [
            ("Последняя цена:", self._market_price),
            ("Спред:", self._market_spread),
            ("Волатильность:", self._market_volatility),
            ("Комиссия (maker/taker):", self._market_fee),
            ("Источник + возраст:", self._market_source),
        ]
        for row, (key_text, value_label) in enumerate(summary_rows):
            summary_layout.addWidget(make_summary_key(key_text), row, 0)
            summary_layout.addWidget(value_label, row, 1)

        summary_layout.setColumnStretch(1, 1)
        layout.addWidget(summary_frame)
        layout.addWidget(self._rules_label)
        algo_pilot_frame = QGroupBox("NC MICRO")
        algo_pilot_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        algo_pilot_frame.setMinimumHeight(260)
        algo_pilot_layout = QVBoxLayout(algo_pilot_frame)
        algo_pilot_layout.setContentsMargins(6, 6, 6, 6)
        algo_pilot_layout.setSpacing(4)

        metrics_widget = QWidget(algo_pilot_frame)
        metrics_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        metrics_widget.setMinimumHeight(220)
        indicator_grid = QGridLayout(metrics_widget)
        indicator_grid.setHorizontalSpacing(8)
        indicator_grid.setVerticalSpacing(2)

        self._pilot_state_value = QLabel(self._pilot_state_label(self._pilot_state))
        self._pilot_regime_value = QLabel("Диапазон")
        self._pilot_anchor_value = QLabel("—")
        self._pilot_distance_value = QLabel("—")
        self._pilot_threshold_value = QLabel(f"{RECENTER_THRESHOLD_PCT:.2f} %")
        self._pilot_position_qty_value = QLabel("—")
        self._pilot_avg_entry_value = QLabel("—")
        self._pilot_break_even_value = QLabel("—")
        self._pilot_unrealized_value = QLabel("—")
        self._pilot_orders_total_value = QLabel("—")
        self._pilot_orders_buy_value = QLabel("—")
        self._pilot_orders_sell_value = QLabel("—")
        self._pilot_orders_oldest_value = QLabel("—")
        self._pilot_orders_threshold_value = QLabel("—")
        self._pilot_orders_stale_value = QLabel("—")
        self._pilot_orders_warning_value = QLabel("—")
        self._pilot_orders_warning_value.setStyleSheet("color: #111827; font-weight: 600;")

        def make_key_label(text: str) -> QLabel:
            label = QLabel(text)
            label.setFixedWidth(170)
            label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            return label

        def configure_value_label(label: QLabel, wrap: bool = False) -> None:
            label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            label.setWordWrap(wrap)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            label.setMinimumWidth(160)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if hasattr(label, "setTextElideMode"):
                label.setTextElideMode(Qt.ElideRight)

        value_labels = [
            self._pilot_state_value,
            self._pilot_regime_value,
            self._pilot_anchor_value,
            self._pilot_distance_value,
            self._pilot_threshold_value,
            self._pilot_position_qty_value,
            self._pilot_avg_entry_value,
            self._pilot_break_even_value,
            self._pilot_unrealized_value,
            self._pilot_orders_total_value,
            self._pilot_orders_buy_value,
            self._pilot_orders_sell_value,
            self._pilot_orders_oldest_value,
            self._pilot_orders_threshold_value,
            self._pilot_orders_stale_value,
        ]
        for value_label in value_labels:
            configure_value_label(value_label)
        configure_value_label(self._pilot_orders_warning_value, wrap=True)

        indicator_rows = [
            ("Состояние пилота:", self._pilot_state_value),
            ("Режим рынка:", self._pilot_regime_value),
            ("Якорная цена:", self._pilot_anchor_value),
            ("Отклонение от якоря %:", self._pilot_distance_value),
            ("Порог %:", self._pilot_threshold_value),
            ("Размер позиции:", self._pilot_position_qty_value),
            ("Средняя цена входа:", self._pilot_avg_entry_value),
            ("Цена безубытка:", self._pilot_break_even_value),
            ("Нереализованный PnL:", self._pilot_unrealized_value),
            ("Открытых ордеров:", self._pilot_orders_total_value),
            ("Покупка:", self._pilot_orders_buy_value),
            ("Продажа:", self._pilot_orders_sell_value),
            ("Самый старый ордер:", self._pilot_orders_oldest_value),
            ("Порог устаревания:", self._pilot_orders_threshold_value),
            ("Устаревшие:", self._pilot_orders_stale_value),
            ("Предупреждение:", self._pilot_orders_warning_value),
        ]

        for row, (key_text, value_label) in enumerate(indicator_rows):
            indicator_grid.addWidget(make_key_label(key_text), row, 0)
            indicator_grid.addWidget(value_label, row, 1)

        indicator_grid.setColumnStretch(1, 1)
        algo_pilot_layout.addWidget(metrics_widget, stretch=0)
        algo_pilot_layout.addSpacing(6)

        self._pilot_toggle_button = QPushButton("Пилот ВКЛ / ВЫКЛ")
        self._pilot_toggle_button.clicked.connect(self._handle_pilot_toggle)
        self._pilot_recenter_button = QPushButton("Перестроить сетку")
        self._pilot_recenter_button.clicked.connect(self._handle_pilot_recenter)
        self._pilot_recovery_button = QPushButton("В режим безубытка")
        self._pilot_recovery_button.clicked.connect(self._handle_pilot_recovery)
        self._pilot_flatten_button = QPushButton("Закрыть в безубыток")
        self._pilot_flatten_button.clicked.connect(self._handle_pilot_flatten)
        self._pilot_flag_stale_button = QPushButton("Пометить устаревшие")
        self._pilot_flag_stale_button.clicked.connect(self._handle_pilot_flag_stale)
        self._pilot_toggle_button.setToolTip(
            "Включить/выключить автологику пилота (пока без торговли)"
        )
        self._pilot_recenter_button.setToolTip(
            "Отменить текущие ордера и построить сетку заново от текущей цены"
        )
        self._pilot_recovery_button.setToolTip(
            "Защитный режим: цель — выйти в 0 с учётом комиссии"
        )
        self._pilot_flatten_button.setToolTip(
            "Отменить всё и закрыть позицию по цене безубытка"
        )
        self._pilot_flag_stale_button.setToolTip(
            "Проверить ордера по дрейфу цены и предложить действие"
        )
        self._pilot_auto_actions_toggle = QCheckBox("Авто-действия пилота")
        self._pilot_auto_actions_toggle.setChecked(True)
        self._pilot_auto_actions_toggle.setEnabled(False)
        self._pilot_auto_actions_toggle.setToolTip(
            "Если включено — пилот может сам выполнять Перестроить/Recovery по триггерам (с подтверждением в LIVE)"
        )
        self._pilot_auto_actions_toggle.toggled.connect(self._handle_pilot_auto_actions_toggle)
        self._pilot_stale_policy_combo = QComboBox()
        self._pilot_stale_policy_combo.addItem("Policy: NONE", StalePolicy.NONE)
        self._pilot_stale_policy_combo.addItem("Policy: RECENTER", StalePolicy.RECENTER)
        self._pilot_stale_policy_combo.addItem("Policy: CANCEL_REPLACE", StalePolicy.CANCEL_REPLACE_STALE)
        self._pilot_stale_policy_combo.setCurrentIndex(0)
        self._pilot_stale_policy_combo.setToolTip(
            "Политика реакции на устаревшие ордера (только при авто-действиях)"
        )
        self._pilot_stale_policy_combo.currentIndexChanged.connect(self._handle_pilot_stale_policy_change)
        self._pilot_confirm_dry_run_toggle = QCheckBox("Всегда спрашивать (DRY)")
        self._pilot_confirm_dry_run_toggle.setChecked(False)
        self._pilot_confirm_dry_run_toggle.setEnabled(False)
        self._pilot_confirm_dry_run_toggle.toggled.connect(self._handle_pilot_confirm_dry_run_toggle)
        self._pilot_allow_market_toggle = QCheckBox("Разрешить MARKET (закрытие)")
        self._pilot_allow_market_toggle.setChecked(False)
        self._pilot_allow_market_toggle.toggled.connect(self._handle_pilot_allow_market_toggle)
        self._pilot_action_status_label = QLabel("Статус: —")
        self._pilot_action_status_label.setStyleSheet("color: #6b7280; font-size: 11px;")

        buttons_widget = QWidget(algo_pilot_frame)
        buttons_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        pilot_buttons_layout = QVBoxLayout(buttons_widget)
        pilot_buttons_layout.setSpacing(6)
        for button in (
            self._pilot_toggle_button,
            self._pilot_recenter_button,
            self._pilot_recovery_button,
            self._pilot_flatten_button,
            self._pilot_flag_stale_button,
        ):
            button.setMinimumHeight(34)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            pilot_buttons_layout.addWidget(button)

        pilot_buttons_layout.addSpacing(6)
        pilot_buttons_layout.addWidget(self._pilot_auto_actions_toggle)
        pilot_buttons_layout.addWidget(self._pilot_stale_policy_combo)
        pilot_buttons_layout.addWidget(self._pilot_confirm_dry_run_toggle)
        pilot_buttons_layout.addWidget(self._pilot_allow_market_toggle)
        pilot_buttons_layout.addWidget(self._pilot_action_status_label)
        pilot_buttons_layout.addStretch(1)
        algo_pilot_layout.addWidget(buttons_widget, stretch=0)

        algo_scroll = QScrollArea(group)
        algo_scroll.setWidgetResizable(True)
        algo_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        algo_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        algo_scroll.setFrameShape(QFrame.NoFrame)
        algo_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        algo_container = QWidget(algo_scroll)
        algo_container_layout = QVBoxLayout(algo_container)
        algo_container_layout.setContentsMargins(0, 0, 0, 0)
        algo_container_layout.setSpacing(0)
        algo_container_layout.addWidget(algo_pilot_frame)
        algo_scroll.setWidget(algo_container)

        layout.addWidget(algo_scroll, stretch=1)
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
        self._tp_fix_button = QPushButton("Fix TP")
        self._tp_fix_button.setFixedHeight(24)
        self._tp_fix_button.setEnabled(False)
        self._tp_fix_button.clicked.connect(self._handle_fix_tp)
        self._tp_helper_label = QLabel("")
        self._tp_helper_label.setStyleSheet("color: #dc2626; font-size: 10px;")
        self._tp_helper_label.setVisible(False)
        tp_row = QHBoxLayout()
        tp_row.addWidget(self._take_profit_input)
        tp_row.addWidget(self._tp_fix_button)
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
        form.addRow(tr("take_profit"), tp_row)
        form.addRow("", self._tp_helper_label)
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
        self._strategy_controls = [
            self._budget_input,
            self._direction_combo,
            self._grid_count_input,
            self._grid_step_mode_combo,
            self._grid_step_input,
            self._manual_override_button,
            self._range_mode_combo,
            self._range_low_input,
            self._range_high_input,
            self._take_profit_input,
            self._tp_fix_button,
            self._stop_loss_toggle,
            self._stop_loss_input,
            self._max_orders_input,
            self._order_size_combo,
            self._reset_button,
            self._dry_run_toggle,
        ]
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
        header = self._orders_table.horizontalHeader()
        header.setStretchLastSection(True)
        for column in (1, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
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

        self._cancel_all_on_stop_toggle = QCheckBox(tr("cancel_all_on_stop"))
        self._cancel_all_on_stop_toggle.setChecked(True)
        layout.addWidget(self._cancel_all_on_stop_toggle)

        self._apply_pnl_style(self._pnl_label, None)
        self._refresh_orders_metrics()
        return group

    def _build_logs(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
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
        self._clear_logs_button = QPushButton("Clear")
        self._copy_logs_button = QPushButton("Copy all")
        self._dump_threads_button = QPushButton("Dump threads")
        self._toggle_logs_button = QPushButton("↕")
        self._toggle_logs_button.setFixedWidth(28)
        self._clear_logs_button.setFixedHeight(24)
        self._copy_logs_button.setFixedHeight(24)
        self._dump_threads_button.setFixedHeight(24)
        self._toggle_logs_button.setFixedHeight(24)
        self._log_filter = QComboBox()
        self._log_filter.addItems(
            [tr("log_filter_all"), tr("log_filter_orders"), tr("log_filter_errors")]
        )
        self._log_filter.currentTextChanged.connect(self._apply_log_filter)
        filter_row.addWidget(filter_label)
        filter_row.addStretch()
        filter_row.addWidget(self._clear_logs_button)
        filter_row.addWidget(self._copy_logs_button)
        filter_row.addWidget(self._dump_threads_button)
        filter_row.addWidget(self._log_filter)
        filter_row.addWidget(self._toggle_logs_button)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(200)
        self._log_view.setMinimumHeight(140)
        self._log_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._clear_logs_button.clicked.connect(self._log_view.clear)
        self._copy_logs_button.clicked.connect(self._copy_all_logs)
        self._dump_threads_button.clicked.connect(self._handle_dump_threads)
        self._toggle_logs_button.clicked.connect(self._toggle_logs_panel)
        layout.addWidget(self._trades_summary_label)
        layout.addLayout(filter_row)
        layout.addWidget(self._log_view)
        self._apply_log_filter()
        return frame

    def _handle_pilot_toggle(self) -> None:
        self._pilot_execute(PilotAction.TOGGLE)

    def _handle_pilot_recenter(self) -> None:
        self._pilot_execute(PilotAction.RECENTER)

    def _handle_pilot_recovery(self) -> None:
        self._pilot_execute(PilotAction.RECOVERY)

    def _handle_pilot_flatten(self) -> None:
        self._pilot_execute(PilotAction.FLATTEN_BE)

    def _handle_pilot_flag_stale(self) -> None:
        self._pilot_execute(PilotAction.FLAG_STALE)

    def _handle_pilot_auto_actions_toggle(self, checked: bool) -> None:
        self._pilot_auto_actions_enabled = True
        if hasattr(self, "_pilot_auto_actions_toggle"):
            self._pilot_auto_actions_toggle.blockSignals(True)
            self._pilot_auto_actions_toggle.setChecked(True)
            self._pilot_auto_actions_toggle.blockSignals(False)
        self._append_log("[NC MICRO] auto-actions FORCED", kind="INFO")

    def _handle_pilot_confirm_dry_run_toggle(self, checked: bool) -> None:
        self._pilot_confirm_dry_run = False
        if hasattr(self, "_pilot_confirm_dry_run_toggle"):
            self._pilot_confirm_dry_run_toggle.blockSignals(True)
            self._pilot_confirm_dry_run_toggle.setChecked(False)
            self._pilot_confirm_dry_run_toggle.blockSignals(False)
        self._append_log("[NC MICRO] dry-run confirm IGNORED", kind="INFO")

    def _handle_pilot_allow_market_toggle(self, checked: bool) -> None:
        self._pilot_allow_market = checked
        state = "enabled" if checked else "disabled"
        self._append_log(f"[NC MICRO] market close {state}", kind="INFO")

    def _handle_pilot_stale_policy_change(self, _: int) -> None:
        if not hasattr(self, "_pilot_stale_policy_combo"):
            return
        policy = self._pilot_stale_policy_combo.currentData()
        if isinstance(policy, StalePolicy):
            self._pilot_stale_policy = policy
            self._append_log(f"[NC MICRO] stale policy={policy.value}", kind="INFO")

    def _set_pilot_buttons_enabled(self, enabled: bool) -> None:
        for name in (
            "_pilot_toggle_button",
            "_pilot_recenter_button",
            "_pilot_recovery_button",
            "_pilot_flatten_button",
            "_pilot_flag_stale_button",
            "_pilot_auto_actions_toggle",
            "_pilot_stale_policy_combo",
            "_pilot_confirm_dry_run_toggle",
            "_pilot_allow_market_toggle",
        ):
            if hasattr(self, name):
                control = getattr(self, name)
                if name in {"_pilot_auto_actions_toggle", "_pilot_confirm_dry_run_toggle"}:
                    control.setEnabled(False)
                else:
                    control.setEnabled(enabled)

    def _set_pilot_action_in_progress(self, enabled: bool, *, status: str | None = None) -> None:
        self._pilot_action_in_progress = enabled
        self._set_pilot_buttons_enabled(not enabled)
        if hasattr(self, "_pilot_action_status_label"):
            text = status if status else "—"
            prefix = "Статус: "
            self._pilot_action_status_label.setText(f"{prefix}{text}")

    def _pilot_action_label(self, action: PilotAction) -> str:
        mapping = {
            PilotAction.TOGGLE: "Пилот ВКЛ/ВЫКЛ",
            PilotAction.RECENTER: "Перестроить сетку",
            PilotAction.RECOVERY: "В режим безубытка",
            PilotAction.FLATTEN_BE: "Закрыть в безубыток",
            PilotAction.FLAG_STALE: "Пометить устаревшие",
            PilotAction.CANCEL_REPLACE_STALE: "Отменить/восстановить устаревшие",
        }
        return mapping.get(action, action.value.lower())

    def _pilot_set_warning(self, message: str) -> None:
        self._pilot_warning_override = message
        self._pilot_warning_override_until = time_fn() + 6
        if hasattr(self, "_pilot_orders_warning_value"):
            self._pilot_orders_warning_value.setText(message)
            self._pilot_orders_warning_value.setStyleSheet("color: #dc2626; font-weight: 600;")

    def _pilot_block_action(self, action: PilotAction, reason: str) -> None:
        action_label = action.value.lower()
        self._pilot_set_warning(reason)
        self._append_log(
            f"[WARN] [NC_MICRO] action blocked action={action_label} reason={reason}",
            kind="WARN",
        )

    def _action_lock_blocked(self, requested: str) -> bool:
        if self._current_action and self._current_action != requested:
            self._append_log(
                f"[NC_MICRO] action blocked current={self._current_action} requested={requested}",
                kind="WARN",
            )
            return True
        return False

    def _pilot_trade_gate_ready(self, action: PilotAction) -> bool:
        if self._last_price is None:
            self._pilot_block_action(action, "no last price")
            return False
        if not self._symbol_tradeable:
            self._pilot_block_action(action, "symbol not trading")
            return False
        if self._live_enabled():
            if not self._engine_ready():
                self._pilot_block_action(action, "engine not ready")
                return False
            if self._trade_gate != TradeGate.TRADE_OK:
                self._pilot_block_action(action, self._trade_gate_reason())
                return False
            if not self._account_can_trade:
                self._pilot_block_action(action, "canTrade=false")
                return False
            if not self._account_client:
                self._pilot_block_action(action, "no account client")
                return False
        return True

    def _pilot_confirm_action(
        self,
        action: PilotAction,
        summary_lines: list[str],
    ) -> bool:
        return True

    def _pilot_execute(
        self,
        action: PilotAction,
        *,
        auto: bool = False,
        reason: str | None = None,
    ) -> None:
        action_key = {
            PilotAction.RECENTER: "recenter",
            PilotAction.RECOVERY: "recovery",
            PilotAction.FLATTEN_BE: "flatten",
            PilotAction.TOGGLE: "toggle",
            PilotAction.FLAG_STALE: "flag_stale",
            PilotAction.CANCEL_REPLACE_STALE: "cancel_replace_stale",
        }
        requested = action_key.get(action, action.value.lower())
        if self._action_lock_blocked(requested):
            return
        if self._pilot_action_in_progress:
            self._pilot_block_action(action, "in_progress")
            return
        if auto:
            exec_reason = reason or "auto"
            now = monotonic()
            cooldown_key = (requested, exec_reason)
            last_ts = self._pilot_auto_exec_cooldown_ts.get(cooldown_key)
            if last_ts is not None and now - last_ts < AUTO_EXEC_ACTION_COOLDOWN_SEC:
                remaining = max(0, int(AUTO_EXEC_ACTION_COOLDOWN_SEC - (now - last_ts)))
                self._append_log(
                    (
                        "[NC_MICRO] auto-exec cooldown "
                        f"action={requested} reason={exec_reason} remaining={remaining}s"
                    ),
                    kind="INFO",
                )
                return
            self._pilot_auto_exec_cooldown_ts[cooldown_key] = now
            self._append_log(
                f"[NC_MICRO] auto-exec action={requested} reason={exec_reason}",
                kind="INFO",
            )
        if action == PilotAction.TOGGLE:
            next_state = PilotState.NORMAL if self._pilot_state == PilotState.OFF else PilotState.OFF
            if next_state == PilotState.NORMAL:
                self._pilot_anchor_price = self._last_price
            else:
                self._pilot_anchor_price = None
            self._set_pilot_state(next_state)
            return
        if action == PilotAction.FLAG_STALE:
            (
                total,
                buy_count,
                sell_count,
                oldest_age_sec,
                oldest_info,
                snapshot_age_sec,
            ) = self._collect_order_metrics()
            if snapshot_age_sec is not None and snapshot_age_sec > STALE_SNAPSHOT_MAX_AGE_SEC:
                self._pilot_set_warning("Снимок ордеров устарел")
                self._maybe_log_open_orders_snapshot_stale(snapshot_age_sec)
                return
            last_seen_age = self._last_seen_age_sec(oldest_info)
            if last_seen_age is not None and last_seen_age > STALE_SNAPSHOT_MAX_AGE_SEC:
                self._pilot_set_warning("Снимок ордеров устарел")
                self._maybe_log_open_orders_snapshot_stale(last_seen_age, skip_actions=True)
                return
            oldest_value = oldest_age_sec if oldest_age_sec is not None else 0
            self._append_log(
                (
                    f"[NC MICRO] snapshot orders total={total} buy={buy_count} "
                    f"sell={sell_count} oldest={oldest_value} symbol={self._symbol}"
                ),
                kind="INFO",
            )
            if self._maybe_log_stale_warmup_skip():
                self._pilot_set_warning("STALE warmup")
                return
            mid_price, _anchor_price, range_low, range_high, settings = self._stale_context()
            stale_orders, _oldest_info, _oldest_age = self._collect_stale_orders(
                mid_price=mid_price,
                range_low=range_low,
                range_high=range_high,
                settings=settings,
            )
            if not stale_orders:
                self._pilot_set_warning("Устаревших ордеров нет")
                return
            action_candidates = self._stale_action_candidates(stale_orders)
            if not action_candidates:
                self._pilot_set_warning("Устаревших GRID ордеров нет")
                return
            self._pilot_set_warning("УСТАРЕВШИЕ ОРДЕРА")
            self._pilot_execute(PilotAction.RECENTER, auto=True, reason="flag_stale")
            return
        if action == PilotAction.CANCEL_REPLACE_STALE:
            if self._maybe_log_stale_warmup_skip():
                self._pilot_set_warning("STALE warmup")
                return
            mid_price, _anchor_price, range_low, range_high, settings = self._stale_context()
            stale_orders, oldest_info, _oldest_age = self._collect_stale_orders(
                mid_price=mid_price,
                range_low=range_low,
                range_high=range_high,
                settings=settings,
            )
            if not stale_orders:
                self._pilot_set_warning("Устаревших ордеров нет")
                return
            last_seen_age = self._last_seen_age_sec(oldest_info)
            if last_seen_age is not None and last_seen_age > STALE_SNAPSHOT_MAX_AGE_SEC:
                self._pilot_set_warning("Снимок ордеров устарел")
                self._maybe_log_open_orders_snapshot_stale(last_seen_age, skip_actions=True)
                return
            if not self._pilot_trade_gate_ready(action):
                return
            self._pilot_execute_cancel_replace_stale(stale_orders, auto=auto, reason=reason)
            return
        if action not in {PilotAction.RECENTER, PilotAction.RECOVERY, PilotAction.FLATTEN_BE}:
            return
        if action in {PilotAction.RECENTER, PilotAction.RECOVERY} and not self._kpi_allows_start():
            self._pilot_block_action(action, "kpi_invalid")
            return
        if not self._pilot_trade_gate_ready(action):
            return
        if action == PilotAction.RECENTER:
            self._pilot_execute_recenter(auto=auto)
        elif action == PilotAction.RECOVERY:
            self._pilot_execute_recovery()
        elif action == PilotAction.FLATTEN_BE:
            self._pilot_execute_flatten()

    def _pilot_execute_recenter(self, *, auto: bool = False) -> None:
        anchor_price = self._pilot_select_anchor(auto=auto)
        if anchor_price is None:
            self._pilot_block_action(PilotAction.RECENTER, "no anchor price")
            return
        plan, _settings, guard_hold = self._pilot_build_grid_plan(anchor_price, action_label="recenter")
        if guard_hold:
            self._pilot_block_action(PilotAction.RECENTER, "profit guard HOLD")
            return
        if plan is None or _settings is None:
            self._pilot_block_action(PilotAction.RECENTER, "grid plan failed")
            return
        if not plan:
            self._pilot_block_action(PilotAction.RECENTER, "empty grid plan")
            return
        allowed, reason = self._pilot_recenter_break_even_guard(plan)
        if not allowed:
            self._pilot_block_action(PilotAction.RECENTER, reason)
            return
        cancel_count = len(self._open_orders)
        buy_count = sum(1 for order in plan if order.side == "BUY")
        sell_count = sum(1 for order in plan if order.side == "SELL")
        summary = [
            f"cancel={cancel_count}",
            f"place={len(plan)} (buy={buy_count} sell={sell_count})",
            f"anchor={anchor_price:.8f}",
            f"mode={'LIVE' if self._live_enabled() else 'DRY'}",
        ]
        if not self._pilot_confirm_action(PilotAction.RECENTER, summary):
            return
        self._current_action = "recenter"
        self._set_pilot_action_in_progress(True, status="Выполняю…")
        old_anchor = self._pilot_anchor_price or anchor_price
        self._pilot_pending_anchor = anchor_price
        self._pilot_recenter_expected_min = len(plan)
        if not self._dry_run_toggle.isChecked():
            balance_snapshot = self._balance_snapshot()
            base_free = self.as_decimal(balance_snapshot.get("base_free", Decimal("0")))
            if base_free <= 0:
                self._pilot_recenter_expected_min = sum(1 for order in plan if order.side == "BUY")
        self._set_pilot_state(PilotState.RECENTERING)
        self._append_log(
            (
                "[INFO] [NC_MICRO] recenter start "
                f"old_anchor={old_anchor} new_anchor={anchor_price} "
                f"cancel_n={cancel_count} place_n={len(plan)}"
            ),
            kind="INFO",
        )
        if self._dry_run_toggle.isChecked():
            self._render_sim_orders(plan)
            self._append_log(
                f"[INFO] [NC_MICRO] recenter done place_n={len(plan)}",
                kind="INFO",
            )
            self._activate_stale_warmup()
            self._pilot_anchor_price = anchor_price
            self._pilot_pending_anchor = None
            self._pilot_recenter_expected_min = None
            self._set_pilot_state(PilotState.NORMAL)
            self._set_pilot_action_in_progress(False)
            self._current_action = None
            return
        self._pilot_pending_action = PilotAction.RECENTER
        self._pilot_pending_plan = plan
        self._pilot_cancel_orders_then_place(plan)

    def _pilot_execute_cancel_replace_stale(
        self,
        stale_orders: list[StaleOrderCandidate],
        *,
        auto: bool = False,
        reason: str | None = None,
    ) -> None:
        if not self._pilot_trade_gate_ready(PilotAction.CANCEL_REPLACE_STALE):
            return
        if not stale_orders:
            self._pilot_set_warning("Устаревших ордеров нет")
            return
        action_candidates = self._stale_action_candidates(stale_orders)
        if not action_candidates:
            self._pilot_set_warning("Устаревших GRID ордеров нет")
            return
        now_ts = time_fn()
        last_action_ts = self._pilot_stale_action_ts.get(self._symbol)
        if last_action_ts and now_ts - last_action_ts < STALE_ACTION_COOLDOWN_SEC:
            remaining = max(0, int(STALE_ACTION_COOLDOWN_SEC - (now_ts - last_action_ts)))
            self._append_log(f"[STALE] skip action cooldown={remaining}s", kind="INFO")
            return
        valid_orders = [
            candidate
            for candidate in action_candidates
            if candidate.info.price > 0 and candidate.info.qty > 0 and candidate.info.side in {"BUY", "SELL"}
        ]
        if not valid_orders:
            self._pilot_set_warning("Нет валидных устаревших ордеров")
            return
        action_order_ids = {candidate.info.order_id for candidate in valid_orders if candidate.info.order_id}
        anchor_price = self._pilot_select_anchor(auto=True)
        target_price_set: set[str] = set()
        target_plan: list[GridPlannedOrder] | None = None
        if anchor_price is not None:
            target_price_set, target_plan = self._build_target_price_set(anchor_price)
        existing_price_set = self._collect_price_set_from_orders(self._open_orders)
        if target_price_set and target_price_set == existing_price_set:
            self._append_log("[STALE] skip replace reason=prices_same", kind="INFO")
            return
        replace_count = len(target_plan) if target_plan else len(valid_orders)
        summary = [
            f"cancel={len(valid_orders)}",
            f"replace={replace_count}",
            f"mode={'LIVE' if self._live_enabled() else 'DRY'}",
            f"auto={'yes' if auto else 'no'}",
        ]
        if auto:
            exec_reason = reason or "auto"
            self._append_log(
                f"[NC_MICRO] auto-exec action=cancel_replace_stale reason={exec_reason}",
                kind="INFO",
            )
        if not self._pilot_confirm_action(PilotAction.CANCEL_REPLACE_STALE, summary):
            return
        self._pilot_stale_action_ts[self._symbol] = time_fn()
        self._set_pilot_action_in_progress(True, status="Выполняю…")
        if self._dry_run_toggle.isChecked():
            self._append_log(
                (
                    "[INFO] [NC_MICRO] stale cancel/replace dry-run "
                    f"cancel_n={len(valid_orders)} replace_n={len(valid_orders)}"
                ),
                kind="INFO",
            )
            self._activate_stale_warmup()
            self._set_pilot_action_in_progress(False)
            return

        def _cancel_and_replace() -> dict[str, Any]:
            canceled = 0
            placed = 0
            errors: list[str] = []
            open_orders_before: list[dict[str, Any]] = []
            open_orders_after: list[dict[str, Any]] = []
            open_orders_retry: list[dict[str, Any]] = []
            stuck_ids: list[str] = []
            skip_reason: str | None = None
            mid_price, _anchor_price, range_low, range_high, settings = self._stale_context()
            balance_snapshot = self._balance_snapshot()
            reserve_quote = Decimal("0")
            reserve_base = Decimal("0")
            working_balances = {
                "base_free": self.as_decimal(balance_snapshot.get("base_free", Decimal("0"))),
                "quote_free": self.as_decimal(balance_snapshot.get("quote_free", Decimal("0"))),
            }

            def _can_allocate(side: str, price: Decimal, qty: Decimal) -> bool:
                if side == "BUY":
                    required_quote = price * qty
                    return required_quote <= working_balances["quote_free"] - reserve_quote
                if side == "SELL":
                    return qty <= working_balances["base_free"] - reserve_base
                return False

            def _allocate(side: str, price: Decimal, qty: Decimal) -> None:
                if side == "BUY":
                    working_balances["quote_free"] = max(
                        Decimal("0"),
                        working_balances["quote_free"] - (price * qty),
                    )
                elif side == "SELL":
                    working_balances["base_free"] = max(
                        Decimal("0"),
                        working_balances["base_free"] - qty,
                    )

            if self._account_client:
                open_orders_before = self._account_client.get_open_orders(self._symbol)
                bot_orders_before = self._filter_bot_orders(
                    [order for order in open_orders_before if isinstance(order, dict)]
                )
                self._update_order_info_from_snapshot(bot_orders_before)
                valid_orders_snapshot = self._collect_stale_orders_from_snapshot(
                    bot_orders_before,
                    mid_price=mid_price,
                    range_low=range_low,
                    range_high=range_high,
                    settings=settings,
                )
            else:
                valid_orders_snapshot = list(valid_orders)
            if action_order_ids:
                valid_orders_snapshot = [
                    candidate
                    for candidate in valid_orders_snapshot
                    if candidate.info.order_id in action_order_ids
                ]
            if not valid_orders_snapshot:
                return {"canceled": 0, "placed": 0, "errors": errors, "stuck": []}
            if target_plan:
                planned_orders_to_place: list[GridPlannedOrder] = []
                for planned in target_plan:
                    if planned.price <= 0 or planned.qty <= 0:
                        continue
                    price = self.as_decimal(planned.price)
                    qty = self.as_decimal(planned.qty)
                    if _can_allocate(planned.side, price, qty):
                        planned_orders_to_place.append(planned)
                        _allocate(planned.side, price, qty)
                orders_to_place: list[GridPlannedOrder] = planned_orders_to_place
                if not orders_to_place:
                    skip_reason = "balance_filtered"
                    return {
                        "canceled": 0,
                        "placed": 0,
                        "errors": errors,
                        "stuck": [],
                        "skip_reason": skip_reason,
                    }
                desired_counts = {
                    "BUY": sum(1 for planned in orders_to_place if planned.side == "BUY"),
                    "SELL": sum(1 for planned in orders_to_place if planned.side == "SELL"),
                }
                cancel_candidates = []
                remaining = dict(desired_counts)
                for candidate in valid_orders_snapshot:
                    side = candidate.info.side
                    if remaining.get(side, 0) <= 0:
                        continue
                    cancel_candidates.append(candidate)
                    remaining[side] = remaining.get(side, 0) - 1
            else:
                filtered_candidates: list[StaleOrderCandidate] = []
                for candidate in valid_orders_snapshot:
                    price = self.as_decimal(candidate.info.price)
                    qty = self.as_decimal(candidate.info.qty)
                    if price <= 0 or qty <= 0:
                        continue
                    if _can_allocate(candidate.info.side, price, qty):
                        filtered_candidates.append(candidate)
                        _allocate(candidate.info.side, price, qty)
                if not filtered_candidates:
                    skip_reason = "balance_filtered"
                    return {
                        "canceled": 0,
                        "placed": 0,
                        "errors": errors,
                        "stuck": [],
                        "skip_reason": skip_reason,
                    }
                cancel_candidates = filtered_candidates
            ignore_keys = {
                self._order_key(
                    candidate.info.side,
                    self.as_decimal(candidate.info.price),
                    self.as_decimal(candidate.info.qty),
                )
                for candidate in cancel_candidates
            }
            for candidate in cancel_candidates:
                order_id = candidate.info.order_id
                ok, error = self._cancel_order_idempotent(order_id)
                if ok:
                    canceled += 1
                    self._pilot_stale_handled[order_id] = time_fn()
                if error:
                    errors.append(error)
            if self._account_client:
                open_orders_after = self._account_client.get_open_orders(self._symbol)
                bot_orders_after = self._filter_bot_orders(
                    [order for order in open_orders_after if isinstance(order, dict)]
                )
                stale_ids = {candidate.info.order_id for candidate in cancel_candidates}
                still_open = [
                    order
                    for order in bot_orders_after
                    if str(order.get("orderId", "")) in stale_ids
                ]
                for order in still_open:
                    order_id = str(order.get("orderId", ""))
                    if not order_id:
                        continue
                    ok, error = self._cancel_order_idempotent(order_id)
                    if ok:
                        canceled += 1
                        self._pilot_stale_handled[order_id] = time_fn()
                    if error:
                        errors.append(error)
                open_orders_retry = self._account_client.get_open_orders(self._symbol)
                bot_orders_retry = self._filter_bot_orders(
                    [order for order in open_orders_retry if isinstance(order, dict)]
                )
                stuck_ids = [
                    str(order.get("orderId", ""))
                    for order in bot_orders_retry
                    if str(order.get("orderId", "")) in stale_ids
                ]
                self._rebuild_registry_from_open_orders(bot_orders_retry)
            else:
                bot_orders_retry = []
            open_keys = {
                key
                for order in (bot_orders_retry or bot_orders_after)
                if isinstance(order, dict)
                for key in (self._order_key_from_order(order),)
                if key
            }
            if target_plan:
                for planned in orders_to_place:
                    side = planned.side
                    price = self.as_decimal(planned.price)
                    qty = self.as_decimal(planned.qty)
                    order_key = self._order_key(side, price, qty)
                    if order_key in open_keys:
                        continue
                    client_id = self._next_client_order_id("STALE")
                    response, error, _status = self._place_limit(
                        side,
                        price,
                        qty,
                        client_id,
                        reason="stale",
                        ignore_keys=ignore_keys,
                        skip_open_order_duplicate=True,
                    )
                    if response:
                        placed += 1
                    if error:
                        errors.append(error)
                    self._sleep_ms(200)
            else:
                for candidate in cancel_candidates:
                    side = candidate.info.side
                    price = self.as_decimal(candidate.info.price)
                    qty = self.as_decimal(candidate.info.qty)
                    order_key = self._order_key(side, price, qty)
                    if candidate.info.order_id in stuck_ids:
                        continue
                    if order_key in open_keys:
                        continue
                    client_id = self._next_client_order_id("STALE")
                    response, error, _status = self._place_limit(
                        side,
                        price,
                        qty,
                        client_id,
                        reason="stale",
                        ignore_order_id=candidate.info.order_id,
                        ignore_keys=ignore_keys,
                        skip_open_order_duplicate=True,
                    )
                    if response:
                        placed += 1
                    if error:
                        errors.append(error)
                    self._sleep_ms(200)
            return {
                "canceled": canceled,
                "placed": placed,
                "errors": errors,
                "stuck": stuck_ids,
                "skip_reason": skip_reason,
            }

        worker = _Worker(_cancel_and_replace, self._can_emit_worker_results)
        worker.signals.success.connect(self._handle_pilot_cancel_replace_stale)
        worker.signals.error.connect(self._handle_pilot_action_error)
        self._thread_pool.start(worker)

    def _pilot_execute_recovery(self) -> None:
        position_qty = self._pilot_position_qty()
        be_price = self._pilot_break_even_price()
        if position_qty <= 0 or be_price is None:
            return
        summary = [
            f"cancel={len(self._open_orders)}",
            f"qty={self._format_balance_decimal(position_qty)}",
            f"be={self._format_balance_decimal(be_price) if be_price else '—'}",
            f"mode={'LIVE' if self._live_enabled() else 'DRY'}",
        ]
        if not self._pilot_confirm_action(PilotAction.RECOVERY, summary):
            return
        self._set_pilot_state(PilotState.RECOVERY)
        self._append_log(
            f"[NC MICRO] recovery mode enabled symbol={self._symbol}",
            kind="INFO",
        )
        self._current_action = "recovery"
        self._set_pilot_action_in_progress(True, status="Выполняю…")
        self._pilot_place_break_even_order(
            be_price=be_price,
            qty=position_qty,
            action_label="recovery",
            action=PilotAction.RECOVERY,
        )

    def _pilot_execute_flatten(self) -> None:
        position_qty = self._pilot_position_qty()
        be_price = self._pilot_break_even_price()
        if position_qty <= 0 or be_price is None:
            return
        summary = [
            f"cancel={len(self._open_orders)}",
            f"qty={self._format_balance_decimal(position_qty)}",
            f"be={self._format_balance_decimal(be_price) if be_price else '—'}",
            f"mode={'LIVE' if self._live_enabled() else 'DRY'}",
        ]
        if self._pilot_allow_market:
            summary.append("market=enabled")
        if not self._pilot_confirm_action(PilotAction.FLATTEN_BE, summary):
            return
        self._append_log(
            f"[NC MICRO] flatten to BE requested symbol={self._symbol}",
            kind="INFO",
        )
        if self._pilot_allow_market:
            self._append_log(
                "[NC MICRO] market close requested but not supported, using BE limit",
                kind="WARN",
            )
        self._current_action = "flatten"
        self._set_pilot_action_in_progress(True, status="Выполняю…")
        self._pilot_place_break_even_order(
            be_price=be_price,
            qty=position_qty,
            action_label="flatten",
            action=PilotAction.FLATTEN_BE,
        )

    def _pilot_place_break_even_order(
        self,
        *,
        be_price: Decimal,
        qty: Decimal,
        action_label: str,
        action: PilotAction,
    ) -> None:
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        qty_rounded = self.q_qty(qty, step)
        price_rounded = self.q_price(be_price, tick)
        if qty_rounded <= 0 or price_rounded <= 0:
            self._pilot_block_action(action, "invalid be order")
            self._set_pilot_action_in_progress(False)
            return
        cancel_count = len(self._open_orders)
        if self._dry_run_toggle.isChecked():
            self._render_sim_orders(
                [
                    GridPlannedOrder(
                        side="SELL",
                        price=float(price_rounded),
                        qty=float(qty_rounded),
                        level_index=0,
                    )
                ]
            )
            self._append_log(
                (
                    f"[INFO] [NC_MICRO] {action_label} "
                    f"dry-run be_price={price_rounded} qty={qty_rounded}"
                ),
                kind="INFO",
            )
            self._set_pilot_action_in_progress(False)
            return
        self._pilot_pending_action = PilotAction.RECOVERY if action_label == "recovery" else PilotAction.FLATTEN_BE
        client_order_id = self._limit_client_order_id(
            f"BBOT_LAS_v1_{self._symbol}_PILOT_{action_label.upper()}"
        )

        def _cancel_and_place() -> dict[str, Any]:
            errors: list[str] = []
            if self._account_client:
                for order in self._open_orders:
                    order_id = str(order.get("orderId", ""))
                    if not order_id:
                        continue
                    ok, error = self._cancel_order_idempotent(order_id)
                    if ok:
                        continue
                    if error:
                        errors.append(error)
                open_orders_after = self._account_client.get_open_orders(self._symbol)
                bot_orders = self._filter_bot_orders(
                    [order for order in open_orders_after if isinstance(order, dict)]
                )
                self._rebuild_registry_from_open_orders(bot_orders)
            response = None
            error = None
            status = ""
            if self._account_client:
                response, error, status = self._place_limit(
                    "SELL",
                    price_rounded,
                    qty_rounded,
                    client_order_id,
                    reason=f"pilot_{action_label}",
                    skip_open_order_duplicate=False,
                    skip_registry=False,
                )
            if error and status != "skip_duplicate_exchange":
                errors.append(error)
            return {"response": response, "errors": errors, "cancel_n": cancel_count}

        worker = _Worker(_cancel_and_place, self._can_emit_worker_results)
        worker.signals.success.connect(self._handle_pilot_break_even_result)
        worker.signals.error.connect(self._handle_pilot_action_error)
        self._thread_pool.start(worker)

    def _pilot_build_grid_plan(
        self,
        anchor_price: float,
        *,
        action_label: str,
    ) -> tuple[list[GridPlannedOrder] | None, GridSettingsState | None, bool]:
        snapshot = self._collect_strategy_snapshot()
        settings = self._resolve_start_settings(snapshot)
        self._apply_auto_clamps(settings, anchor_price)
        guard_decision = self._apply_profit_guard(
            settings,
            anchor_price,
            action_label=action_label,
            update_ui=False,
        )
        if guard_decision == "HOLD":
            return None, settings, True
        try:
            planned = self._grid_engine.start(settings, anchor_price, self._exchange_rules)
        except ValueError as exc:
            self._append_log(f"[NC MICRO] recenter plan failed: {exc}", kind="WARN")
            return None, None, False
        if not self._dry_run_toggle.isChecked():
            balance_snapshot = self._balance_snapshot()
            base_free = self.as_decimal(balance_snapshot.get("base_free", Decimal("0")))
            planned = self._limit_sell_plan_by_balance(planned, base_free)
        planned = planned[: settings.max_active_orders]
        return planned, settings, False

    def _pilot_select_anchor(self, *, auto: bool = False) -> float | None:
        if self._last_price is None:
            return None
        if auto:
            return self._last_price
        if self._pilot_anchor_price is None or self._pilot_anchor_price == self._last_price:
            return self._last_price
        return self._last_price

    def _pilot_recenter_break_even_guard(self, plan: list[GridPlannedOrder]) -> tuple[bool, str]:
        position_qty = self._pilot_position_qty()
        if position_qty == 0:
            return True, ""
        break_even = self._pilot_break_even_price()
        if break_even is None:
            return True, ""
        be_price = float(break_even)
        if position_qty > 0:
            violations = [order for order in plan if order.side == "SELL" and order.price < be_price]
            if violations:
                return False, f"sell_below_be be={be_price:.8f} n={len(violations)}"
        else:
            violations = [order for order in plan if order.side == "BUY" and order.price > be_price]
            if violations:
                return False, f"buy_above_be be={be_price:.8f} n={len(violations)}"
        return True, ""

    def _pilot_cancel_orders_then_place(self, plan: list[GridPlannedOrder]) -> None:
        planned_cancel = len(self._open_orders)
        expected_min = self._pilot_recenter_expected_min or len(plan)

        def _cancel() -> dict[str, Any]:
            errors: list[str] = []
            canceled = 0
            if self._account_client:
                for order in self._open_orders:
                    order_id = str(order.get("orderId", ""))
                    if not order_id:
                        continue
                    ok, error = self._cancel_order_idempotent(order_id)
                    if ok:
                        canceled += 1
                    if error:
                        errors.append(error)
            open_orders_after: list[dict[str, Any]] = []
            if self._account_client:
                deadline = monotonic() + 5.0
                while monotonic() < deadline:
                    open_orders_after = self._account_client.get_open_orders(self._symbol)
                    bot_orders = self._filter_bot_orders(
                        [order for order in open_orders_after if isinstance(order, dict)]
                    )
                    if not bot_orders:
                        break
                    self._sleep_ms(500)
            return {
                "errors": errors,
                "canceled": canceled,
                "planned": planned_cancel,
                "open_orders_after": open_orders_after,
                "expected_min": expected_min,
            }

        worker = _Worker(_cancel, self._can_emit_worker_results)
        worker.signals.success.connect(lambda result, latency: self._handle_pilot_recenter_cancel(result, latency, plan))
        worker.signals.error.connect(self._handle_pilot_action_error)
        self._thread_pool.start(worker)

    def _handle_pilot_recenter_cancel(self, result: object, latency_ms: int, plan: list[GridPlannedOrder]) -> None:
        _ = latency_ms
        if isinstance(result, dict):
            errors = result.get("errors", [])
            if isinstance(errors, list):
                for message in errors:
                    if message:
                        self._append_log(str(message), kind="WARN")
            canceled = int(result.get("canceled", 0) or 0)
            planned = int(result.get("planned", 0) or 0)
            self._append_log(
                f"[NC MICRO] recenter cancel done canceled={canceled} planned={planned}",
                kind="INFO",
            )
            open_orders_after = result.get("open_orders_after", [])
            if isinstance(open_orders_after, list):
                open_count = len([order for order in open_orders_after if isinstance(order, dict)])
                if open_count:
                    self._append_log(
                        f"[NC MICRO] recenter cancel wait timeout open_orders={open_count}",
                        kind="WARN",
                    )
                self._open_orders_all = [item for item in open_orders_after if isinstance(item, dict)]
                self._open_orders = self._filter_bot_orders(self._open_orders_all)
                self._open_orders_map = {
                    str(order.get("orderId", "")): order
                    for order in self._open_orders
                    if str(order.get("orderId", ""))
                }
                self._update_order_info_from_snapshot(self._open_orders)
                if open_count:
                    self._rebuild_registry_from_open_orders(self._open_orders)
                else:
                    self._clear_local_order_registry()
            else:
                self._rebuild_registry_from_open_orders([])
        self._place_live_orders(plan)

    def _pilot_wait_for_recenter_open_orders(self, expected_min: int) -> None:
        if not self._account_client:
            self._finalize_pilot_recenter(met=False, open_orders=[])
            return
        if expected_min <= 0:
            self._finalize_pilot_recenter(met=True, open_orders=list(self._open_orders))
            return

        def _poll() -> dict[str, Any]:
            deadline = monotonic() + 5.0
            open_orders: list[dict[str, Any]] = []
            met = False
            while monotonic() < deadline:
                open_orders = self._account_client.get_open_orders(self._symbol)
                bot_orders = self._filter_bot_orders(
                    [order for order in open_orders if isinstance(order, dict)]
                )
                if len(bot_orders) >= expected_min:
                    met = True
                    break
                self._sleep_ms(500)
            return {"open_orders": open_orders, "met": met, "expected_min": expected_min}

        worker = _Worker(_poll, self._can_emit_worker_results)
        worker.signals.success.connect(self._handle_pilot_recenter_open_orders)
        worker.signals.error.connect(self._handle_pilot_action_error)
        self._thread_pool.start(worker)

    def _handle_pilot_recenter_open_orders(self, result: object, latency_ms: int) -> None:
        _ = latency_ms
        if not isinstance(result, dict):
            self._handle_pilot_action_error("Unexpected recenter open_orders response")
            return
        open_orders = result.get("open_orders", [])
        met = bool(result.get("met", False))
        expected_min = int(result.get("expected_min", 0) or 0)
        if isinstance(open_orders, list):
            self._handle_open_orders(open_orders, latency_ms)
        if not met:
            self._append_log(
                f"[NC MICRO] recenter open_orders wait timeout expected_min={expected_min}",
                kind="WARN",
            )
        self._finalize_pilot_recenter(met=met, open_orders=open_orders if isinstance(open_orders, list) else [])

    def _finalize_pilot_recenter(self, *, met: bool, open_orders: list[dict[str, Any]]) -> None:
        _ = met
        if open_orders is None:
            open_orders = []
        if self._pilot_pending_anchor is not None:
            self._pilot_anchor_price = self._pilot_pending_anchor
        self._pilot_pending_anchor = None
        self._pilot_recenter_expected_min = None
        self._set_pilot_state(PilotState.NORMAL)
        self._append_log(
            f"[INFO] [NC_MICRO] recenter done open_orders={len(open_orders)}",
            kind="INFO",
        )
        self._activate_stale_warmup()
        self._set_pilot_action_in_progress(False)
        self._current_action = None
        self._pilot_pending_action = None
        self._pilot_pending_plan = None

    def _handle_pilot_cancel_replace_stale(self, result: object, latency_ms: int) -> None:
        _ = latency_ms
        if not isinstance(result, dict):
            self._handle_pilot_action_error("Unexpected stale cancel/replace response")
            return
        canceled = int(result.get("canceled", 0) or 0)
        placed = int(result.get("placed", 0) or 0)
        errors = result.get("errors", [])
        stuck = result.get("stuck", [])
        skip_reason = result.get("skip_reason")
        if isinstance(errors, list):
            for message in errors:
                if message:
                    self._append_log(str(message), kind="WARN")
        if isinstance(stuck, list) and stuck:
            self._pilot_auto_actions_paused_until = monotonic() + STALE_AUTO_ACTION_PAUSE_SEC
            for order_id in stuck:
                if order_id:
                    self._append_log(
                        f"[STALE] stuck orderId={order_id} -> auto_actions paused {STALE_AUTO_ACTION_PAUSE_SEC}s",
                        kind="WARN",
                    )
        if skip_reason:
            self._append_log(
                f"[INFO] [NC_MICRO] stale cancel/replace skipped reason={skip_reason}",
                kind="INFO",
            )
            self._set_pilot_action_in_progress(False)
            return
        self._append_log(
            f"[INFO] [NC_MICRO] stale cancel/replace done cancel_n={canceled} place_n={placed}",
            kind="INFO",
        )
        self._activate_stale_warmup()
        self._refresh_open_orders(force=True)
        self._set_pilot_action_in_progress(False)

    def _handle_pilot_break_even_result(self, result: object, latency_ms: int) -> None:
        _ = latency_ms
        if not isinstance(result, dict):
            self._handle_pilot_action_error("Unexpected pilot response")
            return
        response = result.get("response")
        errors = result.get("errors", [])
        cancel_n = int(result.get("cancel_n", 0) or 0)
        if isinstance(errors, list):
            for message in errors:
                if message:
                    self._append_log(str(message), kind="WARN")
        if isinstance(response, dict):
            order_id = str(response.get("orderId", ""))
            side = str(response.get("side", "")).upper()
            price = str(response.get("price", ""))
            qty = str(response.get("origQty", ""))
            self._append_log(
                (
                    f"[NC MICRO] be order placed cancel={cancel_n} "
                    f"orderId={order_id} side={side} price={price} qty={qty}"
                ),
                kind="INFO",
            )
        self._refresh_open_orders(force=True)
        self._set_pilot_action_in_progress(False)
        self._current_action = None
        self._pilot_pending_action = None
        self._pilot_pending_plan = None

    def _handle_pilot_action_error(self, message: str) -> None:
        self._append_log(f"[NC MICRO] action error: {message}", kind="WARN")
        self._set_pilot_action_in_progress(False)
        self._current_action = None
        if self._pilot_state == PilotState.RECENTERING:
            self._set_pilot_state(PilotState.NORMAL)
            self._pilot_pending_anchor = None
            self._pilot_recenter_expected_min = None
        self._pilot_pending_action = None
        self._pilot_pending_plan = None

    def _reset_position_state(self) -> None:
        self._position_fees_paid_quote = 0.0
        self._position_state = PositionState(
            position_qty=0.0,
            avg_entry_price=None,
            realized_pnl_quote=0.0,
            fees_paid_quote=0.0,
            break_even_price=None,
        )

    def _rebuild_position_state(self) -> PositionState:
        total_qty = sum(Decimal(str(lot.qty)) for lot in self._base_lots)
        realized_pnl = float(self._realized_pnl)
        fees_paid = float(self._position_fees_paid_quote)
        if total_qty <= 0:
            return PositionState(
                position_qty=0.0,
                avg_entry_price=None,
                realized_pnl_quote=realized_pnl,
                fees_paid_quote=fees_paid,
                break_even_price=None,
            )
        total_cost = sum(
            Decimal(str(lot.qty)) * Decimal(str(lot.cost_per_unit)) for lot in self._base_lots
        )
        avg_entry = total_cost / total_qty if total_qty > 0 else Decimal("0")
        break_even_paid = (total_cost + self.as_decimal(fees_paid)) / total_qty
        maker_pct, _taker_pct, _used_default = self._profit_guard_fee_inputs()
        exit_fee_pct = maker_pct + PROFIT_GUARD_EXIT_BUFFER_PCT
        break_even_exit = avg_entry * (Decimal("1") + self.as_decimal(exit_fee_pct) / Decimal("100"))
        break_even = max(break_even_paid, break_even_exit)
        return PositionState(
            position_qty=float(total_qty),
            avg_entry_price=float(avg_entry),
            realized_pnl_quote=realized_pnl,
            fees_paid_quote=fees_paid,
            break_even_price=float(break_even),
        )

    def _pilot_position_state(self) -> PositionState:
        self._position_state = self._rebuild_position_state()
        return self._position_state

    def _pilot_position_qty(self) -> Decimal:
        state = self._pilot_position_state()
        return self.as_decimal(state.position_qty)

    def _pilot_average_entry_price(self) -> Decimal | None:
        state = self._pilot_position_state()
        if state.avg_entry_price is None:
            return None
        return self.as_decimal(state.avg_entry_price)

    def _pilot_break_even_price(self) -> Decimal | None:
        state = self._pilot_position_state()
        if state.break_even_price is None:
            return None
        return self.as_decimal(state.break_even_price)

    def _set_pilot_state(self, state: PilotState) -> None:
        if self._pilot_state == state:
            return
        self._pilot_state = state
        self._append_log(
            f"[NC MICRO] state={state.value} symbol={self._symbol}",
            kind="INFO",
        )

    @staticmethod
    def _pilot_state_label(state: PilotState) -> str:
        mapping = {
            PilotState.OFF: "Выключен",
            PilotState.NORMAL: "Нормальный",
            PilotState.RECENTERING: "Перестроение",
            PilotState.RECOVERY: "Безубыток",
            PilotState.PAUSED_BY_RISK: "Пауза риска",
        }
        return mapping.get(state, state.value)

    def _update_pilot_panel(self) -> None:
        try:
            if not hasattr(self, "_pilot_state_value"):
                return
            self._pilot_state_value.setText(self._pilot_state_label(self._pilot_state))
            regime = "Диапазон"
            if len(self._price_history) >= 10:
                first_price = self._price_history[0]
                last_price = self._price_history[-1]
                if first_price:
                    if last_price > first_price * 1.001:
                        regime = "Тренд вверх"
                    elif last_price < first_price * 0.999:
                        regime = "Тренд вниз"
            self._pilot_regime_value.setText(regime)

            tick = self._rule_decimal(self._exchange_rules.get("tick"))
            step = self._rule_decimal(self._exchange_rules.get("step"))
            anchor_text = "—"
            if self._pilot_anchor_price is not None:
                anchor_text = self.fmt_price(self.as_decimal(self._pilot_anchor_price), tick)
            self._pilot_anchor_value.setText(anchor_text)

            distance_text = "—"
            distance_pct: float | None = None
            if self._pilot_anchor_price and self._last_price is not None:
                distance_pct = abs(self._last_price - self._pilot_anchor_price) / self._pilot_anchor_price * 100
                distance_text = f"{distance_pct:.2f} %"
            self._pilot_distance_value.setText(distance_text)

            if (
                self._pilot_state == PilotState.NORMAL
                and distance_pct is not None
                and distance_pct > RECENTER_THRESHOLD_PCT
            ):
                self._append_log(
                    f"[NC MICRO] auto-recenter trigger distance={distance_pct:.2f}% symbol={self._symbol}",
                    kind="INFO",
                )
                if self._pilot_auto_actions_enabled:
                    self._pilot_execute(PilotAction.RECENTER, auto=True, reason="distance")
                else:
                    self._pilot_set_warning("Рекомендуется перестроить")

            position_state = self._pilot_position_state()
            position_qty = self.as_decimal(position_state.position_qty)
            position_qty_text = self.fmt_qty(position_qty, step) if position_qty > 0 else "—"
            self._pilot_position_qty_value.setText(position_qty_text)

            avg_entry = self._pilot_average_entry_price()
            avg_entry_text = "—"
            if avg_entry is not None and avg_entry > 0:
                avg_entry_text = self.fmt_price(avg_entry, tick)
            self._pilot_avg_entry_value.setText(avg_entry_text)

            break_even = self._pilot_break_even_price()
            break_even_text = "—"
            if break_even is not None and break_even > 0:
                break_even_text = self.fmt_price(break_even, tick)
            self._pilot_break_even_value.setText(break_even_text)
            if break_even is None or position_state.position_qty <= 0:
                self._pilot_break_even_value.setToolTip("")
            else:
                self._pilot_break_even_value.setToolTip("")
            has_position = position_qty > 0
            be_actions_enabled = has_position and break_even is not None and break_even > 0
            if hasattr(self, "_pilot_recovery_button"):
                self._pilot_recovery_button.setEnabled(be_actions_enabled and not self._pilot_action_in_progress)
            if hasattr(self, "_pilot_flatten_button"):
                self._pilot_flatten_button.setEnabled(be_actions_enabled and not self._pilot_action_in_progress)

            pnl_text = "—"
            if self._last_price is not None and self._base_asset:
                base_free = self._balances.get(self._base_asset, (0.0, 0.0))[0]
                pnl_text = self.fmt_price(self.as_decimal(base_free), None)
            self._pilot_unrealized_value.setText(pnl_text)
        except Exception:
            self._logger.exception("[NC_MICRO][CRASH] exception in _update_pilot_panel")
            self._notify_crash("_update_pilot_panel")
            return

    def _collect_order_metrics(self) -> tuple[int, int, int, int | None, OrderInfo | None, int | None]:
        total = self._orders_table.rowCount() if hasattr(self, "_orders_table") else 0
        buy_count = 0
        sell_count = 0
        for row in range(total):
            side_item = self._orders_table.item(row, 1)
            side_text = side_item.text().upper() if side_item else ""
            if side_text == "BUY":
                buy_count += 1
            elif side_text == "SELL":
                sell_count += 1
        oldest_age_sec, oldest_info = self._calculate_oldest_order_age_sec()
        snapshot_age_sec = self._snapshot_age_sec()
        return total, buy_count, sell_count, oldest_age_sec, oldest_info, snapshot_age_sec

    def _snapshot_age_sec(self) -> int | None:
        if self._open_orders_snapshot_ts is None:
            return None
        return int(max(time_fn() - self._open_orders_snapshot_ts, 0))

    @staticmethod
    def _last_seen_age_sec(info: OrderInfo | None) -> int | None:
        if info is None:
            return None
        return int(max(time_fn() - info.last_seen_ts, 0))

    def _stale_drift_pct(self) -> float:
        symbol = self._symbol.replace("/", "").upper()
        if symbol == "BTCUSDT":
            return STALE_DRIFT_PCT_DEFAULT
        if symbol in {"EURIUSDT", "USDCUSDT"}:
            return STALE_DRIFT_PCT_STABLE
        return STALE_DRIFT_PCT_DEFAULT

    def _stale_mid_price(self) -> float | None:
        best_bid, best_ask, _source = self._resolve_book_bid_ask()
        if best_bid is None or best_ask is None:
            return None
        mid = (best_bid + best_ask) / 2
        return mid if mid > 0 else None

    def _stale_range_context(
        self, anchor_price: float
    ) -> tuple[float | None, float | None, GridSettingsState | None]:
        if anchor_price <= 0:
            return None, None, None
        snapshot = self._collect_strategy_snapshot()
        settings = self._resolve_start_settings(snapshot)
        self._apply_auto_clamps(settings, anchor_price)
        range_low = settings.range_low_pct
        range_high = settings.range_high_pct
        if range_low <= 0 or range_high <= 0:
            return None, None, settings
        low = anchor_price * (1 - range_low / 100.0)
        high = anchor_price * (1 + range_high / 100.0)
        return low, high, settings

    def _stale_context(
        self,
    ) -> tuple[float | None, float | None, float | None, float | None, GridSettingsState | None]:
        mid = self._stale_mid_price()
        anchor_price = self._pilot_anchor_price or self._last_price
        range_low, range_high = None, None
        settings = None
        if anchor_price is not None:
            range_low, range_high, settings = self._stale_range_context(anchor_price)
        return mid, anchor_price, range_low, range_high, settings

    @staticmethod
    def _stale_range_limit_pct(
        mid_price: float | None, range_low: float | None, range_high: float | None
    ) -> float | None:
        if mid_price is None or mid_price <= 0:
            return None
        limits: list[float] = []
        if range_low is not None and range_low > 0:
            limits.append(abs(mid_price - range_low) / mid_price * 100)
        if range_high is not None and range_high > 0:
            limits.append(abs(range_high - mid_price) / mid_price * 100)
        if not limits:
            return None
        return max(limits)

    def _stale_grid_threshold_pct(self, settings: GridSettingsState | None) -> float:
        if settings is None:
            return self._stale_drift_pct()
        step_pct = max(float(settings.grid_step_pct or 0.0), 0.0)
        range_low_pct = max(float(settings.range_low_pct or 0.0), 0.0)
        range_high_pct = max(float(settings.range_high_pct or 0.0), 0.0)
        buffer_pct = max(step_pct, STALE_BUFFER_MIN_PCT)
        threshold = max(range_low_pct, range_high_pct) + buffer_pct
        if threshold <= 0:
            return self._stale_drift_pct()
        return threshold

    def _stale_threshold_text(self, settings: GridSettingsState | None) -> str:
        drift_pct = self._stale_grid_threshold_pct(settings)
        return f"дрейф>{drift_pct:.2f}% или вне диапазона"

    def _stale_tp_profitability(self, settings: GridSettingsState | None) -> dict[str, float | bool] | None:
        if settings is None:
            return None
        tp_pct = settings.take_profit_pct or settings.grid_step_pct
        if tp_pct <= 0:
            return None
        return self._evaluate_tp_profitability(tp_pct)

    def _stale_action_candidates(
        self, stale_orders: list[StaleOrderCandidate]
    ) -> list[StaleOrderCandidate]:
        return [
            candidate
            for candidate in stale_orders
            if self._order_registry_type_from_client_id(
                str(candidate.order.get("clientOrderId", ""))
            )
            != "TP"
        ]

    def _activate_stale_warmup(self) -> None:
        self._pilot_stale_warmup_until = monotonic() + STALE_WARMUP_SEC
        self._pilot_stale_warmup_last_log_ts = None

    def _stale_warmup_remaining(self) -> int | None:
        if self._pilot_stale_warmup_until is None:
            return None
        remaining = int(self._pilot_stale_warmup_until - monotonic())
        if remaining <= 0:
            self._pilot_stale_warmup_until = None
            return None
        return remaining

    def _maybe_log_stale_warmup_skip(self) -> bool:
        remaining = self._stale_warmup_remaining()
        if remaining is None:
            return False
        now = monotonic()
        last_log = self._pilot_stale_warmup_last_log_ts
        if last_log is None or now - last_log >= STALE_ORDER_LOG_DEDUP_SEC:
            self._append_log(f"[STALE] warmup skip remaining={remaining}s", kind="INFO")
            self._pilot_stale_warmup_last_log_ts = now
        return True

    def _should_log_stale_order(self, order_id: str, reason: str, now: float) -> bool:
        if not order_id:
            return False
        key = (order_id, reason)
        last_log = self._pilot_stale_order_log_ts.get(key)
        if last_log is not None and now - last_log < STALE_ORDER_LOG_DEDUP_SEC:
            return False
        self._pilot_stale_order_log_ts[key] = now
        return True

    def _should_log_stale_check(self, order_id: str, reason: str, now: float) -> bool:
        if not order_id:
            return False
        key = (order_id, reason)
        last_log = self._pilot_stale_check_log_ts.get(key)
        if last_log is not None and now - last_log < STALE_ORDER_LOG_DEDUP_SEC:
            return False
        self._pilot_stale_check_log_ts[key] = now
        return True

    def _price_key(self, side: str, price: Decimal) -> str:
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        price_key = self._format_decimal(self.q_price(price, tick), tick)
        return f"{self._symbol}|{side}|{price_key}"

    def _price_key_from_order(self, order: dict[str, Any]) -> str | None:
        side = str(order.get("side", "")).upper()
        if not side:
            return None
        price = self._coerce_float(str(order.get("price", ""))) or 0.0
        if price <= 0:
            return None
        return self._price_key(side, self.as_decimal(price))

    def _collect_price_set_from_orders(self, orders: list[dict[str, Any]]) -> set[str]:
        return {
            key
            for order in orders
            if isinstance(order, dict)
            for key in (self._price_key_from_order(order),)
            if key
        }

    def _build_target_price_set(
        self, anchor_price: float
    ) -> tuple[set[str], list[GridPlannedOrder] | None]:
        plan, _settings, guard_hold = self._pilot_build_grid_plan(anchor_price, action_label="stale")
        if guard_hold or not plan:
            return set(), plan
        return {
            self._price_key(order.side, self.as_decimal(order.price))
            for order in plan
            if order.price > 0
        }, plan

    def _calculate_oldest_order_age_sec(self) -> tuple[int | None, OrderInfo | None]:
        if not self._open_orders:
            return None, None
        now = time_fn()
        oldest_age_sec: int | None = None
        oldest_info: OrderInfo | None = None
        for order in self._open_orders:
            order_id = str(order.get("orderId", ""))
            if not order_id:
                continue
            info = self._order_info_map.get(order_id)
            if not info:
                continue
            age_sec = int(max(now - info.created_ts, 0))
            if oldest_age_sec is None or age_sec > oldest_age_sec:
                oldest_age_sec = age_sec
                oldest_info = info
        return oldest_age_sec, oldest_info

    def _collect_stale_orders(
        self,
        *,
        mid_price: float | None,
        range_low: float | None,
        range_high: float | None,
        settings: GridSettingsState | None,
    ) -> tuple[list[StaleOrderCandidate], OrderInfo | None, int | None]:
        if not self._open_orders:
            return [], None, None
        now = time_fn()
        now_mono = monotonic()
        drift_threshold = self._stale_grid_threshold_pct(settings)
        stale: list[StaleOrderCandidate] = []
        oldest_info: OrderInfo | None = None
        oldest_age_sec: int | None = None
        for order in self._open_orders:
            order_id = str(order.get("orderId", ""))
            if not order_id:
                continue
            if order_id in self._pilot_pending_cancel_ids:
                continue
            info = self._order_info_map.get(order_id)
            if not info:
                continue
            age_sec = int(max(now - info.created_ts, 0))
            if oldest_age_sec is None or age_sec > oldest_age_sec:
                oldest_age_sec = age_sec
                oldest_info = info
            order_type = self._order_registry_type_from_client_id(str(order.get("clientOrderId", "")))
            dist_pct = None
            range_limit_pct = self._stale_range_limit_pct(mid_price, range_low, range_high)
            excess_pct = None
            drift_stale = False
            if mid_price is not None and mid_price > 0 and info.price > 0:
                dist_pct = abs(info.price - mid_price) / mid_price * 100
                if range_limit_pct is not None:
                    excess_pct = max(0.0, dist_pct - range_limit_pct)
                if order_type != "TP":
                    drift_stale = dist_pct > drift_threshold
            range_stale = False
            if dist_pct is not None and range_limit_pct is not None:
                range_stale = dist_pct > range_limit_pct
            if order_type == "TP":
                if not range_stale:
                    continue
            elif not drift_stale and not range_stale:
                continue
            handled_ts = self._pilot_stale_handled.get(order_id)
            if handled_ts is not None and now - handled_ts < STALE_COOLDOWN_SEC:
                last_log = self._pilot_stale_skip_log_ts.get(order_id)
                if last_log is None or now - last_log >= STALE_LOG_DEDUP_SEC:
                    self._append_log(
                        (
                            "[STALE] skip already-handled "
                            f"orderId={order_id} cooldown={STALE_COOLDOWN_SEC}s"
                        ),
                        kind="INFO",
                    )
                    self._pilot_stale_skip_log_ts[order_id] = now
                continue
            last_seen = self._pilot_stale_seen_ids.get(order_id)
            if last_seen is not None and now - last_seen < STALE_COOLDOWN_SEC:
                continue
            self._pilot_stale_seen_ids[order_id] = now
            reason = "drift" if drift_stale else "range"
            if drift_stale and range_stale:
                reason = "drift,range"
            if order_type == "TP":
                reason = "range"
            if self._should_log_stale_check(order_id, reason, now_mono):
                dist_text = f"{dist_pct:.2f}%" if dist_pct is not None else "—"
                range_limit_text = (
                    f"{range_limit_pct:.4f}%" if range_limit_pct is not None else "—"
                )
                excess_text = f"{excess_pct:.4f}%" if excess_pct is not None else "—"
                threshold_text = f"{drift_threshold:.2f}%"
                mid_text = f"{mid_price:.8f}" if mid_price is not None else "—"
                price_text = f"{info.price:.8f}" if info.price > 0 else "—"
                self._append_log(
                    (
                        f"[STALE] check orderId={order_id} side={info.side} price={price_text} "
                        f"dist={dist_text} mid={mid_text} range_limit={range_limit_text} "
                        f"threshold={threshold_text} excess={excess_text} reason={reason}"
                    ),
                    kind="INFO",
                )
            stale.append(
                StaleOrderCandidate(
                    info=info,
                    order=order,
                    age_s=age_sec,
                    reason=reason,
                    dist_pct=dist_pct,
                    range_limit_pct=range_limit_pct,
                    excess_pct=excess_pct,
                )
            )
        return stale, oldest_info, oldest_age_sec

    def _collect_stale_orders_from_snapshot(
        self,
        orders: list[dict[str, Any]],
        *,
        mid_price: float | None,
        range_low: float | None,
        range_high: float | None,
        settings: GridSettingsState | None,
    ) -> list[StaleOrderCandidate]:
        if not orders:
            return []
        now = time_fn()
        drift_threshold = self._stale_grid_threshold_pct(settings)
        stale: list[StaleOrderCandidate] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("orderId", ""))
            if not order_id or order_id in self._pilot_pending_cancel_ids:
                continue
            info = self._order_info_map.get(order_id)
            if not info:
                continue
            age_sec = int(max(now - info.created_ts, 0))
            order_type = self._order_registry_type_from_client_id(str(order.get("clientOrderId", "")))
            dist_pct = None
            range_limit_pct = self._stale_range_limit_pct(mid_price, range_low, range_high)
            excess_pct = None
            drift_stale = False
            if mid_price is not None and mid_price > 0 and info.price > 0:
                dist_pct = abs(info.price - mid_price) / mid_price * 100
                if range_limit_pct is not None:
                    excess_pct = max(0.0, dist_pct - range_limit_pct)
                if order_type != "TP":
                    drift_stale = dist_pct > drift_threshold
            range_stale = False
            if dist_pct is not None and range_limit_pct is not None:
                range_stale = dist_pct > range_limit_pct
            if order_type == "TP":
                if not range_stale:
                    continue
            elif not drift_stale and not range_stale:
                continue
            handled_ts = self._pilot_stale_handled.get(order_id)
            if handled_ts is not None and now - handled_ts < STALE_COOLDOWN_SEC:
                continue
            reason = "drift" if drift_stale else "range"
            if drift_stale and range_stale:
                reason = "drift,range"
            if order_type == "TP":
                reason = "range"
            stale.append(
                StaleOrderCandidate(
                    info=info,
                    order=order,
                    age_s=age_sec,
                    reason=reason,
                    dist_pct=dist_pct,
                    range_limit_pct=range_limit_pct,
                    excess_pct=excess_pct,
                )
            )
        return stale

    def _update_order_info_from_snapshot(self, orders: list[dict[str, Any]]) -> None:
        now = time_fn()
        self._open_orders_snapshot_ts = now
        seen_ids: set[str] = set()
        for order in orders:
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("orderId", ""))
            if not order_id:
                continue
            seen_ids.add(order_id)
            side = str(order.get("side", "")).upper()
            price = self._coerce_float(str(order.get("price", ""))) or 0.0
            qty = self._coerce_float(str(order.get("origQty", ""))) or 0.0
            exchange_time = order.get("time")
            info = self._order_info_map.get(order_id)
            if isinstance(exchange_time, (int, float)):
                created_ts = float(exchange_time) / 1000.0
            elif info is not None:
                created_ts = info.created_ts
            else:
                created_ts = now
            self._order_info_map[order_id] = OrderInfo(
                order_id=order_id,
                side=side,
                price=price,
                qty=qty,
                created_ts=created_ts,
                last_seen_ts=now,
            )
        missing_ids = [order_id for order_id in self._order_info_map if order_id not in seen_ids]
        for order_id in missing_ids:
            self._order_info_map.pop(order_id, None)
            self._pilot_pending_cancel_ids.discard(order_id)
            self._pilot_stale_seen_ids.pop(order_id, None)
            self._pilot_stale_handled.pop(order_id, None)
            self._pilot_stale_skip_log_ts.pop(order_id, None)
            self._purge_stale_log_keys(order_id)

    def _clear_order_info(self) -> None:
        self._open_orders_snapshot_ts = None
        self._order_info_map = {}
        self._pilot_pending_cancel_ids.clear()
        self._pilot_stale_seen_ids.clear()
        self._pilot_stale_handled.clear()
        self._pilot_stale_skip_log_ts.clear()
        self._pilot_stale_order_log_ts.clear()
        self._pilot_stale_check_log_ts.clear()

    def _purge_stale_log_keys(self, order_id: str) -> None:
        if not order_id:
            return
        for key in [key for key in self._pilot_stale_order_log_ts if key[0] == order_id]:
            self._pilot_stale_order_log_ts.pop(key, None)
        for key in [key for key in self._pilot_stale_check_log_ts if key[0] == order_id]:
            self._pilot_stale_check_log_ts.pop(key, None)

    @staticmethod
    def _format_order_age_value(age_sec: int) -> str:
        minutes, seconds = divmod(age_sec, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}ч {minutes}м"
        if minutes:
            return f"{minutes}м {seconds}с"
        return f"{seconds}с"

    def _update_pilot_orders_metrics(self) -> None:
        if not hasattr(self, "_pilot_orders_total_value"):
            return
        (
            total,
            buy_count,
            sell_count,
            oldest_age_sec,
            oldest_info,
            snapshot_age_sec,
        ) = self._collect_order_metrics()
        if total == 0:
            self._pilot_orders_total_value.setText("—")
            self._pilot_orders_buy_value.setText("—")
            self._pilot_orders_sell_value.setText("—")
            self._pilot_orders_oldest_value.setText("—")
            _mid, _anchor, _range_low, _range_high, settings = self._stale_context()
            self._pilot_orders_threshold_value.setText(self._stale_threshold_text(settings))
            self._pilot_orders_stale_value.setText(
                f"— (policy={self._pilot_stale_policy.value})"
            )
            if self._pilot_warning_override and self._pilot_warning_override_until:
                if time_fn() <= self._pilot_warning_override_until:
                    self._pilot_orders_warning_value.setText(self._pilot_warning_override)
                    self._pilot_orders_warning_value.setStyleSheet("color: #dc2626; font-weight: 600;")
                else:
                    self._pilot_warning_override = None
                    self._pilot_warning_override_until = None
                    self._pilot_orders_warning_value.setText("—")
                    self._pilot_orders_warning_value.setStyleSheet("color: #111827; font-weight: 600;")
            else:
                self._pilot_orders_warning_value.setText("—")
                self._pilot_orders_warning_value.setStyleSheet("color: #111827; font-weight: 600;")
            self._pilot_stale_active = False
            self._pilot_snapshot_stale_active = False
            return
        self._pilot_orders_total_value.setText(str(total))
        self._pilot_orders_buy_value.setText(str(buy_count))
        self._pilot_orders_sell_value.setText(str(sell_count))
        if oldest_age_sec is None:
            self._pilot_orders_oldest_value.setText("—")
        else:
            self._pilot_orders_oldest_value.setText(self._format_order_age_value(oldest_age_sec))
        _mid, _anchor, _range_low, _range_high, settings = self._stale_context()
        self._pilot_orders_threshold_value.setText(self._stale_threshold_text(settings))
        self._pilot_orders_stale_value.setText(f"0 (policy={self._pilot_stale_policy.value})")

        if snapshot_age_sec is not None and snapshot_age_sec > STALE_SNAPSHOT_MAX_AGE_SEC:
            self._pilot_orders_warning_value.setText("Снимок ордеров устарел")
            self._pilot_orders_warning_value.setStyleSheet("color: #dc2626; font-weight: 600;")
            self._maybe_log_open_orders_snapshot_stale(snapshot_age_sec)
            self._pilot_stale_active = False
            return
        self._pilot_snapshot_stale_active = False

        if self._maybe_log_stale_warmup_skip():
            self._pilot_orders_warning_value.setText("—")
            self._pilot_orders_warning_value.setStyleSheet("color: #111827; font-weight: 600;")
            self._pilot_stale_active = False
            return
        mid_price, _anchor_price, range_low, range_high, settings = self._stale_context()
        stale_orders, oldest_info, oldest_age_sec = self._collect_stale_orders(
            mid_price=mid_price,
            range_low=range_low,
            range_high=range_high,
            settings=settings,
        )
        last_seen_age = self._last_seen_age_sec(oldest_info)
        if last_seen_age is not None and last_seen_age > STALE_SNAPSHOT_MAX_AGE_SEC:
            self._pilot_orders_warning_value.setText("Снимок ордеров устарел")
            self._pilot_orders_warning_value.setStyleSheet("color: #dc2626; font-weight: 600;")
            self._maybe_log_open_orders_snapshot_stale(last_seen_age, skip_actions=True)
            self._pilot_stale_active = False
            return
        is_stale = bool(stale_orders)
        if is_stale:
            self._pilot_orders_stale_value.setText(
                f"{len(stale_orders)} (policy={self._pilot_stale_policy.value})"
            )
            self._pilot_orders_warning_value.setText("Ордера ушли от mid / диапазона")
            self._pilot_orders_warning_value.setStyleSheet("color: #dc2626; font-weight: 600;")
            self._maybe_log_stale_orders(total, stale_orders[0] if stale_orders else None)
            self._maybe_handle_stale_auto_action(stale_orders)
        else:
            now = time_fn()
            if self._pilot_warning_override and self._pilot_warning_override_until:
                if now <= self._pilot_warning_override_until:
                    self._pilot_orders_warning_value.setText(self._pilot_warning_override)
                    self._pilot_orders_warning_value.setStyleSheet("color: #dc2626; font-weight: 600;")
                else:
                    self._pilot_warning_override = None
                    self._pilot_warning_override_until = None
                    self._pilot_orders_warning_value.setText("—")
                    self._pilot_orders_warning_value.setStyleSheet("color: #111827; font-weight: 600;")
            else:
                self._pilot_orders_warning_value.setText("—")
                self._pilot_orders_warning_value.setStyleSheet("color: #111827; font-weight: 600;")
            self._pilot_stale_active = False

    def _maybe_log_stale_orders(
        self,
        total: int,
        candidate: StaleOrderCandidate | None,
    ) -> None:
        if candidate is None:
            return
        now = monotonic()
        info = candidate.info
        policy_value = self._pilot_stale_policy.value
        if not self._pilot_stale_active and self._should_log_stale_order(
            info.order_id, candidate.reason, now
        ):
            gate_state = "ok" if self._trade_gate == TradeGate.TRADE_OK else "ro"
            price_text = f"{info.price:.8f}" if info.price > 0 else "—"
            dist_text = f"{candidate.dist_pct:.2f}%" if candidate.dist_pct is not None else "—"
            range_limit_text = (
                f"{candidate.range_limit_pct:.4f}%"
                if candidate.range_limit_pct is not None
                else "—"
            )
            excess_text = (
                f"{candidate.excess_pct:.4f}%"
                if candidate.excess_pct is not None
                else "—"
            )
            mid_price = self._stale_mid_price()
            mid_text = f"{mid_price:.8f}" if mid_price is not None else "—"
            _mid, _anchor, _range_low, _range_high, settings = self._stale_context()
            drift_threshold = self._stale_grid_threshold_pct(settings)
            self._append_log(
                (
                    "[WARN] [NC_MICRO] stale detected "
                    f"total={total} orderId={info.order_id} side={info.side} price={price_text} "
                    f"dist={dist_text} mid={mid_text} range_limit={range_limit_text} "
                    f"threshold={drift_threshold:.2f}% excess={excess_text} "
                    f"reason={candidate.reason} policy={policy_value} "
                    f"auto=on gate={gate_state}"
                ),
                kind="WARN",
            )
            self._pilot_stale_last_log_ts = now
            self._pilot_stale_last_log_order_id = info.order_id
            self._pilot_stale_last_log_total = total
        self._pilot_stale_active = True

    def _maybe_log_open_orders_snapshot_stale(self, snapshot_age_sec: int, *, skip_actions: bool = False) -> None:
        now = monotonic()
        should_log = False
        if not self._pilot_snapshot_stale_active:
            should_log = True
        elif self._pilot_snapshot_stale_last_log_ts is None:
            should_log = True
        elif now - self._pilot_snapshot_stale_last_log_ts >= STALE_LOG_DEDUP_SEC:
            should_log = True
        if should_log:
            suffix = " -> skip stale actions" if skip_actions else ""
            self._append_log(
                f"[WARN] [NC_MICRO] open_orders snapshot stale age={snapshot_age_sec}s{suffix}",
                kind="WARN",
            )
            self._pilot_snapshot_stale_last_log_ts = now
        self._pilot_snapshot_stale_active = True

    def _maybe_handle_stale_auto_action(self, stale_orders: list[StaleOrderCandidate]) -> None:
        if not stale_orders:
            return
        stale_orders = self._stale_action_candidates(stale_orders)
        if not stale_orders:
            return
        if not self._pilot_auto_actions_enabled:
            return
        if self._pilot_auto_actions_paused_until and monotonic() < self._pilot_auto_actions_paused_until:
            return
        if self._pilot_stale_policy == StalePolicy.NONE:
            return
        if self._trade_gate != TradeGate.TRADE_OK:
            return
        now = monotonic()
        if self._pilot_stale_last_action_ts and now - self._pilot_stale_last_action_ts < STALE_AUTO_ACTION_COOLDOWN_SEC:
            return
        if self._pilot_action_in_progress:
            return
        self._pilot_stale_last_action_ts = now
        if self._pilot_stale_policy == StalePolicy.RECENTER:
            self._pilot_execute(PilotAction.RECENTER, auto=True, reason="stale")
        elif self._pilot_stale_policy == StalePolicy.CANCEL_REPLACE_STALE:
            self._pilot_execute_cancel_replace_stale(stale_orders, auto=True, reason="stale")

    def _emit_price_update(self, update: PriceUpdate) -> None:
        try:
            self._signals.price_update.emit(update)
        except Exception:
            self._logger.exception("[NC_MICRO][CRASH] exception in _emit_price_update")
            self._notify_crash("_emit_price_update")
            return

    def _emit_status_update(self, status: str, message: str) -> None:
        try:
            self._signals.status_update.emit(status, message)
        except Exception:
            self._logger.exception("[NC_MICRO][CRASH] exception in _emit_status_update")
            self._notify_crash("_emit_status_update")
            return

    def _kpi_allows_start(self) -> bool:
        if self._kpi_state != KPI_STATE_INVALID:
            return True
        snapshot = self._price_feed_manager.get_snapshot(self._symbol) if hasattr(self, "_price_feed_manager") else None
        price = snapshot.last_price if snapshot and snapshot.last_price is not None else self._last_price
        best_bid, best_ask, _source = self._resolve_book_bid_ask()
        return price is not None and best_bid is not None and best_ask is not None

    def _apply_price_update(self, update: PriceUpdate) -> None:
        self._last_price_update = update
        price = update.last_price if isinstance(update.last_price, (int, float)) else None
        if price is not None and price > 0:
            self._last_price = price
            self._last_price_ts = monotonic()
            self._record_price(price)
            self._last_price_label.setText(tr("last_price", price=f"{price:.8f}"))
            self._grid_engine.on_price(price)
        elif self._last_price is None:
            self._last_price_label.setText(tr("last_price", price="—"))

        latency = f"{update.latency_ms}ms" if update.latency_ms is not None else "—"
        age = f"{update.price_age_ms}ms" if update.price_age_ms is not None else "—"
        self._age_label.setText(tr("age", age=age))
        self._latency_label.setText(tr("latency", latency=latency))
        clock_status = "✓" if update.price_age_ms is not None else "—"
        self._feed_indicator.setText(f"HTTP ✓ | WS {self._ws_indicator_symbol()} | CLOCK {clock_status}")
        self.update_market_kpis()
        self._update_runtime_balances()
        self._refresh_unrealized_pnl()

    def _resolve_book_bid_ask(self) -> tuple[float | None, float | None, str]:
        snapshot = self._price_feed_manager.get_snapshot(self._symbol) if hasattr(self, "_price_feed_manager") else None
        micro = snapshot
        if self._last_price_update and micro is None:
            micro = self._last_price_update.microstructure
        best_bid = micro.best_bid if micro else None
        best_ask = micro.best_ask if micro else None
        if best_bid is not None and best_ask is not None:
            return best_bid, best_ask, "WS_BOOK"
        cached_book = self._get_http_cached("book_ticker")
        if isinstance(cached_book, dict):
            bid = self._coerce_float(str(cached_book.get("bidPrice", "")))
            ask = self._coerce_float(str(cached_book.get("askPrice", "")))
            if bid is not None and ask is not None:
                return bid, ask, "HTTP_BOOK"
        return None, None, "—"

    @staticmethod
    def _compute_spread_pct_from_book(best_bid: float | None, best_ask: float | None) -> float | None:
        if not isinstance(best_bid, (int, float)) or not isinstance(best_ask, (int, float)):
            return None
        if best_bid <= 0 or best_ask <= 0:
            return None
        mid = (best_bid + best_ask) / 2
        if mid <= 0:
            return None
        return (best_ask - best_bid) / mid * 100

    @staticmethod
    def _format_spread_display(spread_pct: float | None) -> str:
        if spread_pct is None:
            return "—"
        bps = spread_pct * 100
        if spread_pct < SPREAD_DISPLAY_EPS_PCT:
            pct_text = f"<{SPREAD_DISPLAY_EPS_PCT:.4f}%"
        else:
            pct_text = f"{spread_pct:.4f}%"
        return f"{pct_text} ({bps:.2f} bps)"

    def _record_kpi_vol_sample(self, mid: float, now_ts: float) -> None:
        if mid <= 0:
            return
        self._kpi_vol_samples.append((now_ts, mid))
        cutoff = now_ts - KPI_VOL_WINDOW_SEC
        while self._kpi_vol_samples and self._kpi_vol_samples[0][0] < cutoff:
            self._kpi_vol_samples.popleft()

    def _compute_kpi_volatility_pct(self, now_ts: float) -> tuple[float | None, int]:
        cutoff = now_ts - KPI_VOL_WINDOW_SEC
        samples = [price for ts, price in self._kpi_vol_samples if ts >= cutoff]
        count = len(samples)
        if count < KPI_VOL_MIN_SAMPLES:
            return None, count
        low = min(samples)
        high = max(samples)
        if low <= 0:
            return None, count
        return (high - low) / low * 100, count

    def _request_book_ticker(self) -> None:
        now_ts = monotonic()
        if self._kpi_last_book_request_ts and (
            (now_ts - self._kpi_last_book_request_ts) * 1000 < KPI_BOOK_REQUEST_INTERVAL_MS
        ):
            return
        self._kpi_last_book_request_ts = now_ts

        def _fetch() -> dict[str, Any]:
            book = self._http_client.get_book_ticker(self._symbol)
            return book if isinstance(book, dict) else {}

        worker = _Worker(_fetch, self._can_emit_worker_results)
        worker.signals.success.connect(self._handle_book_ticker_success)
        self._thread_pool.start(worker)

    def _handle_book_ticker_success(self, payload: object, _latency_ms: int) -> None:
        assert isinstance(self, LiteAllStrategyNcMicroWindow)
        try:
            if isinstance(payload, dict) and payload:
                self._set_http_cache("book_ticker", payload)
        except Exception as exc:
            self._append_log(
                f"[HTTP_BOOK] handle success error: {exc}",
                kind="WARN",
            )

    def _fetch_bidask_http_book(self) -> None:
        now_ts = monotonic()
        if self._bidask_last_request_ts and (
            (now_ts - self._bidask_last_request_ts) * 1000 < BIDASK_POLL_MS
        ):
            return
        self._bidask_last_request_ts = now_ts

        def _fetch() -> dict[str, Any]:
            book = self._http_client.get_book_ticker(self._symbol)
            return book if isinstance(book, dict) else {}

        worker = _Worker(_fetch, self._can_emit_worker_results)
        worker.signals.success.connect(self._handle_success)
        self._thread_pool.start(worker)

    def _handle_success(self, payload: object, _latency_ms: int) -> None:
        assert isinstance(self, LiteAllStrategyNcMicroWindow)
        try:
            if not isinstance(payload, dict) or not payload:
                return
            bid = self._coerce_float(payload.get("bidPrice"))
            ask = self._coerce_float(payload.get("askPrice"))
            last_price = self._coerce_float(payload.get("lastPrice"))
            if last_price is None:
                last_price = self._coerce_float(payload.get("price"))
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                if bid is None or ask is None:
                    payload_keys = sorted(payload.keys())
                    self._append_log(
                        "[BIDASK] missing bid/ask src=HTTP_BOOK "
                        f"symbol={self._symbol} keys={payload_keys}",
                        kind="WARN",
                    )
                return
            self._bidask_last = (bid, ask)
            self._bidask_ts = monotonic()
            self._bidask_src = "HTTP_BOOK"
            self._set_http_cache("book_ticker", payload)
            if not self._bidask_ready_logged:
                self._append_log(
                    f"[BIDASK] ready src=HTTP_BOOK bid={bid:.8f} ask={ask:.8f}",
                    kind="INFO",
                )
                self._bidask_ready_logged = True
        except Exception as exc:
            self._append_log(
                f"[BIDASK] handle success error: {exc}",
                kind="WARN",
            )

    def update_market_kpis(self) -> None:
        try:
            if self._closing:
                return
            if not hasattr(self, "_market_price"):
                return
            snapshot = (
                self._price_feed_manager.get_snapshot(self._symbol)
                if hasattr(self, "_price_feed_manager")
                else None
            )
            price = snapshot.last_price if snapshot else None
            source = snapshot.source if snapshot else None
            age_ms = snapshot.price_age_ms if snapshot else None
            micro = snapshot
            if self._last_price_update:
                if price is None and self._last_price_update.last_price is not None:
                    price = self._last_price_update.last_price
                if source is None and self._last_price_update.source is not None:
                    source = self._last_price_update.source
                if age_ms is None and self._last_price_update.price_age_ms is not None:
                    age_ms = self._last_price_update.price_age_ms
                if micro is None:
                    micro = self._last_price_update.microstructure

            now_ts = monotonic()
            price_cache_used = False
            price_cache_age_ms = None
            if not isinstance(price, (int, float)) or price <= 0:
                price = None
            if price is not None:
                self._last_price = price
                self._last_price_ts = now_ts
            elif self._last_price is not None and self._last_price_ts is not None:
                price_cache_age_ms = int((now_ts - self._last_price_ts) * 1000)
                if price_cache_age_ms <= PRICE_STALE_MS:
                    price = self._last_price
                    price_cache_used = True
            if price_cache_used:
                if age_ms is None:
                    age_ms = price_cache_age_ms
                if source is None:
                    source = "CACHE"
            if (
                self._bidask_last_request_ts is None
                or (now_ts - self._bidask_last_request_ts) * 1000 >= BIDASK_POLL_MS
            ):
                self._fetch_bidask_http_book()
            best_bid = None
            best_ask = None
            book_source = self._bidask_src or "HTTP_BOOK"
            bidask_age_ms = None
            bidask_state = "missing"
            if self._bidask_last and self._bidask_ts is not None:
                age_ms = int((now_ts - self._bidask_ts) * 1000)
                if age_ms <= BIDASK_STALE_MS:
                    best_bid, best_ask = self._bidask_last
                    bidask_age_ms = age_ms
                    bidask_state = "stale_cache" if age_ms > BIDASK_FAIL_SOFT_MS else "fresh"
                else:
                    bidask_age_ms = age_ms

            if price is None:
                self._market_price.setText("—")
            else:
                self._market_price.setText(f"{price:.8f}")
            self._set_market_label_state(self._market_price, active=price is not None)
            spread_pct = self._compute_spread_pct_from_book(best_bid, best_ask)
            spread_text = self._format_spread_display(spread_pct)
            self._market_spread.setText(spread_text)
            self._set_market_label_state(self._market_spread, active=spread_text != "—")

            sample_ts = time_fn()
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2
                self._record_kpi_vol_sample(mid, sample_ts)
            volatility_pct, sample_count = self._compute_kpi_volatility_pct(sample_ts)
            cache_used = False
            cache_ttl = None
            if volatility_pct is not None:
                self._kpi_last_good_vol = volatility_pct
                self._kpi_last_good_ts = sample_ts
            elif self._kpi_last_good_vol is not None and self._kpi_last_good_ts is not None:
                age_sec = sample_ts - self._kpi_last_good_ts
                if age_sec < KPI_VOL_TTL_SEC:
                    volatility_pct = self._kpi_last_good_vol
                    cache_used = True
                    cache_ttl = int(max(KPI_VOL_TTL_SEC - age_sec, 0))
            volatility_text = f"{volatility_pct:.2f}%" if volatility_pct is not None else "—"
            self._market_volatility.setText(volatility_text)
            self._set_market_label_state(self._market_volatility, active=volatility_text != "—")

            maker, taker = self._trade_fees
            if maker is None and taker is None:
                self._market_fee.setText("—")
            self._set_market_label_state(self._market_fee, active=maker is not None or taker is not None)

            source_age_text = "—"
            if price is not None and source is not None:
                if source == "WS" and (age_ms is None or age_ms > WS_STALE_MS):
                    source_age_text = "WS | stale"
                elif age_ms is not None:
                    source_age_text = f"{source} | {age_ms}ms"
            self._market_source.setText(source_age_text)
            self._set_market_label_state(self._market_source, active=source_age_text != "—")

            feed_ok = bool(source == "WS" and age_ms is not None and age_ms < WS_STALE_MS)
            self._feed_ok = feed_ok
            feed_age_text = f"{age_ms}ms" if age_ms is not None else "—"
            if (
                self._feed_last_ok is None
                or self._feed_last_ok != feed_ok
                or self._feed_last_source != source
            ):
                self._append_log(
                    f"[FEED] src={source or '—'} age={feed_age_text} ok={str(feed_ok).lower()}",
                    kind="INFO",
                )
                self._feed_last_ok = feed_ok
                self._feed_last_source = source

            is_spread_zero = spread_pct is not None and spread_pct == 0
            spread_zero_ok = is_spread_zero and self._symbol in SPREAD_ZERO_OK_SYMBOLS
            if volatility_pct is None:
                self._kpi_zero_vol_ticks = 0
                self._kpi_zero_vol_start_ts = None
                vol_state = KPI_STATE_UNKNOWN
                vol_reason = "vol=unknown"
            else:
                self._kpi_zero_vol_ticks = 0
                self._kpi_zero_vol_start_ts = None
                vol_state = KPI_STATE_OK
                if cache_used and cache_ttl is not None:
                    vol_reason = "vol_cached"
                else:
                    vol_reason = "vol>0"
            spread_log = spread_text if spread_text != "—" else "—"
            vol_text = f"{volatility_pct:.4f}%" if volatility_pct is not None else "—"
            bidask_missing = best_bid is None or best_ask is None
            if bidask_missing and self._kpi_missing_bidask_since_ts is None:
                self._kpi_missing_bidask_since_ts = now_ts
            if not bidask_missing:
                self._kpi_missing_bidask_since_ts = None
            price_available = price is not None
            if bidask_missing:
                if not self._kpi_missing_bidask_active:
                    self._append_log(
                        "[KPI] state=UNKNOWN reason=no_bidask_yet",
                        kind="INFO",
                    )
                    self._kpi_missing_bidask_active = True
            else:
                self._kpi_missing_bidask_active = False
            if not price_available:
                kpi_state = KPI_STATE_UNKNOWN
                kpi_reason = "no_price_yet"
            elif spread_zero_ok:
                kpi_state = KPI_STATE_OK
                kpi_reason = "spread=0 allowed"
            elif bidask_missing:
                kpi_state = KPI_STATE_UNKNOWN
                kpi_reason = "no_bidask_yet"
            elif bidask_state == "stale_cache":
                kpi_state = KPI_STATE_OK
                kpi_reason = "bidask_stale_cache"
            elif vol_state == KPI_STATE_UNKNOWN:
                kpi_state = KPI_STATE_UNKNOWN
                kpi_reason = vol_reason
            else:
                kpi_state = KPI_STATE_OK
                kpi_reason = vol_reason
            if (
                kpi_state == KPI_STATE_UNKNOWN
                and self._kpi_last_state == KPI_STATE_OK
                and self._kpi_last_state_ts is not None
                and now_ts - self._kpi_last_state_ts < KPI_DEBOUNCE_SEC
            ):
                kpi_state = KPI_STATE_OK
                kpi_reason = "debounce"
            log_reason = kpi_reason
            if cache_used and cache_ttl is not None and kpi_reason == "vol_cached":
                log_reason = f"{kpi_reason} ttl={cache_ttl}s"
            if self._kpi_last_state != kpi_state or self._kpi_last_reason != kpi_reason:
                bidask_age_text = f"{bidask_age_ms}ms" if bidask_age_ms is not None else "—"
                self._append_log(
                    (
                        f"[KPI] state={kpi_state} spread={spread_log} vol={vol_text} "
                        f"src={book_source} age={bidask_age_text} reason={log_reason}"
                    ),
                    kind="INFO",
                )
                self._kpi_last_state = kpi_state
                self._kpi_last_reason = kpi_reason
                self._kpi_last_state_ts = now_ts
            self._kpi_invalid_reason = None
            self._kpi_vol_state = vol_state
            self._kpi_state = kpi_state
            self._kpi_valid = kpi_state != KPI_STATE_INVALID
            if kpi_state == KPI_STATE_OK and spread_pct is not None:
                self._kpi_last_good = {
                    "spread": spread_pct,
                    "vol": volatility_pct,
                    "src": book_source,
                    "ts": now_ts,
                }

            if price is not None and source is not None:
                should_log = False
                if not self._kpi_has_data or self._kpi_last_source != source:
                    should_log = True
                if should_log:
                    if source == "WS" and (age_ms is None or age_ms > WS_STALE_MS):
                        age_text = "stale"
                    else:
                        age_text = f"{age_ms}ms" if age_ms is not None else "—"
                    self._append_log(
                        f"[NC_MICRO] kpi updated src={source} age={age_text} price={price:.8f}",
                        kind="INFO",
                    )
                self._kpi_has_data = True
                self._kpi_last_source = source
            else:
                self._kpi_has_data = False
                self._kpi_last_source = None
                self._kpi_zero_vol_ticks = 0
                self._kpi_zero_vol_start_ts = None
                self._kpi_missing_bidask_since_ts = None
        except Exception:
            self._logger.exception("[NC_MICRO][CRASH] exception in update_market_kpis")
            self._notify_crash("update_market_kpis")
            return

    def _compute_recent_volatility_pct(self) -> float | None:
        if len(self._price_history) < 2:
            return None
        prices = self._price_history[-50:]
        low = min(prices)
        high = max(prices)
        if low <= 0:
            return None
        return (high - low) / low * 100

    def _apply_status_update(self, status: str, _: str) -> None:
        overall_status = (
            self._price_feed_manager.get_ws_overall_status()
            if hasattr(self, "_price_feed_manager")
            else status
        )
        if overall_status == WS_CONNECTED:
            self._ws_status = WS_CONNECTED
            self._feed_indicator.setToolTip("")
        elif overall_status == WS_DEGRADED:
            self._ws_status = WS_DEGRADED
            self._feed_indicator.setToolTip(tr("ws_degraded_tooltip"))
        elif overall_status == WS_LOST:
            self._ws_status = WS_LOST
            self._feed_indicator.setToolTip(tr("ws_degraded_tooltip"))
        else:
            self._ws_status = ""
            self._feed_indicator.setToolTip("")
        self._feed_indicator.setText(f"HTTP ✓ | WS {self._ws_indicator_symbol()} | CLOCK —")

    @staticmethod
    def _set_market_label_state(label: QLabel, active: bool) -> None:
        label.setStyleSheet("color: #f3f4f6; font-size: 11px;")

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
        if hasattr(self, "_tp_fix_button"):
            self._tp_fix_button.setEnabled(not auto and self._tp_fix_target is not None)
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

    def _handle_fix_tp(self) -> None:
        if self._tp_fix_target is None:
            return
        self._take_profit_input.setValue(self._tp_fix_target)
        self._clear_tp_break_even_helper()

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
        self._dry_run_enabled = checked
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

    def _set_strategy_controls_enabled(self, enabled: bool) -> None:
        if not hasattr(self, "_strategy_controls"):
            return
        for widget in self._strategy_controls:
            widget.setEnabled(enabled)

    def _show_tp_break_even_helper(self, min_tp: float, break_even: float) -> None:
        safety_tp = min_tp + 0.02
        self._tp_fix_target = safety_tp
        self._tp_helper_label.setText(
            f"min TP: {min_tp:.2f}% (break-even {break_even:.2f}%)"
        )
        self._tp_helper_label.setVisible(True)
        if not self._take_profit_input.isReadOnly():
            self._tp_fix_button.setEnabled(True)

    def _clear_tp_break_even_helper(self) -> None:
        self._tp_fix_target = None
        if hasattr(self, "_tp_helper_label"):
            self._tp_helper_label.setText("")
            self._tp_helper_label.setVisible(False)
        if hasattr(self, "_tp_fix_button"):
            self._tp_fix_button.setEnabled(False)

    def _handle_start(self) -> None:
        if self._action_lock_blocked("start"):
            return
        if self._state in {"RUNNING", "PLACING_GRID", "PAUSED", "WAITING_FILLS"}:
            self._append_log("Start ignored: engine already running.", kind="WARN")
            return
        if self._start_locked_until_change:
            if not self._start_locked_logged:
                self._append_log("[START] blocked: fix required (TP)", kind="WARN")
                self._start_locked_logged = True
            return
        if self._start_in_progress:
            if not self._start_in_progress_logged:
                self._append_log("[START] ignored: in_progress", kind="WARN")
                self._start_in_progress_logged = True
            return
        snapshot = self._collect_strategy_snapshot()
        snapshot_hash = self._hash_snapshot(snapshot)
        if snapshot_hash == self._last_preflight_hash and self._last_preflight_blocked:
            return
        self._start_in_progress = True
        self._start_in_progress_logged = False
        self._current_action = "start"
        self._start_token += 1
        self._last_preflight_hash = snapshot_hash
        self._last_preflight_blocked = False
        self._start_button.setEnabled(False)
        self._set_strategy_controls_enabled(False)
        started = False
        try:
            balances_ok = self._force_refresh_balances_and_wait(timeout_ms=2_000)
            if not balances_ok or not self._balances_ready_for_start():
                self._append_log(
                    "[START] blocked: balances_not_ready (force_refresh_failed)",
                    kind="WARN",
                )
                self._mark_preflight_blocked()
                return
            if not self._engine_ready():
                self._append_log(
                    "Start blocked: engine not ready "
                    f"(trade_gate={self._trade_gate_state.value} "
                    f"live_enabled={str(self._live_enabled()).lower()} "
                    f"canTrade={str(self._account_can_trade).lower()})",
                    kind="WARN",
                )
                self._mark_preflight_blocked()
                return
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
                self._mark_preflight_blocked()
                return
            try:
                anchor_price = self.get_anchor_price(self._symbol)
                if anchor_price is None:
                    raise ValueError("No price available.")
                balance_snapshot = self._balance_snapshot()
                settings = self._resolve_start_settings(snapshot)
                self._apply_auto_clamps(settings, anchor_price)
                guard_decision = self._apply_profit_guard(
                    settings,
                    anchor_price,
                    action_label="start",
                    update_ui=True,
                )
                if guard_decision == "HOLD":
                    self._append_log("[START] blocked: profit guard HOLD", kind="WARN")
                    self._mark_preflight_blocked()
                    return
                if settings.take_profit_pct <= 0:
                    raise ValueError("Invalid take_profit_pct")
                profitability = self._evaluate_tp_profitability(settings.take_profit_pct)
                if not profitability.get("is_profitable", True):
                    min_tp = profitability.get("min_tp_pct")
                    break_even = profitability.get("break_even_tp_pct")
                    fix_target = None
                    if isinstance(min_tp, float):
                        fix_target = min_tp + 0.02
                    if self._auto_fix_tp_enabled and isinstance(fix_target, float):
                        self._apply_tp_fix(fix_target, min_tp, auto_fix=True)
                        settings.take_profit_pct = fix_target
                    else:
                        should_fix = False
                        if isinstance(fix_target, float):
                            should_fix = self._confirm_tp_fix(fix_target, min_tp)
                        if should_fix and isinstance(fix_target, float):
                            self._apply_tp_fix(fix_target, min_tp, auto_fix=False)
                            settings.take_profit_pct = fix_target
                        else:
                            self._append_log("[START] blocked: fix required (TP)", kind="WARN")
                            if isinstance(min_tp, float) and isinstance(break_even, float):
                                self._show_tp_break_even_helper(min_tp, break_even)
                            self._lock_start_for_tp()
                            self._change_state("IDLE")
                            return
                self._clear_tp_break_even_helper()
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
                self._mark_preflight_blocked()
                return
            self._bootstrap_mode = False
            self._bootstrap_sell_enabled = False
            if not dry_run:
                base_free = self.as_decimal(balance_snapshot.get("base_free", Decimal("0")))
                planned = self._limit_sell_plan_by_balance(planned, base_free)
                if base_free <= 0:
                    self._bootstrap_mode = True
                    planned = [order for order in planned if order.side == "BUY"]
                    self._append_log(
                        "[START] BUY-only mode: sells postponed (base_free insufficient)",
                        kind="INFO",
                    )
            planned = planned[: settings.max_active_orders]
            buy_count = sum(1 for order in planned if order.side == "BUY")
            sell_count = sum(1 for order in planned if order.side == "SELL")
            if not dry_run and buy_count > 0 and sell_count == 0 and not self._bootstrap_mode:
                self._bootstrap_mode = True
                self._append_log(
                    "[START] BUY-only mode: sells postponed (base_free insufficient)",
                    kind="INFO",
                )
            if not dry_run and (len(planned) < 1 or buy_count < 1):
                self._append_log(
                    f"Start blocked: insufficient orders after filters (buys={buy_count}, sells={sell_count}).",
                    kind="WARN",
                )
                self._change_state("IDLE")
                self._mark_preflight_blocked()
                return
            self._append_log(
                f"grid plan: buys={buy_count} sells={sell_count}",
                kind="ORDERS",
            )
            if dry_run:
                started = True
                self._change_state("RUNNING")
                self._render_sim_orders(planned)
                self._activate_stale_warmup()
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
            started = True
            self._last_preflight_blocked = False
            self._bot_session_id = uuid4().hex[:8]
            self._bot_order_ids.clear()
            self._bot_client_ids.clear()
            self._bot_order_keys.clear()
            self._fill_keys.clear()
            self._fill_accumulator = FillAccumulator()
            self._active_action_keys.clear()
            self._active_order_keys.clear()
            self._recent_order_keys.clear()
            self._order_id_to_registry_key.clear()
            self._order_id_to_level_index.clear()
            self._open_orders_map = {}
            self._fills = []
            self._base_lots.clear()
            self._seen_trade_ids.clear()
            self._realized_pnl = 0.0
            self._fees_total = 0.0
            self._reset_position_state()
            self._closed_trades = 0
            self._win_trades = 0
            self._replacement_counter = 0
            self._closed_order_statuses.clear()
            self._update_pnl(None, None)
            self._update_trade_summary()
            self._live_settings = settings
            self._first_live_session = False
            self._sell_side_enabled = False
            self._active_tp_ids.clear()
            self._active_restore_ids.clear()
            self._pending_tp_ids.clear()
            self._pending_restore_ids.clear()
            if not dry_run:
                self._prime_bot_registry_from_exchange()
            self._change_state("PLACING_GRID")
            self._place_live_orders(planned)
        finally:
            self._set_strategy_controls_enabled(True)
            if not started:
                self._start_in_progress = False
                self._start_in_progress_logged = False
                self._current_action = None
                if self._state == "IDLE":
                    self._start_button.setEnabled(True)

    def _handle_pause(self) -> None:
        self._append_log("Pause pressed.", kind="ORDERS")
        self._grid_engine.pause()
        self._change_state("PAUSED")

    def _handle_stop(self) -> None:
        if self._stop_in_progress:
            self._append_log("[STOP] ignored: already in progress", kind="WARN")
            return
        self._stop_in_progress = True
        self._closing = True
        self._stop_local_timers()
        self._append_log("Stop pressed.", kind="ORDERS")
        self._grid_engine.stop(cancel_all=True)
        self._change_state("STOPPING")
        self._start_in_progress = False
        self._start_in_progress_logged = False
        self._start_button.setEnabled(True)
        self._bootstrap_mode = False
        self._bootstrap_sell_enabled = False
        self._sell_side_enabled = False
        self._active_tp_ids.clear()
        self._active_restore_ids.clear()
        self._pending_tp_ids.clear()
        self._pending_restore_ids.clear()
        if self._dry_run_toggle.isChecked():
            self._finalize_stop()
            return
        if not self._account_client:
            self._append_log("[STOP] cancel skipped: no account client.", kind="WARN")
            self._finalize_stop()
            return
        self._cancel_bot_orders_on_stop()

    def _finalize_stop(self, *, keep_open_orders: bool = False) -> None:
        self._bot_order_ids.clear()
        self._bot_client_ids.clear()
        self._bot_order_keys.clear()
        self._fill_keys.clear()
        self._fill_accumulator = FillAccumulator()
        self._active_action_keys.clear()
        self._clear_local_order_registry()
        self._closed_order_ids.clear()
        self._closed_order_statuses.clear()
        self._order_id_to_level_index.clear()
        self._open_orders_map = {}
        self._bot_session_id = None
        if not keep_open_orders:
            self._open_orders = []
            self._open_orders_all = []
            self._clear_order_info()
        self._reset_position_state()
        self._sell_side_enabled = False
        self._active_tp_ids.clear()
        self._active_restore_ids.clear()
        self._pending_tp_ids.clear()
        self._pending_restore_ids.clear()
        self._stop_in_progress = False
        self._closing = False
        self._resume_local_timers()
        self._render_open_orders()
        self._change_state("STOPPED")

    def _cancel_bot_orders_on_stop(self) -> None:
        if not self._account_client:
            self._finalize_stop()
            return
        self._append_log("[STOP] canceling bot orders...", kind="INFO")
        prefix = f"BBOT_LAS_v1_{self._symbol}"

        def _cancel() -> dict[str, Any]:
            errors: list[str] = []
            canceled_by_tag = 0
            failed = 0
            failed_ids: list[str] = []
            open_orders = self._account_client.get_open_orders(self._symbol)
            tagged_orders = [
                order
                for order in open_orders
                if isinstance(order, dict)
                and str(order.get("clientOrderId", "")).startswith(prefix)
            ]
            planned = len(tagged_orders)
            for order in tagged_orders:
                order_id = str(order.get("orderId", ""))
                client_id = str(order.get("clientOrderId", ""))
                if not order_id and not client_id:
                    continue
                try:
                    if order_id:
                        ok, error = self._cancel_order_idempotent(order_id)
                        if error:
                            failed += 1
                            failed_ids.append(order_id)
                            errors.append(error)
                            continue
                    else:
                        self._account_client.cancel_order(
                            self._symbol,
                            order_id=None,
                            orig_client_order_id=client_id,
                        )
                    canceled_by_tag += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    failed_ids.append(order_id or client_id)
                    errors.append(self._format_cancel_exception(exc, order_id or client_id))
            open_orders_after = self._account_client.get_open_orders(self._symbol)
            remaining_tagged = [
                order
                for order in open_orders_after
                if isinstance(order, dict)
                and str(order.get("clientOrderId", "")).startswith(prefix)
            ]
            used_cancel_all = False
            if remaining_tagged:
                self._account_client.cancel_open_orders(self._symbol)
                used_cancel_all = True
                open_orders_after = self._account_client.get_open_orders(self._symbol)
            final_remaining = [
                order
                for order in open_orders_after
                if isinstance(order, dict)
                and str(order.get("clientOrderId", "")).startswith(prefix)
            ]
            deadline = monotonic() + 5.0
            while final_remaining and monotonic() < deadline:
                self._sleep_ms(500)
                open_orders_after = self._account_client.get_open_orders(self._symbol)
                final_remaining = [
                    order
                    for order in open_orders_after
                    if isinstance(order, dict)
                    and str(order.get("clientOrderId", "")).startswith(prefix)
                ]
            return {
                "planned": planned,
                "canceled_by_tag": canceled_by_tag,
                "failed": failed,
                "failed_ids": failed_ids,
                "used_cancel_all": used_cancel_all,
                "open_orders_after": open_orders_after,
                "remaining_after": len(final_remaining),
                "errors": errors,
            }

        worker = _Worker(_cancel, self._can_emit_worker_results)
        worker.signals.success.connect(self._handle_stop_cancel_result)
        worker.signals.error.connect(self._handle_stop_cancel_error)
        self._thread_pool.start(worker)

    def _handle_stop_cancel_result(self, result: object, latency_ms: int) -> None:
        if not isinstance(result, dict):
            self._handle_cancel_error("Unexpected cancel response")
            self._finalize_stop()
            return
        planned = int(result.get("planned", 0) or 0)
        canceled_by_tag = int(result.get("canceled_by_tag", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        self._append_log(
            f"Cancel bot orders: planned={planned} cancelled={canceled_by_tag} failed={failed}",
            kind="INFO",
        )
        failed_ids = result.get("failed_ids", [])
        if isinstance(failed_ids, list) and failed_ids:
            failed_list = ", ".join(str(entry) for entry in failed_ids)
            self._append_log(f"[STOP] failed orderIds: {failed_list}", kind="WARN")
        used_cancel_all = bool(result.get("used_cancel_all", False))
        if used_cancel_all:
            self._append_log(
                f"[STOP] cancel_all_open_orders symbol={self._symbol} (cancels all open orders for symbol)",
                kind="WARN",
            )
        self._append_log(f"[STOP] fallback_cancel_all used={used_cancel_all}", kind="INFO")
        remaining_after = int(result.get("remaining_after", 0) or 0)
        if remaining_after:
            self._append_log(
                f"[STOP] remaining tagged orders after cancel={remaining_after}",
                kind="WARN",
            )
        open_orders_after = result.get("open_orders_after", [])
        if isinstance(open_orders_after, list):
            self._append_log(f"[STOP] open_orders_after n={len(open_orders_after)}", kind="INFO")
            self._open_orders_all = [item for item in open_orders_after if isinstance(item, dict)]
            self._open_orders = self._filter_bot_orders(self._open_orders_all)
            self._open_orders_map = {
                str(order.get("orderId", "")): order
                for order in self._open_orders
                if str(order.get("orderId", ""))
            }
            self._clear_local_order_registry()
        errors = result.get("errors", [])
        if isinstance(errors, list):
            for message in errors:
                self._append_log(str(message), kind="WARN")
        self._refresh_open_orders(force=True)
        self._finalize_stop(keep_open_orders=bool(self._open_orders))

    def _handle_stop_cancel_error(self, message: str) -> None:
        self._handle_cancel_error(message)
        self._finalize_stop()

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
        if new_state in {"RUNNING", "PAUSED", "WAITING_FILLS"} and self._current_action == "start":
            self._current_action = None
        if new_state == "IDLE" and not self._start_in_progress:
            self._start_in_progress_logged = False
            self._start_button.setEnabled(True)

    def _update_setting(self, key: str, value: Any) -> None:
        if hasattr(self._settings_state, key):
            setattr(self._settings_state, key, value)
        if key == "grid_step_pct" and self._settings_state.grid_step_mode == "MANUAL":
            self._manual_grid_step_pct = float(value)
        if key == "take_profit_pct":
            self._clear_tp_break_even_helper()
        if key == "budget":
            self._refresh_orders_metrics()
        self._on_strategy_changed()
        self._update_grid_preview()

    def _on_strategy_changed(self) -> None:
        if self._start_locked_until_change or self._last_preflight_blocked:
            self._start_locked_until_change = False
            self._start_locked_logged = False
            self._last_preflight_hash = None
            self._last_preflight_blocked = False

    def _hash_snapshot(self, snapshot: dict[str, Any]) -> str:
        items = tuple(sorted(snapshot.items()))
        preflight_state = (
            self._balances_ready_for_start(),
            self._engine_ready(),
            bool(self._rules_loaded),
            bool(self._fees_last_fetch_ts),
        )
        return str(hash((items, preflight_state)))

    def _mark_preflight_blocked(self) -> None:
        self._last_preflight_blocked = True

    def _lock_start_for_tp(self) -> None:
        self._start_locked_until_change = True
        self._start_locked_logged = True
        self._last_preflight_blocked = True

    def _confirm_tp_fix(self, fix_target: float, min_tp: float | None) -> bool:
        min_tp_text = f"{min_tp:.4f}%" if isinstance(min_tp, float) else "—"
        message = f"TP too low. Apply fix?\nmin_tp={min_tp_text}\nnew_tp={fix_target:.4f}%"
        response = QMessageBox.question(
            self,
            "TP too low",
            message,
            QMessageBox.Apply | QMessageBox.Cancel,
            QMessageBox.Apply,
        )
        return response == QMessageBox.Apply

    def _apply_tp_fix(self, fix_target: float, min_tp: float | None, *, auto_fix: bool) -> None:
        self._take_profit_input.setValue(fix_target)
        self._clear_tp_break_even_helper()
        min_tp_text = f"{min_tp:.4f}%" if isinstance(min_tp, float) else "—"
        auto_label = "auto-fixed" if auto_fix else "fixed"
        self._append_log(
            f"[TP] {auto_label} to {fix_target:.4f}% (min_tp={min_tp_text})",
            kind="INFO",
        )

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

    def _auto_grid_params_from_history(self, grid_count: int | None = None) -> dict[str, float] | None:
        if len(self._price_history) < 2:
            return None
        prices = self._price_history[-300:]
        return self._compute_auto_grid_params(prices, grid_count=grid_count)

    def _auto_grid_params_from_http(self, grid_count: int | None = None) -> dict[str, float] | None:
        try:
            cached = self._get_http_cached("klines_1h")
            if cached is None:
                klines = self._http_client.get_klines(self._symbol, interval="1h", limit=120)
                self._set_http_cache("klines_1h", klines)
            else:
                klines = cached
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
        return self._compute_auto_grid_params(closes, grid_count=grid_count)

    def _compute_auto_grid_params(
        self,
        prices: list[float],
        *,
        grid_count: int | None = None,
    ) -> dict[str, float] | None:
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
        grid_count = grid_count or self._settings_state.grid_count
        range_pct = self._clamp(grid_step_pct * grid_count / 2, 0.5, 6.0)
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

    def _collect_strategy_snapshot(self) -> dict[str, Any]:
        state = self._settings_state
        return {
            "budget_usdt": state.budget,
            "levels": state.grid_count,
            "step_mode": state.grid_step_mode,
            "step_pct": state.grid_step_pct,
            "range_mode": state.range_mode,
            "range_low_pct": state.range_low_pct,
            "range_high_pct": state.range_high_pct,
            "tp_pct": state.take_profit_pct,
            "stoploss_enabled": state.stop_loss_enabled,
            "stoploss_pct": state.stop_loss_pct,
            "max_orders": state.max_active_orders,
            "order_size_mode": state.order_size_mode,
            "direction": state.direction,
        }

    def _resolve_start_settings(self, snapshot: dict[str, Any]) -> GridSettingsState:
        settings = GridSettingsState(
            budget=snapshot["budget_usdt"],
            direction=snapshot["direction"],
            grid_count=snapshot["levels"],
            grid_step_pct=snapshot["step_pct"],
            grid_step_mode=snapshot["step_mode"],
            range_mode=snapshot["range_mode"],
            range_low_pct=snapshot["range_low_pct"],
            range_high_pct=snapshot["range_high_pct"],
            take_profit_pct=snapshot["tp_pct"],
            stop_loss_enabled=snapshot["stoploss_enabled"],
            stop_loss_pct=snapshot["stoploss_pct"],
            max_active_orders=snapshot["max_orders"],
            order_size_mode=snapshot["order_size_mode"],
        )
        if settings.grid_step_mode != "AUTO_ATR":
            return settings
        auto_params = self._auto_grid_params_from_history(grid_count=settings.grid_count)
        if not auto_params:
            auto_params = self._auto_grid_params_from_http(grid_count=settings.grid_count)
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
        cached_book = self._get_http_cached("book_ticker")
        if isinstance(cached_book, dict):
            bid = self._coerce_float(str(cached_book.get("bidPrice", "")))
            ask = self._coerce_float(str(cached_book.get("askPrice", "")))
            if bid and ask:
                self._append_log("PRICE: HTTP_BOOK age=cached", kind="INFO")
                return (bid + ask) / 2
        try:
            book = self._http_client.get_book_ticker(symbol)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"PRICE: HTTP_BOOK failed ({exc})", kind="WARN")
            book = {}
        bid = self._coerce_float(str(book.get("bidPrice", ""))) if isinstance(book, dict) else None
        ask = self._coerce_float(str(book.get("askPrice", ""))) if isinstance(book, dict) else None
        if bid and ask:
            if isinstance(book, dict):
                self._set_http_cache("book_ticker", book)
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

    def _get_http_cached(self, key: str) -> Any | None:
        cached = self._http_cache.get(self._symbol, key)
        if not cached:
            return None
        data, saved_at = cached
        ttl = self._http_cache_ttls.get(key)
        if ttl is None or not self._http_cache.is_fresh(saved_at, ttl):
            return None
        return data

    def _set_http_cache(self, key: str, payload: Any) -> None:
        self._http_cache.set(self._symbol, key, payload)

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

    def _balance_snapshot(self) -> dict[str, Decimal]:
        balances = dict(self._balances)
        base_free = Decimal("0")
        quote_free = Decimal("0")
        if self._base_asset:
            base_free = self.as_decimal(balances.get(self._base_asset, (0.0, 0.0))[0])
        if self._quote_asset:
            quote_free = self.as_decimal(balances.get(self._quote_asset, (0.0, 0.0))[0])
        return {"base_free": base_free, "quote_free": quote_free}

    def _balances_ready_for_start(self) -> bool:
        if not self._balances_loaded:
            return False
        if not self._quote_asset or not self._base_asset:
            return False
        if self._quote_asset not in self._balances or self._base_asset not in self._balances:
            return False
        balance_age_s = self._balance_age_s()
        if balance_age_s is None:
            return False
        return balance_age_s <= 2.0

    def _force_refresh_balances_and_wait(self, timeout_ms: int) -> bool:
        if not self._account_client:
            return False
        loop = QEventLoop()
        result = {"done": False, "ok": False}

        def _on_refresh(success: bool) -> None:
            if result["done"]:
                return
            result["done"] = True
            result["ok"] = success
            loop.quit()

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        self._signals.balances_refresh.connect(_on_refresh)
        self._refresh_balances(force=True)
        timer.start(timeout_ms)
        loop.exec()
        self._signals.balances_refresh.disconnect(_on_refresh)
        timer.stop()
        return result["done"] and result["ok"]

    def _engine_ready(self) -> bool:
        return self._engine_ready_state

    def _update_engine_ready(self) -> None:
        new_ready = (
            self._trade_gate_state == TradeGateState.OK
            and self._account_can_trade
            and self._live_enabled()
        )
        if new_ready == self._engine_ready_state:
            return
        self._engine_ready_state = new_ready
        if new_ready:
            self._append_log("[ENGINE] ready=true (trade_gate=ok)", kind="INFO")

    def _live_enabled(self) -> bool:
        if not hasattr(self, "_dry_run_toggle"):
            return False
        return not self._dry_run_toggle.isChecked()

    def _balance_age_s(self) -> float | None:
        if self._balance_ready_ts_monotonic_ms is None:
            return None
        age_ms = int(monotonic() * 1000) - self._balance_ready_ts_monotonic_ms
        return max(age_ms / 1000.0, 0.0)

    def _reset_defaults(self) -> None:
        defaults = GridSettingsState()
        if self._settings_state == defaults:
            return
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
        try:
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
            worker = _Worker(self._account_client.get_account_info, self._can_emit_worker_results)
            worker.signals.success.connect(self._handle_account_info)
            worker.signals.error.connect(self._handle_account_error)
            self._thread_pool.start(worker)
        except Exception:
            self._logger.exception("[NC_MICRO][CRASH] exception in _refresh_balances")
            self._notify_crash("_refresh_balances")
            return

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
        if self._rules_loaded:
            self._balance_ready_ts_monotonic_ms = int(monotonic() * 1000)
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
        self._signals.balances_refresh.emit(True)
        if self._balances_ready_for_start() and not self._start_locked_until_change:
            self._last_preflight_blocked = False
            self._last_preflight_hash = None

    def _handle_account_error(self, message: str) -> None:
        self._balances_in_flight = False
        self._account_can_trade = False
        self._balances_loaded = False
        self._balance_ready_ts_monotonic_ms = None
        self._account_api_error = True
        self._account_permissions = []
        self._last_account_trade_snapshot = None
        self._set_account_status(self._infer_account_status(message))
        self._append_log(f"balances fetch failed: {message}", kind="WARN")
        self._auto_pause_on_api_error(message)
        self._auto_pause_on_exception(message)
        self._apply_trade_gate()
        self._update_runtime_balances()
        self._signals.balances_refresh.emit(False)

    def _refresh_open_orders(self, force: bool = False) -> None:
        try:
            self._orders_tick_count += 1
            if self._orders_tick_count % 30 == 0:
                self._logger.debug("orders refresh tick")
            if not self._account_client:
                self._set_account_status("no_keys")
                self._open_orders_all = []
                self._open_orders = []
                self._open_orders_map = {}
                self._clear_order_info()
                self._bot_client_ids.clear()
                self._bot_order_keys = set()
                self._active_order_keys.clear()
                self._recent_order_keys.clear()
                self._order_id_to_registry_key.clear()
                self._active_tp_ids.clear()
                self._active_restore_ids.clear()
                self._render_open_orders()
                self._apply_trade_gate()
                return
            if not force:
                snapshot_age_sec = self._snapshot_age_sec()
                if snapshot_age_sec is not None and snapshot_age_sec > STALE_SNAPSHOT_MAX_AGE_SEC:
                    self._append_log("[ORDERS] snapshot stale -> refresh", kind="INFO")
                    force = True
            if self._orders_in_flight and not force:
                return
            self._orders_in_flight = True
            worker = _Worker(lambda: self._account_client.get_open_orders(self._symbol), self._can_emit_worker_results)
            worker.signals.success.connect(self._handle_open_orders)
            worker.signals.error.connect(self._handle_open_orders_error)
            self._thread_pool.start(worker)
        except Exception:
            self._logger.exception("[NC_MICRO][CRASH] exception in _refresh_open_orders")
            self._notify_crash("_refresh_open_orders")
            return

    def _prime_bot_registry_from_exchange(self) -> None:
        if not self._account_client:
            return
        try:
            open_orders = self._account_client.get_open_orders(self._symbol)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[START] openOrders preload failed: {exc}", kind="WARN")
            return
        if not isinstance(open_orders, list):
            return
        self._open_orders_all = [item for item in open_orders if isinstance(item, dict)]
        self._open_orders = self._filter_bot_orders(self._open_orders_all)
        self._open_orders_map = {
            str(order.get("orderId", "")): order
            for order in self._open_orders
            if str(order.get("orderId", ""))
        }
        self._update_order_info_from_snapshot(self._open_orders)
        self._bot_order_keys = {
            key
            for order in self._open_orders
            if (key := self._order_key_from_order(order)) is not None
        }
        for order in self._open_orders:
            order_id = str(order.get("orderId", ""))
            if order_id:
                self._bot_order_ids.add(order_id)
            client_order_id = str(order.get("clientOrderId", ""))
            if client_order_id:
                self._bot_client_ids.add(client_order_id)
        self._sync_active_ids_from_open_orders()
        self._sync_registry_from_open_orders(self._open_orders)

    def _refresh_fills(self) -> None:
        try:
            if self._dry_run_toggle.isChecked():
                return
            if self._state != "RUNNING":
                return
            if not self._account_client:
                return
            if self._fills_in_flight:
                return
            self._fills_in_flight = True
            worker = _Worker(
                lambda: self._account_client.get_my_trades(self._symbol, limit=50),
                self._can_emit_worker_results,
            )
            worker.signals.success.connect(self._handle_fill_poll)
            worker.signals.error.connect(self._handle_fill_poll_error)
            self._thread_pool.start(worker)
        except Exception:
            self._logger.exception("[NC_MICRO][CRASH] exception in _refresh_fills")
            self._notify_crash("_refresh_fills")
            return

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
        self._purge_recent_order_keys()
        previous_map = dict(self._open_orders_map)
        self._open_orders_all = [item for item in result if isinstance(item, dict)]
        self._open_orders = self._filter_bot_orders(self._open_orders_all)
        self._open_orders_map = {
            str(order.get("orderId", "")): order
            for order in self._open_orders
            if str(order.get("orderId", ""))
        }
        self._update_order_info_from_snapshot(self._open_orders)
        self._bot_order_keys = {
            key
            for order in self._open_orders
            if (key := self._order_key_from_order(order)) is not None
        }
        for order in self._open_orders:
            order_id = str(order.get("orderId", ""))
            if order_id:
                self._bot_order_ids.add(order_id)
            client_order_id = str(order.get("clientOrderId", ""))
            if client_order_id:
                self._bot_client_ids.add(client_order_id)
        self._sync_active_ids_from_open_orders()
        self._sync_registry_from_open_orders(self._open_orders)
        closed_order_ids = [
            order_id
            for order_id in list(self._order_id_to_registry_key)
            if order_id and order_id not in self._open_orders_map
        ]
        for order_id in closed_order_ids:
            self._closed_order_ids.add(order_id)
            self._discard_registry_for_order_id(order_id)
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
        return f"BBOT_LAS_v1_{self._symbol}_"

    def _sync_active_ids_from_open_orders(self) -> None:
        tp_prefix = f"{self._bot_order_prefix()}TP_"
        restore_prefixes = (
            f"{self._bot_order_prefix()}RESTORE_",
            f"{self._bot_order_prefix()}R_",
        )
        tp_ids: set[str] = set()
        restore_ids: set[str] = set()
        for order in self._open_orders_all:
            if not isinstance(order, dict):
                continue
            client_order_id = str(order.get("clientOrderId", ""))
            if client_order_id.startswith(tp_prefix):
                tp_ids.add(client_order_id)
            elif any(client_order_id.startswith(prefix) for prefix in restore_prefixes):
                restore_ids.add(client_order_id)
        self._active_tp_ids = tp_ids
        self._active_restore_ids = restore_ids
        self._pending_tp_ids.difference_update(tp_ids)
        self._pending_restore_ids.difference_update(restore_ids)

    def _clear_local_order_registry(self) -> None:
        self._active_order_keys.clear()
        self._recent_order_keys.clear()
        self._order_id_to_registry_key.clear()
        self._registry_key_last_seen_ts.clear()

    def _rebuild_registry_from_open_orders(self, orders: list[dict[str, Any]]) -> None:
        self._clear_local_order_registry()
        if orders:
            self._sync_registry_from_open_orders(orders)

    def _sync_registry_from_open_orders(self, orders: list[dict[str, Any]]) -> None:
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        now = time_fn()
        for order in orders:
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("orderId", ""))
            side = str(order.get("side", "")).upper()
            if not side:
                continue
            price = self._coerce_float(str(order.get("price", ""))) or 0.0
            qty = self._coerce_float(str(order.get("origQty", ""))) or 0.0
            if price <= 0 or qty <= 0:
                continue
            client_order_id = str(order.get("clientOrderId", ""))
            order_type = self._order_registry_type_from_client_id(client_order_id)
            price_key = self._format_decimal(self.q_price(self.as_decimal(price), tick), tick)
            qty_key = self._format_decimal(self.q_qty(self.as_decimal(qty), step), step)
            registry_key = self._order_registry_key(side, price_key, qty_key, order_type)
            if registry_key not in self._active_order_keys:
                self._register_order_key(registry_key)
            self._registry_key_last_seen_ts[registry_key] = now
            if order_id:
                self._order_id_to_registry_key[order_id] = registry_key

    def _has_open_order_client_id(self, client_id: str) -> bool:
        if not client_id:
            return False
        for order in self._open_orders_all:
            if not isinstance(order, dict):
                continue
            if str(order.get("clientOrderId", "")) == client_id:
                return True
        return False

    def _has_open_order_type(self, order_type: str) -> bool:
        prefix = f"{self._bot_order_prefix()}{order_type}"
        if not prefix:
            return False
        for order in self._open_orders_all:
            if not isinstance(order, dict):
                continue
            client_order_id = str(order.get("clientOrderId", ""))
            if client_order_id.startswith(prefix):
                return True
        return False

    @staticmethod
    def _order_registry_type(reason: str) -> str:
        reason_upper = reason.upper()
        if reason_upper.startswith("TP"):
            return "TP"
        if reason_upper == "RESTORE":
            return "RESTORE"
        return "GRID"

    def _order_registry_type_from_client_id(self, client_order_id: str) -> str:
        if not client_order_id:
            return "GRID"
        prefix = self._bot_order_prefix()
        if client_order_id.startswith(f"{prefix}TP_"):
            return "TP"
        if client_order_id.startswith(f"{prefix}RESTORE_") or client_order_id.startswith(f"{prefix}R_"):
            return "RESTORE"
        return "GRID"

    def _order_registry_key(self, side: str, price_str: str, qty_str: str, order_type: str) -> str:
        return f"{self._symbol}:{side}:{price_str}:{qty_str}:{order_type}"

    def _purge_recent_order_keys(self) -> None:
        if not self._recent_order_keys:
            return
        now = monotonic()
        expired = [key for key, expiry in self._recent_order_keys.items() if expiry <= now]
        for key in expired:
            self._recent_order_keys.pop(key, None)

    def _mark_recent_order_key(self, key: str, ttl_s: float) -> None:
        self._recent_order_keys[key] = monotonic() + ttl_s

    def _register_order_key(self, key: str, ttl_s: float | None = None) -> None:
        self._active_order_keys.add(key)
        self._registry_key_last_seen_ts[key] = time_fn()
        self._mark_recent_order_key(key, ttl_s or self._recent_key_ttl_s)

    def _discard_order_registry_key(self, key: str, *, drop_recent: bool = False) -> None:
        self._active_order_keys.discard(key)
        self._registry_key_last_seen_ts.pop(key, None)
        if drop_recent:
            self._recent_order_keys.pop(key, None)

    def _discard_registry_for_order_id(self, order_id: str) -> None:
        key = self._order_id_to_registry_key.pop(order_id, None)
        self._order_id_to_level_index.pop(order_id, None)
        if key:
            self._discard_order_registry_key(key, drop_recent=True)

    def _discard_registry_for_values(self, side: str, price: Decimal, qty: Decimal) -> None:
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        price_key = self._format_decimal(self.q_price(price, tick), tick)
        qty_key = self._format_decimal(self.q_qty(qty, step), step)
        prefix = f"{self._symbol}:{side}:{price_key}:{qty_key}:"
        keys = [key for key in self._active_order_keys if key.startswith(prefix)]
        for key in keys:
            self._discard_order_registry_key(key, drop_recent=True)

    def _discard_registry_for_order(self, order: dict[str, Any]) -> None:
        order_id = str(order.get("orderId", ""))
        if order_id:
            self._discard_registry_for_order_id(order_id)
        side = str(order.get("side", "")).upper()
        if not side:
            return
        price = self._coerce_float(str(order.get("price", ""))) or 0.0
        qty = self._coerce_float(str(order.get("origQty", ""))) or 0.0
        if price > 0 and qty > 0:
            self._discard_registry_for_values(side, self.as_decimal(price), self.as_decimal(qty))

    def _gc_registry_keys(self) -> None:
        try:
            now = time_fn()
            if self._registry_gc_last_ts and now - self._registry_gc_last_ts < REGISTRY_GC_INTERVAL_MS / 1000:
                return
            self._registry_gc_last_ts = now
            expired_keys = [
                key
                for key, last_seen in self._registry_key_last_seen_ts.items()
                if now - last_seen > REGISTRY_GC_TTL_SEC
            ]
            for key in expired_keys:
                self._discard_order_registry_key(key, drop_recent=True)
            for order_id, key in list(self._order_id_to_registry_key.items()):
                if order_id in self._open_orders_map:
                    continue
                last_seen = self._registry_key_last_seen_ts.get(key)
                if last_seen is None or now - last_seen > REGISTRY_GC_TTL_SEC:
                    self._discard_registry_for_order_id(order_id)
            stale_cutoff = now - STALE_COOLDOWN_SEC
            self._pilot_stale_handled = {
                order_id: ts for order_id, ts in self._pilot_stale_handled.items() if ts >= stale_cutoff
            }
            self._pilot_stale_skip_log_ts = {
                order_id: ts for order_id, ts in self._pilot_stale_skip_log_ts.items() if ts >= stale_cutoff
            }
            self._pilot_stale_order_log_ts = {
                order_id: ts for order_id, ts in self._pilot_stale_order_log_ts.items() if ts >= stale_cutoff
            }
            self._pilot_stale_check_log_ts = {
                order_id: ts for order_id, ts in self._pilot_stale_check_log_ts.items() if ts >= stale_cutoff
            }
        except Exception:
            self._logger.exception("[NC_MICRO][CRASH] exception in _gc_registry_keys")
            self._notify_crash("_gc_registry_keys")
            return

    @staticmethod
    def _classify_2010_reason(message: str) -> str:
        lower_message = message.lower()
        if "duplicate order sent" in lower_message:
            return "DUPLICATE"
        if "insufficient balance" in lower_message or "not enough balance" in lower_message:
            return "INSUFFICIENT_BALANCE"
        return "UNKNOWN"

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

    def _discard_order_key_from_order(self, order: dict[str, Any]) -> None:
        key = self._order_key_from_order(order)
        if key:
            self._bot_order_keys.discard(key)

    @staticmethod
    def _limit_client_order_id(client_id: str) -> str:
        return client_id[:36]

    def _make_client_order_id(self, role: str, idx: int, suffix: str = "") -> str:
        prefix = self._bot_order_prefix()
        short_id = f"{uuid4().hex[:4]}{idx:02d}"
        return self._limit_client_order_id(f"{prefix}{role}_{short_id}{suffix}")

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

        worker = _Worker(_reconcile, self._can_emit_worker_results)
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
                client_order_id = str(order.get("clientOrderId", ""))
                if client_order_id:
                    self._active_tp_ids.discard(client_order_id)
                    self._active_restore_ids.discard(client_order_id)
                    self._pending_tp_ids.discard(client_order_id)
                    self._pending_restore_ids.discard(client_order_id)
                if order_id:
                    self._closed_order_ids.add(order_id)
                    self._discard_registry_for_order_id(order_id)
                continue
            if status in {"CANCELED", "EXPIRED", "REJECTED"}:
                client_order_id = str(order.get("clientOrderId", ""))
                if client_order_id:
                    self._active_tp_ids.discard(client_order_id)
                    self._active_restore_ids.discard(client_order_id)
                    self._pending_tp_ids.discard(client_order_id)
                    self._pending_restore_ids.discard(client_order_id)
                self._discard_registry_for_order(order)
                if order_id:
                    status_key = (order_id, status)
                    if status_key in self._closed_order_statuses:
                        continue
                    self._closed_order_statuses.add(status_key)
                    self._closed_order_ids.add(order_id)
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
        if fill.order_id:
            existing_order = self._open_orders_map.get(fill.order_id, {})
            client_order_id = str(existing_order.get("clientOrderId", ""))
            if client_order_id:
                self._active_tp_ids.discard(client_order_id)
                self._active_restore_ids.discard(client_order_id)
                self._pending_tp_ids.discard(client_order_id)
                self._pending_restore_ids.discard(client_order_id)
        if fill.order_id:
            self._discard_registry_for_order_id(fill.order_id)
        else:
            self._discard_registry_for_values(
                fill.side,
                self.as_decimal(fill.price),
                self.as_decimal(fill.qty),
            )
        if fill.trade_id:
            self._seen_trade_ids.add(fill.trade_id)
        total_filled = self.as_decimal(fill.qty)
        delta = total_filled
        if fill.order_id:
            total_filled, delta = self._fill_accumulator.record(
                fill.order_id,
                self.as_decimal(fill.qty),
                is_total=False,
            )
        if delta <= 0:
            return
        self._record_fill(fill)
        self._apply_local_balance_fill(fill)
        self._bot_order_keys.discard(
            self._order_key(fill.side, self.as_decimal(fill.price), self.as_decimal(fill.qty))
        )
        step = self._rule_decimal(self._exchange_rules.get("step"))
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        self._append_log(
            (
                f"[LIVE] FILLED orderId={fill.order_id} side={fill.side}"
                f" total={self.fmt_qty(total_filled, step)} delta={self.fmt_qty(delta, step)}"
            ),
            kind="ORDERS",
        )
        self._append_log(
            (
                f"[FILL] side={fill.side} orderId={fill.order_id} "
                f"price={self.fmt_price(self.as_decimal(fill.price), tick)} "
                f"qty={self.fmt_qty(self.as_decimal(fill.qty), step)}"
            ),
            kind="INFO",
        )
        self._place_replacement_order(fill, delta_qty=delta)
        if fill.order_id:
            self._fill_accumulator.mark_handled(fill.order_id, total_filled)
        self._maybe_enable_bootstrap_sell_side(fill)

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

    def _effective_fee_rate(self) -> Decimal:
        maker, taker = self._trade_fees
        maker_rate = maker if maker is not None else 0.0
        taker_rate = taker if taker is not None else 0.0
        fee_rate = max(maker_rate, taker_rate, 0.0)
        return self.as_decimal(fee_rate)

    def _profit_profile_name(self) -> str:
        return "BALANCED"

    def _runtime_profit_inputs(self) -> dict[str, Any]:
        return {
            "expected_fill_mode": "MAKER",
            "slippage_pct": 0.02,
            "safety_edge_pct": 0.02,
            "fee_discount_pct": None,
        }

    def _profit_guard_fee_inputs(self) -> tuple[float, float, bool]:
        maker, taker = self._trade_fees
        used_default = False
        if maker is None:
            maker_pct = PROFIT_GUARD_DEFAULT_MAKER_FEE_PCT
            used_default = True
        else:
            maker_pct = maker * 100
        if taker is None:
            taker_pct = PROFIT_GUARD_DEFAULT_TAKER_FEE_PCT
            used_default = True
        else:
            taker_pct = taker * 100
        return maker_pct, taker_pct, used_default

    def _profit_guard_round_trip_cost_pct(self) -> float:
        maker_pct, taker_pct, _used_default = self._profit_guard_fee_inputs()
        fill_mode = self._runtime_profit_inputs().get("expected_fill_mode") or "MAKER"
        if fill_mode == "TAKER":
            fee_cost = maker_pct + taker_pct
        else:
            fee_cost = 2 * maker_pct
        slippage_pct = PROFIT_GUARD_SLIPPAGE_BPS * 0.01
        return fee_cost + slippage_pct + PROFIT_GUARD_EXTRA_BUFFER_PCT

    def _profit_guard_min_profit_pct(self) -> float:
        round_trip_cost = self._profit_guard_round_trip_cost_pct()
        return max(round_trip_cost + PROFIT_GUARD_BUFFER_PCT, PROFIT_GUARD_MIN_FLOOR_PCT)

    def _current_spread_pct(self) -> float | None:
        best_bid, best_ask, _source = self._resolve_book_bid_ask()
        return self._compute_spread_pct_from_book(best_bid, best_ask)

    def _min_tick_step_pct(self, anchor_price: float) -> float:
        tick = self._exchange_rules.get("tick")
        if not tick or anchor_price <= 0:
            return 0.0
        return tick / anchor_price * 100

    def _profit_guard_should_hold(self, min_profit_pct: float) -> tuple[str, float | None, float | None]:
        spread_pct = self._current_spread_pct()
        volatility_pct = self._compute_recent_volatility_pct()
        if volatility_pct is not None and volatility_pct <= 0:
            volatility_pct = None
        if self._kpi_state != KPI_STATE_OK:
            return "OK", spread_pct, volatility_pct
        if spread_pct is None or spread_pct <= 0:
            return "OK", spread_pct, volatility_pct
        if volatility_pct is None:
            return "OK", spread_pct, volatility_pct
        round_trip_cost = self._profit_guard_round_trip_cost_pct()
        if min_profit_pct > spread_pct + round_trip_cost:
            return "HOLD", spread_pct, volatility_pct
        return "OK", spread_pct, volatility_pct

    def _apply_profit_guard(
        self,
        settings: GridSettingsState,
        anchor_price: float,
        *,
        action_label: str,
        update_ui: bool,
    ) -> str:
        min_profit_pct = self._profit_guard_min_profit_pct()
        tp_old = settings.take_profit_pct
        step_old = settings.grid_step_pct
        range_low_old = settings.range_low_pct
        range_high_old = settings.range_high_pct
        min_tick_step_pct = self._min_tick_step_pct(anchor_price)
        min_step_pct = max(min_tick_step_pct, min_profit_pct / 2)
        if settings.take_profit_pct < min_profit_pct:
            settings.take_profit_pct = min_profit_pct
        if settings.grid_step_pct < min_step_pct:
            settings.grid_step_pct = min_step_pct
        if settings.take_profit_pct < settings.grid_step_pct:
            settings.take_profit_pct = settings.grid_step_pct
        min_range_pct = settings.grid_step_pct * max(settings.grid_count, 1) / 2
        if settings.range_low_pct < min_range_pct:
            settings.range_low_pct = min_range_pct
        if settings.range_high_pct < min_range_pct:
            settings.range_high_pct = min_range_pct
        if settings.take_profit_pct != tp_old:
            self._append_log(
                (
                    f"[GUARD] tp raised {tp_old:.4f}% -> {settings.take_profit_pct:.4f}% "
                    f"min_profit={min_profit_pct:.4f}% action={action_label}"
                ),
                kind="INFO",
            )
        if settings.grid_step_pct != step_old:
            self._append_log(
                (
                    f"[GUARD] step raised {step_old:.4f}% -> {settings.grid_step_pct:.4f}% "
                    f"min_step={min_step_pct:.4f}% action={action_label}"
                ),
                kind="INFO",
            )
        if settings.range_low_pct != range_low_old or settings.range_high_pct != range_high_old:
            self._append_log(
                (
                    "[GUARD] range widened "
                    f"low {range_low_old:.4f}% -> {settings.range_low_pct:.4f}% "
                    f"high {range_high_old:.4f}% -> {settings.range_high_pct:.4f}%"
                ),
                kind="INFO",
            )
        if update_ui:
            if settings.take_profit_pct != tp_old:
                self._take_profit_input.blockSignals(True)
                self._take_profit_input.setValue(settings.take_profit_pct)
                self._take_profit_input.blockSignals(False)
                self._settings_state.take_profit_pct = settings.take_profit_pct
            if settings.grid_step_pct != step_old:
                self._grid_step_input.blockSignals(True)
                self._grid_step_input.setValue(settings.grid_step_pct)
                self._grid_step_input.blockSignals(False)
                self._settings_state.grid_step_pct = settings.grid_step_pct
                if self._settings_state.grid_step_mode == "MANUAL":
                    self._manual_grid_step_pct = settings.grid_step_pct
            if settings.range_low_pct != range_low_old:
                self._range_low_input.blockSignals(True)
                self._range_low_input.setValue(settings.range_low_pct)
                self._range_low_input.blockSignals(False)
                self._settings_state.range_low_pct = settings.range_low_pct
            if settings.range_high_pct != range_high_old:
                self._range_high_input.blockSignals(True)
                self._range_high_input.setValue(settings.range_high_pct)
                self._range_high_input.blockSignals(False)
                self._settings_state.range_high_pct = settings.range_high_pct
            if (
                settings.take_profit_pct != tp_old
                or settings.grid_step_pct != step_old
                or settings.range_low_pct != range_low_old
                or settings.range_high_pct != range_high_old
            ):
                self._update_grid_preview()
        decision, spread_pct, volatility_pct = self._profit_guard_should_hold(min_profit_pct)
        maker_pct, taker_pct, used_default = self._profit_guard_fee_inputs()
        if used_default:
            self._append_log(
                (
                    f"[GUARD] fees defaulted maker={maker_pct:.4f}% taker={taker_pct:.4f}% "
                    f"round_trip={self._profit_guard_round_trip_cost_pct():.4f}%"
                ),
                kind="INFO",
            )
        if decision == "REQUEST_DATA":
            return "REQUEST_DATA"
        if decision == "HOLD":
            spread_text = self._format_spread_display(spread_pct)
            vol_text = f"{volatility_pct:.4f}%" if volatility_pct is not None else "—"
            self._append_log(
                (
                    "[GUARD] HOLD reason=thin_edge "
                    f"spread={spread_text} min_profit={min_profit_pct:.4f}% vol={vol_text}"
                ),
                kind="WARN",
            )
            return "HOLD"
        return "OK"

    def _evaluate_tp_profitability(self, tp_pct: float) -> dict[str, float | bool]:
        profile = get_profile_preset(self._profit_profile_name())
        inputs = self._runtime_profit_inputs()
        maker, taker = self._trade_fees
        maker_fee_pct = (maker * 100) if maker is not None else 0.0
        taker_fee_pct = (taker * 100) if taker is not None else 0.0
        fee_total_pct = compute_fee_total_pct(
            maker_fee_pct,
            taker_fee_pct,
            fill_mode=inputs.get("expected_fill_mode") or "MAKER",
            fee_discount_pct=inputs.get("fee_discount_pct"),
        )
        return evaluate_tp_profitability(
            tp_pct=tp_pct,
            fee_total_pct=fee_total_pct,
            slippage_pct=inputs.get("slippage_pct"),
            safety_edge_pct=inputs.get("safety_edge_pct"),
            target_profit_pct=profile.target_profit_pct,
        )

    def _evaluate_tp_min_profit(
        self, entry_side: str, entry_price: Decimal, tp_price: Decimal
    ) -> dict[str, float | bool]:
        if entry_price <= 0 or tp_price <= 0:
            return {"is_profitable": False}
        entry_side = entry_side.upper()
        if entry_side == "SELL":
            profit_pct = (entry_price - tp_price) / entry_price * Decimal("100")
        elif entry_side == "BUY":
            profit_pct = (tp_price - entry_price) / entry_price * Decimal("100")
        else:
            return {"is_profitable": False}
        inputs = self._runtime_profit_inputs()
        maker, taker = self._trade_fees
        maker_fee_pct = (maker * 100) if maker is not None else 0.0
        taker_fee_pct = (taker * 100) if taker is not None else 0.0
        fee_total_pct = compute_fee_total_pct(
            maker_fee_pct,
            taker_fee_pct,
            fill_mode=inputs.get("expected_fill_mode") or "MAKER",
            fee_discount_pct=inputs.get("fee_discount_pct"),
        )
        min_profit_pct = compute_break_even_tp_pct(
            fee_total_pct=fee_total_pct,
            slippage_pct=inputs.get("slippage_pct"),
            safety_edge_pct=inputs.get("safety_edge_pct"),
        )
        net_profit_pct = float(profit_pct) - min_profit_pct
        return {
            "profit_pct": round(float(profit_pct), 6),
            "min_profit_pct": round(min_profit_pct, 6),
            "net_profit_pct": round(net_profit_pct, 6),
            "is_profitable": float(profit_pct) >= min_profit_pct,
        }

    def _apply_sell_fee_buffer(self, qty: Decimal, step: Decimal | None) -> Decimal:
        fee_rate = self._effective_fee_rate()
        if fee_rate > 0:
            qty = qty * (Decimal("1") - fee_rate)
        return self.q_qty(qty, step)

    def _apply_local_balance_fill(self, fill: TradeFill) -> None:
        base_asset = self._base_asset
        quote_asset = self._quote_asset
        if not base_asset or not quote_asset:
            return
        base_free, base_locked = self._balances.get(base_asset, (0.0, 0.0))
        quote_free, quote_locked = self._balances.get(quote_asset, (0.0, 0.0))
        base_free_dec = self.as_decimal(base_free)
        quote_free_dec = self.as_decimal(quote_free)
        qty = self.as_decimal(fill.qty)
        quote_qty = self.as_decimal(fill.quote_qty)
        commission = self.as_decimal(fill.commission)
        if fill.side == "BUY":
            base_free_dec += qty
            quote_free_dec -= quote_qty
            if fill.commission_asset == base_asset:
                base_free_dec -= commission
            elif fill.commission_asset == quote_asset:
                quote_free_dec -= commission
        else:
            base_free_dec -= qty
            quote_free_dec += quote_qty
            if fill.commission_asset == base_asset:
                base_free_dec -= commission
            elif fill.commission_asset == quote_asset:
                quote_free_dec -= commission
        base_free_dec = max(base_free_dec, Decimal("0"))
        quote_free_dec = max(quote_free_dec, Decimal("0"))
        self._balances[base_asset] = (float(base_free_dec), base_locked)
        self._balances[quote_asset] = (float(quote_free_dec), quote_locked)
        self._update_runtime_balances()

    def _has_base_balance(self, qty: Decimal, step: Decimal | None) -> bool:
        base_asset = self._base_asset
        if not base_asset:
            return True
        base_free, _ = self._balances.get(base_asset, (0.0, 0.0))
        buffer = step or Decimal("0")
        required = qty + buffer
        return self.as_decimal(base_free) >= required

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
            self._position_fees_paid_quote += fee_usdt
            realized_delta -= fee_usdt
        self._realized_pnl += realized_delta
        if self._pilot_position_qty() <= 0:
            self._position_fees_paid_quote = 0.0
            self._position_state = self._rebuild_position_state()
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

    def _place_replacement_order(self, fill: TradeFill, *, delta_qty: Decimal) -> None:
        if not self._account_client or not self._bot_session_id:
            return
        if self._state not in {"RUNNING", "WAITING_FILLS"}:
            return
        settings = self._live_settings or self._settings_state
        tp_pct = settings.take_profit_pct or settings.grid_step_pct
        if tp_pct <= 0:
            return
        self._rebuild_registry_from_open_orders(self._open_orders)
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        tp_dec = self.as_decimal(tp_pct) / Decimal("100")
        fill_price = self.as_decimal(fill.price)
        fill_qty = delta_qty
        balances_snapshot = self._balance_snapshot()
        base_free = self.as_decimal(balances_snapshot.get("base_free", Decimal("0")))
        quote_free = self.as_decimal(balances_snapshot.get("quote_free", Decimal("0")))
        min_notional = self._rule_decimal(self._exchange_rules.get("min_notional"))
        tp_side = "SELL" if fill.side == "BUY" else "BUY"
        tp_qty = Decimal("0")
        tp_notional = Decimal("0")
        tp_reason = "ok"
        allow_tp = True
        tp_min_profit_detail = ""
        tp_qty_cap = Decimal("0")
        tp_required_qty = fill_qty
        tp_required_quote = Decimal("0")
        if tp_side == "SELL":
            tp_price = self.q_price(fill_price * (Decimal("1") + tp_dec), tick)
            tp_required_qty = fill_qty
            if tp_required_qty > base_free:
                tp_reason = "skip_insufficient_base"
                allow_tp = False
            else:
                tp_qty_cap = min(fill_qty, base_free)
        else:
            tp_price = self.q_price(fill_price * (Decimal("1") - tp_dec), tick)
            tp_required_quote = tp_price * fill_qty
            if tp_required_quote > quote_free:
                tp_reason = "skip_insufficient_quote"
                allow_tp = False
            else:
                quote_cap = quote_free / tp_price if tp_price > 0 else Decimal("0")
                tp_qty_cap = min(fill_qty, quote_cap)
        fill_order_key = fill.order_id or fill.trade_id or self._fill_key(fill)
        tp_client_order_id = self._limit_client_order_id(
            f"BBOT_LAS_v1_{self._symbol}_TP_{fill.side}_{fill_order_key}"
        )
        if allow_tp:
            profitability = self._evaluate_tp_min_profit(fill.side, fill_price, tp_price)
            if not profitability.get("is_profitable", True):
                now_ts = time_fn()
                min_profit_key = tp_client_order_id or fill.order_id or fill_order_key
                last_action_ts = self._tp_min_profit_action_ts.get(min_profit_key)
                if last_action_ts and now_ts - last_action_ts < TP_MIN_PROFIT_COOLDOWN_SEC:
                    tp_reason = "skip_tp_min_profit_cooldown"
                else:
                    self._tp_min_profit_action_ts[min_profit_key] = now_ts
                    tp_reason = "skip_tp_min_profit"
                    tp_min_profit_detail = (
                        f"profit={profitability.get('profit_pct', 0.0):.4f}% "
                        f"min_profit={profitability.get('min_profit_pct', 0.0):.4f}%"
                    )
                allow_tp = False
        if allow_tp and (tp_price <= 0 or tp_qty_cap <= 0):
            tp_reason = "skip_unknown"
            allow_tp = False
        intended_tp_price = tp_price
        intended_tp_qty = tp_qty_cap
        if allow_tp and (
            self._has_open_order_client_id(tp_client_order_id)
            or tp_client_order_id in self._active_tp_ids
        ):
            tp_reason = "skip_duplicate_local"
            allow_tp = False
        if allow_tp and tp_client_order_id in self._pending_tp_ids:
            tp_reason = "skip_duplicate_local"
            allow_tp = False
        if allow_tp:
            desired_tp_notional = tp_price * tp_qty_cap
            if min_notional is not None and desired_tp_notional < min_notional:
                tp_reason = "skip_min_notional"
                allow_tp = False
            else:
                tp_qty, tp_notional, tp_reason = compute_order_qty(
                    tp_side,
                    tp_price,
                    desired_tp_notional,
                    balances_snapshot,
                    self._exchange_rules,
                    self._effective_fee_rate(),
                    None,
                )
                if tp_reason != "ok":
                    if tp_reason == "min_notional":
                        tp_reason = "skip_min_notional"
                    else:
                        tp_reason = "skip_unknown"
                    allow_tp = False
                    tp_qty = Decimal("0")
                    tp_notional = Decimal("0")
                if tp_price <= 0 or tp_qty <= 0:
                    tp_reason = "skip_unknown"
                    allow_tp = False
                    tp_qty = Decimal("0")
                    tp_notional = Decimal("0")
                if allow_tp:
                    intended_tp_qty = tp_qty
        if not allow_tp:
            tp_qty = Decimal("0")
            tp_notional = Decimal("0")
        if allow_tp:
            tp_action_key = build_action_key("TP", fill.order_id, tp_price, tp_qty, step)
            if tp_action_key in self._active_action_keys:
                tp_reason = "skip_duplicate_local"
                allow_tp = False
        if allow_tp:
            self._active_action_keys.add(tp_action_key)
        if allow_tp:
            self._append_log(
                (
                    "[TP] plan "
                    f"tp_side={tp_side} tp_price={self.fmt_price(tp_price, tick)} "
                    f"tp_qty={self.fmt_qty(tp_qty, step)} clientId={tp_client_order_id} reason={tp_reason}"
                ),
                kind="INFO",
            )
        else:
            if tp_side == "SELL":
                tp_detail = (
                    f"required_qty={self._format_balance_decimal(tp_required_qty)} "
                    f"base_free={self._format_balance_decimal(base_free)}"
                )
            else:
                tp_detail = (
                    f"required_quote={self._format_balance_decimal(tp_required_quote)} "
                    f"quote_free={self._format_balance_decimal(quote_free)}"
                )
            tp_key_detail = ""
            if tp_reason == "skip_duplicate_local":
                tp_key_detail = (
                    f"clientId={tp_client_order_id} "
                    f"local_key={build_action_key('TP', fill.order_id, intended_tp_price, intended_tp_qty, step)} "
                )
            if tp_reason == "skip_tp_min_profit" and tp_min_profit_detail:
                tp_key_detail = f"{tp_key_detail}{tp_min_profit_detail} "
            if tp_reason != "skip_tp_min_profit_cooldown":
                self._append_log(
                    (
                        "[SKIP] TP "
                        f"side={tp_side} price={self.fmt_price(intended_tp_price, tick)} "
                        f"qty={self.fmt_qty(intended_tp_qty, step)} "
                        f"reason={tp_reason} {tp_key_detail}{tp_detail}"
                    ),
                    kind="WARN",
                )

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
        min_qty = self._rule_decimal(self._exchange_rules.get("min_qty"))
        restore_qty = Decimal("0")
        restore_notional = Decimal("0")
        restore_reason = "ok"
        target_qty = fill_qty
        if self._pilot_state == PilotState.RECOVERY and restore_side == "BUY":
            restore_reason = "skip_recovery_no_buy"
            target_qty = Decimal("0")

        def _validate_restore_plan() -> tuple[str, Decimal, str, dict[str, Decimal]]:
            debug_fields: dict[str, Decimal] = {
                "required_qty": target_qty,
                "required_quote": restore_price * target_qty,
                "base_free": base_free,
                "quote_free": quote_free,
            }
            if target_qty <= 0:
                return "skip", Decimal("0"), restore_reason, debug_fields
            if restore_side == "SELL":
                if target_qty > base_free:
                    return "skip", Decimal("0"), "skip_insufficient_base", debug_fields
            else:
                if debug_fields["required_quote"] > quote_free:
                    return "skip", Decimal("0"), "skip_insufficient_quote", debug_fields
            qty_rounded = self.q_qty(target_qty, step)
            if qty_rounded <= 0 or (min_qty is not None and qty_rounded < min_qty):
                return "skip", Decimal("0"), "skip_min_qty", debug_fields
            restore_value = restore_price * qty_rounded
            if min_notional is not None and restore_value < min_notional:
                return "skip", Decimal("0"), "skip_min_notional", debug_fields
            return "ok", qty_rounded, "ok", debug_fields

        restore_decision, restore_qty, restore_reason, restore_debug = _validate_restore_plan()
        intended_restore_price = restore_price
        intended_restore_qty = target_qty
        if restore_decision == "ok":
            restore_notional = restore_price * restore_qty
            intended_restore_qty = restore_qty
        allow_restore = restore_decision == "ok" and restore_qty > 0
        filled_order_id = fill.order_id or fill.trade_id or self._fill_key(fill)
        level_index = None
        if fill.order_id:
            level_index = self._order_id_to_level_index.get(fill.order_id)
        if level_index is None:
            level_key = int(
                round(
                    float(restore_price / tick)
                    if tick is not None and tick > 0
                    else float(restore_price)
                )
            )
            level_suffix = str(level_key)
        else:
            level_suffix = str(level_index)
        restore_client_order_id = ""
        if allow_restore:
            restore_client_order_id = self._limit_client_order_id(
                f"BBOT_LAS_v1_{self._symbol}_R_{fill.side}_{filled_order_id}_{level_suffix}"
            )
            if (
                self._has_open_order_client_id(restore_client_order_id)
                or restore_client_order_id in self._active_restore_ids
            ):
                restore_reason = "skip_duplicate_local"
                allow_restore = False
            elif restore_client_order_id in self._pending_restore_ids:
                restore_reason = "skip_duplicate_local"
                allow_restore = False
        if allow_restore:
            restore_action_key = build_action_key("RESTORE", fill.order_id, restore_price, restore_qty, step)
            if restore_action_key in self._active_action_keys:
                restore_reason = "skip_duplicate_local"
                allow_restore = False
        if allow_restore:
            self._active_action_keys.add(restore_action_key)
            self._append_log(
                (
                    "[RESTORE] plan "
                    f"side={restore_side} price={self.fmt_price(restore_price, tick)} "
                    f"qty={self.fmt_qty(restore_qty, step)} clientId={restore_client_order_id} "
                    f"reason={restore_reason}"
                ),
                kind="INFO",
            )
        else:
            if restore_side == "SELL":
                restore_detail = (
                    f"required_qty={self._format_balance_decimal(restore_debug['required_qty'])} "
                    f"base_free={self._format_balance_decimal(restore_debug['base_free'])}"
                )
            else:
                restore_detail = (
                    f"required_quote={self._format_balance_decimal(restore_debug['required_quote'])} "
                    f"quote_free={self._format_balance_decimal(restore_debug['quote_free'])}"
                )
            restore_key_detail = ""
            if restore_reason == "skip_duplicate_local":
                restore_key_detail = (
                    f"clientId={restore_client_order_id} "
                    f"local_key={build_action_key('RESTORE', fill.order_id, intended_restore_price, intended_restore_qty, step)} "
                )
            self._append_log(
                (
                    "[SKIP] RESTORE "
                    f"side={restore_side} price={self.fmt_price(intended_restore_price, tick)} "
                    f"qty={self.fmt_qty(intended_restore_qty, step)} "
                    f"reason={restore_reason} {restore_key_detail}{restore_detail}"
                ),
                kind="WARN",
            )

        if not allow_tp:
            tp_client_order_id = ""
        def _place() -> dict[str, Any]:
            results: dict[str, Any] = {"tp": None, "restore": None, "errors": []}
            if allow_tp and tp_client_order_id and tp_qty > 0:
                self._pending_tp_ids.add(tp_client_order_id)
                tp_response, tp_error, tp_status = self._place_limit(
                    tp_side,
                    tp_price,
                    tp_qty,
                    tp_client_order_id,
                    reason="tp",
                    skip_open_order_duplicate=True,
                    skip_registry=False,
                )
                if tp_response:
                    self._pending_tp_ids.discard(tp_client_order_id)
                    self._active_tp_ids.add(tp_client_order_id)
                else:
                    self._pending_tp_ids.discard(tp_client_order_id)
                    if tp_status == "skip_duplicate_exchange":
                        self._signals.log_append.emit(
                            f"[TP] skip reason=skip_duplicate_exchange clientId={tp_client_order_id}",
                            "WARN",
                        )
                if tp_response:
                    results["tp"] = tp_response
                if tp_error and tp_status != "skip_duplicate_exchange":
                    results["errors"].append(tp_error)
                self._sleep_ms(75)
            if allow_restore and restore_qty > 0 and restore_client_order_id:
                self._pending_restore_ids.add(restore_client_order_id)
                restore_response, restore_error, restore_status = self._place_limit(
                    restore_side,
                    restore_price,
                    restore_qty,
                    restore_client_order_id,
                    reason="restore",
                    skip_open_order_duplicate=True,
                    skip_registry=False,
                )
                if restore_response:
                    self._pending_restore_ids.discard(restore_client_order_id)
                    self._active_restore_ids.add(restore_client_order_id)
                else:
                    self._pending_restore_ids.discard(restore_client_order_id)
                    if restore_status == "skip_duplicate_exchange":
                        self._signals.log_append.emit(
                            f"[RESTORE] skip reason=skip_duplicate_exchange clientId={restore_client_order_id}",
                            "WARN",
                        )
                if restore_response:
                    results["restore"] = restore_response
                if restore_error and restore_status != "skip_duplicate_exchange":
                    results["errors"].append(restore_error)
            return results

        worker = _Worker(_place, self._can_emit_worker_results)
        worker.signals.success.connect(self._handle_replacement_order)
        worker.signals.error.connect(self._handle_live_order_error)
        self._thread_pool.start(worker)

    def _maybe_enable_bootstrap_sell_side(self, fill: TradeFill) -> None:
        if fill.side != "BUY":
            return
        balance_snapshot = self._balance_snapshot()
        base_free = self.as_decimal(balance_snapshot.get("base_free", Decimal("0")))
        if self._bootstrap_mode and not self._sell_side_enabled:
            step = self._rule_decimal(self._exchange_rules.get("step"))
            min_qty = self._rule_decimal(self._exchange_rules.get("min_qty")) or Decimal("0")
            required_base = min_qty + self._base_dust_buffer(step)
            if base_free < required_base:
                return
        if not self._sell_side_enabled:
            self._sell_side_enabled = True
            self._append_log("[STATE] sell_side_enabled -> true", kind="INFO")
        if not self._bootstrap_mode or self._bootstrap_sell_enabled:
            return
        settings = self._live_settings or self._settings_state
        reference_price = self._last_price or fill.price
        if not settings or reference_price <= 0:
            return
        sell_orders = self._grid_engine.build_side_plan(
            settings,
            last_price=reference_price,
            rules=self._exchange_rules,
            side="SELL",
        )
        sell_orders = self._limit_sell_plan_by_balance(sell_orders, base_free)
        sell_orders = sell_orders[: settings.max_active_orders]
        if not sell_orders:
            self._bootstrap_sell_enabled = True
            self._bootstrap_mode = False
            return
        self._append_log(
            "[ENGINE] first buy filled -> enabling SELL side + TP + restore",
            kind="INFO",
        )
        self._bootstrap_sell_enabled = True
        self._bootstrap_mode = False
        self._place_live_orders(sell_orders)

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
            client_order_id = str(entry.get("clientOrderId", ""))
            if client_order_id:
                self._bot_client_ids.add(client_order_id)
            side = str(entry.get("side", "")).upper()
            price = self._coerce_float(str(entry.get("price", ""))) or 0.0
            qty = self._coerce_float(str(entry.get("origQty", ""))) or 0.0
            if side in {"BUY", "SELL"} and price > 0 and qty > 0:
                self._bot_order_keys.add(
                    self._order_key(side, self.as_decimal(price), self.as_decimal(qty))
                )
            self._append_log(f"[LIVE] PLACE {label} orderId={order_id}", kind="ORDERS")
            if client_order_id:
                if label == "TP":
                    self._active_tp_ids.add(client_order_id)
                    self._pending_tp_ids.discard(client_order_id)
                elif label == "RESTORE":
                    self._active_restore_ids.add(client_order_id)
                    self._pending_restore_ids.discard(client_order_id)
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
        skip_open_order_duplicate: bool = False,
        skip_registry: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None, str]:
        if not self._account_client:
            return None, "[LIVE] place skipped: no account client", "skip_no_account"
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
            return None, None, "skip_min_qty"
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
                return None, None, "skip_min_notional"
        if price <= 0 or qty <= 0:
            self._signals.log_append.emit(
                (
                    "[LIVE] place skipped: invalid "
                    f"side={side} price={self.fmt_price(price, tick)} qty={self.fmt_qty(qty, step)}"
                ),
                "WARN",
            )
            return None, None, "skip_invalid"
        if client_id and self._has_open_order_client_id(client_id):
            self._signals.log_append.emit(
                (
                    "[LIVE] skipped: duplicate reason=exchange "
                    f"clientId={client_id} side={side} price={self.fmt_price(price, tick)} "
                    f"qty={self.fmt_qty(qty, step)}"
                ),
                "WARN",
            )
            return None, "skip_duplicate_exchange", "skip_duplicate_exchange"
        self._purge_recent_order_keys()
        price_str = self.fmt_price(price, tick)
        qty_str = self.fmt_qty(qty, step)
        order_type = self._order_registry_type(reason)
        registry_key = None
        if not skip_registry:
            registry_key = self._order_registry_key(side, price_str, qty_str, order_type)
            if registry_key in self._active_order_keys or registry_key in self._recent_order_keys:
                self._signals.log_append.emit(
                    (
                        f"[LIVE] skip duplicate key={registry_key} reason=registry "
                        f"side={side} price={price_str} qty={qty_str}"
                    ),
                    "WARN",
                )
                return None, None, "skip_registry"
        if not skip_open_order_duplicate and self._has_duplicate_order(
            side,
            price,
            qty,
            tolerance_ticks=1,
            ignore_order_id=ignore_order_id,
            ignore_keys=ignore_keys,
        ):
            self._signals.log_append.emit(
                (
                    "[LIVE] skipped: duplicate reason=open_orders "
                    f"side={side} price={price_str} qty={qty_str}"
                ),
                "WARN",
            )
            return None, None, "skip_open_orders"
        if registry_key:
            self._register_order_key(registry_key)
        optimistic_added = False
        open_key = self._order_key(side, price, qty)
        if open_key not in self._bot_order_keys:
            self._bot_order_keys.add(open_key)
            optimistic_added = True
        log_message = (
            f"[LIVE] place {reason} side={side} price={price_str} qty={qty_str} "
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
            if optimistic_added:
                self._bot_order_keys.discard(open_key)
            status, code, message, response_body = self._parse_binance_exception(exc)
            if code == -2010:
                reason_tag = self._classify_2010_reason(message)
                if reason_tag == "DUPLICATE":
                    if registry_key:
                        self._discard_order_registry_key(registry_key)
                        self._mark_recent_order_key(registry_key, self._recent_key_ttl_s)
                    self._signals.log_append.emit(
                        (
                            "[LIVE] skipped: duplicate reason=exchange "
                            f"side={side} price={price_str} qty={qty_str} "
                            f"status={status} code={code} msg={message} response={response_body}"
                        ),
                        "WARN",
                    )
                    QTimer.singleShot(0, self, lambda: self._refresh_open_orders(force=True))
                    return None, "skip_duplicate_exchange", "skip_duplicate_exchange"
                if reason_tag == "INSUFFICIENT_BALANCE":
                    if registry_key:
                        self._discard_order_registry_key(registry_key)
                        self._mark_recent_order_key(registry_key, self._recent_key_insufficient_ttl_s)
                    self._signals.log_append.emit(
                        (
                            "[LIVE] skipped: insufficient_balance "
                            f"side={side} price={price_str} qty={qty_str} "
                            f"status={status} code={code} msg={message} response={response_body}"
                        ),
                        "WARN",
                    )
                    return None, None, "skip_insufficient_balance"
                if registry_key:
                    self._discard_order_registry_key(registry_key, drop_recent=True)
                self._signals.log_append.emit(
                    (
                        "[LIVE] place failed: unknown_2010 "
                        f"side={side} price={price_str} qty={qty_str} "
                        f"status={status} code={code} msg={message} response={response_body}"
                    ),
                    "ERROR",
                )
                return None, None, "error_2010"
            if registry_key:
                self._discard_order_registry_key(registry_key, drop_recent=True)
            message = self._format_binance_exception(
                exc,
                context=f"place {reason}",
                side=side,
                price=price,
                qty=qty,
                notional=notional,
            )
            return None, message, "error"
        order_id = str(response.get("orderId", "")) if isinstance(response, dict) else ""
        if order_id:
            if registry_key:
                self._order_id_to_registry_key[order_id] = registry_key
        return response, None, "ok"

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

    @staticmethod
    def _format_balance_decimal(value: Decimal) -> str:
        quant = Decimal("0.00000001")
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
    def _base_dust_buffer(step: Decimal | None) -> Decimal:
        if step is None or step <= 0:
            return Decimal("0")
        buffer_value = step * Decimal("2")
        return max(Decimal("0"), buffer_value)

    def _limit_sell_plan_by_balance(
        self,
        planned: list[GridPlannedOrder],
        base_free: Decimal,
    ) -> list[GridPlannedOrder]:
        step = self._rule_decimal(self._exchange_rules.get("step"))
        base_dust_buffer = self._base_dust_buffer(step)
        max_sell_qty_total = max(Decimal("0"), base_free - base_dust_buffer)
        if max_sell_qty_total <= 0:
            return [order for order in planned if order.side != "SELL"]
        total_sell_qty = Decimal("0")
        trimmed: list[GridPlannedOrder] = []
        stop_sells = False
        for order in planned:
            if order.side != "SELL":
                trimmed.append(order)
                continue
            if stop_sells:
                continue
            order_qty = self.as_decimal(order.qty)
            if total_sell_qty + order_qty > max_sell_qty_total:
                stop_sells = True
                continue
            total_sell_qty += order_qty
            trimmed.append(order)
        return trimmed

    @staticmethod
    def _rule_decimal(value: float | None) -> Decimal | None:
        if value is None:
            return None
        return LiteAllStrategyNcMicroWindow.as_decimal(value)

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

    def _cancel_order_idempotent(self, order_id: str) -> tuple[bool, str | None]:
        if not order_id or not self._account_client:
            return False, None
        if order_id in self._closed_order_ids:
            return False, None
        try:
            self._account_client.cancel_order(self._symbol, order_id)
        except Exception as exc:  # noqa: BLE001
            _status, code, _message, _response = self._parse_binance_exception(exc)
            if code == -2011:
                self._signals.log_append.emit(
                    f"[INFO] cancel unknown -> treated as done orderId={order_id}",
                    "INFO",
                )
                self._pilot_pending_cancel_ids.add(order_id)
                self._closed_order_ids.add(order_id)
                self._discard_registry_for_order_id(order_id)
                self._open_orders_map.pop(order_id, None)
                self._order_info_map.pop(order_id, None)
                self._bot_order_ids.discard(order_id)
                return True, None
            return False, self._format_cancel_exception(exc, order_id)
        self._pilot_pending_cancel_ids.add(order_id)
        self._closed_order_ids.add(order_id)
        self._discard_registry_for_order_id(order_id)
        self._open_orders_map.pop(order_id, None)
        self._order_info_map.pop(order_id, None)
        self._bot_order_ids.discard(order_id)
        return True, None

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
        if not force:
            cached = self._get_http_cached("exchange_info_symbol")
            if isinstance(cached, dict):
                self._handle_exchange_info(cached, latency_ms=0)
                return
        self._rules_in_flight = True
        worker = _Worker(lambda: self._http_client.get_exchange_info_symbol(self._symbol), self._can_emit_worker_results)
        worker.signals.success.connect(self._handle_exchange_info)
        worker.signals.error.connect(self._handle_exchange_error)
        self._thread_pool.start(worker)

    def _sync_account_time(self) -> None:
        if not self._account_client:
            return
        worker = _Worker(self._account_client.sync_time_offset, self._can_emit_worker_results)
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
        self._set_http_cache("exchange_info_symbol", result)
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
        self._balance_ready_ts_monotonic_ms = None
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
        now = time_fn()
        if not force and self._fees_last_fetch_ts and now - self._fees_last_fetch_ts < 1800:
            return
        self._fees_in_flight = True
        worker = _Worker(lambda: self._account_client.get_trade_fees(self._symbol), self._can_emit_worker_results)
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
        self._fees_last_fetch_ts = time_fn()
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
        self._fees_last_fetch_ts = time_fn()
        self._update_rules_label()

    def _apply_trade_gate(self) -> None:
        gate = self._determine_trade_gate()
        if gate != self._trade_gate:
            self._trade_gate = gate
        state, reason = self._determine_trade_gate_state()
        if state != self._trade_gate_state:
            self._append_log(
                f"[TRADE_GATE] state: {self._trade_gate_state.value} -> {state.value} "
                f"(reason={reason})",
                kind="INFO",
            )
            self._trade_gate_state = state
        self._update_engine_ready()
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

    def _determine_trade_gate_state(self) -> tuple[TradeGateState, str]:
        if not self._has_api_keys:
            return TradeGateState.READ_ONLY_API_ERROR, "no keys"
        if self._account_api_error or not self._can_read_account:
            return TradeGateState.READ_ONLY_API_ERROR, "api error"
        if not self._live_enabled():
            return TradeGateState.READ_ONLY_NO_LIVE_CONFIRM, "live disabled"
        if not self._live_mode_confirmed:
            return TradeGateState.READ_ONLY_NO_LIVE_CONFIRM, "no live confirm"
        return TradeGateState.OK, "ok"

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
            self._set_rules_label_text(tr("rules_line", rules="—"))
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
        self._set_rules_label_text(tr("rules_line", rules=rules))
        if maker is not None or taker is not None:
            self._market_fee.setText(f"{maker_text}/{taker_text}")
            self._set_market_label_state(self._market_fee, active=True)

    def _set_rules_label_text(self, text: str) -> None:
        if not hasattr(self, "_rules_label"):
            return
        metrics = self._rules_label.fontMetrics()
        available_width = max(self._rules_label.width(), 240)
        self._rules_label.setToolTip(text)
        self._rules_label.setText(metrics.elidedText(text, Qt.ElideRight, available_width))

    def _copy_all_logs(self) -> None:
        if not hasattr(self, "_log_view"):
            return
        QApplication.clipboard().setText(self._log_view.toPlainText())

    def _write_crash_log_line(self, message: str) -> None:
        try:
            self._crash_file.write(f"{message}\n")
            self._crash_file.flush()
        except Exception:
            return

    def _handle_dump_threads(self) -> None:
        timestamp = datetime.now().isoformat()
        self._write_crash_log_line(f"=== DUMP THREADS ts={timestamp} ===")
        try:
            faulthandler.dump_traceback(file=self._crash_file, all_threads=True)
        except Exception:
            return

    def _handle_about_to_quit(self) -> None:
        self._logger.info("[NC_MICRO] aboutToQuit received")
        self._write_crash_log_line(f"[NC_MICRO] aboutToQuit received ts={datetime.now().isoformat()}")

    def _toggle_logs_panel(self) -> None:
        if not hasattr(self, "_right_splitter"):
            return
        sizes = self._right_splitter.sizes()
        if len(sizes) < 2:
            return
        if sizes[1] == 0:
            restored = getattr(self, "_logs_splitter_sizes", None)
            if not restored or len(restored) < 2:
                restored = [720, 240]
            self._right_splitter.setSizes(restored)
        else:
            self._logs_splitter_sizes = sizes
            self._right_splitter.setSizes([max(1, sizes[0] + sizes[1]), 0])

    @staticmethod
    def _extract_filter_value(filters: object, filter_type: str, key: str) -> float | None:
        if not isinstance(filters, list):
            return None
        for entry in filters:
            if not isinstance(entry, dict):
                continue
            if entry.get("filterType") == filter_type:
                return LiteAllStrategyNcMicroWindow._coerce_float(str(entry.get(key, "")))
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
        now_ms = int(time_fn() * 1000)
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
        now_ms = int(time_fn() * 1000)
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
        balance_snapshot = self._balance_snapshot()
        base_free = float(balance_snapshot.get("base_free", Decimal("0")))
        skip_sells = base_free <= 0
        step = self._rule_decimal(self._exchange_rules.get("step"))
        has_sells = any(order.side == "SELL" for order in planned)
        if skip_sells and has_sells:
            self._append_log(
                "[LIVE] sell orders skipped: base balance is 0.",
                kind="INFO",
            )
        self._rebuild_registry_from_open_orders(self._open_orders)

        def _place() -> dict[str, Any]:
            results: list[dict[str, Any]] = []
            errors: list[str] = []
            last_open_orders: list[dict[str, Any]] | None = None
            working_balances = {
                "base_free": self.as_decimal(balance_snapshot.get("base_free", Decimal("0"))),
                "quote_free": self.as_decimal(balance_snapshot.get("quote_free", Decimal("0"))),
            }
            for idx, order in enumerate(planned, start=1):
                if skip_sells and order.side == "SELL":
                    continue
                order_type = f"GRID_{order.side}"
                if self._has_open_order_type(order_type):
                    if order.side == "SELL":
                        detail = (
                            f"required_qty={self._format_balance_decimal(self.as_decimal(order.qty))} "
                            f"base_free={self._format_balance_decimal(working_balances['base_free'])}"
                        )
                    else:
                        required_notional = self.as_decimal(order.price) * self.as_decimal(order.qty)
                        detail = (
                            f"required_quote={self._format_balance_decimal(required_notional)} "
                            f"quote_free={self._format_balance_decimal(working_balances['quote_free'])}"
                        )
                    self._signals.log_append.emit(
                        (
                            "[SKIP] GRID "
                            f"side={order.side} price={self.fmt_price(self.as_decimal(order.price), None)} "
                            f"qty={self.fmt_qty(self.as_decimal(order.qty), step)} "
                            f"reason=skip_duplicate_local {detail}"
                        ),
                        "WARN",
                    )
                    continue
                client_order_id = self._make_client_order_id(order_type, idx)
                try:
                    price = self.as_decimal(order.price)
                    desired_notional = price * self.as_decimal(order.qty)
                    qty, notional, reason = compute_order_qty(
                        order.side,
                        price,
                        desired_notional,
                        working_balances,
                        self._exchange_rules,
                        self._effective_fee_rate(),
                        None,
                    )
                    if reason != "ok":
                        required_qty = self.as_decimal(order.qty)
                        required_notional = price * required_qty
                        min_notional = self._rule_decimal(self._exchange_rules.get("min_notional"))
                        min_qty = self._rule_decimal(self._exchange_rules.get("min_qty"))
                        if order.side == "SELL":
                            if required_qty > working_balances["base_free"]:
                                log_reason = "skip_insufficient_base"
                            elif min_notional is not None and required_notional < min_notional:
                                log_reason = "skip_min_notional"
                            elif min_qty is not None and required_qty < min_qty:
                                log_reason = "skip_min_qty"
                            else:
                                log_reason = "skip_min_qty"
                            detail = (
                                f"required_qty={self._format_balance_decimal(required_qty)} "
                                f"base_free={self._format_balance_decimal(working_balances['base_free'])}"
                            )
                        else:
                            if required_notional > working_balances["quote_free"]:
                                log_reason = "skip_insufficient_quote"
                            elif min_notional is not None and required_notional < min_notional:
                                log_reason = "skip_min_notional"
                            elif min_qty is not None and required_qty < min_qty:
                                log_reason = "skip_min_qty"
                            else:
                                log_reason = "skip_min_qty"
                            detail = (
                                f"required_quote={self._format_balance_decimal(required_notional)} "
                                f"quote_free={self._format_balance_decimal(working_balances['quote_free'])}"
                            )
                        self._signals.log_append.emit(
                            (
                                "[SKIP] GRID "
                                f"side={order.side} price={self.fmt_price(price, None)} "
                                f"qty={self.fmt_qty(required_qty, step)} reason={log_reason} {detail}"
                            ),
                            "WARN",
                        )
                        continue
                    if qty <= 0:
                        required_qty = self.as_decimal(order.qty)
                        required_notional = price * required_qty
                        min_notional = self._rule_decimal(self._exchange_rules.get("min_notional"))
                        min_qty = self._rule_decimal(self._exchange_rules.get("min_qty"))
                        if order.side == "SELL":
                            if required_qty > working_balances["base_free"]:
                                log_reason = "skip_insufficient_base"
                            elif min_notional is not None and required_notional < min_notional:
                                log_reason = "skip_min_notional"
                            elif min_qty is not None and required_qty < min_qty:
                                log_reason = "skip_min_qty"
                            else:
                                log_reason = "skip_min_qty"
                            detail = (
                                f"required_qty={self._format_balance_decimal(required_qty)} "
                                f"base_free={self._format_balance_decimal(working_balances['base_free'])}"
                            )
                        else:
                            if required_notional > working_balances["quote_free"]:
                                log_reason = "skip_insufficient_quote"
                            elif min_notional is not None and required_notional < min_notional:
                                log_reason = "skip_min_notional"
                            elif min_qty is not None and required_qty < min_qty:
                                log_reason = "skip_min_qty"
                            else:
                                log_reason = "skip_min_qty"
                            detail = (
                                f"required_quote={self._format_balance_decimal(required_notional)} "
                                f"quote_free={self._format_balance_decimal(working_balances['quote_free'])}"
                            )
                        self._signals.log_append.emit(
                            (
                                "[SKIP] GRID "
                                f"side={order.side} price={self.fmt_price(price, None)} "
                                f"qty={self.fmt_qty(required_qty, step)} reason={log_reason} {detail}"
                            ),
                            "WARN",
                        )
                        continue
                    response, error, status = self._place_limit(
                        order.side,
                        price,
                        qty,
                        client_order_id,
                        reason="grid",
                    )
                    if response:
                        if isinstance(response, dict):
                            response["level_index"] = order.level_index
                        results.append(response)
                    if error and status != "skip_duplicate_exchange":
                        errors.append(error)
                    if order.side == "BUY":
                        working_balances["quote_free"] = max(
                            Decimal("0"),
                            working_balances["quote_free"] - notional,
                        )
                    else:
                        working_balances["base_free"] = max(
                            Decimal("0"),
                            working_balances["base_free"] - qty,
                        )
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

        worker = _Worker(_place, self._can_emit_worker_results)
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
                    level_index = entry.get("level_index")
                    if isinstance(level_index, int):
                        self._order_id_to_level_index[order_id] = level_index
                client_order_id = str(entry.get("clientOrderId", ""))
                if client_order_id:
                    self._bot_client_ids.add(client_order_id)
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
        self._activate_stale_warmup()
        self._change_state("RUNNING")
        if self._pilot_pending_action == PilotAction.RECENTER:
            expected_min = self._pilot_recenter_expected_min or 0
            self._append_log(
                f"[INFO] [NC_MICRO] recenter placed place_n={len(results)} expected_min={expected_min}",
                kind="INFO",
            )
            self._pilot_wait_for_recenter_open_orders(expected_min)

    def _handle_live_order_error(self, message: str) -> None:
        self._append_log(f"[LIVE] order error: {message}", kind="WARN")
        if self._state == "PLACING_GRID":
            self._change_state("RUNNING")
        if self._pilot_pending_action is not None:
            self._set_pilot_action_in_progress(False)
            if self._pilot_state == PilotState.RECENTERING:
                self._set_pilot_state(PilotState.NORMAL)
                self._pilot_pending_anchor = None
                self._pilot_recenter_expected_min = None
            self._pilot_pending_action = None
            self._pilot_pending_plan = None
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
                ok, error = self._cancel_order_idempotent(order_id)
                if ok:
                    responses.append({"orderId": order_id})
                elif error:
                    self._signals.log_append.emit(error, "ERROR")
                    self._signals.api_error.emit(error)
                if idx % batch_size == 0:
                    self._sleep_ms(250)
            return responses

        worker = _Worker(_cancel, self._can_emit_worker_results)
        worker.signals.success.connect(self._handle_cancel_selected_result)
        worker.signals.error.connect(self._handle_cancel_error)
        self._thread_pool.start(worker)

    def _handle_cancel_selected_result(self, result: object, latency_ms: int) -> None:
        if not isinstance(result, list):
            self._handle_cancel_error("Unexpected cancel response")
            return
        for entry in result:
            order_id = str(entry.get("orderId", "—")) if isinstance(entry, dict) else "—"
            if isinstance(entry, dict):
                self._discard_order_key_from_order(entry)
                self._discard_registry_for_order(entry)
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
            if isinstance(entry, dict):
                self._discard_order_key_from_order(entry)
                self._discard_registry_for_order(entry)
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
    def _coerce_float(value: str | float | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if not isinstance(value, str):
            return None
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
        self._update_pilot_orders_metrics()

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

    def _notify_crash(self, source: str) -> None:
        if not self._crash_log_path:
            return
        if self._crash_notified:
            return
        self._crash_notified = True
        self._append_log(
            f"[NC_MICRO][CRASH] see crash log: {self._crash_log_path} (source={source})",
            kind="ERR",
        )

    def _apply_log_filter(self) -> None:
        if not hasattr(self, "_log_view"):
            return
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

    def _can_emit_worker_results(self) -> bool:
        return not self._closing

    def _stop_local_timers(self) -> None:
        if self._market_kpi_timer.isActive():
            self._market_kpi_timer.stop()
        if self._pilot_ui_timer.isActive():
            self._pilot_ui_timer.stop()
        if self._registry_gc_timer.isActive():
            self._registry_gc_timer.stop()

    def _resume_local_timers(self) -> None:
        if not self._market_kpi_timer.isActive():
            self._market_kpi_timer.start()
        if not self._pilot_ui_timer.isActive():
            self._pilot_ui_timer.start()
        if not self._registry_gc_timer.isActive():
            self._registry_gc_timer.start()

    def closeEvent(self, event: object) -> None:  # noqa: N802
        self._closing = True
        self._balances_timer.stop()
        self._orders_timer.stop()
        self._fills_timer.stop()
        self._stop_local_timers()
        self._price_feed_manager.unsubscribe(self._symbol, self._emit_price_update)
        self._price_feed_manager.unsubscribe_status(self._symbol, self._emit_status_update)
        self._price_feed_manager.unregister_symbol(self._symbol)
        self._append_log("Lite Grid Terminal closed.", kind="INFO")
        super().closeEvent(event)

    def dump_settings(self) -> dict[str, Any]:
        return asdict(self._settings_state)
