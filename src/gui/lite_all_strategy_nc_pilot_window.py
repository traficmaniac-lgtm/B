from __future__ import annotations

import atexit
import functools
import httpx
import faulthandler
import json
import logging
import os
import random
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from math import ceil, floor, isfinite
from pathlib import Path
from time import monotonic, perf_counter, sleep, time as time_fn
from typing import Any, Callable
from uuid import uuid4

from PySide6.QtCore import QObject, QEventLoop, QRunnable, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
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
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.ai.operator_math import compute_break_even_tp_pct, compute_fee_total_pct, evaluate_tp_profitability
from src.ai.operator_profiles import get_profile_preset
from src.binance.account_client import AccountStatus, BinanceAccountClient
from src.binance.http_client import BinanceHttpClient
from src.core.cancel_reconcile import CancelResult
from src.core.config import Config
from src.config.fee_overrides import FEE_OVERRIDES_ROUNDTRIP
from src.core.httpx_singleton import close_shared_client
from src.core.logging import get_logger
from src.core.micro_edge import compute_expected_edge_bps
from src.core.crash_guard import register_crash_log_path, register_panic_hold_callback, safe_qt
from src.core.nc_micro_safe_call import safe_call
from src.core.nc_micro_stop import finalize_stop_state
from src.core.nc_micro_exec_dedup import should_block_new_orders
from src.core.nc_micro_refresh import StaleRefreshLogLimiter, compute_refresh_allowed
from src.gui.dialogs.pilot_settings_dialog import PilotConfig, PilotSettingsDialog
from src.gui.i18n import TEXT, tr
from src.gui.pilot_log_window import PilotLogWindow
from src.gui.lite_grid_math import (
    FillAccumulator,
    align_tick_based_qty,
    build_action_key,
    compute_order_qty,
    evaluate_tp_min_profit_bps,
    should_block_bidask_actions,
)
from src.gui.models.app_state import AppState
from src.services.data_cache import DataCache
from src.services.nc_pilot.pilot_controller import PilotController
from src.services.nc_pilot.session import GridSettingsState, NcPilotSession
from src.services.net import get_net_worker, shutdown_net_worker
from src.services.net.net_worker import NetWorker
from src.services.price_feed_manager import (
    PriceFeedManager,
    PriceUpdate,
    WS_CONNECTED,
    WS_DEGRADED,
    WS_LOST,
    WS_STALE_MS,
)

NC_PILOT_VERSION = "v1.0.9"
_NC_PILOT_CRASH_HANDLES: list[object] = []
EXEC_DUP_LOG_COOLDOWN_SEC = 10.0
PILOT_SYMBOLS = ("EURIUSDT", "EUREURI", "USDCUSDT", "TUSDUSDT")


@dataclass
class ExecDupLogState:
    count: int = 0
    last_log_ts: float | None = None


def _format_session_log(symbol: str, message: str) -> str:
    prefix = f"[NC_PILOT][{symbol}]"
    if prefix in message:
        return message
    if "[NC_PILOT]" in message:
        return message.replace("[NC_PILOT]", prefix, 1)
    return f"{prefix} {message}"


class NcPilotLoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, symbol: str) -> None:
        super().__init__(logger, {"symbol": symbol})
        self._symbol = symbol

    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return _format_session_log(self._symbol, msg), kwargs


def _install_nc_pilot_crash_catcher(logger: Any, symbol: str) -> tuple[str, object]:
    base_dir = os.getcwd()
    crash_dir = os.path.join(base_dir, "logs", "crash")
    os.makedirs(crash_dir, exist_ok=True)
    safe_symbol = symbol.replace("/", "_")
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    crash_log_path = os.path.join(crash_dir, f"NC_PILOT_{safe_symbol}_{timestamp}.log")
    crash_file = open(crash_log_path, "a", buffering=1, encoding="utf-8")
    _NC_PILOT_CRASH_HANDLES.append(crash_file)
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

    register_crash_log_path(crash_log_path)
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


@dataclass(frozen=True)
class MarketHealth:
    has_bidask: bool
    spread_bps: float | None
    vol_bps: float | None
    age_ms: int | None
    src: str | None
    edge_raw_bps: float | None
    expected_profit_bps: float | None


class LedIndicator(QWidget):
    def __init__(self, diameter: int = 10, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._active = False
        self._diameter = diameter
        self.setFixedSize(diameter, diameter)

    def set_state(self, active: bool) -> None:
        if self._active == active:
            return
        self._active = active
        self.update()

    def set_tooltip(self, text: str) -> None:
        self.setToolTip(text or "")

    def paintEvent(self, _event: Any) -> None:
        color = QColor("#16a34a" if self._active else "#dc2626")
        border = QColor("#111827")
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        radius = self._diameter - 2
        painter.setBrush(color)
        painter.setPen(border)
        painter.drawEllipse(1, 1, radius, radius)
        painter.end()


class _LiteGridSignals(QObject):
    price_update = Signal(object)
    status_update = Signal(str, str)
    log_append = Signal(str, str)
    api_error = Signal(str)
    balances_refresh = Signal(bool)
    open_orders_refresh = Signal(bool)


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
    HOLD = "HOLD"
    ACCUMULATE_BASE = "ACCUMULATE_BASE"


class PilotAction(Enum):
    RECENTER = "RECENTER"
    RECOVERY = "RECOVERY"
    FLATTEN_BE = "FLATTEN_BE"
    FLAG_STALE = "FLAG_STALE"
    CANCEL_REPLACE_STALE = "CANCEL_REPLACE_STALE"


class StalePolicy(Enum):
    NONE = "NONE"
    RECENTER = "RECENTER"
    CANCEL_REPLACE_STALE = "CANCEL_REPLACE_STALE"


class LegacyPolicy(Enum):
    CANCEL = "CANCEL"
    IGNORE = "IGNORE"


class ProfitGuardMode(Enum):
    BLOCK = "BLOCK"
    WARN_ONLY = "WARN_ONLY"


RECENTER_THRESHOLD_PCT = 0.7
STALE_DRIFT_PCT_DEFAULT = 0.20
STALE_DRIFT_PCT_STABLE = 0.05
STALE_BUFFER_MIN_PCT = 0.10
STALE_WARMUP_SEC = 30
STALE_ORDER_LOG_DEDUP_SEC = 10
STALE_LOG_DEDUP_SEC = 600
STALE_AUTO_ACTION_COOLDOWN_SEC = 300
AUTO_EXEC_ACTION_COOLDOWN_SEC = 30
SNAPSHOT_REFRESH_COOLDOWN_MS = 1500
STALE_REFRESH_MIN_MS = 15_000
STALE_REFRESH_POLL_MIN_MS = 15_000
STALE_REFRESH_POLL_AGE_MS = 2000
STALE_REFRESH_POLL_HARD_MAX_SEC = 10
STALE_REFRESH_HARD_MAX_MS = 90_000
STALE_REFRESH_HASH_STABLE_CYCLES = 3
STALE_REFRESH_LOG_DEDUP_SEC = 20
STALE_REFRESH_POLL_LOG_MIN_SEC = 10.0
STALE_REFRESH_POLL_SUPPRESS_LOG_SEC = 30.0
STALE_SNAPSHOT_MAX_AGE_SEC = 5
STOP_DEADLINE_MS = 8000
LOG_DEDUP_HEARTBEAT_SEC = 10.0
ORDERS_POLL_IDLE_MS = 1500
ORDERS_POLL_RUNNING_MS = 1500
ORDERS_POLL_STOPPING_MS = 500
ORDERS_POLL_MAX_MS = 5000
ORDERS_EVENT_DEDUP_SEC = 0.5
UI_SOURCE_DEBOUNCE_MS = 300
STALE_COOLDOWN_SEC = 120
STALE_ACTION_COOLDOWN_SEC = 120
STALE_AUTO_ACTION_PAUSE_SEC = 300
TP_MIN_PROFIT_COOLDOWN_SEC = 30
PROFIT_GUARD_DEFAULT_MAKER_FEE_PCT = 0.0
PROFIT_GUARD_DEFAULT_TAKER_FEE_PCT = 0.0
PILOT_SLIPPAGE_BPS = 0.10
PILOT_PAD_BPS = 0.10
PROFIT_GUARD_SLIPPAGE_BPS = 1.5
PROFIT_GUARD_EXTRA_BUFFER_PCT = 0.02
PROFIT_GUARD_BUFFER_PCT = 0.03
PROFIT_GUARD_MIN_FLOOR_PCT = 0.10
PROFIT_GUARD_EXIT_BUFFER_PCT = 0.02
PROFIT_GUARD_THIN_SPREAD_FACTOR = 0.25
PROFIT_GUARD_LOW_VOL_FACTOR = 0.5
PROFIT_GUARD_LOW_VOL_FLOOR_PCT = 0.05
PROFIT_GUARD_MIN_VOL_PCT = 0.05
MARKET_HEALTH_RISK_BUFFER_MULT = 0.5
MARKET_HEALTH_LOG_COOLDOWN_SEC = 5
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
KPI_STATE_WAITING_BOOK = "WAITING_BOOK"
BIDASK_POLL_MS = 700
BIDASK_FAIL_SOFT_MS = 1500
BIDASK_STALE_MS = 4000
PRICE_STALE_MS = 4000
SPREAD_DISPLAY_EPS_PCT = 0.0001
WAIT_EDGE_LOG_COOLDOWN_SEC = 5.0
NO_BIDASK_LOG_INTERVAL_SEC = 5.0
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
MICRO_STABLECOIN_SYMBOLS = {"EURIUSDT", "USDCUSDT"}
MICRO_STABLECOIN_STEP_ABS_DEFAULT = 0.0001
MICRO_STABLECOIN_RANGE_ABS_DEFAULT = 0.0010
MICRO_STABLECOIN_TP_ABS_DEFAULT = 0.0001
MICRO_STABLECOIN_STEP_PCT_DEFAULT = 0.01
MICRO_STABLECOIN_STEP_PCT_MIN = 0.005
MICRO_STABLECOIN_STEP_PCT_MAX = 0.06
MICRO_STABLECOIN_RANGE_PCT_DEFAULT = 0.10
MICRO_STABLECOIN_RANGE_PCT_MAX = 0.40
MICRO_STABLECOIN_TP_PCT_MAX = 0.15
MICRO_STABLECOIN_THIN_EDGE_MIN_PROFIT_PCT = 0.02
MICRO_STABLECOIN_SLIPPAGE_PCT = 0.003
MICRO_STABLECOIN_PAD_PCT = 0.005
MICRO_STABLECOIN_BUFFER_PCT = 0.0


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
    is_total: bool = False


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


@dataclass(frozen=True)
class OrderRow:
    order_id: str
    side: str
    price: str
    qty: str
    filled: str
    age: str


@dataclass(frozen=True)
class OrdersSnapshot:
    orders: list[OrderRow]
    fingerprint: str
    ts: float


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


class NcPilotTabWidget(QWidget):
    def __init__(
        self,
        symbol: str,
        config: Config,
        app_state: AppState,
        price_feed_manager: PriceFeedManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._app_state = app_state
        self._symbol = symbol.strip().upper()
        self._logger = NcPilotLoggerAdapter(get_logger("gui.lite_all_strategy_nc_pilot"), self._symbol)
        self._price_feed_manager = price_feed_manager
        self._session = NcPilotSession(symbol=self._symbol)
        self.session = self._session
        self._pilot_controller = PilotController(self, self._session)
        self.trade_source = "HTTP_ONLY"
        self._pending_trade_source: str | None = None
        self._last_trade_source_change_ms = 0
        self._signals = _LiteGridSignals()
        self._signals.price_update.connect(
            lambda update: safe_qt(
                lambda: self._safe_call(
                    lambda: self._apply_price_update(update),
                    label="signal:price_update",
                ),
                label="signal:price_update",
                logger=self._logger,
            )
        )
        self._signals.status_update.connect(
            lambda status, message: safe_qt(
                lambda: self._safe_call(
                    lambda: self._apply_status_update(status, message),
                    label="signal:status_update",
                ),
                label="signal:status_update",
                logger=self._logger,
            )
        )
        self._signals.log_append.connect(
            lambda message, kind: safe_qt(
                lambda: self._safe_call(
                    lambda: self._append_log(message, kind=kind),
                    label="signal:log_append",
                ),
                label="signal:log_append",
                logger=self._logger,
            )
        )
        self._signals.api_error.connect(
            lambda message: safe_qt(
                lambda: self._safe_call(
                    lambda: self._handle_live_api_error(message),
                    label="signal:api_error",
                ),
                label="signal:api_error",
                logger=self._logger,
            )
        )
        self._log_entries: list[tuple[str, str]] = []
        self._log_throttle_ts: dict[str, float] = {}
        self._crash_log_path, self._crash_file = _install_nc_pilot_crash_catcher(
            self._logger, self._symbol
        )
        self._crash_notified = False
        self._append_log(
            f"[NC_PILOT] crash catcher installed path={self._crash_log_path}",
            kind="INFO",
        )
        register_panic_hold_callback(self.panic_hold)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._handle_about_to_quit)
        self._state = "IDLE"
        self._engine_state = "WAITING"
        self._session.runtime.engine_state = self._engine_state
        self._ws_status = ""
        self._closing = False
        self._close_pending = False
        self._close_finalizing = False
        self._shutdown_started = False
        self._disposed = False
        self._feed_active = True
        self._bootstrap_mode = False
        self._bootstrap_sell_enabled = False
        self._sell_side_enabled = False
        self._active_tp_ids: set[str] = set()
        self._active_restore_ids: set[str] = set()
        self._pending_tp_ids: set[str] = set()
        self._pending_restore_ids: set[str] = set()
        self._settings_state = self._session.config
        self._pilot_feature_enabled = False
        self.pilot_config = PilotConfig()
        self._pilot_allow_market = self.pilot_config.allow_market_close
        self._pilot_stale_policy = self._resolve_pilot_stale_policy(self.pilot_config.stale_policy)
        self._session.runtime.stop_inflight = False
        self._stop_done_with_errors = False
        self._stop_last_error: str | None = None
        self._stop_watchdog_token = 0
        self._stop_watchdog_started_ts: float | None = None
        self._stop_finalize_waiting = False
        self._inflight_watchdog_started_ts: float | None = None
        self._inflight_watchdog_last_log_ts: float | None = None
        self._settings_save_timer: QTimer | None = None
        self._last_price: float | None = None
        self._last_price_ts: float | None = None
        self._load_settings_from_disk()
        self._apply_profile_defaults_if_pristine()
        self._grid_engine = GridEngine(self._set_engine_state, self._append_log)
        self._manual_grid_step_pct = self._settings_state.grid_step_pct
        self._net_worker: NetWorker | None = None
        self._net_pending: dict[str, tuple[Callable[[object, int], None] | None, Callable[[object], None] | None]] = {}
        self._net_request_counter = 0
        self._account_client: BinanceAccountClient | None = None
        self._http_client: BinanceHttpClient | None = None
        api_key, api_secret = self._app_state.get_binance_keys()
        self._has_api_keys = bool(api_key and api_secret)
        self._can_read_account = False
        self._last_account_status = ""
        self._last_account_trade_snapshot: tuple[bool, tuple[str, ...]] | None = None
        self._http_cache = DataCache()
        self._http_cache_ttls = {
            "book_ticker": 2.0,
            "klines_1h": 60.0,
            "exchange_info_symbol": 3600.0,
        }
        self._pilot_log_window: PilotLogWindow | None = None
        self._pilot_route_snapshots: dict[str, object] = {}
        self._pilot_market_cache = DataCache()
        self._pilot_market_cache_ttls = {
            "book_ticker": 1.5,
            "orderbook_depth_50": 2.0,
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
        self._http_client = BinanceHttpClient(
            base_url=self._config.binance.base_url,
            timeout_s=self._config.http.timeout_s,
            retries=self._config.http.retries,
            backoff_base_s=self._config.http.backoff_base_s,
            backoff_max_s=self._config.http.backoff_max_s,
        )
        self._setup_net_worker()
        if self._has_api_keys:
            self._sync_account_time()
        self._logger.info("binance keys present: %s", self._has_api_keys)
        self._balances: dict[str, tuple[float, float]] = {}
        self._open_orders = self._session.order_tracking.open_orders
        self._open_orders_all = self._session.order_tracking.open_orders_all
        self._open_orders_loaded = False
        self._pilot_virtual_orders: list[dict[str, Any]] = []
        self._bot_order_ids: set[str] = set()
        self._bot_client_ids: set[str] = set()
        self._bot_order_keys: set[str] = set()
        self._fill_keys = self._session.order_tracking.fill_keys
        self._fill_accumulator = FillAccumulator()
        self._fill_exec_cumulative = self._session.order_tracking.fill_exec_cumulative
        self._legacy_policy = self._resolve_legacy_policy()
        self._legacy_order_ids: set[str] = set()
        self._owned_order_ids: set[str] = set()
        self._legacy_bootstrap_pending = True
        self._legacy_cancel_in_progress = False
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
        self._price_history: list[float] = []
        self.book_snapshot: dict[str, float] | None = None
        self._ws_last_tick_ts: float | None = None
        self._ws_last_price: float | None = None
        self._ws_alive = False
        self._account_can_trade = False
        self._account_permissions: list[str] = []
        self._account_api_error = False
        self._symbol_tradeable = False
        self._suppress_dry_run_event = False
        self._dry_run_enabled = True
        self._exchange_rules: dict[str, float | None] = {}
        self._trade_fees: tuple[float | None, float | None] = (None, None)
        self._fees_last_fetch_ts: float | None = None
        self._fee_unverified = True
        self._fee_source: str | None = None
        self._profit_guard_mode = self._resolve_profit_guard_mode()
        self._update_fee_state(None, None, "DEFAULT_UNVERIFIED")
        self._quote_asset = ""
        self._base_asset = ""
        self._balances_in_flight = False
        self._balances_loaded = False
        self._balances_error_streak = 0
        self._balances_poll_ms = 1000
        self._last_account_snapshot: dict[str, Any] | None = None
        self._balance_ready_ts_monotonic_ms: int | None = None
        self._last_good_balances: dict[str, tuple[float, float, float]] = {}
        self._last_good_ts: float | None = None
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
        self._open_orders_map = self._session.order_tracking.open_orders_map
        self._fills: list[TradeFill] = []
        self._base_lots: deque[BaseLot] = deque()
        self._fills_in_flight = False
        self._trade_id_deduper = self._session.order_tracking.trade_id_deduper
        self._exec_key_deduper = self._session.order_tracking.exec_key_deduper
        self._exec_dup_log_state: dict[str, ExecDupLogState] = {}
        self._realized_pnl = 0.0
        self._fees_total = 0.0
        self._position_fees_paid_quote = 0.0
        self._closed_trades = 0
        self._win_trades = 0
        self._replacement_counter = 0
        self._balances_tick_count = 0
        self._orders_tick_count = 0
        self._orders_last_count: int | None = None
        self._orders_poll_last_ts: int | None = None
        self._orders_poll_backoff_ms = 0
        self._orders_event_refresh_ts: dict[str, float] = {}
        self._stale_refresh_last_ts: dict[str, float] = {}
        self._stale_refresh_last_log_ts: float | None = None
        self._stale_refresh_last_state: tuple[str, bool] | None = None
        self._stale_refresh_log_limiter = StaleRefreshLogLimiter(
            min_interval_sec=STALE_REFRESH_POLL_LOG_MIN_SEC
        )
        self._stale_refresh_hits = 0
        self._stale_refresh_calls = 0
        self._stale_refresh_skipped_rate_limit = 0
        self._stale_refresh_skipped_no_change = 0
        self._stale_refresh_last_force_ts_ms: int | None = None
        self._stale_refresh_poll_log_ts: dict[str, float] = {}
        self._stale_refresh_poll_suppressed: dict[str, int] = {}
        self._stale_refresh_poll_suppressed_log_ts: dict[str, float] = {}
        self._orders_snapshot_hash_value: int | None = None
        self._orders_snapshot_hash_changed = False
        self._orders_snapshot_hash_stable_cycles = 0
        self._registry_dirty = False
        self._fills_changed_since_refresh = False
        self._orders_snapshot_signature: tuple[tuple[str, str, str, str], ...] | None = None
        self._orders_last_sync_ts: float | None = None
        self._orders_last_change_ts: float | None = None
        self._balances_snapshot: tuple[float, float] | None = None
        self._sell_retry_limit = 5
        self._session.runtime.start_in_progress = False
        self._start_in_progress_logged = False
        self._profit_guard_override_pending = False
        self._wait_edge_last_log_ts: float | None = None
        self._start_run_id = 0
        self._start_cancel_event: threading.Event | None = None
        self._last_preflight_hash: str | None = None
        self._last_preflight_blocked = False
        self._start_locked_until_change = False
        self._start_locked_logged = False
        self._tp_fix_target: float | None = None
        self._auto_fix_tp_enabled = True
        self.enable_guard_autofix = True
        self._stop_in_progress = False
        self._stop_requested = False
        self._pilot_state = PilotState.OFF
        self.pilot_enabled = False
        self._pilot_confirm_dry_run = False
        self._pilot_action_in_progress = False
        self._current_action: str | None = None
        self._pilot_pending_action: PilotAction | None = None
        self._pilot_pending_plan: list[GridPlannedOrder] | None = None
        self._pilot_warning_override: str | None = None
        self._pilot_warning_override_until: float | None = None
        self._pilot_anchor_price: float | None = None
        self._pilot_pending_anchor: float | None = None
        self._pilot_recenter_expected_min: int | None = None
        self._pilot_hold_until: float | None = None
        self._pilot_hold_reason: str | None = None
        self._pilot_stale_active = False
        self._pilot_stale_last_log_ts: float | None = None
        self._pilot_stale_last_log_order_id: str | None = None
        self._pilot_stale_last_log_total: int | None = None
        self._pilot_stale_last_action_ts: float | None = None
        self._pilot_snapshot_stale_active = False
        self._pilot_snapshot_stale_last_log_ts: float | None = None
        self._last_snapshot_refresh_ts = 0.0
        self._snapshot_refresh_inflight = False
        self._snapshot_refresh_suppressed_log_ts: float | None = None
        self._snapshot_refresh_suppressed_state: tuple[str, str | None] | None = None
        self._orders_snapshot: OrdersSnapshot | None = None
        self._last_orders_fingerprint: str | None = None
        self._orders_refresh_reason: str = "bootstrap"
        self._cancel_inflight = False
        self._cancel_reconcile_pending = 0
        self._pilot_pending_cancel_ids: set[str] = set()
        self._order_age_registry: dict[str, float] = {}
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
        self._pilot_budget_cap_last_value: float | None = None
        self._pilot_budget_cap_last_reason: str | None = None
        self._pilot_budget_cap_last_log_ts: float | None = None
        self._last_price_update: PriceUpdate | None = None
        self._feed_ok = False
        self._feed_last_ok: bool | None = None
        self._feed_last_source: str | None = None
        self._bidask_state: str | None = None
        self._bidask_block_log_ts: float | None = None
        self._bidask_block_reason: str | None = None
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
        self._no_bidask_last_log_ts: float | None = None
        self._no_bidask_warned = False
        self._bidask_last: tuple[float, float] | None = None
        self._bidask_ts: float | None = None
        self._bidask_src: str | None = None
        self.bidask_ready = False
        self._bidask_last_request_ts: float | None = None
        self._bidask_ready_logged = False
        self._kpi_vol_samples: deque[tuple[float, float]] = deque(maxlen=KPI_VOL_SAMPLE_MAXLEN)
        self._kpi_last_good_vol: float | None = None
        self._kpi_last_good_ts: float | None = None
        self._kpi_last_state_ts: float | None = None
        self._kpi_last_reason: str | None = None
        self._kpi_last_good: dict[str, Any] | None = None
        self._market_health: MarketHealth | None = None
        self._market_health_last_reason: str | None = None
        self._market_health_last_log_ts: float | None = None
        self._market_health_last_values: tuple[float, float, float, str] | None = None
        self._open_orders_snapshot_ts: float | None = None
        self._order_info_map: dict[str, OrderInfo] = {}
        self._registry_key_last_seen_ts: dict[str, float] = {}
        self._registry_gc_last_ts: float | None = None
        self._tp_micro_gate_log_ts: float | None = None
        self._position_state = PositionState(
            position_qty=0.0,
            avg_entry_price=None,
            realized_pnl_quote=0.0,
            fees_paid_quote=0.0,
            break_even_price=None,
        )

        self._balances_timer = QTimer(self)
        self._balances_timer.setInterval(self._balances_poll_ms)
        self._balances_timer.timeout.connect(
            lambda: self._safe_call(self._refresh_balances, label="timer:balances")
        )
        self._orders_timer = QTimer(self)
        self._orders_timer.setInterval(ORDERS_POLL_IDLE_MS)
        self._orders_timer.timeout.connect(
            lambda: self._safe_call(
                lambda: self._refresh_orders(reason="poll"),
                label="timer:orders",
            )
        )
        self._trade_source_timer = QTimer(self)
        self._trade_source_timer.setSingleShot(True)
        self._trade_source_timer.timeout.connect(
            lambda: self._safe_call(self._apply_pending_trade_source, label="timer:trade_source")
        )
        self._fills_timer = QTimer(self)
        self._fills_timer.setInterval(2_500)
        self._fills_timer.timeout.connect(
            lambda: self._safe_call(self._refresh_fills, label="timer:fills")
        )
        self._pilot_ui_timer: QTimer | None = None
        if self._pilot_feature_enabled:
            self._pilot_ui_timer = QTimer(self)
            self._pilot_ui_timer.setInterval(750)
            self._pilot_ui_timer.timeout.connect(
                lambda: self._safe_call(self._update_pilot_panel, label="timer:pilot_ui")
            )
        self._market_kpi_timer = QTimer(self)
        self._market_kpi_timer.setInterval(500)
        self._market_kpi_timer.timeout.connect(
            lambda: self._safe_call(self.update_market_kpis, label="timer:market_kpis")
        )
        self._registry_gc_timer = QTimer(self)
        self._registry_gc_timer.setInterval(REGISTRY_GC_INTERVAL_MS)
        self._registry_gc_timer.timeout.connect(
            lambda: self._safe_call(self._gc_registry_keys, label="timer:registry_gc")
        )
        self._order_age_timer = QTimer(self)
        self._order_age_timer.setInterval(1_000)
        self._order_age_timer.timeout.connect(
            lambda: self._safe_call(self._tick_order_ages, label="timer:order_ages")
        )

        self.setWindowTitle(f"NC PILOT {NC_PILOT_VERSION} — {self._symbol}")
        self.resize(1050, 720)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(10, 10, 10, 10)
        outer_layout.setSpacing(8)

        outer_layout.addLayout(self._build_header())
        outer_layout.addWidget(self._build_body())
        self._apply_trade_gate()
        if self._pilot_ui_timer is not None:
            self._pilot_ui_timer.start()
        self._market_kpi_timer.start()
        self._registry_gc_timer.start()
        self._order_age_timer.start()

        self._price_feed_manager.subscribe(self._symbol, self._emit_price_update)
        self._price_feed_manager.subscribe_status(self._symbol, self._emit_status_update)
        self._price_feed_manager.start()
        self._refresh_exchange_rules()
        if self._account_client:
            self._balances_timer.start(self._balances_poll_ms)
            self._orders_timer.start()
            self._refresh_balances()
            self._refresh_orders(reason="bootstrap")
            self._update_orders_timer_interval()
        else:
            self._set_account_status("no_keys")
            self._apply_trade_gate()
        self._append_log(
            f"[NC_PILOT] opened. version={NC_PILOT_VERSION} symbol={self._symbol}",
            kind="INFO",
        )
        self.update_ui_lock_state()
        self._log_ui_tab_created()

    @property
    def symbol(self) -> str:
        return self._symbol

    def ui_from_config(self) -> None:
        self._refresh_settings_ui_from_state()

    def config_from_ui(self) -> GridSettingsState:
        if not hasattr(self, "_budget_input"):
            return self._settings_state
        return GridSettingsState(
            budget=float(self._budget_input.value()),
            direction=str(self._direction_combo.currentData() or self._settings_state.direction),
            grid_count=int(self._grid_count_input.value()),
            grid_step_pct=float(self._grid_step_input.value()),
            grid_step_mode=str(self._grid_step_mode_combo.currentData() or self._settings_state.grid_step_mode),
            range_mode=str(self._range_mode_combo.currentData() or self._settings_state.range_mode),
            range_low_pct=float(self._range_low_input.value()),
            range_high_pct=float(self._range_high_input.value()),
            take_profit_pct=float(self._take_profit_input.value()),
            stop_loss_enabled=bool(self._stop_loss_toggle.isChecked()),
            stop_loss_pct=float(self._stop_loss_input.value()),
            max_active_orders=int(self._max_orders_input.value()),
            order_size_mode=str(self._order_size_combo.currentData() or self._settings_state.order_size_mode),
        )

    def set_controls_enabled(self, enabled: bool, reason: str = "") -> None:
        self._set_strategy_controls_enabled(enabled)
        if reason:
            self._append_log(
                f"[NC_PILOT][UI] controls_enabled={str(enabled).lower()} reason={reason}",
                kind="INFO",
            )

    def update_ui_lock_state(self) -> None:
        locked = (
            self._session.runtime.start_in_progress
            or self._stop_in_progress
            or self._engine_state == "STOPPING"
        )
        self._set_strategy_controls_enabled(not locked)

    def on_start_clicked(self) -> None:
        self._handle_start()

    def on_stop_clicked(self) -> None:
        self._handle_stop()

    def on_pause_clicked(self) -> None:
        self._handle_pause()

    def on_tab_activated(self) -> None:
        self.update_ui_lock_state()
        self._log_ui_tab_activated()

    def _ui_fields_enabled(self) -> list[bool]:
        fields = [
            getattr(self, "_budget_input", None),
            getattr(self, "_grid_step_input", None),
            getattr(self, "_range_low_input", None),
            getattr(self, "_range_high_input", None),
            getattr(self, "_take_profit_input", None),
            getattr(self, "_max_orders_input", None),
        ]
        return [bool(field and field.isEnabled()) for field in fields]

    def _log_ui_tab_created(self) -> None:
        form_enabled = bool(getattr(self, "_grid_panel", None) and self._grid_panel.isEnabled())
        fields_enabled = self._ui_fields_enabled()
        self._append_log(
            (
                "[NC_PILOT][UI] tab_created "
                f"symbol={self._symbol} widgets_id={hex(id(self))} "
                f"form_enabled={str(form_enabled).lower()} "
                f"fields_enabled={fields_enabled}"
            ),
            kind="INFO",
        )

    def _log_ui_tab_activated(self) -> None:
        fields_enabled = self._ui_fields_enabled()
        self._append_log(
            (
                "[NC_PILOT][UI] tab_activated "
                f"symbol={self._symbol} enabled={fields_enabled} engine_state={self._engine_state}"
            ),
            kind="INFO",
        )

    def _resolve_legacy_policy(self) -> LegacyPolicy:
        policy_raw = getattr(self._app_state, "nc_micro_legacy_policy", "CANCEL")
        policy_value = str(policy_raw or "CANCEL").upper()
        for policy in LegacyPolicy:
            if policy.value == policy_value:
                return policy
        return LegacyPolicy.CANCEL

    def _resolve_profit_guard_mode(self) -> ProfitGuardMode:
        mode_raw = getattr(self._app_state, "nc_micro_profit_guard_mode", "BLOCK")
        mode_value = str(mode_raw or "BLOCK").upper()
        for mode in ProfitGuardMode:
            if mode.value == mode_value:
                return mode
        return ProfitGuardMode.BLOCK

    def _register_owned_order_id(self, order_id: str) -> None:
        if not order_id:
            return
        self._owned_order_ids.add(order_id)
        self._legacy_order_ids.discard(order_id)

    def _bootstrap_legacy_orders(self, orders: list[dict[str, Any]]) -> None:
        if not self._legacy_bootstrap_pending:
            return
        legacy_ids = {
            str(order.get("orderId", ""))
            for order in orders
            if isinstance(order, dict) and str(order.get("orderId", ""))
        }
        self._legacy_order_ids = legacy_ids
        self._legacy_bootstrap_pending = False
        if legacy_ids:
            self._append_log(
                f"[BOOTSTRAP] legacy orders detected n={len(legacy_ids)} policy={self._legacy_policy.value}",
                kind="INFO",
            )
        else:
            self._append_log("[BOOTSTRAP] legacy orders detected n=0", kind="INFO")

    def _cancel_legacy_orders_and_wait(self, timeout_ms: int = 6_000) -> bool:
        if self._legacy_cancel_in_progress:
            return False
        if not self._account_client or not self._legacy_order_ids:
            return True
        self._legacy_cancel_in_progress = True
        legacy_ids = sorted(self._legacy_order_ids)
        result: dict[str, Any] = {}
        try:
            errors: list[str] = []
            canceled = 0
            failed_ids: list[str] = []
            for order_id in legacy_ids:
                if not order_id:
                    continue
                try:
                    self._cancel_order_sync(symbol=self._symbol, order_id=order_id)
                    canceled += 1
                except Exception as exc:  # noqa: BLE001
                    failed_ids.append(order_id)
                    errors.append(self._format_cancel_exception(exc, order_id))
            remaining_ids: set[str] = set(legacy_ids)
            deadline = monotonic() + 4.0
            while remaining_ids and monotonic() < deadline:
                try:
                    open_orders = self._fetch_open_orders_sync(self._symbol)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"[LEGACY] openOrders refresh failed: {exc}")
                    break
                open_ids = {
                    str(order.get("orderId", ""))
                    for order in open_orders
                    if isinstance(order, dict) and str(order.get("orderId", ""))
                }
                remaining_ids = remaining_ids.intersection(open_ids)
                if remaining_ids:
                    sleep(0.25)
            result = {
                "planned": len(legacy_ids),
                "canceled": canceled,
                "failed_ids": failed_ids,
                "remaining_ids": list(remaining_ids),
                "errors": errors,
                "ok": True,
            }
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        self._legacy_cancel_in_progress = False
        remaining_ids = set(result.get("remaining_ids", []) if isinstance(result, dict) else [])
        self._legacy_order_ids = remaining_ids
        planned = int(result.get("planned", 0) or 0)
        canceled = int(result.get("canceled", 0) or 0)
        if planned:
            self._append_log(
                f"[LEGACY] cancel result planned={planned} canceled={canceled} remaining={len(remaining_ids)}",
                kind="INFO",
            )
        errors = result.get("errors", [])
        if isinstance(errors, list):
            for message in errors:
                if message:
                    self._append_log(str(message), kind="WARN")
        return not remaining_ids and result.get("ok", False)

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
        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.setChildrenCollapsible(False)
        top_splitter.addWidget(self._build_market_panel())
        top_splitter.addWidget(self._build_grid_panel())
        top_splitter.addWidget(self._build_right_panel())
        top_splitter.setStretchFactor(0, 1)
        top_splitter.setStretchFactor(1, 2)
        top_splitter.setStretchFactor(2, 1)
        return top_splitter

    def _is_micro_profile(self) -> bool:
        return self._symbol.replace("/", "").upper() in MICRO_STABLECOIN_SYMBOLS

    def _settings_anchor_price(self) -> float:
        anchor_price = self._last_price
        if anchor_price is None or anchor_price <= 0:
            return 1.0
        return anchor_price

    def _abs_to_pct(self, value_abs: float, anchor_price: float) -> float:
        if anchor_price <= 0:
            return 0.0
        return value_abs / anchor_price * 100

    def _pct_to_abs(self, value_pct: float, anchor_price: float) -> float:
        return anchor_price * value_pct / 100

    def _micro_grid_defaults(self) -> GridSettingsState:
        anchor_price = self._settings_anchor_price()
        step_pct = self._abs_to_pct(MICRO_STABLECOIN_STEP_ABS_DEFAULT, anchor_price) or MICRO_STABLECOIN_STEP_PCT_DEFAULT
        range_pct = self._abs_to_pct(MICRO_STABLECOIN_RANGE_ABS_DEFAULT, anchor_price) or MICRO_STABLECOIN_RANGE_PCT_DEFAULT
        tp_default = self._abs_to_pct(MICRO_STABLECOIN_TP_ABS_DEFAULT, anchor_price) or step_pct
        tp_default = min(tp_default, MICRO_STABLECOIN_TP_PCT_MAX)
        return GridSettingsState(
            grid_step_pct=step_pct,
            take_profit_pct=tp_default,
            range_low_pct=range_pct,
            range_high_pct=range_pct,
            grid_count=2,
            grid_step_mode="MANUAL",
            range_mode="Manual",
            order_size_mode="Equal",
        )

    def _apply_profile_defaults_if_pristine(self) -> None:
        defaults = GridSettingsState()
        if self._settings_state != defaults:
            return
        if self._is_micro_profile():
            self._apply_micro_defaults(reason="pristine_settings")

    def _resolve_settings_path(self) -> Path:
        base_path = Path(self._app_state.user_config_path or "config.user.yaml")
        return base_path.with_name("nc_pilot_settings.json")

    def _settings_symbol_key(self) -> str:
        return self._symbol.replace("/", "").upper()

    @staticmethod
    def _normalize_symbol_key(symbol: str) -> str:
        return symbol.replace("/", "").upper()

    def _set_settings_state(self, settings: GridSettingsState) -> None:
        self._session.config = settings
        self._settings_state = settings

    def _load_settings_from_disk(self) -> None:
        if not self._is_micro_profile():
            return
        path = self._resolve_settings_path()
        if not path.exists():
            self._apply_micro_defaults(reason="missing_settings")
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self._apply_micro_defaults(reason="invalid_settings_or_crash")
            return
        if not isinstance(payload, dict):
            self._apply_micro_defaults(reason="invalid_settings_or_crash")
            return
        symbols_payload = payload.get("symbols", {}) if isinstance(payload.get("symbols"), dict) else {}
        symbol_payload = symbols_payload.get(self._settings_symbol_key(), {})
        if not isinstance(symbol_payload, dict):
            symbol_payload = {}
        if symbol_payload:
            grid = symbol_payload.get("grid", {}) if isinstance(symbol_payload.get("grid"), dict) else {}
            pilot = symbol_payload.get("pilot", {}) if isinstance(symbol_payload.get("pilot"), dict) else {}
        else:
            defaults_payload = payload.get("defaults", {}) if isinstance(payload.get("defaults"), dict) else {}
            grid = payload.get("grid", {}) if isinstance(payload.get("grid"), dict) else {}
            pilot = payload.get("pilot", {}) if isinstance(payload.get("pilot"), dict) else {}
            if defaults_payload:
                grid = defaults_payload.get("grid", grid) if isinstance(defaults_payload.get("grid"), dict) else grid
                pilot = defaults_payload.get("pilot", pilot) if isinstance(defaults_payload.get("pilot"), dict) else pilot
        anchor_price = self._settings_anchor_price()
        step_abs = self._coerce_float(grid.get("step_abs") or grid.get("grid_step_abs"))
        range_low_abs = self._coerce_float(grid.get("range_low_abs") or grid.get("range_abs"))
        range_high_abs = self._coerce_float(grid.get("range_high_abs") or grid.get("range_abs"))
        tp_abs = self._coerce_float(grid.get("tp_abs") or grid.get("take_profit_abs"))
        if (
            step_abs is None
            or range_low_abs is None
            or range_high_abs is None
            or tp_abs is None
            or not isfinite(step_abs)
            or not isfinite(range_low_abs)
            or not isfinite(range_high_abs)
            or not isfinite(tp_abs)
            or step_abs <= 0
            or range_low_abs <= 0
            or range_high_abs <= 0
            or tp_abs <= 0
        ):
            self._apply_micro_defaults(reason="invalid_settings_or_crash")
            return
        grid_count = int(grid.get("grid_count", 10))
        if grid_count < 2:
            self._apply_micro_defaults(reason="invalid_settings_or_crash")
            return
        budget = self._coerce_float(grid.get("budget")) or GridSettingsState.budget
        stop_loss_pct = self._coerce_float(grid.get("stop_loss_pct")) or GridSettingsState.stop_loss_pct
        max_active_orders = grid.get("max_active_orders", grid_count)
        try:
            max_active_orders = int(max_active_orders)
        except (TypeError, ValueError):
            max_active_orders = grid_count
        settings = GridSettingsState(
            budget=budget,
            direction=str(grid.get("direction", GridSettingsState.direction)),
            grid_count=grid_count,
            grid_step_pct=self._abs_to_pct(step_abs, anchor_price),
            grid_step_mode=str(grid.get("grid_step_mode", "MANUAL")),
            range_mode=str(grid.get("range_mode", "Manual")),
            range_low_pct=self._abs_to_pct(range_low_abs, anchor_price),
            range_high_pct=self._abs_to_pct(range_high_abs, anchor_price),
            take_profit_pct=self._abs_to_pct(tp_abs, anchor_price),
            stop_loss_enabled=bool(grid.get("stop_loss_enabled", GridSettingsState.stop_loss_enabled)),
            stop_loss_pct=stop_loss_pct,
            max_active_orders=max_active_orders,
            order_size_mode=str(grid.get("order_size_mode", GridSettingsState.order_size_mode)),
        )
        if settings.grid_step_pct <= 0 or settings.range_low_pct <= 0 or settings.range_high_pct <= 0:
            self._apply_micro_defaults(reason="invalid_settings_or_crash")
            return
        self._set_settings_state(settings)
        self._manual_grid_step_pct = settings.grid_step_pct
        self.pilot_config.min_profit_bps = (
            self._coerce_float(pilot.get("min_profit_bps")) or self.pilot_config.min_profit_bps
        )
        self.pilot_config.max_step_pct = (
            self._coerce_float(pilot.get("max_step_pct")) or self.pilot_config.max_step_pct
        )
        self.pilot_config.max_range_pct = (
            self._coerce_float(pilot.get("max_range_pct")) or self.pilot_config.max_range_pct
        )
        hold_timeout_raw = pilot.get("hold_timeout_sec", self.pilot_config.hold_timeout_sec)
        try:
            self.pilot_config.hold_timeout_sec = int(hold_timeout_raw)
        except (TypeError, ValueError):
            self.pilot_config.hold_timeout_sec = self.pilot_config.hold_timeout_sec
        self.pilot_config.stale_policy = str(pilot.get("stale_policy", self.pilot_config.stale_policy))
        self.pilot_config.allow_market_close = bool(
            pilot.get("allow_market_close", self.pilot_config.allow_market_close)
        )
        if hasattr(self.pilot_config, "allow_guard_autofix"):
            self.pilot_config.allow_guard_autofix = bool(
                pilot.get("allow_guard_autofix", getattr(self.pilot_config, "allow_guard_autofix", False))
            )
        self._pilot_allow_market = self.pilot_config.allow_market_close
        self._pilot_stale_policy = self._resolve_pilot_stale_policy(self.pilot_config.stale_policy)
        selected_symbols = pilot.get("selected_symbols")
        if isinstance(selected_symbols, list) and selected_symbols:
            normalized = {self._normalize_symbol_key(symbol) for symbol in selected_symbols if symbol}
            if normalized:
                self._session.pilot.selected_symbols = normalized

    def _apply_micro_defaults(self, *, reason: str) -> None:
        self._set_settings_state(self._micro_grid_defaults())
        self._manual_grid_step_pct = self._settings_state.grid_step_pct
        if self._pilot_feature_enabled:
            self._apply_pilot_defaults()
        self._append_log(f"[SETTINGS] defaults applied reason={reason}", kind="INFO")
        if self._pilot_feature_enabled and self._is_micro_profile():
            self._append_log("[NC_PILOT] micro_defaults_applied=true", kind="INFO")
        self._save_settings_to_disk()

    def _refresh_settings_ui_from_state(self) -> None:
        if not hasattr(self, "_budget_input"):
            return
        settings = self._settings_state
        self._budget_input.blockSignals(True)
        self._budget_input.setValue(settings.budget)
        self._budget_input.blockSignals(False)
        self._direction_combo.setCurrentIndex(
            self._direction_combo.findData(settings.direction)
        )
        self._grid_count_input.setValue(settings.grid_count)
        self._grid_step_mode_combo.setCurrentIndex(
            self._grid_step_mode_combo.findData(settings.grid_step_mode)
        )
        self._grid_step_input.blockSignals(True)
        self._grid_step_input.setValue(settings.grid_step_pct)
        self._grid_step_input.blockSignals(False)
        self._range_mode_combo.setCurrentIndex(
            self._range_mode_combo.findData(settings.range_mode)
        )
        self._range_low_input.blockSignals(True)
        self._range_low_input.setValue(settings.range_low_pct)
        self._range_low_input.blockSignals(False)
        self._range_high_input.blockSignals(True)
        self._range_high_input.setValue(settings.range_high_pct)
        self._range_high_input.blockSignals(False)
        self._take_profit_input.blockSignals(True)
        self._take_profit_input.setValue(settings.take_profit_pct)
        self._take_profit_input.blockSignals(False)
        self._stop_loss_toggle.setChecked(settings.stop_loss_enabled)
        self._stop_loss_input.setValue(settings.stop_loss_pct)
        self._max_orders_input.setValue(settings.max_active_orders)
        self._order_size_combo.setCurrentIndex(
            self._order_size_combo.findData(settings.order_size_mode)
        )
        self._apply_grid_step_mode(settings.grid_step_mode)
        self._apply_range_mode(settings.range_mode)
        self._update_grid_preview()

    def _set_open_orders(self, orders: list[dict[str, Any]]) -> None:
        self._session.order_tracking.open_orders = orders
        self._open_orders = orders

    def _set_open_orders_all(self, orders: list[dict[str, Any]]) -> None:
        self._session.order_tracking.open_orders_all = orders
        self._open_orders_all = orders

    def _set_open_orders_map(self, mapping: dict[str, dict[str, Any]]) -> None:
        self._session.order_tracking.open_orders_map = mapping
        self._open_orders_map = mapping

    def _apply_pilot_defaults(self) -> None:
        self.pilot_config.min_profit_bps = 0.05
        self.pilot_config.stale_policy = "CANCEL_REPLACE"
        self.pilot_config.allow_market_close = True
        if hasattr(self.pilot_config, "allow_guard_autofix"):
            self.pilot_config.allow_guard_autofix = False
        self._pilot_allow_market = self.pilot_config.allow_market_close
        self._pilot_stale_policy = self._resolve_pilot_stale_policy(self.pilot_config.stale_policy)

    def _save_settings_to_disk(self) -> None:
        if self._closing:
            return
        if not self._is_micro_profile():
            return
        path = self._resolve_settings_path()
        anchor_price = self._settings_anchor_price()
        grid = self._settings_state
        range_low_abs = self._pct_to_abs(grid.range_low_pct, anchor_price)
        range_high_abs = self._pct_to_abs(grid.range_high_pct, anchor_price)
        grid_payload = {
            "step_abs": self._pct_to_abs(grid.grid_step_pct, anchor_price),
            "range_low_abs": range_low_abs,
            "range_high_abs": range_high_abs,
            "tp_abs": self._pct_to_abs(grid.take_profit_pct, anchor_price),
            "grid_count": grid.grid_count,
            "grid_step_mode": grid.grid_step_mode,
            "range_mode": grid.range_mode,
            "direction": grid.direction,
            "order_size_mode": grid.order_size_mode,
            "budget": grid.budget,
            "max_active_orders": grid.max_active_orders,
            "stop_loss_enabled": grid.stop_loss_enabled,
            "stop_loss_pct": grid.stop_loss_pct,
        }
        pilot_payload = {
            "min_profit_bps": self.pilot_config.min_profit_bps,
            "max_step_pct": self.pilot_config.max_step_pct,
            "max_range_pct": self.pilot_config.max_range_pct,
            "hold_timeout_sec": self.pilot_config.hold_timeout_sec,
            "stale_policy": self.pilot_config.stale_policy,
            "allow_market_close": self.pilot_config.allow_market_close,
            "allow_guard_autofix": getattr(self.pilot_config, "allow_guard_autofix", False),
            "selected_symbols": sorted(self._session.pilot.selected_symbols),
            "slippage_bps": PILOT_SLIPPAGE_BPS,
            "pad_bps": PILOT_PAD_BPS,
            "thin_edge_action": "HOLD",
            "auto_anchor_enabled": True,
            "step_expand_factor": 1.20,
            "range_expand_factor": 1.10,
        }
        payload: dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                existing = {}
            if isinstance(existing, dict):
                payload = existing
        symbols_payload = payload.get("symbols") if isinstance(payload.get("symbols"), dict) else {}
        symbols_payload[self._settings_symbol_key()] = {
            "grid": grid_payload,
            "pilot": pilot_payload,
        }
        payload["symbols"] = symbols_payload
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _schedule_settings_save(self) -> None:
        if self._settings_save_timer is None:
            self._settings_save_timer = QTimer(self)
            self._settings_save_timer.setSingleShot(True)
            self._settings_save_timer.timeout.connect(
                lambda: self._safe_call(self._save_settings_to_disk, label="timer:save_settings")
            )
        self._settings_save_timer.start(500)

    def _profit_guard_slippage_pct(self) -> float:
        return max(PILOT_SLIPPAGE_BPS, 0.0) * 0.01

    def _profit_guard_pad_pct(self) -> float:
        return max(PILOT_PAD_BPS, 0.0) * 0.01

    def _profit_guard_buffer_pct(self) -> float:
        if self._is_micro_profile():
            return MICRO_STABLECOIN_BUFFER_PCT
        return PROFIT_GUARD_BUFFER_PCT

    def _profit_guard_min_floor_pct(self) -> float:
        return max(self.pilot_config.min_profit_bps, 0.0) * 0.01

    def _build_right_panel(self) -> QSplitter:
        self._right_splitter = QSplitter(Qt.Vertical)
        self._right_splitter.addWidget(self._build_runtime_panel())
        self._right_splitter.addWidget(self._build_pilot_dashboard_panel())
        self._right_splitter.setStretchFactor(0, 3)
        self._right_splitter.setStretchFactor(1, 2)
        self._right_splitter.setCollapsible(0, False)
        self._right_splitter.setCollapsible(1, True)
        self._right_splitter.setSizes([600, 320])
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
        self._market_mode = QLabel("—")
        self._rules_label = QLabel(tr("rules_line", rules="—"))
        self._rules_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._rules_label.setToolTip(tr("rules_line", rules="—"))
        self._rules_label.setWordWrap(False)

        self._set_market_label_state(self._market_price, active=False)
        self._set_market_label_state(self._market_spread, active=False)
        self._set_market_label_state(self._market_volatility, active=False)
        self._set_market_label_state(self._market_fee, active=False)
        self._set_market_label_state(self._market_source, active=False)
        self._set_market_label_state(self._market_mode, active=False)
        self._rules_label.setStyleSheet("color: #d1d5db; font-size: 10px;")
        if self._is_micro_profile():
            self._market_mode.setText("MICRO (spread-capture)")
            self._set_market_label_state(self._market_mode, active=True)

        summary_rows = [
            ("Последняя цена:", self._market_price),
            ("Спред:", self._market_spread),
            ("Волатильность:", self._market_volatility),
            ("Комиссия (maker/taker):", self._market_fee),
            ("Источник + возраст:", self._market_source),
            ("Режим:", self._market_mode),
        ]
        for row, (key_text, value_label) in enumerate(summary_rows):
            summary_layout.addWidget(make_summary_key(key_text), row, 0)
            summary_layout.addWidget(value_label, row, 1)

        summary_layout.setColumnStretch(1, 1)
        layout.addWidget(summary_frame)
        layout.addWidget(self._rules_label)
        layout.addWidget(self._build_arb_indicators_panel())
        layout.addStretch(1)
        return group

    def _build_arb_indicators_panel(self) -> QWidget:
        group = QGroupBox("ARB SIGNALS")
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QGridLayout(group)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(6)

        self._arb_signal_rows: dict[str, tuple[LedIndicator, QLabel]] = {}
        rows = [
            ("SPREAD", "SPREAD: NO WINDOW"),
            ("REBAL", "REBAL: NO DATA"),
            ("2LEG", "2LEG: NO DATA"),
            ("LOOP", "LOOP: NO DATA"),
        ]
        for row, (algo_id, text) in enumerate(rows):
            led = LedIndicator(10, group)
            label = QLabel(text)
            label.setStyleSheet("color: #374151; font-size: 10px;")
            label.setWordWrap(True)
            layout.addWidget(led, row, 0)
            layout.addWidget(label, row, 1)
            self._arb_signal_rows[algo_id] = (led, label)
        layout.setColumnStretch(1, 1)
        return group

    def _build_grid_panel(self) -> QWidget:
        group = QGroupBox(tr("grid_settings"))
        self._grid_panel = group
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setVerticalSpacing(6)
        form.setHorizontalSpacing(10)

        def _form_label(text: str) -> QLabel:
            label = QLabel(text)
            label.setFixedWidth(140)
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return label

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
        self._direction_combo.setVisible(False)

        self._grid_count_input = QSpinBox()
        self._grid_count_input.setRange(2, 200)
        self._grid_count_input.setValue(self._settings_state.grid_count)
        self._grid_count_input.valueChanged.connect(lambda value: self._update_setting("grid_count", value))
        self._grid_count_input.setVisible(False)

        self._grid_step_mode_combo = QComboBox()
        self._grid_step_mode_combo.addItem("AUTO ATR", "AUTO_ATR")
        self._grid_step_mode_combo.addItem("MANUAL", "MANUAL")
        step_mode_index = self._grid_step_mode_combo.findData(self._settings_state.grid_step_mode)
        if step_mode_index < 0:
            step_mode_index = self._grid_step_mode_combo.findData("MANUAL")
        self._grid_step_mode_combo.setCurrentIndex(step_mode_index)
        self._grid_step_mode_combo.currentIndexChanged.connect(
            lambda _: self._handle_grid_step_mode_change(self._grid_step_mode_combo.currentData())
        )
        self._grid_step_mode_combo.setVisible(False)

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
        self._manual_override_button.setVisible(False)

        self._range_mode_combo = QComboBox()
        self._range_mode_combo.addItem("Авто", "Auto")
        self._range_mode_combo.addItem("Ручной", "Manual")
        self._range_mode_combo.currentIndexChanged.connect(
            lambda _: self._handle_range_mode_change(self._range_mode_combo.currentData())
        )
        self._range_mode_combo.setVisible(False)

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
        self._tp_fix_button.setVisible(False)
        self._auto_values_label = QLabel(tr("auto_values_line", values="—"))
        self._auto_values_label.setStyleSheet("color: #6b7280; font-size: 10px;")
        self._auto_values_label.setVisible(False)

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
        self._stop_loss_toggle.setVisible(False)
        self._stop_loss_input.setVisible(False)

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
        self._order_size_combo.setVisible(False)

        form.addRow(_form_label(tr("budget")), self._budget_input)
        form.addRow(_form_label(tr("grid_step")), grid_step_row)
        form.addRow(_form_label(tr("range_low")), self._range_low_input)
        form.addRow(_form_label(tr("range_high")), self._range_high_input)
        form.addRow(_form_label(tr("take_profit")), tp_row)
        form.addRow(_form_label(tr("max_active_orders")), self._max_orders_input)

        layout.addLayout(form)

        actions = QHBoxLayout()
        actions.addStretch()
        self._apply_button = QPushButton("Apply")
        self._apply_button.setFixedHeight(24)
        self._apply_button.clicked.connect(self._apply_settings_from_ui)
        actions.addWidget(self._apply_button)
        self._reset_button = QPushButton(tr("reset_defaults"))
        self._reset_button.clicked.connect(self._reset_defaults)
        self._reset_button.setVisible(False)
        actions.addWidget(self._reset_button)
        layout.addLayout(actions)

        self._grid_preview_label = QLabel("—")
        self._grid_preview_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        layout.addWidget(self._grid_preview_label)
        layout.addWidget(self._build_pilot_control_panel())

        self._apply_range_mode(self._settings_state.range_mode)
        self._apply_grid_step_mode(self._settings_state.grid_step_mode)
        self._update_grid_preview()
        self._strategy_controls = [
            self._budget_input,
            self._grid_step_input,
            self._range_low_input,
            self._range_high_input,
            self._take_profit_input,
            self._max_orders_input,
            self._apply_button,
            self._dry_run_toggle,
        ]
        return group

    def _build_runtime_panel(self) -> QWidget:
        group = QGroupBox(tr("runtime"))
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        group.setMinimumHeight(260)
        group.setMinimumHeight(280)
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

        self._cancel_selected_button.setVisible(False)
        self._cancel_all_button.setVisible(False)
        buttons.addWidget(self._refresh_button)
        layout.addLayout(buttons)

        self._cancel_all_on_stop_toggle = QCheckBox(tr("cancel_all_on_stop"))
        self._cancel_all_on_stop_toggle.setChecked(True)
        layout.addWidget(self._cancel_all_on_stop_toggle)

        self._apply_pnl_style(self._pnl_label, None)
        self._refresh_orders_metrics()
        return group

    def _build_pilot_control_panel(self) -> QWidget:
        group = QGroupBox("PILOT")
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        buttons = QHBoxLayout()
        self._pilot_start_analysis_button = QPushButton("Старт анализ")
        self._pilot_start_trading_button = QPushButton("Старт торговля")
        self._pilot_stop_trading_button = QPushButton("Стоп")
        self._pilot_log_button = QPushButton("Лог пилота")
        for button in (
            self._pilot_start_analysis_button,
            self._pilot_start_trading_button,
            self._pilot_stop_trading_button,
            self._pilot_log_button,
        ):
            button.setFixedHeight(26)
        self._pilot_start_analysis_button.clicked.connect(self._handle_pilot_start_analysis)
        self._pilot_start_trading_button.clicked.connect(self._handle_pilot_start_trading)
        self._pilot_stop_trading_button.clicked.connect(self._handle_pilot_stop_trading)
        self._pilot_log_button.clicked.connect(self._show_pilot_log_window)
        buttons.addWidget(self._pilot_start_analysis_button)
        buttons.addWidget(self._pilot_start_trading_button)
        buttons.addWidget(self._pilot_stop_trading_button)
        buttons.addWidget(self._pilot_log_button)
        layout.addLayout(buttons)

        self._pilot_status_line = QLabel("state=IDLE | best=— | edge=— bps | last=—")
        self._pilot_status_line.setStyleSheet("color: #6b7280; font-size: 11px;")
        layout.addWidget(self._pilot_status_line)

        pairs_title = QLabel("Пары пилота")
        pairs_title.setStyleSheet("color: #374151; font-size: 11px; font-weight: 600;")
        layout.addWidget(pairs_title)
        pairs_layout = QGridLayout()
        pairs_layout.setHorizontalSpacing(6)
        pairs_layout.setVerticalSpacing(2)
        self._pilot_pair_checkboxes: dict[str, QCheckBox] = {}
        selected_symbols = {symbol.upper() for symbol in self._session.pilot.selected_symbols}
        for idx, symbol in enumerate(PILOT_SYMBOLS):
            checkbox = QCheckBox(symbol)
            checkbox.setChecked(symbol in selected_symbols)
            checkbox.stateChanged.connect(self._update_pilot_selected_symbols)
            self._pilot_pair_checkboxes[symbol] = checkbox
            row = idx // 2
            col = idx % 2
            pairs_layout.addWidget(checkbox, row, col)
        layout.addLayout(pairs_layout)

        self._pilot_cancel_on_stop_toggle = QCheckBox("Cancel pilot orders on Stop")
        self._pilot_cancel_on_stop_toggle.setChecked(True)
        layout.addWidget(self._pilot_cancel_on_stop_toggle)

        self._set_pilot_controls(analysis_on=False, trading_on=False)
        return group

    def _build_pilot_dashboard_panel(self) -> QWidget:
        group = QGroupBox("PILOT DASHBOARD")
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        group.setMinimumHeight(200)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._pilot_dashboard_counters_label = QLabel(
            "Pilot: state=— | windows_today=0 | max_edge=— bps | active_routes=0 | last_decision=—"
        )
        self._pilot_dashboard_counters_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        layout.addWidget(self._pilot_dashboard_counters_label)

        self._pilot_routes_table = QTableWidget(4, 7, self)
        self._pilot_routes_table.setHorizontalHeaderLabels(
            [
                "Family",
                "Route",
                "Profit abs",
                "Profit bps",
                "Life",
                "Valid",
                "Reason",
            ]
        )
        header = self._pilot_routes_table.horizontalHeader()
        header.setStretchLastSection(True)
        for column in range(6):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self._pilot_routes_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pilot_routes_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._pilot_routes_table.verticalHeader().setDefaultSectionSize(22)
        self._pilot_routes_table.setMinimumHeight(200)
        self._pilot_routes_table.cellClicked.connect(self._handle_pilot_route_click)
        self._pilot_routes_table.setRowCount(4)
        self._pilot_routes_table.setVerticalHeaderLabels(["SPREAD", "REBAL", "2LEG", "LOOP"])
        self._pilot_route_rows = {"SPREAD": 0, "REBAL": 1, "2LEG": 2, "LOOP": 3}
        for family, row in self._pilot_route_rows.items():
            initial = [family, "—", "—", "—", "—", "—", "—"]
            for column, value in enumerate(initial):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignCenter)
                self._pilot_routes_table.setItem(row, column, item)
        layout.addWidget(self._pilot_routes_table)
        return group

    def _build_logs(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setMinimumHeight(200)
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

    def _handle_pilot_enable(self) -> None:
        self._set_pilot_enabled(True)

    def _handle_pilot_disable(self) -> None:
        self._set_pilot_enabled(False)

    def _handle_pilot_settings(self) -> None:
        dialog = PilotSettingsDialog(self.pilot_config, on_save=self._apply_pilot_config, parent=self)
        dialog.exec()

    def _handle_pilot_start_analysis(self) -> None:
        self._pilot_controller.start_analysis()

    def _handle_pilot_start_trading(self) -> None:
        self._pilot_controller.start_trading()

    def _handle_pilot_stop_trading(self) -> None:
        cancel_orders = bool(
            getattr(self, "_pilot_cancel_on_stop_toggle", None)
            and self._pilot_cancel_on_stop_toggle.isChecked()
        )
        self._pilot_controller.stop(cancel_orders=cancel_orders)

    def _ensure_pilot_log_window(self) -> PilotLogWindow:
        if self._pilot_log_window is None:
            self._pilot_log_window = PilotLogWindow(self)
        return self._pilot_log_window

    def _show_pilot_log_window(self) -> None:
        window = self._ensure_pilot_log_window()
        window.show()
        window.raise_()
        window.activateWindow()

    def _append_pilot_log(self, message: str) -> None:
        window = self._ensure_pilot_log_window()
        window.append_line(message)

    def _handle_pilot_route_click(self, row: int, _column: int) -> None:
        if not hasattr(self, "_pilot_routes_table"):
            return
        algo_item = self._pilot_routes_table.item(row, 0)
        algo_id = algo_item.text() if algo_item else "—"
        snapshot = self._pilot_route_snapshots.get(algo_id)
        if snapshot and isinstance(snapshot, dict):
            details = snapshot.get("details", {})
            route_text = snapshot.get("route_text", "—")
            profit_bps = snapshot.get("profit_bps", 0.0)
            profit_abs = snapshot.get("profit_abs_usdt", 0.0)
            message = (
                f"[PILOT] {algo_id} route={route_text} "
                f"profit={profit_abs:+.2f}USDT ({profit_bps:+.2f}bps) "
                f"details={details}"
            )
        else:
            message = f"[PILOT] {algo_id} route details unavailable"
        self._append_pilot_log(message)

    def _set_pilot_controls(self, *, analysis_on: bool, trading_on: bool) -> None:
        if hasattr(self, "_pilot_start_analysis_button"):
            self._pilot_start_analysis_button.setEnabled(not analysis_on)
        if hasattr(self, "_pilot_start_trading_button"):
            self._pilot_start_trading_button.setEnabled(analysis_on and not trading_on)
        if hasattr(self, "_pilot_stop_trading_button"):
            self._pilot_stop_trading_button.setEnabled(analysis_on or trading_on)

    def _update_pilot_selected_symbols(self) -> None:
        if not hasattr(self, "_pilot_pair_checkboxes"):
            return
        fixed = set(PILOT_SYMBOLS)
        for symbol, checkbox in self._pilot_pair_checkboxes.items():
            checkbox.setChecked(symbol in fixed)
        self._session.pilot.selected_symbols = fixed
        self._schedule_settings_save()
        self._update_arb_signals()

    def _update_pilot_status_line(self) -> None:
        if not hasattr(self, "_pilot_status_line"):
            return
        runtime = self._session.pilot
        best = runtime.last_best_route or "—"
        edge = f"{runtime.last_best_edge_bps:.2f}" if runtime.last_best_edge_bps is not None else "—"
        last = runtime.last_decision or "—"
        alias_summary = self._pilot_alias_summary()
        suffix = f" | alias={alias_summary}" if alias_summary else ""
        self._pilot_status_line.setText(
            f"state={runtime.state} | best={best} | edge={edge} bps | last={last}{suffix}"
        )
        self._set_pilot_controls(analysis_on=runtime.analysis_on, trading_on=runtime.trading_on)

    def _update_pilot_dashboard_counters(self) -> None:
        if not hasattr(self, "_pilot_dashboard_counters_label"):
            return
        runtime = self._session.pilot
        counters = runtime.counters
        edge = f"{runtime.last_best_edge_bps:.2f}" if runtime.last_best_edge_bps is not None else "—"
        last_decision = runtime.last_decision or "—"
        active_routes = 0
        if hasattr(self, "_pilot_route_snapshots"):
            active_routes = sum(
                1
                for snap in self._pilot_route_snapshots.values()
                if isinstance(snap, dict) and snap.get("valid")
            )
        self._pilot_dashboard_counters_label.setText(
            "Pilot: state="
            f"{runtime.state} | windows_today={counters.windows_found_today} "
            f"| max_edge={edge} bps | active_routes={active_routes} | last_decision={last_decision}"
        )

    def _update_pilot_routes_table(self, snapshots: list[dict[str, Any]]) -> None:
        if not hasattr(self, "_pilot_routes_table") or not hasattr(self, "_pilot_route_rows"):
            return
        for snapshot in snapshots:
            family = snapshot.get("family")
            if not family or family not in self._pilot_route_rows:
                continue
            row = self._pilot_route_rows[family]
            reason = snapshot.get("reason", "")
            valid = bool(snapshot.get("valid"))
            is_alive = bool(snapshot.get("is_alive"))
            status = self._pilot_invalid_label(reason)
            life_s = snapshot.get("life_s", 0.0)
            life_s = life_s if isinstance(life_s, (int, float)) else 0.0
            values = [
                family,
                snapshot.get("route_text", "—"),
                f"{snapshot.get('profit_abs_usdt', 0.0):+.2f}" if valid and is_alive else status,
                f"{snapshot.get('profit_bps', 0.0):+.2f}" if valid and is_alive else status,
                f"{life_s:.1f}s",
                "YES" if valid and is_alive else "NO",
                reason or "—",
            ]
            for column, value in enumerate(values):
                item = self._pilot_routes_table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    item.setTextAlignment(Qt.AlignCenter)
                    self._pilot_routes_table.setItem(row, column, item)
                item.setText(str(value))
        self._pilot_route_snapshots = {snap.get("family", ""): snap for snap in snapshots}

    def _update_arb_signals(self) -> None:
        if not hasattr(self, "_arb_signal_rows"):
            return
        snapshots = [
            snap for snap in self._pilot_route_snapshots.values() if isinstance(snap, dict)
        ]
        for snapshot in snapshots:
            family = snapshot.get("family")
            row = self._arb_signal_rows.get(family)
            if not row:
                continue
            led, label = row
            profit_abs = snapshot.get("profit_abs_usdt", 0.0)
            profit_bps = snapshot.get("profit_bps", 0.0)
            life_s = snapshot.get("life_s", 0.0)
            route_text = snapshot.get("route_text", "—")
            valid = bool(snapshot.get("valid"))
            reason = snapshot.get("reason", "")
            status = self._pilot_invalid_label(reason)
            is_alive = bool(snapshot.get("is_alive"))
            life_s = life_s if isinstance(life_s, (int, float)) else 0.0
            if valid and is_alive:
                label.setText(
                    f"{family}: {route_text} | "
                    f"{profit_abs:+.2f} USDT ({profit_bps:+.2f} bps) | "
                    f"life {life_s:.1f}s"
                )
            else:
                label.setText(f"{family}: NO WINDOW ({status})")
            led.set_state(valid and is_alive)
            details = snapshot.get("details", {})
            tooltip = (
                f"{family}\n"
                f"route={route_text}\n"
                f"profit={profit_abs:+.2f}USDT ({profit_bps:+.2f}bps)\n"
                f"life={life_s:.1f}s valid={str(valid).lower()} alive={str(is_alive).lower()}\n"
                f"reason={reason} details={details}"
            )
            label.setToolTip(tooltip)
            led.set_tooltip(tooltip)

    @staticmethod
    def _pilot_invalid_label(reason: str) -> str:
        if reason.startswith("invalid_symbol:"):
            return "INVALID"
        return {
            "no_data": "NO DATA",
            "no_bidask": "NO BID/ASK",
            "stale": "STALE",
            "pair_disabled": "DISABLED",
            "no_window": "NO WINDOW",
        }.get(reason, "NO DATA")

    def _pilot_client_order_id(
        self,
        *,
        arb_id: str,
        leg_index: int,
        leg_type: str,
        side: str,
    ) -> str:
        client_id = f"PILOT|{arb_id}|{self._symbol}|L{leg_index}|{leg_type}|{side}"
        return self._limit_client_order_id(client_id)

    def _pilot_trade_gate_ready(self) -> bool:
        if not self._live_enabled():
            return True
        return self._trade_gate == TradeGate.TRADE_OK

    def _send_pilot_order(
        self,
        *,
        side: str,
        price: Decimal,
        qty: Decimal,
        client_order_id: str,
        arb_id: str,
        leg_index: int,
        leg_type: str,
        dry_run: bool,
    ) -> bool:
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        price_text = self.fmt_price(price, tick)
        qty_text = self.fmt_qty(qty, step)
        self._append_log(
            (
                f"[EXEC] arb_id={arb_id} leg={leg_index} {leg_type} "
                f"side={side} price={price_text} qty={qty_text} dry={str(dry_run).lower()}"
            ),
            kind="INFO",
        )
        if dry_run:
            self._append_pilot_virtual_order(
                {
                    "orderId": client_order_id,
                    "clientOrderId": client_order_id,
                    "side": f"{side} [DRY]",
                    "price": price_text,
                    "origQty": qty_text,
                    "executedQty": "[DRY]",
                    "status": "NEW",
                    "time": int(time_fn() * 1000),
                }
            )
            self._session.pilot.order_client_ids.add(client_order_id)
            return True
        response, error, status = self._place_limit(
            side,
            price,
            qty,
            client_order_id,
            reason="pilot",
            skip_open_order_duplicate=True,
            skip_registry=False,
            allow_existing_client_id=False,
        )
        if response:
            self._session.pilot.order_client_ids.add(client_order_id)
            return True
        if error and status != "skip_duplicate_exchange":
            self._append_log(str(error), kind="WARN")
        return False

    def _append_pilot_virtual_order(self, order: dict[str, Any]) -> None:
        self._pilot_virtual_orders.append(order)
        snapshot = self._build_orders_snapshot(self._open_orders)
        self._set_orders_snapshot(snapshot, reason="pilot_dry_run")

    def _cancel_pilot_orders(self) -> int:
        if self._dry_run_toggle.isChecked():
            count = len(self._pilot_virtual_orders)
            self._pilot_virtual_orders.clear()
            self._session.pilot.order_client_ids.clear()
            snapshot = self._build_orders_snapshot(self._open_orders)
            self._set_orders_snapshot(snapshot, reason="pilot_cancel_dry_run")
            return count
        canceled = 0
        for order in list(self._open_orders):
            if not isinstance(order, dict):
                continue
            client_id = str(order.get("clientOrderId", ""))
            if not client_id.startswith("PILOT|"):
                continue
            order_id = str(order.get("orderId", ""))
            if order_id:
                ok, _error, _outcome = self._cancel_order_idempotent(order_id)
                if ok:
                    canceled += 1
                    self._session.pilot.order_client_ids.discard(client_id)
            else:
                try:
                    self._cancel_order_sync(symbol=self._symbol, orig_client_order_id=client_id)
                    canceled += 1
                    self._session.pilot.order_client_ids.discard(client_id)
                except Exception:  # noqa: BLE001
                    continue
        return canceled

    def _apply_pilot_config(self, config: PilotConfig) -> None:
        if not self._pilot_feature_enabled:
            return
        self.pilot_config = config
        self._pilot_stale_policy = self._resolve_pilot_stale_policy(config.stale_policy)
        self._pilot_allow_market = config.allow_market_close
        self._append_log(
            (
                "[NC_PILOT] settings updated "
                f"min_profit_bps={config.min_profit_bps:.2f} "
                f"max_step_pct={config.max_step_pct:.2f} "
                f"max_range_pct={config.max_range_pct:.2f} "
                f"hold_timeout_sec={config.hold_timeout_sec} "
                f"allow_market_close={str(config.allow_market_close).lower()}"
            ),
            kind="INFO",
        )
        self._save_settings_to_disk()

    @staticmethod
    def _resolve_pilot_stale_policy(value: str) -> StalePolicy:
        mapping = {
            "NONE": StalePolicy.NONE,
            "RECENTER": StalePolicy.RECENTER,
            "CANCEL_REPLACE": StalePolicy.CANCEL_REPLACE_STALE,
            "CANCEL_REPLACE_STALE": StalePolicy.CANCEL_REPLACE_STALE,
        }
        return mapping.get(value.upper(), StalePolicy.CANCEL_REPLACE_STALE)

    def _set_pilot_enabled(self, enabled: bool) -> None:
        if not self._pilot_feature_enabled:
            return
        if self.pilot_enabled == enabled:
            return
        self.pilot_enabled = enabled
        if enabled:
            self._pilot_anchor_price = self._last_price
            self._set_pilot_state(PilotState.NORMAL, reason="button_on")
            self._append_log("[PILOT] enabled", kind="INFO")
        else:
            self._pilot_anchor_price = None
            self._pilot_hold_until = None
            self._pilot_hold_reason = None
            self._set_pilot_state(PilotState.OFF, reason="button_off")
            self._log_pilot_disabled()
        self._set_pilot_buttons_enabled(not self._pilot_action_in_progress)

    def _pilot_status_text(self) -> str:
        return "Пилот: ON (MICRO)" if self.pilot_enabled else "Пилот: OFF"

    def _log_pilot_disabled(self) -> None:
        if self._pilot_feature_enabled:
            self._append_log("[NC_PILOT] guard=warn_only", kind="INFO")

    def _pilot_min_sell_qty(self) -> Decimal:
        step = self._rule_decimal(self._exchange_rules.get("step"))
        min_qty = self._rule_decimal(self._exchange_rules.get("min_qty")) or Decimal("0")
        return min_qty + self._base_dust_buffer(step)

    def _pilot_accumulate_base_active(self, base_free: Decimal | None = None) -> bool:
        if base_free is None:
            balance_snapshot = self._balance_snapshot()
            base_free = self.as_decimal(balance_snapshot.get("base_free", Decimal("0")))
        min_sell_qty = self._pilot_min_sell_qty()
        if min_sell_qty <= 0:
            return False
        return base_free < min_sell_qty

    def _pilot_expected_profit_bps(self) -> float | None:
        if self._market_health and self._market_health.expected_profit_bps is not None:
            return float(self._market_health.expected_profit_bps)
        return None

    def _pilot_thin_edge_active(self) -> bool:
        expected_profit_bps = self._pilot_expected_profit_bps()
        min_profit_bps = max(self.pilot_config.min_profit_bps, 0.0)
        if expected_profit_bps is not None and expected_profit_bps < min_profit_bps:
            return True
        return self._market_health_last_reason == "thin_edge"

    def _pilot_shift_anchor(self, *, reason: str) -> None:
        if self._last_price is None:
            return
        if self._pilot_anchor_price == self._last_price:
            return
        self._pilot_anchor_price = self._last_price
        self._append_log(
            f"[PILOT] transition anchor -> {self._last_price:.8f} reason={reason}",
            kind="INFO",
        )

    def _pilot_update_state_machine(self) -> None:
        if not self.pilot_enabled:
            return
        balance_snapshot = self._balance_snapshot()
        base_free = self.as_decimal(balance_snapshot.get("base_free", Decimal("0")))
        if self._pilot_accumulate_base_active(base_free):
            reason = f"base_free={self._format_balance_decimal(base_free)}"
            self._set_pilot_state(PilotState.ACCUMULATE_BASE, reason=reason)
            return
        now = monotonic()
        if self._pilot_thin_edge_active():
            self._pilot_hold_reason = "thin_edge"
            self._pilot_hold_until = now + max(self.pilot_config.hold_timeout_sec, 0)
        if self._pilot_hold_until and now <= self._pilot_hold_until:
            reason = self._pilot_hold_reason or "guard"
            self._set_pilot_state(PilotState.HOLD, reason=reason)
            return
        self._pilot_hold_until = None
        self._pilot_hold_reason = None
        self._set_pilot_state(PilotState.NORMAL, reason="kpi_ok")

    def _log_pilot_auto_action(self, *, action: str, reason: str) -> None:
        expected_profit = None
        if self._market_health and self._market_health.expected_profit_bps is not None:
            expected_profit = self._market_health.expected_profit_bps
        expected_text = f"{expected_profit:.2f}" if isinstance(expected_profit, (int, float)) else "—"
        self._append_log(
            f"[PILOT] action={action} reason={reason} expected_profit_bps={expected_text}",
            kind="INFO",
        )

    def _set_pilot_buttons_enabled(self, enabled: bool) -> None:
        for name in (
            "_pilot_enable_button",
            "_pilot_disable_button",
            "_pilot_settings_button",
        ):
            if hasattr(self, name):
                getattr(self, name).setEnabled(enabled)
        if enabled:
            if hasattr(self, "_pilot_enable_button"):
                self._pilot_enable_button.setEnabled(not self.pilot_enabled)
            if hasattr(self, "_pilot_disable_button"):
                self._pilot_disable_button.setEnabled(self.pilot_enabled)

    def _set_pilot_action_in_progress(self, enabled: bool, *, status: str | None = None) -> None:
        self._pilot_action_in_progress = enabled
        self._set_pilot_buttons_enabled(not enabled)
        if hasattr(self, "_pilot_action_status_label"):
            text = status if status else "—"
            prefix = "Статус: "
            self._pilot_action_status_label.setText(f"{prefix}{text}")

    def _pilot_action_label(self, action: PilotAction) -> str:
        mapping = {
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
            f"[WARN] [NC_PILOT] action blocked action={action_label} reason={reason}",
            kind="WARN",
        )

    def _action_lock_blocked(self, requested: str) -> bool:
        if self._current_action and self._current_action != requested:
            self._append_log(
                f"[NC_PILOT] action blocked current={self._current_action} requested={requested}",
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
            PilotAction.FLAG_STALE: "flag_stale",
            PilotAction.CANCEL_REPLACE_STALE: "cancel_replace_stale",
        }
        requested = action_key.get(action, action.value.lower())
        if self._action_lock_blocked(requested):
            return
        if not self.pilot_enabled:
            self._log_pilot_disabled()
            return
        if self._pilot_action_in_progress:
            self._pilot_block_action(action, "in_progress")
            return
        if action in {PilotAction.RECENTER, PilotAction.CANCEL_REPLACE_STALE} and self._pilot_thin_edge_active():
            self._pilot_block_action(action, "thin_edge")
            if action == PilotAction.RECENTER:
                self._pilot_shift_anchor(reason="thin_edge")
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
                        "[NC_PILOT] auto-exec cooldown "
                        f"action={requested} reason={exec_reason} remaining={remaining}s"
                    ),
                    kind="INFO",
                )
                return
            self._pilot_auto_exec_cooldown_ts[cooldown_key] = now
            self._append_log(
                f"[NC_PILOT] auto-exec action={requested} reason={exec_reason}",
                kind="INFO",
            )
            self._log_pilot_auto_action(
                action="shift_grid" if action == PilotAction.RECENTER else requested,
                reason=exec_reason,
            )
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
                    f"[NC_PILOT] snapshot orders total={total} buy={buy_count} "
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
            if self._pilot_thin_edge_active():
                self._pilot_shift_anchor(reason="thin_edge")
                return
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
            reason = self._pilot_hold_reason or "profit guard HOLD"
            self._pilot_block_action(PilotAction.RECENTER, reason)
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
            if self._pilot_accumulate_base_active(base_free):
                self._pilot_recenter_expected_min = sum(1 for order in plan if order.side == "BUY")
        self._append_log(
            (
                "[INFO] [NC_PILOT] recenter start "
                f"old_anchor={old_anchor} new_anchor={anchor_price} "
                f"cancel_n={cancel_count} place_n={len(plan)}"
            ),
            kind="INFO",
        )
        if self._dry_run_toggle.isChecked():
            self._render_sim_orders(plan)
            self._append_log(
                f"[INFO] [NC_PILOT] recenter done place_n={len(plan)}",
                kind="INFO",
            )
            self._activate_stale_warmup()
            self._pilot_anchor_price = anchor_price
            self._pilot_pending_anchor = None
            self._pilot_recenter_expected_min = None
            self._set_pilot_state(PilotState.NORMAL, reason="recenter_complete")
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
        if not self.pilot_enabled:
            self._log_pilot_disabled()
            return
        if self._pilot_thin_edge_active():
            self._pilot_set_warning("thin_edge")
            self._pilot_shift_anchor(reason="thin_edge")
            return
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
                f"[NC_PILOT] auto-exec action=cancel_replace_stale reason={exec_reason}",
                kind="INFO",
            )
            self._log_pilot_auto_action(action="cancel_replace_stale", reason=exec_reason)
        if not self._pilot_confirm_action(PilotAction.CANCEL_REPLACE_STALE, summary):
            return
        self._pilot_stale_action_ts[self._symbol] = time_fn()
        self._set_pilot_action_in_progress(True, status="Выполняю…")
        if self._dry_run_toggle.isChecked():
            self._append_log(
                (
                    "[INFO] [NC_PILOT] stale cancel/replace dry-run "
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
                open_orders_before = self._fetch_open_orders_sync(self._symbol)
                bot_orders_before = self._filter_bot_orders(open_orders_before)
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
                ok, error, _outcome = self._cancel_order_idempotent(order_id)
                if ok:
                    canceled += 1
                    self._pilot_stale_handled[order_id] = time_fn()
                if error:
                    errors.append(error)
            if self._account_client:
                open_orders_after = self._fetch_open_orders_sync(self._symbol)
                bot_orders_after = self._filter_bot_orders(open_orders_after)
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
                    ok, error, _outcome = self._cancel_order_idempotent(order_id)
                    if ok:
                        canceled += 1
                        self._pilot_stale_handled[order_id] = time_fn()
                    if error:
                        errors.append(error)
                open_orders_retry = self._fetch_open_orders_sync(self._symbol)
                bot_orders_retry = self._filter_bot_orders(open_orders_retry)
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

        try:
            result = _cancel_and_replace()
            self._handle_pilot_cancel_replace_stale(result, 0)
        except Exception as exc:  # noqa: BLE001
            self._handle_pilot_action_error(str(exc))

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
        self._set_pilot_state(PilotState.NORMAL, reason="recovery_start")
        self._append_log(
            f"[NC_PILOT] recovery mode enabled symbol={self._symbol}",
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
            f"[NC_PILOT] flatten to BE requested symbol={self._symbol}",
            kind="INFO",
        )
        if self._pilot_allow_market:
            self._append_log(
                "[NC_PILOT] market close requested but not supported, using BE limit",
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
                    f"[INFO] [NC_PILOT] {action_label} "
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
                    ok, error, _outcome = self._cancel_order_idempotent(order_id)
                    if ok:
                        continue
                    if error:
                        errors.append(error)
                open_orders_after = self._fetch_open_orders_sync(self._symbol)
                bot_orders = self._filter_bot_orders(open_orders_after)
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

        try:
            result = _cancel_and_place()
            self._handle_pilot_break_even_result(result, 0)
        except Exception as exc:  # noqa: BLE001
            self._handle_pilot_action_error(str(exc))

    def _pilot_build_grid_plan(
        self,
        anchor_price: float,
        *,
        action_label: str,
    ) -> tuple[list[GridPlannedOrder] | None, GridSettingsState | None, bool]:
        snapshot = self._collect_strategy_snapshot()
        settings = self._resolve_start_settings(snapshot)
        self._apply_grid_clamps(settings, anchor_price)
        if self._is_micro_profile():
            plan, guard_hold, guard_reason = self._build_spread_capture_plan(settings, action_label=action_label)
            if guard_hold:
                self._pilot_hold_reason = guard_reason
                self._pilot_hold_until = monotonic() + max(self.pilot_config.hold_timeout_sec, 0)
                return None, settings, True
        guard_decision = self._apply_profit_guard(
            settings,
            anchor_price,
            action_label=action_label,
            update_ui=False,
        )
        if guard_decision == "HOLD":
            return None, settings, True
        try:
            if self._is_micro_profile():
                planned = plan or []
            else:
                planned = self._grid_engine.start(settings, anchor_price, self._exchange_rules)
        except ValueError as exc:
            self._append_log(f"[NC_PILOT] recenter plan failed: {exc}", kind="WARN")
            return None, None, False
        if not self._dry_run_toggle.isChecked():
            balance_snapshot = self._balance_snapshot()
            base_free = self.as_decimal(balance_snapshot.get("base_free", Decimal("0")))
            if self._pilot_accumulate_base_active(base_free):
                planned = [order for order in planned if order.side == "BUY"]
            else:
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

    def _build_spread_capture_plan(
        self,
        settings: GridSettingsState,
        *,
        action_label: str,
    ) -> tuple[list[GridPlannedOrder] | None, bool, str]:
        guard_ok, guard_reason, context = self._micro_spread_capture_guard()
        if not guard_ok:
            return None, True, guard_reason
        bid = context.get("bid")
        ask = context.get("ask")
        tick = context.get("tick")
        mid = context.get("mid")
        if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)) or bid <= 0 or ask <= 0:
            return None, True, "invalid_bidask"
        if not isinstance(tick, (int, float)) or tick <= 0:
            return None, True, "invalid_tick"
        if not isinstance(mid, (int, float)) or mid <= 0:
            mid = (bid + ask) / 2
        step_pct = self._micro_step_pct_from_tick(mid) or settings.grid_step_pct
        settings.grid_step_mode = "TICK_BASED"
        settings.grid_step_pct = step_pct
        settings.grid_count = 2
        settings.max_active_orders = min(settings.max_active_orders, 2)
        spread_ticks = context.get("spread_ticks")
        tp_ticks = self._micro_tp_ticks(spread_ticks if isinstance(spread_ticks, int) else None)
        settings.take_profit_pct = step_pct * tp_ticks
        settings.range_low_pct = max(settings.range_low_pct, step_pct)
        settings.range_high_pct = max(settings.range_high_pct, step_pct)
        self._grid_engine._plan_stats = GridPlanStats()
        per_order_quote = max(float(settings.budget) / 2, 0.0)
        buy_order = self._build_spread_capture_order(
            side="BUY",
            price=bid,
            per_order_quote=per_order_quote,
            tick=tick,
            step=self._exchange_rules.get("step"),
            min_notional=self._exchange_rules.get("min_notional"),
            min_qty=self._exchange_rules.get("min_qty"),
            max_qty=self._exchange_rules.get("max_qty"),
        )
        sell_order = self._build_spread_capture_order(
            side="SELL",
            price=ask,
            per_order_quote=per_order_quote,
            tick=tick,
            step=self._exchange_rules.get("step"),
            min_notional=self._exchange_rules.get("min_notional"),
            min_qty=self._exchange_rules.get("min_qty"),
            max_qty=self._exchange_rules.get("max_qty"),
        )
        if buy_order and sell_order:
            step_dec = self._rule_decimal(self._exchange_rules.get("step"))
            buy_qty = self.as_decimal(buy_order.qty)
            sell_qty = self.as_decimal(sell_order.qty)
            self._append_log(
                (
                    "[GRID] plan "
                    f"qty_buy={self.fmt_qty(buy_qty, step_dec)} "
                    f"qty_sell={self.fmt_qty(sell_qty, step_dec)} mode=TICK_BASED"
                ),
                kind="INFO",
            )
            aligned_buy, aligned_sell, fixed = align_tick_based_qty(buy_qty, sell_qty, step_dec)
            if fixed:
                buy_order.qty = float(aligned_buy)
                sell_order.qty = float(aligned_sell)
                self._append_log(
                    (
                        "[GRID] qty mismatch -> aligned "
                        f"qty_buy={self.fmt_qty(aligned_buy, step_dec)} "
                        f"qty_sell={self.fmt_qty(aligned_sell, step_dec)} mode=TICK_BASED"
                    ),
                    kind="WARN",
                )
        plan = [order for order in (buy_order, sell_order) if order is not None]
        if not plan:
            self._append_log(
                f"[PLAN] spread-capture empty action={action_label}",
                kind="WARN",
            )
        return plan, False, "ok"

    def _build_spread_capture_order(
        self,
        *,
        side: str,
        price: float,
        per_order_quote: float,
        tick: float | None,
        step: float | None,
        min_notional: float | None,
        min_qty: float | None,
        max_qty: float | None,
    ) -> GridPlannedOrder | None:
        side = side.upper()
        if price <= 0 or per_order_quote <= 0:
            return None
        round_mode = "down" if side == "BUY" else "up"
        price_rounded = self._grid_engine.round_price_to_tick(price, tick, mode=round_mode)
        if price_rounded <= 0:
            return None
        qty = per_order_quote / price_rounded if price_rounded > 0 else 0.0
        qty = self._grid_engine.quantize_qty(qty, step, mode="down")
        qty = self._grid_engine._adjust_qty_for_filters(
            side=side,
            price=price_rounded,
            qty=qty,
            step=step,
            min_notional=min_notional,
            min_qty=min_qty,
            max_qty=max_qty,
        )
        if qty <= 0:
            return None
        return GridPlannedOrder(side=side, price=price_rounded, qty=qty, level_index=1)

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
                    ok, error, _outcome = self._cancel_order_idempotent(order_id)
                    if ok:
                        canceled += 1
                    if error:
                        errors.append(error)
            open_orders_after: list[dict[str, Any]] = []
            if self._account_client:
                deadline = monotonic() + 5.0
                while monotonic() < deadline:
                    open_orders_after = self._fetch_open_orders_sync(self._symbol)
                    bot_orders = self._filter_bot_orders(open_orders_after)
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
        try:
            result = _cancel()
            self._handle_pilot_recenter_cancel(result, 0, plan)
        except Exception as exc:  # noqa: BLE001
            self._handle_pilot_action_error(str(exc))

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
                f"[NC_PILOT] recenter cancel done canceled={canceled} planned={planned}",
                kind="INFO",
            )
            open_orders_after = result.get("open_orders_after", [])
            if isinstance(open_orders_after, list):
                open_count = len([order for order in open_orders_after if isinstance(order, dict)])
                if open_count:
                    self._append_log(
                        f"[NC_PILOT] recenter cancel wait timeout open_orders={open_count}",
                        kind="WARN",
                    )
                self._set_open_orders_all([item for item in open_orders_after if isinstance(item, dict)])
                self._set_open_orders(self._filter_bot_orders(self._open_orders_all))
                self._set_open_orders_map({
                    str(order.get("orderId", "")): order
                    for order in self._open_orders
                    if str(order.get("orderId", ""))
                })
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
                open_orders = self._fetch_open_orders_sync(self._symbol)
                bot_orders = self._filter_bot_orders(open_orders)
                if len(bot_orders) >= expected_min:
                    met = True
                    break
                self._sleep_ms(500)
            return {"open_orders": open_orders, "met": met, "expected_min": expected_min}
        try:
            result = _poll()
            self._handle_pilot_recenter_open_orders(result, 0)
        except Exception as exc:  # noqa: BLE001
            self._handle_pilot_action_error(str(exc))

    def _handle_pilot_recenter_open_orders(self, result: object, latency_ms: int) -> None:
        _ = latency_ms
        if not isinstance(result, dict):
            self._handle_pilot_action_error("Unexpected recenter open_orders response")
            return
        open_orders = result.get("open_orders", [])
        met = bool(result.get("met", False))
        expected_min = int(result.get("expected_min", 0) or 0)
        if isinstance(open_orders, list):
            self._orders_refresh_reason = "recenter"
            self._handle_open_orders(open_orders, latency_ms)
        if not met:
            self._append_log(
                f"[NC_PILOT] recenter open_orders wait timeout expected_min={expected_min}",
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
        self._set_pilot_state(PilotState.NORMAL, reason="recenter_complete")
        self._append_log(
            f"[INFO] [NC_PILOT] recenter done open_orders={len(open_orders)}",
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
                f"[INFO] [NC_PILOT] stale cancel/replace skipped reason={skip_reason}",
                kind="INFO",
            )
            self._set_pilot_action_in_progress(False)
            return
        self._append_log(
            f"[INFO] [NC_PILOT] stale cancel/replace done cancel_n={canceled} place_n={placed}",
            kind="INFO",
        )
        self._activate_stale_warmup()
        self._request_orders_refresh("pilot_cancel_replace")
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
                    f"[NC_PILOT] be order placed cancel={cancel_n} "
                    f"orderId={order_id} side={side} price={price} qty={qty}"
                ),
                kind="INFO",
            )
        self._request_orders_refresh("pilot_break_even")
        self._set_pilot_action_in_progress(False)
        self._current_action = None
        self._pilot_pending_action = None
        self._pilot_pending_plan = None

    def _handle_pilot_action_error(self, message: str) -> None:
        self._append_log(f"[NC_PILOT] action error: {message}", kind="WARN")
        self._set_pilot_action_in_progress(False)
        self._current_action = None
        if self._pilot_pending_anchor is not None:
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

    def _pilot_force_hold(self, *, reason: str) -> None:
        if self._closing:
            return
        try:
            now = monotonic()
            hold_timeout = max(self.pilot_config.hold_timeout_sec, 0)
            self._pilot_hold_reason = reason
            self._pilot_hold_until = now + hold_timeout if hold_timeout else now
            self._set_pilot_state(PilotState.HOLD, reason=reason)
        except Exception:
            return

    def panic_hold(self, reason: str) -> None:
        if self._closing:
            return
        try:
            self._logger.error("[NC_PILOT][PANIC] hold reason=%s", reason)
            self._current_action = None
            self._pilot_pending_action = None
            self._pilot_pending_plan = None
            self._set_pilot_action_in_progress(False)
            self._pilot_force_hold(reason=f"panic:{reason}")
        except Exception:
            return

    def _safe_call(
        self,
        fn: Callable[[], object],
        *,
        label: str,
        on_error: Callable[[], object] | None = None,
    ) -> None:
        def _wrapped_on_error() -> None:
            if on_error is not None:
                on_error()
            self._pilot_force_hold(reason=f"exception:{label}")

        safe_call(
            fn,
            label=label,
            on_error=_wrapped_on_error,
            logger=self._logger,
            crash_writer=self._write_crash_log_line if self._crash_log_path else None,
        )

    def _set_pilot_state(self, state: PilotState, *, reason: str | None = None) -> None:
        if self._pilot_state == state:
            return
        prev = self._pilot_state
        self._pilot_state = state
        if not self._pilot_feature_enabled:
            return
        reason_text = reason or "unknown"
        self._append_log(
            f"[PILOT] transition {prev.value} -> {state.value} reason={reason_text}",
            kind="INFO",
        )
        self._append_log(f"[PILOT] state={state.value}", kind="INFO")
        if state == PilotState.HOLD and reason:
            self._append_log(f"[PILOT] HOLD reason={reason}", kind="INFO")
        if state == PilotState.ACCUMULATE_BASE and reason:
            self._append_log(f"[PILOT] ACCUMULATE_BASE {reason}", kind="INFO")

    @staticmethod
    def _pilot_state_label(state: PilotState) -> str:
        mapping = {
            PilotState.OFF: "Выключен",
            PilotState.NORMAL: "Нормальный",
            PilotState.HOLD: "Ожидание",
            PilotState.ACCUMULATE_BASE: "Накопление базы",
        }
        return mapping.get(state, state.value)

    def _update_pilot_panel(self) -> None:
        if self._closing:
            return
        try:
            self._update_pilot_status_line()
            self._update_pilot_dashboard_counters()
            if hasattr(self, "_pilot_route_snapshots"):
                snapshots = [
                    snap for snap in self._pilot_route_snapshots.values() if isinstance(snap, dict)
                ]
                if snapshots:
                    self._update_pilot_routes_table(snapshots)
            if not hasattr(self, "_pilot_state_value"):
                return
            self._pilot_update_state_machine()
            self._pilot_state_value.setText(self._pilot_state_label(self._pilot_state))
            if hasattr(self, "_pilot_status_label"):
                self._pilot_status_label.setText(self._pilot_status_text())
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
                    f"[NC_PILOT] auto-recenter trigger distance={distance_pct:.2f}% symbol={self._symbol}",
                    kind="INFO",
                )
                if self.pilot_enabled:
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
            pnl_text = "—"
            if self._last_price is not None and self._base_asset:
                base_free = self._balances.get(self._base_asset, (0.0, 0.0))[0]
                pnl_text = self.fmt_price(self.as_decimal(base_free), None)
            self._pilot_unrealized_value.setText(pnl_text)
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _update_pilot_panel")
            self._notify_crash("_update_pilot_panel")
            self._pilot_force_hold(reason="exception:_update_pilot_panel")
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
        self._apply_grid_clamps(settings, anchor_price)
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
        if self._is_micro_profile():
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

    def _http_book_age_ms(self) -> int | None:
        snapshot = self.book_snapshot or {}
        snapshot_ts = snapshot.get("ts") if snapshot else None
        if not isinstance(snapshot_ts, (int, float)):
            return None
        return int((monotonic() - snapshot_ts) * 1000)

    def _stale_warmup_remaining(self) -> int | None:
        if self._pilot_stale_warmup_until is None:
            return None
        if self.bidask_ready:
            book_age_ms = self._http_book_age_ms()
            if book_age_ms is not None and book_age_ms < 1000:
                self._pilot_stale_warmup_until = None
                self._pilot_stale_warmup_last_log_ts = None
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
            if not hasattr(self, "_orders_table") or not self._order_age_registry:
                return None, None
            now = time_fn()
            oldest_age_sec: int | None = None
            for row in range(self._orders_table.rowCount()):
                order_id = self._order_id_for_row(row)
                if not order_id:
                    continue
                created_ts = self._order_age_registry.get(order_id)
                if created_ts is None:
                    continue
                age_sec = int(max(now - created_ts, 0))
                if oldest_age_sec is None or age_sec > oldest_age_sec:
                    oldest_age_sec = age_sec
            return oldest_age_sec, None
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
        self._orders_last_sync_ts = now
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
            self._remember_order_created_ts(order_id, created_ts)
        missing_ids = [order_id for order_id in self._order_info_map if order_id not in seen_ids]
        for order_id in missing_ids:
            self._order_info_map.pop(order_id, None)
            self._order_age_registry.pop(order_id, None)
            self._pilot_pending_cancel_ids.discard(order_id)
            self._pilot_stale_seen_ids.pop(order_id, None)
            self._pilot_stale_handled.pop(order_id, None)
            self._pilot_stale_skip_log_ts.pop(order_id, None)
            self._purge_stale_log_keys(order_id)

    def _clear_order_info(self) -> None:
        self._open_orders_snapshot_ts = None
        self._orders_last_sync_ts = None
        self._orders_last_change_ts = None
        self._order_info_map = {}
        self._order_age_registry = {}
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
        if self._pilot_thin_edge_active():
            self._pilot_orders_warning_value.setText("—")
            self._pilot_orders_warning_value.setStyleSheet("color: #111827; font-weight: 600;")
            self._pilot_stale_active = False
            return
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
                    "[WARN] [NC_PILOT] stale detected "
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

    def _maybe_log_snapshot_refresh_suppressed(self, reason: str, detail: str | None = None) -> None:
        if self._stop_in_progress:
            return
        now = monotonic()
        state = (reason, detail)
        last_log = self._snapshot_refresh_suppressed_log_ts
        if (
            self._snapshot_refresh_suppressed_state != state
            or last_log is None
            or now - last_log >= LOG_DEDUP_HEARTBEAT_SEC
        ):
            suffix = f" ({detail})" if detail else ""
            self._append_log(
                f"[ORDERS] stale -> suppressed {reason}{suffix}",
                kind="INFO",
            )
            self._snapshot_refresh_suppressed_log_ts = now
            self._snapshot_refresh_suppressed_state = state

    @staticmethod
    def _is_critical_refresh_reason(reason: str) -> bool:
        if reason.startswith(("stop", "cancel")):
            return True
        return reason in {"reconcile", "after_cancel_backoff", "cancel_all_finalize", "cancel_all_error"}

    def _should_allow_stale_refresh(
        self,
        reason: str,
        age_ms: int | None,
    ) -> tuple[bool, tuple[str, ...], int | None]:
        now_ms = int(monotonic() * 1000)
        last_refresh_ms = self._stale_refresh_last_ts.get(reason)
        if reason != "poll":
            if last_refresh_ms is not None and now_ms - last_refresh_ms < STALE_REFRESH_MIN_MS:
                self._stale_refresh_skipped_rate_limit += 1
                self._session.counters.stale_poll_skips += 1
                return False, (), None
            self._stale_refresh_last_ts[reason] = now_ms
            self._stale_refresh_calls += 1
            return True, (), None
        decision = compute_refresh_allowed(
            now_ms=now_ms,
            last_refresh_ms=last_refresh_ms,
            last_force_refresh_ms=self._stale_refresh_last_force_ts_ms,
            refresh_min_interval_ms=STALE_REFRESH_POLL_MIN_MS,
            orders_age_ms=age_ms,
            stale_ms=STALE_REFRESH_POLL_AGE_MS,
            registry_dirty=self._registry_dirty or self._fills_changed_since_refresh,
            hash_changed=self._orders_snapshot_hash_changed,
            stable_hash_cycles=self._orders_snapshot_hash_stable_cycles,
            stable_hash_limit=STALE_REFRESH_HASH_STABLE_CYCLES,
            hard_max_age_ms=STALE_REFRESH_HARD_MAX_MS,
        )
        if decision.blocked_by_interval:
            self._stale_refresh_skipped_rate_limit += 1
            self._session.counters.stale_poll_skips += 1
        elif not decision.allowed:
            self._stale_refresh_skipped_no_change += 1
            self._session.counters.stale_poll_skips += 1
        if decision.allowed:
            self._stale_refresh_last_ts[reason] = now_ms
            self._stale_refresh_last_force_ts_ms = now_ms
            self._stale_refresh_calls += 1
        return decision.allowed, decision.reasons, decision.dt_since_last_ms

    def _maybe_log_stale_refresh(
        self,
        reason: str,
        allowed: bool,
        why: tuple[str, ...],
        dt_since_last_ms: int | None,
    ) -> None:
        why_text = ",".join(why) if why else "none"
        dt_text = f"{dt_since_last_ms}ms" if dt_since_last_ms is not None else "—"
        message = (
            "[ORDERS] stale -> refresh "
            f"reason={reason} allowed={str(allowed).lower()} why={why_text} "
            f"dt_since_last={dt_text} hits={self._stale_refresh_hits} calls={self._stale_refresh_calls} "
            f"skip_rate={self._stale_refresh_skipped_rate_limit} "
            f"skip_no_change={self._stale_refresh_skipped_no_change}"
        )
        now = monotonic()
        if reason == "poll":
            symbol = self._symbol
            last_log = self._stale_refresh_poll_log_ts.get(symbol)
            if last_log is not None and now - last_log < STALE_REFRESH_POLL_LOG_MIN_SEC:
                suppressed = self._stale_refresh_poll_suppressed.get(symbol, 0) + 1
                self._stale_refresh_poll_suppressed[symbol] = suppressed
                last_suppressed_log = self._stale_refresh_poll_suppressed_log_ts.get(symbol)
                if (
                    last_suppressed_log is None
                    or now - last_suppressed_log >= STALE_REFRESH_POLL_SUPPRESS_LOG_SEC
                ):
                    self._append_log(
                        (
                            "[ORDERS] stale storm suppressed "
                            f"(n={suppressed} interval={int(STALE_REFRESH_POLL_LOG_MIN_SEC)}s)"
                        ),
                        kind="INFO",
                    )
                    self._stale_refresh_poll_suppressed_log_ts[symbol] = now
                    self._stale_refresh_poll_suppressed[symbol] = 0
                return
            self._stale_refresh_poll_log_ts[symbol] = now
        state = (allowed, why_text)
        if not self._stale_refresh_log_limiter.should_log(reason, state, now):
            return
        self._append_log(message, kind="INFO")
        self._stale_refresh_last_log_ts = now
        self._stale_refresh_last_state = (reason, allowed)

    def _ensure_open_orders_snapshot_fresh(
        self,
        reason: str,
        *,
        max_age_ms: int = 1500,
        cooldown_s: float = 2.0,
    ) -> bool:
        if self._orders_last_sync_ts is None:
            age_ms = None
        else:
            age_ms = int(max(time_fn() - self._orders_last_sync_ts, 0) * 1000)
        if age_ms is not None and age_ms <= max_age_ms:
            return False
        self._stale_refresh_hits += 1
        now = monotonic()
        critical_reason = self._is_critical_refresh_reason(reason)
        if self._orders_in_flight and not critical_reason:
            self._maybe_log_snapshot_refresh_suppressed("inflight")
            return False
        if self._snapshot_refresh_inflight and not critical_reason:
            self._maybe_log_snapshot_refresh_suppressed("inflight")
            return False
        allowed, why, dt_since_last_ms = self._should_allow_stale_refresh(reason, age_ms)
        if reason:
            self._maybe_log_stale_refresh(reason, allowed, why, dt_since_last_ms)
        if not allowed:
            return False
        if now - self._last_snapshot_refresh_ts < cooldown_s:
            remaining_ms = max(int((cooldown_s - (now - self._last_snapshot_refresh_ts)) * 1000), 0)
            self._maybe_log_snapshot_refresh_suppressed("cooldown", f"{remaining_ms} ms")
            return False
        self._last_snapshot_refresh_ts = now
        self._snapshot_refresh_inflight = True
        self._orders_poll_last_ts = int(now * 1000)
        self._refresh_orders(force=True, reason=reason)
        return True

    def _maybe_log_open_orders_snapshot_stale(self, snapshot_age_sec: int, *, skip_actions: bool = False) -> None:
        if self._stop_in_progress:
            return
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
                f"[WARN] [NC_PILOT] open_orders snapshot stale age={snapshot_age_sec}s{suffix}",
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
        if not self.pilot_enabled:
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
        if self._pilot_thin_edge_active():
            self._pilot_shift_anchor(reason="thin_edge")
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
            self._logger.exception("[NC_PILOT][CRASH] exception in _emit_price_update")
            self._notify_crash("_emit_price_update")
            self._pilot_force_hold(reason="exception:_emit_price_update")
            return

    def _emit_status_update(self, status: str, message: str) -> None:
        try:
            self._signals.status_update.emit(status, message)
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _emit_status_update")
            self._notify_crash("_emit_status_update")
            self._pilot_force_hold(reason="exception:_emit_status_update")
            return

    def _kpi_allows_start(self) -> bool:
        if self._kpi_state != KPI_STATE_INVALID:
            return True
        snapshot = self._price_feed_manager.get_snapshot(self._symbol) if hasattr(self, "_price_feed_manager") else None
        price = snapshot.last_price if snapshot and snapshot.last_price is not None else self._last_price
        best_bid, best_ask, _source = self._resolve_book_bid_ask()
        return price is not None and best_bid is not None and best_ask is not None

    def _apply_price_update(self, update: PriceUpdate) -> None:
        if self._closing:
            return
        try:
            self._last_price_update = update
            self._ws_last_tick_ts = monotonic()
            self._ws_alive = True
            price = update.last_price if isinstance(update.last_price, (int, float)) else None
            if price is not None and price > 0:
                self._ws_last_price = price
                self._session.market.last_price = price
            if isinstance(update.best_bid, (int, float)) and isinstance(update.best_ask, (int, float)):
                self._update_ws_book_snapshot(update.best_bid, update.best_ask)
            self._session.market.source = update.source
            self._session.market.age_ms = update.price_age_ms
            self._session.runtime.last_ui_update_ts = monotonic()
            self._queue_trade_source(update.source)
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _apply_price_update")
            self._notify_crash("_apply_price_update")
            self._pilot_force_hold(reason="exception:_apply_price_update")
            return

    def _queue_trade_source(self, source: str | None) -> None:
        if not hasattr(self, "_trade_source_timer"):
            return
        new_source = source or "HTTP"
        if new_source == self.trade_source:
            return
        self._pending_trade_source = new_source
        if not self._trade_source_timer.isActive():
            self._trade_source_timer.start(UI_SOURCE_DEBOUNCE_MS)

    def _apply_pending_trade_source(self) -> None:
        if self._closing:
            return
        if self._pending_trade_source is None:
            return
        self.trade_source = self._pending_trade_source
        self._pending_trade_source = None
        self._last_trade_source_change_ms = int(monotonic() * 1000)

    def _update_http_book_snapshot(self, payload: dict[str, Any]) -> tuple[float | None, float | None]:
        bid = self._coerce_float(payload.get("bidPrice"))
        ask = self._coerce_float(payload.get("askPrice"))
        if not self._validate_bid_ask(bid, ask, source="HTTP_BOOK"):
            return None, None
        now_ts = monotonic()
        self.book_snapshot = {"bid": bid, "ask": ask, "ts": now_ts}
        self._bidask_last = (bid, ask)
        self._bidask_ts = now_ts
        self._bidask_src = "HTTP_BOOK"
        self.bidask_ready = True
        return bid, ask

    def _update_ws_book_snapshot(self, bid: float, ask: float) -> None:
        if not self._validate_bid_ask(bid, ask, source="WS_BOOK"):
            return
        now_ts = monotonic()
        self.book_snapshot = {"bid": bid, "ask": ask, "ts": now_ts}
        self._bidask_last = (bid, ask)
        self._bidask_ts = now_ts
        self._bidask_src = "WS_BOOK"
        self.bidask_ready = True

    def _should_skip_http_book_ticker(self) -> bool:
        if not hasattr(self, "_price_feed_manager") or not self._price_feed_manager:
            return False
        snapshot = self._price_feed_manager.get_snapshot(self._symbol)
        if snapshot is None:
            return False
        if snapshot.source != "WS":
            return False
        if snapshot.best_bid is None or snapshot.best_ask is None:
            return False
        age_ms = snapshot.router_age_ms if snapshot.router_age_ms is not None else snapshot.price_age_ms
        if age_ms is None or age_ms >= 1000:
            return False
        return True

    def _maybe_log_no_bidask(self, reason: str) -> None:
        now = monotonic()
        if not self._no_bidask_warned:
            self._append_log(f"[SKIP] no bid/ask reason={reason}", kind="WARN")
            self._no_bidask_warned = True
            self._no_bidask_last_log_ts = now
            return
        last_log = self._no_bidask_last_log_ts
        if last_log is None or now - last_log >= NO_BIDASK_LOG_INTERVAL_SEC:
            self._append_log(f"[SKIP] no bid/ask reason={reason}", kind="INFO")
            self._no_bidask_last_log_ts = now

    def _validate_bid_ask(self, bid: float | None, ask: float | None, *, source: str) -> bool:
        if bid is None or ask is None:
            if not self.bidask_ready:
                self._maybe_log_no_bidask(self._bidask_state or "missing")
            return False
        if not isfinite(bid) or not isfinite(ask):
            return False
        if ask <= bid:
            return False
        return True

    def _resolve_book_bid_ask(self) -> tuple[float | None, float | None, str]:
        snapshot = self.book_snapshot or {}
        bid = self._coerce_float(snapshot.get("bid")) if snapshot else None
        ask = self._coerce_float(snapshot.get("ask")) if snapshot else None
        if self._validate_bid_ask(bid, ask, source="HTTP_BOOK"):
            return bid, ask, self.trade_source
        return None, None, "—"

    def _update_kpi_waiting_book(
        self,
        now_ts: float,
        book_source: str,
        bidask_age_ms: int | None,
    ) -> None:
        if self._kpi_missing_bidask_since_ts is None:
            self._kpi_missing_bidask_since_ts = now_ts
        if not self._kpi_missing_bidask_active:
            self._append_log(
                "[KPI] state=WAITING_BOOK reason=no_bidask_yet",
                kind="INFO",
            )
            self._kpi_missing_bidask_active = True
        bidask_age_text = f"{bidask_age_ms}ms" if bidask_age_ms is not None else "—"
        if self._kpi_last_state != KPI_STATE_WAITING_BOOK or self._kpi_last_reason != "no_bidask_yet":
            self._append_log(
                (
                    f"[KPI] state=WAITING_BOOK spread=— vol=— "
                    f"src={book_source} age={bidask_age_text} reason=no_bidask_yet"
                ),
                kind="INFO",
            )
            self._kpi_last_state = KPI_STATE_WAITING_BOOK
            self._kpi_last_reason = "no_bidask_yet"
            self._kpi_last_state_ts = now_ts
        self._session.market.source = book_source
        self._session.market.age_ms = bidask_age_ms
        self._kpi_invalid_reason = None
        self._kpi_vol_state = KPI_STATE_UNKNOWN
        self._kpi_state = KPI_STATE_WAITING_BOOK
        self._kpi_valid = False
        self._update_arb_signals()
        self._maybe_emit_minute_summary(now_ts)

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
        if self._should_skip_http_book_ticker():
            self._append_log_throttled(
                "[FEED] ws fresh -> skip http",
                kind="INFO",
                key="skip_http_book",
                cooldown_sec=2.0,
            )
            return
        self._kpi_last_book_request_ts = now_ts
        self._queue_net_request(
            "book_ticker",
            {"symbol": self._symbol},
            on_success=lambda payload, latency_ms: self._safe_call(
                lambda: self._handle_book_ticker_success(payload, latency_ms),
                label="signal:book_ticker_success",
            ),
            on_error=self._handle_book_ticker_error,
        )

    def _handle_book_ticker_success(self, payload: object, _latency_ms: int) -> None:
        assert isinstance(self, NcPilotTabWidget)
        try:
            if self._closing:
                return
            if isinstance(payload, dict) and payload:
                bid, ask = self._update_http_book_snapshot(payload)
                if bid is not None and ask is not None:
                    self._set_http_cache("book_ticker", payload)
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _handle_book_ticker_success")
            self._notify_crash("_handle_book_ticker_success")
            self._pilot_force_hold(reason="exception:_handle_book_ticker_success")
            return

    def _handle_book_ticker_error(self, error: object) -> None:
        message = self._format_account_error(error)
        self._append_log_throttled(
            f"[BIDASK] book ticker error={message}",
            kind="WARN",
            key="book_ticker_error",
            cooldown_sec=5.0,
        )

    def _fetch_bidask_http_book(self) -> None:
        now_ts = monotonic()
        if self._bidask_last_request_ts and (
            (now_ts - self._bidask_last_request_ts) * 1000 < BIDASK_POLL_MS
        ):
            return
        if self._should_skip_http_book_ticker():
            self._append_log_throttled(
                "[FEED] ws fresh -> skip http",
                kind="INFO",
                key="skip_http_book",
                cooldown_sec=2.0,
            )
            return
        self._bidask_last_request_ts = now_ts
        self._queue_net_request(
            "book_ticker",
            {"symbol": self._symbol},
            on_success=lambda payload, latency_ms: self._safe_call(
                lambda: self._handle_success(payload, latency_ms),
                label="signal:book_ticker_poll",
            ),
            on_error=self._handle_book_ticker_error,
        )

    def _handle_success(self, payload: object, _latency_ms: int) -> None:
        assert isinstance(self, NcPilotTabWidget)
        try:
            if self._closing:
                return
            if not isinstance(payload, dict) or not payload:
                return
            bid, ask = self._update_http_book_snapshot(payload)
            if bid is None or ask is None:
                return
            self._set_http_cache("book_ticker", payload)
            if not self._bidask_ready_logged:
                self._append_log(
                    f"[BIDASK] ready src=HTTP_BOOK bid={bid:.8f} ask={ask:.8f}",
                    kind="INFO",
                )
                self._bidask_ready_logged = True
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _handle_success")
            self._notify_crash("_handle_success")
            self._pilot_force_hold(reason="exception:_handle_success")
            return

    def update_market_kpis(self) -> None:
        if self._closing:
            return
        try:
            if not hasattr(self, "_market_price"):
                return
            now_ts = monotonic()
            if (
                self._bidask_last_request_ts is None
                or (now_ts - self._bidask_last_request_ts) * 1000 >= BIDASK_POLL_MS
            ):
                self._fetch_bidask_http_book()

            best_bid = None
            best_ask = None
            bidask_age_ms = None
            bidask_state = "missing"
            book_source = self.trade_source or "UNKNOWN"
            snapshot = self.book_snapshot or {}
            snapshot_ts = snapshot.get("ts") if snapshot else None
            bid = self._coerce_float(snapshot.get("bid")) if snapshot else None
            ask = self._coerce_float(snapshot.get("ask")) if snapshot else None
            if isinstance(snapshot_ts, (int, float)):
                age_ms = int((now_ts - snapshot_ts) * 1000)
                bidask_age_ms = age_ms
                if age_ms <= BIDASK_STALE_MS:
                    best_bid, best_ask = bid, ask
                    bidask_state = "stale_cache" if age_ms > BIDASK_FAIL_SOFT_MS else "fresh"
                else:
                    bidask_state = "stale"
            self._bidask_state = bidask_state
            if not self._validate_bid_ask(best_bid, best_ask, source="HTTP_BOOK"):
                self._update_kpi_waiting_book(now_ts, book_source, bidask_age_ms)
                return

            price = (best_bid + best_ask) / 2
            self._last_price = price
            self._last_price_ts = now_ts
            self._session.market.bid = best_bid
            self._session.market.ask = best_ask
            self._session.market.last_price = price
            self._session.market.source = book_source
            self._session.market.age_ms = bidask_age_ms
            self._session.runtime.last_ui_update_ts = now_ts
            self._record_price(price)
            self._last_price_label.setText(tr("last_price", price=f"{price:.8f}"))
            self._market_price.setText(f"{price:.8f}")
            self._set_market_label_state(self._market_price, active=True)
            spread_pct = self._compute_spread_pct_from_book(best_bid, best_ask)
            spread_text = self._format_spread_display(spread_pct)
            self._market_spread.setText(spread_text)
            self._set_market_label_state(self._market_spread, active=spread_text != "—")

            sample_ts = time_fn()
            mid = price
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

            self._market_fee.setText(self._fee_display_text())
            self._set_market_label_state(self._market_fee, active=True)

            if bidask_age_ms is not None:
                source_age_text = f"{book_source} | {bidask_age_ms}ms"
            else:
                source_age_text = f"{book_source} | —"
            self._market_source.setText(source_age_text)
            self._set_market_label_state(self._market_source, active=source_age_text != "—")

            feed_ok = bool(bidask_age_ms is not None and bidask_age_ms < BIDASK_FAIL_SOFT_MS)
            self._feed_ok = feed_ok
            feed_age_text = f"{bidask_age_ms}ms" if bidask_age_ms is not None else "—"
            if (
                self._feed_last_ok is None
                or self._feed_last_ok != feed_ok
                or self._feed_last_source != book_source
            ):
                self._append_log(
                    f"[FEED] src={book_source} age={feed_age_text} ok={str(feed_ok).lower()}",
                    kind="INFO",
                )
                self._feed_last_ok = feed_ok
                self._feed_last_source = book_source

            spread_bps = spread_pct * 100 if spread_pct is not None else None
            vol_bps = volatility_pct * 100 if volatility_pct is not None else None
            maker_pct, taker_pct, _fee_defaulted = self._profit_guard_fee_inputs()
            fill_mode = self._runtime_profit_inputs().get("expected_fill_mode") or "MAKER"
            fee_pct = taker_pct if fill_mode == "TAKER" else maker_pct
            fees_roundtrip_bps = fee_pct * 200
            slippage_bps = self._profit_guard_slippage_pct() * 100
            pad_bps = self._profit_guard_pad_pct() * 100
            edge_raw_bps = None
            expected_profit_bps = None
            risk_buffer_bps = None
            if spread_bps is not None:
                risk_buffer_bps = (vol_bps or 0.0) * MARKET_HEALTH_RISK_BUFFER_MULT
                edge_result = compute_expected_edge_bps(
                    spread_bps=spread_bps,
                    fees_roundtrip_bps=fees_roundtrip_bps,
                    slippage_bps=slippage_bps,
                    pad_bps=pad_bps,
                    risk_buffer_bps=risk_buffer_bps,
                )
                edge_raw_bps = edge_result.edge_raw_bps
                expected_profit_bps = edge_result.expected_profit_bps
            self._market_health = MarketHealth(
                has_bidask=best_bid is not None and best_ask is not None,
                spread_bps=spread_bps,
                vol_bps=vol_bps,
                age_ms=bidask_age_ms,
                src=book_source,
                edge_raw_bps=edge_raw_bps,
                expected_profit_bps=expected_profit_bps,
            )
            if expected_profit_bps is not None:
                edge_reason = "thin_edge" if expected_profit_bps <= 0 else "ok"
                edge_raw_round = round(edge_raw_bps or 0.0, 2)
                expected_round = round(expected_profit_bps, 2)
                risk_round = round(risk_buffer_bps or 0.0, 2)
                edge_state = (edge_raw_round, expected_round, risk_round, edge_reason)
                should_log = False
                if self._market_health_last_values != edge_state:
                    should_log = True
                elif (
                    self._market_health_last_log_ts is None
                    or now_ts - self._market_health_last_log_ts >= LOG_DEDUP_HEARTBEAT_SEC
                ):
                    should_log = True
                if should_log:
                    self._append_log(
                        (
                            "[KPI] edge_raw_bps="
                            f"{edge_raw_round:.2f} expected_profit_bps={expected_round:.2f} "
                            f"risk_buffer_bps={risk_round:.2f} reason={edge_reason}"
                        ),
                        kind="INFO",
                    )
                    self._market_health_last_log_ts = now_ts
                self._market_health_last_reason = edge_reason
                self._market_health_last_values = edge_state

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
            if bidask_missing:
                if not self._kpi_missing_bidask_active:
                    self._append_log(
                        "[KPI] state=UNKNOWN reason=no_bidask_yet",
                        kind="INFO",
                    )
                    self._kpi_missing_bidask_active = True
            else:
                self._kpi_missing_bidask_active = False
            if spread_zero_ok:
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

            if price is not None:
                should_log = False
                if not self._kpi_has_data or self._kpi_last_source != book_source:
                    should_log = True
                if should_log:
                    age_text = f"{bidask_age_ms}ms" if bidask_age_ms is not None else "—"
                    self._append_log(
                        f"[NC_PILOT] kpi updated src={book_source} age={age_text} price={price:.8f}",
                        kind="INFO",
                    )
                self._kpi_has_data = True
                self._kpi_last_source = book_source
            else:
                self._kpi_has_data = False
                self._kpi_last_source = None
                self._kpi_zero_vol_ticks = 0
                self._kpi_zero_vol_start_ts = None
                self._kpi_missing_bidask_since_ts = None
            ws_age_ms = None
            if self._ws_last_tick_ts is not None:
                ws_age_ms = int((now_ts - self._ws_last_tick_ts) * 1000)
            age_text = f"{ws_age_ms}ms" if ws_age_ms is not None else "—"
            self._age_label.setText(tr("age", age=age_text))
            latency_text = "—"
            if self._last_price_update and self._last_price_update.latency_ms is not None:
                latency_text = f"{self._last_price_update.latency_ms}ms"
            self._latency_label.setText(tr("latency", latency=latency_text))
            ws_clock_ok = bool(ws_age_ms is not None and ws_age_ms < WS_STALE_MS)
            clock_status = "✓" if ws_clock_ok else "—"
            if self._ws_status in {WS_DEGRADED, WS_LOST}:
                self._feed_indicator.setToolTip(tr("ws_degraded_tooltip"))
            else:
                self._feed_indicator.setToolTip("")
            self._feed_indicator.setText(
                f"HTTP ✓ | WS {self._ws_indicator_symbol()} | CLOCK {clock_status}"
            )
            self._handle_wait_edge_tick()
            self._grid_engine.on_price(price)
            self._update_runtime_balances()
            self._refresh_unrealized_pnl()
            self._update_arb_signals()
            self._session.counters.kpi_updates += 1
            self._maybe_emit_minute_summary(now_ts)
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in update_market_kpis")
            self._notify_crash("update_market_kpis")
            self._pilot_force_hold(reason="exception:update_market_kpis")
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
        if self._closing:
            return
        try:
            overall_status = (
                self._price_feed_manager.get_ws_overall_status()
                if hasattr(self, "_price_feed_manager")
                else status
            )
            if overall_status == WS_CONNECTED:
                self._ws_status = WS_CONNECTED
            elif overall_status == WS_DEGRADED:
                self._ws_status = WS_DEGRADED
            elif overall_status == WS_LOST:
                self._ws_status = WS_LOST
            else:
                self._ws_status = ""
            self._session.runtime.last_ui_update_ts = monotonic()
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _apply_status_update")
            self._notify_crash("_apply_status_update")
            self._pilot_force_hold(reason="exception:_apply_status_update")
            return

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
        self._profit_guard_override_pending = True
        if self._state == "WAIT_EDGE":
            self._append_log("[EDGE] ok -> placing orders", kind="INFO")
            self._handle_start()
            return
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
        if (
            self._session.runtime.start_in_progress
            or self._stop_in_progress
            or self._current_action in {"start", "stop"}
        ):
            self._suppress_dry_run_event = True
            self._dry_run_toggle.setChecked(not checked)
            self._suppress_dry_run_event = False
            self._append_log("[TRADE] toggle rejected: reason=stop/start flow", kind="WARN")
            return
        if not checked and not self._can_trade():
            self._suppress_dry_run_event = True
            self._dry_run_toggle.setChecked(True)
            self._suppress_dry_run_event = False
            self._append_log("LIVE unavailable: trade gate not ready (running DRY-RUN).", kind="WARN")
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
        if not checked and self._trade_gate != TradeGate.TRADE_OK:
            reason = self._trade_gate_reason()
            self._append_log(f"LIVE unavailable: {reason} (running DRY-RUN).", kind="WARN")
            self._suppress_dry_run_event = True
            self._dry_run_toggle.setChecked(True)
            self._suppress_dry_run_event = False
            self._dry_run_enabled = True
            self._live_mode_confirmed = False
            self._grid_engine.set_mode("DRY_RUN")
            self._apply_trade_gate()
            return
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
        if self._stop_in_progress or self._session.runtime.stop_inflight or self._engine_state == "STOPPING":
            reason = "STOPPING" if self._engine_state == "STOPPING" or self._stop_in_progress else "INFIGHT"
            self._append_log(f"[START] blocked reason={reason}", kind="WARN")
            return
        self._start_price_feed_if_needed()
        self._append_log(
            (
                f"[START] mode={'DRY-RUN' if self._dry_run_toggle.isChecked() else 'LIVE'} "
                f"trade_gate={self._trade_gate.value} state={self._trade_gate_state.value}"
            ),
            kind="INFO",
        )
        if self._account_client:
            if not self._balances_timer.isActive():
                self._balances_timer.start(self._balances_poll_ms)
            if not self._orders_timer.isActive():
                self._orders_timer.start()
            if not self._fills_timer.isActive():
                self._fills_timer.start()
            self._update_orders_timer_interval()
        if self._state in {"RUNNING", "PLACING_GRID", "PAUSED", "WAITING_FILLS"}:
            self._append_log("Start ignored: engine already running.", kind="WARN")
            return
        if self._start_locked_until_change:
            if not self._start_locked_logged:
                self._append_log("[START] blocked: fix required (TP)", kind="WARN")
                self._start_locked_logged = True
            return
        if self._session.runtime.start_in_progress:
            if not self._start_in_progress_logged:
                self._append_log("[START] ignored: in_progress", kind="WARN")
                self._start_in_progress_logged = True
            return
        ui_settings = self.config_from_ui()
        self._append_log(
            (
                "[NC_PILOT][UI] start_clicked "
                f"symbol={self._symbol} "
                "cfg={"
                f"budget={ui_settings.budget:.2f},"
                f"step={ui_settings.grid_step_pct:.4f},"
                f"range_dn={ui_settings.range_low_pct:.4f},"
                f"range_up={ui_settings.range_high_pct:.4f},"
                f"tp={ui_settings.take_profit_pct:.4f},"
                f"max_active={ui_settings.max_active_orders}"
                "}"
            ),
            kind="INFO",
        )
        self._apply_settings_from_ui()
        snapshot = self._collect_strategy_snapshot()
        self._append_log(
            (
                "[START] config "
                f"budget={self._settings_state.budget:.2f} "
                f"step_pct={self._settings_state.grid_step_pct:.4f} "
                f"range_low_pct={self._settings_state.range_low_pct:.4f} "
                f"range_high_pct={self._settings_state.range_high_pct:.4f} "
                f"tp_pct={self._settings_state.take_profit_pct:.4f} "
                f"max_active_orders={self._settings_state.max_active_orders}"
            ),
            kind="INFO",
        )
        snapshot_hash = self._hash_snapshot(snapshot)
        if snapshot_hash == self._last_preflight_hash and self._last_preflight_blocked:
            return
        self._session.runtime.start_in_progress = True
        self._start_in_progress_logged = False
        self._current_action = "start"
        self._start_run_id += 1
        run_id = self._start_run_id
        cancel_event = threading.Event()
        self._start_cancel_event = cancel_event
        self._last_preflight_hash = snapshot_hash
        self._last_preflight_blocked = False
        self._start_button.setEnabled(False)
        self._set_strategy_controls_enabled(False)
        started = False
        try:
            def _start_cancelled() -> bool:
                if cancel_event.is_set() or run_id != self._start_run_id:
                    self._append_log("Start cancelled by user.", kind="INFO")
                    self._change_state("IDLE")
                    return True
                return False

            balances_ok = self._force_refresh_balances_and_wait(timeout_ms=2_000)
            self._force_refresh_open_orders_and_wait(timeout_ms=2_000)
            if _start_cancelled():
                return
            if not self._balances_ready_for_start():
                suffix = " (force_refresh_failed)" if not balances_ok else ""
                self._append_log(
                    f"[START] blocked: balances_not_ready{suffix}",
                    kind="WARN",
                )
                self._mark_preflight_blocked()
                return
            if not self._open_orders_loaded:
                self._append_log("[START] blocked: open_orders_not_ready", kind="WARN")
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
            if self._legacy_policy == LegacyPolicy.CANCEL and self._legacy_order_ids:
                if not self._account_client:
                    self._append_log(
                        "[START] blocked: legacy orders present but no account client.",
                        kind="WARN",
                    )
                    self._mark_preflight_blocked()
                    return
                self._append_log(
                    f"[START] canceling legacy orders n={len(self._legacy_order_ids)}",
                    kind="INFO",
                )
                if not self._cancel_legacy_orders_and_wait():
                    self._append_log(
                        "[START] blocked: legacy orders cancellation incomplete",
                        kind="WARN",
                    )
                    self._mark_preflight_blocked()
                    return
                self._legacy_order_ids.clear()
            elif self._legacy_policy == LegacyPolicy.IGNORE and self._legacy_order_ids:
                self._append_log(
                    f"[START] legacy orders ignored n={len(self._legacy_order_ids)}",
                    kind="INFO",
                )
            self._append_log(f"Start pressed (dry-run={dry_run}).", kind="ORDERS")
            self._append_log(
                f"account.canTrade={str(self._account_can_trade).lower()} permissions={self._account_permissions}",
                kind="INFO",
            )
            if _start_cancelled():
                return
            if not dry_run and self._trade_gate != TradeGate.TRADE_OK:
                reason = self._trade_gate_reason()
                self._append_log(f"Start blocked: TRADE DISABLED (reason={reason}).", kind="WARN")
                self._mark_preflight_blocked()
                self._change_state("IDLE")
                return
            self._grid_engine.set_mode("DRY_RUN" if dry_run else "LIVE")
            if not dry_run and (not self._rules_loaded or (not self._fees_last_fetch_ts and not self._fee_unverified)):
                self._append_log("Start blocked: rules/fees not loaded.", kind="WARN")
                self._refresh_exchange_rules(force=True)
                self._refresh_trade_fees(force=True)
                self._change_state("IDLE")
                self._mark_preflight_blocked()
                return
            try:
                if _start_cancelled():
                    return
                anchor_price = self.get_anchor_price(self._symbol)
                if anchor_price is None:
                    raise ValueError("No price available.")
                balance_snapshot = self._balance_snapshot()
                last_good_snapshot = self._last_good_balance_snapshot()
                if (
                    balance_snapshot.get("quote_free", Decimal("0")) <= 0
                    and last_good_snapshot.get("quote_free", Decimal("0")) > 0
                ):
                    self._append_log(
                        "[START] blocked: balances_invalid_using_last_good",
                        kind="WARN",
                    )
                    balance_snapshot = last_good_snapshot
                settings = self._resolve_start_settings(snapshot)
                self._apply_grid_clamps(settings, anchor_price)
                if not self._fees_last_fetch_ts:
                    self._refresh_trade_fees(force=True)
                guard_mode = self._current_profit_guard_mode()
                guard_reason = self._current_profit_guard_reason()
                if guard_reason == "fees_override":
                    self._append_log(
                        f"[GUARD] mode={guard_mode.value} reason=fees_override",
                        kind="INFO",
                    )
                else:
                    reason_suffix = f" reason={guard_reason}" if guard_reason else ""
                    self._append_log(
                        (
                            f"[GUARD] mode={guard_mode.value} "
                            f"fee_unverified={str(self._fee_unverified).lower()}{reason_suffix}"
                        ),
                        kind="INFO",
                    )
                guard_decision = self._apply_profit_guard(
                    settings,
                    anchor_price,
                    action_label="start",
                    update_ui=True,
                )
                override_guard = self._profit_guard_override_pending
                if guard_decision == "HOLD" and not override_guard:
                    self._append_log("[START] blocked -> WAIT_EDGE", kind="WARN")
                    self._wait_edge_last_log_ts = None
                    self._change_state("WAIT_EDGE")
                    return
                if guard_decision == "HOLD" and override_guard:
                    self._append_log("[START] guard override: profit guard ignored", kind="INFO")
                if override_guard:
                    self._profit_guard_override_pending = False
                if not self._is_micro_profile():
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
                if self._is_micro_profile():
                    planned, guard_hold, guard_reason = self._build_spread_capture_plan(
                        settings,
                        action_label="start",
                    )
                    if guard_hold:
                        self._append_log(
                            f"[START] blocked: micro guard reason={guard_reason}",
                            kind="WARN",
                        )
                        self._change_state("WAIT_EDGE")
                        return
                else:
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
                if self._pilot_accumulate_base_active(base_free):
                    self._bootstrap_mode = True
                    planned = [order for order in planned if order.side == "BUY"]
                    self._append_log(
                        f"[PILOT] mode=ACCUMULATE_BASE base_free={self._format_balance_decimal(base_free)}",
                        kind="INFO",
                    )
                else:
                    planned = self._limit_sell_plan_by_balance(planned, base_free)
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
                self._append_log("Start cancelled (confirmation declined).", kind="INFO")
                self._change_state("IDLE")
                return
            if _start_cancelled():
                return
            started = True
            self._last_preflight_blocked = False
            self._bot_session_id = uuid4().hex[:8]
            self._bot_order_ids.clear()
            self._bot_client_ids.clear()
            self._bot_order_keys.clear()
            self._fill_keys.clear()
            self._fill_accumulator = FillAccumulator()
            self._fill_exec_cumulative.totals.clear()
            self._active_action_keys.clear()
            self._active_order_keys.clear()
            self._recent_order_keys.clear()
            self._order_id_to_registry_key.clear()
            self._order_id_to_level_index.clear()
            self._set_open_orders_map({})
            self._fills = []
            self._base_lots.clear()
            self._trade_id_deduper.clear()
            self._exec_key_deduper.clear()
            self._exec_dup_log_state.clear()
            self._fill_exec_cumulative.totals.clear()
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
            if self._start_cancel_event is cancel_event:
                self._start_cancel_event = None
            if not started:
                self._session.runtime.start_in_progress = False
                self._start_in_progress_logged = False
                self._current_action = None
                if self._state == "IDLE":
                    self._start_button.setEnabled(True)

    def _handle_pause(self) -> None:
        self._append_log("Pause pressed.", kind="ORDERS")
        self._grid_engine.pause()
        self._change_state("PAUSED")

    def _schedule_cancel_reconcile_followups(self) -> None:
        if not self._account_client:
            return
        self._cancel_reconcile_pending = 2

        def _run_followup() -> None:
            if self._closing:
                return
            if self._cancel_reconcile_pending <= 0:
                return
            self._refresh_orders(force=True, reason="after_cancel_backoff")

        QTimer.singleShot(
            300,
            self,
            lambda: self._safe_call(_run_followup, label="timer:cancel_followup"),
        )
        QTimer.singleShot(
            700,
            self,
            lambda: self._safe_call(_run_followup, label="timer:cancel_followup"),
        )

    def _cancel_all_and_reconcile(
        self,
        *,
        reason: str,
        finalize_stop: bool = False,
        order_ids: list[str] | None = None,
    ) -> bool:
        allow_inflight_override = reason in {"stop", "cancel_all", "reconcile"}
        if self._cancel_inflight and not allow_inflight_override:
            return False
        if self._cancel_inflight and allow_inflight_override:
            self._append_log(
                f"[ORDERS][CANCEL] inflight override reason={reason}",
                kind="WARN",
            )
        if not self._account_client:
            self._append_log("[ORDERS][CANCEL] skipped: no account client", kind="WARN")
            if finalize_stop:
                self._stop_done_with_errors = True
                self._finalize_stop(keep_open_orders=True)
            return False
        self._cancel_inflight = True
        self._cancel_reconcile_pending = 0
        self._update_orders_timer_interval()
        self._append_log(
            f"[ORDERS][CANCEL] start reason={reason}",
            kind="INFO",
        )

        def _cancel() -> CancelResult:
            start_ts = monotonic()
            cancelled_ids: set[str] = set()
            remaining_ids: list[str] = []
            open_orders: list[dict[str, Any]] = []
            errors: list[str] = []
            log_entries: list[str] = []

            def _log(message: str) -> None:
                log_entries.append(message)

            def _fetch_open_orders() -> list[dict[str, Any]]:
                try:
                    return self._fetch_open_orders_sync(self._symbol)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"[CANCEL] list open_orders failed: {exc}")
                    return []

            open_orders = _fetch_open_orders()
            remaining_ids = [
                str(order.get("orderId", ""))
                for order in open_orders
                if str(order.get("orderId", ""))
            ]
            _log(f"[CANCEL] open_orders fetched n={len(remaining_ids)} reason={reason}")
            if not remaining_ids:
                _log("[CANCEL] done canceled=0 remaining=0 errors=0")
                return CancelResult(
                    cancelled_ids=[],
                    remaining_ids=[],
                    errors=errors,
                    open_orders=open_orders,
                    passes=0,
                    log_entries=log_entries,
                )
            for order_id in remaining_ids:
                _log(f"[CANCEL] cancel send order_id={order_id}")
                try:
                    self._cancel_order_sync(symbol=self._symbol, order_id=order_id)
                    cancelled_ids.add(order_id)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"[CANCEL] cancel failed order_id={order_id} error={exc}")
            passes = 0
            while monotonic() - start_ts < 8.0:
                remaining = 8.0 - (monotonic() - start_ts)
                if remaining <= 0:
                    break
                sleep(min(0.25, remaining))
                passes += 1
                open_orders = _fetch_open_orders()
                remaining_ids = [
                    str(order.get("orderId", ""))
                    for order in open_orders
                    if str(order.get("orderId", ""))
                ]
                _log(f"[CANCEL] poll pass={passes} remaining={len(remaining_ids)}")
                if not remaining_ids:
                    break
            if remaining_ids:
                _log(f"[CANCEL] timeout remaining_ids={remaining_ids}")
            _log(
                f"[CANCEL] done canceled={len(cancelled_ids)} "
                f"remaining={len(remaining_ids)} errors={len(errors)}"
            )
            return CancelResult(
                cancelled_ids=sorted(cancelled_ids),
                remaining_ids=remaining_ids,
                errors=errors,
                open_orders=open_orders,
                passes=passes,
                log_entries=log_entries,
            )

        def _handle_cancel_result(result: object, latency_ms: int) -> None:
            self._orders_in_flight = False
            self._snapshot_refresh_inflight = False
            if not isinstance(result, CancelResult):
                self._handle_cancel_error("Unexpected cancel response")
                self._record_stop_error("Unexpected cancel response")
                self._cancel_inflight = False
                if finalize_stop:
                    self._stop_done_with_errors = True
                    self._finalize_stop(keep_open_orders=True)
                return
            for message in result.log_entries:
                self._append_log(message, kind="INFO")
            for message in result.errors:
                self._append_log(str(message), kind="WARN")
            if result.errors:
                self._record_stop_error(str(result.errors[-1]))
            if result.open_orders:
                self._apply_open_orders_snapshot(result.open_orders, reason="after_cancel")
            after_count = len(self._open_orders)
            canceled = len(result.cancelled_ids)
            remaining = len(result.remaining_ids)
            if finalize_stop:
                cancel_status = "ok" if remaining == 0 and not result.errors else "partial"
                self._append_log(f"[STOP] cancel orders: {cancel_status}", kind="INFO")
            self._append_log(
                (
                    f"[ORDERS][CANCEL] done reason={reason} "
                    f"canceled={canceled} remaining={remaining} open_orders={after_count}"
                ),
                kind="INFO",
            )
            if remaining != 0:
                self._schedule_cancel_reconcile_followups()
            self._cancel_inflight = False
            self._update_orders_timer_interval()
            self._set_cancel_buttons_enabled(True)
            self._request_orders_refresh("cancel_all_finalize")
            self._append_log("orders refreshed after cancel", kind="INFO")
            if finalize_stop:
                if result.errors or remaining:
                    self._stop_done_with_errors = True
                if remaining:
                    self._stop_finalize_waiting = True
                    self._session.runtime.stop_inflight = True
                    return
                self._finalize_stop(keep_open_orders=False)

        def _handle_cancel_failure(message: str) -> None:
            self._orders_in_flight = False
            self._snapshot_refresh_inflight = False
            self._cancel_inflight = False
            self._cancel_reconcile_pending = 0
            self._update_orders_timer_interval()
            self._handle_cancel_error(message)
            self._record_stop_error(message)
            if finalize_stop:
                self._append_log("[STOP] cancel orders: partial", kind="WARN")
            self._set_cancel_buttons_enabled(True)
            self._request_orders_refresh("cancel_all_error")
            self._append_log("orders refreshed after cancel", kind="INFO")
            if finalize_stop:
                self._stop_done_with_errors = True
                self._finalize_stop(keep_open_orders=True)

        try:
            result = _cancel()
            _handle_cancel_result(result, 0)
        except Exception as exc:  # noqa: BLE001
            _handle_cancel_failure(str(exc))
        return True

    def _start_stop_watchdog(self) -> None:
        self._stop_watchdog_token += 1
        token = self._stop_watchdog_token
        self._stop_watchdog_started_ts = monotonic()

        def _on_timeout() -> None:
            if token != self._stop_watchdog_token:
                return
            if self._closing:
                return
            if not self._stop_in_progress or self._state != "STOPPING":
                return
            pending = len(self._open_orders)
            last_error = self._stop_last_error or "—"
            self._append_log(
                f"[STOP] forced finalize after timeout; pending={pending}; last_error={last_error}",
                kind="WARN",
            )
            self._stop_done_with_errors = True
            self._session.runtime.stop_inflight = False
            self._cancel_inflight = False
            self._orders_in_flight = False
            self._snapshot_refresh_inflight = False
            self._finalize_stop(keep_open_orders=bool(self._open_orders))

        QTimer.singleShot(
            STOP_DEADLINE_MS,
            self,
            lambda: self._safe_call(_on_timeout, label="timer:stop_watchdog"),
        )

    def _cancel_stop_watchdog(self) -> None:
        self._stop_watchdog_token += 1
        self._stop_watchdog_started_ts = None

    def _check_inflight_watchdog(self) -> None:
        if self._closing:
            self._inflight_watchdog_started_ts = None
            return
        if self._stop_in_progress:
            self._inflight_watchdog_started_ts = None
            return
        inflight_active = self._orders_in_flight or self._snapshot_refresh_inflight or self._cancel_inflight
        if not inflight_active:
            self._inflight_watchdog_started_ts = None
            return
        now = monotonic()
        if self._inflight_watchdog_started_ts is None:
            self._inflight_watchdog_started_ts = now
            return
        if now - self._inflight_watchdog_started_ts < 5.0:
            return
        last_log = self._inflight_watchdog_last_log_ts
        if last_log is not None and now - last_log < 15.0:
            return
        self._inflight_watchdog_last_log_ts = now
        self._orders_in_flight = False
        self._snapshot_refresh_inflight = False
        self._cancel_inflight = False
        self._append_log("[WATCHDOG] inflight reset >5s; forcing refresh + HOLD", kind="WARN")
        if self.pilot_enabled:
            self._pilot_hold_reason = "inflight_watchdog"
            self._set_pilot_state(PilotState.HOLD, reason="inflight_watchdog")
        self._schedule_force_open_orders_refresh("inflight_watchdog")

    def _handle_stop(self, *, reason: str = "user") -> None:
        self._append_log("[STOP] requested", kind="INFO")
        if self._stop_in_progress:
            self._append_log("[STOP] ignored (already stopping)", kind="INFO")
            return
        self._stop_in_progress = True
        self._session.runtime.stop_inflight = True
        self._stop_requested = True
        self._stop_last_error = None
        self._stop_done_with_errors = False
        self._stop_finalize_waiting = False
        if self._start_cancel_event:
            self._start_cancel_event.set()
        self._start_run_id += 1
        self._start_stop_watchdog()
        self._begin_shutdown(reason=reason)
        self._append_log("Stop pressed.", kind="ORDERS")
        cancel_all = bool(self._cancel_all_on_stop_toggle.isChecked())
        self._append_log(
            (
                "[STOP] begin "
                f"cancel_all={str(cancel_all).lower()} "
                f"inflight={str(self._session.runtime.stop_inflight).lower()}"
            ),
            kind="INFO",
        )
        cancel_scheduled = False
        keep_open_orders_on_finalize = True
        cancel_required = False
        try:
            self._grid_engine.stop(cancel_all=cancel_all)
            self._change_state("STOPPING")
            self._orders_in_flight = True
            self._session.runtime.start_in_progress = False
            self._start_in_progress_logged = False
            self._start_button.setEnabled(False)
            self._bootstrap_mode = False
            self._bootstrap_sell_enabled = False
            self._sell_side_enabled = False
            self._active_tp_ids.clear()
            self._active_restore_ids.clear()
            self._pending_tp_ids.clear()
            self._pending_restore_ids.clear()
            if self._dry_run_toggle.isChecked():
                self._append_log("[STOP] cancel done", kind="INFO")
                self._append_log("[STOP] cancel orders: ok", kind="INFO")
                self._refresh_orders(reason="stop_dry_run", force=True)
                keep_open_orders_on_finalize = False
                return
            if not self._account_client:
                self._append_log("[STOP] cancel skipped: no account client.", kind="WARN")
                self._append_log("[STOP] cancel done", kind="INFO")
                self._append_log("[STOP] cancel orders: ok", kind="INFO")
                self._refresh_orders(reason="stop_no_client", force=True)
                return
            if not cancel_all:
                self._append_log("[STOP] cancel skipped: toggle disabled.", kind="INFO")
                self._append_log("[STOP] cancel done", kind="INFO")
                self._append_log("[STOP] cancel orders: ok", kind="INFO")
                self._refresh_orders(reason="stop_no_cancel", force=True)
                return
            cancel_required = True
            removed = self._mark_orders_cancelling(None, reason="stop")
            self._append_log(
                f"[ORDERS][CANCEL] ui_marked reason=stop rows={removed}",
                kind="INFO",
            )
            self._set_cancel_buttons_enabled(False)
            cancel_scheduled = self._cancel_all_and_reconcile(reason="stop", finalize_stop=True)
        finally:
            if not cancel_scheduled:
                self._stop_done_with_errors = cancel_required
                self._finalize_stop(keep_open_orders=keep_open_orders_on_finalize)

    def _finalize_stop(self, *, keep_open_orders: bool = False) -> None:
        self._cancel_stop_watchdog()
        self._bot_order_ids.clear()
        self._bot_client_ids.clear()
        self._bot_order_keys.clear()
        self._fill_keys.clear()
        self._fill_accumulator = FillAccumulator()
        self._trade_id_deduper.clear()
        self._exec_key_deduper.clear()
        self._exec_dup_log_state.clear()
        self._fill_exec_cumulative.totals.clear()
        self._active_action_keys.clear()
        self._owned_order_ids.clear()
        self._clear_local_order_registry()
        self._closed_order_ids.clear()
        self._closed_order_statuses.clear()
        self._order_id_to_level_index.clear()
        self._set_open_orders_map({})
        self._bot_session_id = None
        if not keep_open_orders:
            self._set_open_orders([])
            self._set_open_orders_all([])
            self._clear_order_info()
        self._reset_position_state()
        self._sell_side_enabled = False
        self._active_tp_ids.clear()
        self._active_restore_ids.clear()
        self._pending_tp_ids.clear()
        self._pending_restore_ids.clear()
        self._stop_in_progress = False
        self._session.runtime.stop_inflight = False
        self._stop_requested = False
        self._stop_finalize_waiting = False
        self._orders_in_flight = False
        self._snapshot_refresh_inflight = False
        should_resume = not self._close_pending
        if should_resume:
            self._closing = False
            self._shutdown_started = False
            self._resume_local_timers()
        if keep_open_orders:
            self._set_orders_snapshot(self._build_orders_snapshot(self._open_orders), reason="stop_finalize")
        else:
            self._set_orders_snapshot(self._build_orders_snapshot([]), reason="stop_finalize")
        remaining = len(self._open_orders)
        finalize_stop_state(
            remaining=remaining,
            done_with_errors=self._stop_done_with_errors,
            set_inflight=lambda value: setattr(self._session.runtime, "stop_inflight", value),
            set_state=self._change_state,
            set_start_enabled=self._start_button.setEnabled,
            log_fn=self._append_log,
            state="STOPPED",
        )
        self._stop_done_with_errors = False
        self._set_cancel_buttons_enabled(True)
        self._stop_last_error = None
        if self._close_pending:
            self._complete_close_if_pending()

    def _cancel_bot_orders_on_stop(self) -> None:
        if not self._account_client:
            self._finalize_stop()
            return
        symbol = self._symbol

        def _cancel() -> dict[str, Any]:
            events: list[tuple[str, str]] = []
            errors: list[str] = []
            open_orders_after: list[dict[str, Any]] = []
            verify_ok = False
            for attempt in range(1, 4):
                events.append(("INFO", f"[STOP] cancel_all attempt={attempt}"))
                ok = True
                err_text: str | None = None
                try:
                    self._cancel_open_orders_sync(symbol)
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    err_text = str(exc)
                    errors.append(self._format_cancel_exception(exc, "cancel_all"))
                self._sleep_ms(random.randint(300, 500))
                try:
                    open_orders_after = self._fetch_open_orders_sync(symbol)
                    verify_ok = True
                except Exception as exc:  # noqa: BLE001
                    verify_ok = False
                    open_orders_after = []
                    errors.append(f"[STOP] verify open_orders failed: {exc}")
                verify_count = len(open_orders_after) if verify_ok else -1
                events.append(("INFO", f"[STOP] verify exchange open_orders n={verify_count}"))
                if verify_ok and not open_orders_after:
                    break
                if attempt < 3:
                    events.append(("WARN", f"[STOP] retry cancel_all attempt={attempt + 1}"))
            return {
                "events": events,
                "open_orders_after": open_orders_after,
                "verify_ok": verify_ok,
                "errors": errors,
            }
        try:
            result = _cancel()
            self._handle_stop_cancel_result(result, 0)
        except Exception as exc:  # noqa: BLE001
            self._handle_stop_cancel_error(str(exc))

    def _handle_stop_cancel_result(self, result: object, latency_ms: int) -> None:
        self._orders_in_flight = False
        self._snapshot_refresh_inflight = False
        if not isinstance(result, dict):
            self._handle_cancel_error("Unexpected cancel response")
            self._append_log("[STOP] cancel done", kind="INFO")
            self._append_log("[STOP] force gui refresh", kind="INFO")
            self._force_refresh_open_orders_and_wait(timeout_ms=2_000)
            self._force_refresh_balances_and_wait(timeout_ms=2_000)
            self._append_log(f"[STOP] final sync open_orders={len(self._open_orders)}", kind="INFO")
            self._schedule_force_open_orders_refresh("stop")
            self._stop_done_with_errors = True
            self._finalize_stop()
            self._append_log("[STOP] gui orders cleared (n=0)", kind="INFO")
            self._append_log("[STOP] done", kind="INFO")
            return
        events = result.get("events", [])
        if isinstance(events, list):
            for entry in events:
                if isinstance(entry, tuple) and len(entry) == 2:
                    level, message = entry
                    kind = "WARN" if level == "WARN" else "INFO"
                    self._append_log(str(message), kind=kind)
                elif isinstance(entry, str):
                    self._append_log(entry, kind="INFO")
        verify_ok = bool(result.get("verify_ok", False))
        open_orders_after = result.get("open_orders_after", [])
        if verify_ok and isinstance(open_orders_after, list):
            self._append_log(
                f"[STOP] verify exchange open_orders n={len(open_orders_after)}",
                kind="INFO",
            )
            self._set_open_orders_all([item for item in open_orders_after if isinstance(item, dict)])
            self._set_open_orders(self._filter_bot_orders(self._open_orders_all))
            self._set_open_orders_map({
                str(order.get("orderId", "")): order
                for order in self._open_orders
                if str(order.get("orderId", ""))
            })
            self._clear_local_order_registry()
        elif not verify_ok:
            self._append_log("[STOP] verify open_orders failed; keeping local state until refresh", kind="WARN")
        errors = result.get("errors", [])
        if isinstance(errors, list):
            for message in errors:
                self._append_log(str(message), kind="WARN")
            if errors:
                self._stop_done_with_errors = True
        self._append_log("[STOP] cancel done", kind="INFO")
        self._append_log("[STOP] force gui refresh", kind="INFO")
        self._force_refresh_open_orders_and_wait(timeout_ms=2_000)
        self._force_refresh_balances_and_wait(timeout_ms=2_000)
        self._append_log(f"[STOP] final sync open_orders={len(self._open_orders)}", kind="INFO")
        self._schedule_force_open_orders_refresh("stop")
        keep_open_orders = (not verify_ok) or bool(self._open_orders)
        if keep_open_orders:
            self._stop_done_with_errors = True
        self._finalize_stop(keep_open_orders=keep_open_orders)
        if not keep_open_orders:
            self._append_log("[STOP] gui orders cleared (n=0)", kind="INFO")
        self._append_log("[STOP] done", kind="INFO")

    def _handle_stop_cancel_error(self, message: str) -> None:
        self._orders_in_flight = False
        self._snapshot_refresh_inflight = False
        self._handle_cancel_error(message)
        self._append_log("[STOP] cancel done", kind="INFO")
        self._append_log("[STOP] cancel orders: partial", kind="WARN")
        self._append_log("[STOP] force gui refresh", kind="INFO")
        self._force_refresh_open_orders_and_wait(timeout_ms=2_000)
        self._force_refresh_balances_and_wait(timeout_ms=2_000)
        self._append_log(f"[STOP] final sync open_orders={len(self._open_orders)}", kind="INFO")
        self._schedule_force_open_orders_refresh("stop")
        self._stop_done_with_errors = True
        self._finalize_stop(keep_open_orders=True)
        self._append_log("[STOP] done", kind="INFO")

    def _handle_cancel_selected(self) -> None:
        selected_rows = sorted({index.row() for index in self._orders_table.selectionModel().selectedRows()})
        if not selected_rows:
            self._append_log("Cancel selected: —", kind="ORDERS")
            return
        if self._dry_run_toggle.isChecked():
            for row in reversed(selected_rows):
                order_id = self._order_id_for_row(row)
                self._orders_table.removeRow(row)
                self._order_age_registry.pop(order_id, None)
                self._append_log(f"Cancel selected: {order_id}", kind="ORDERS")
            self._refresh_orders_metrics()
            return
        if not self._account_client:
            self._append_log("Cancel selected: no account client.", kind="WARN")
            return
        order_ids = [self._order_id_for_row(row) for row in selected_rows]
        removed = self._mark_orders_cancelling(order_ids, reason="selected")
        self._append_log(
            f"[ORDERS][CANCEL] ui_marked reason=cancel_selected rows={removed}",
            kind="INFO",
        )
        self._set_cancel_buttons_enabled(False)
        self._cancel_live_orders(order_ids, reason="cancel_selected")

    def _cancel_bot_orders(self) -> None:
        if not self._account_client:
            self._append_log("Cancel bot orders: no account client.", kind="WARN")
            return
        order_ids = [str(order.get("orderId", "")) for order in self._open_orders]
        order_ids = [order_id for order_id in order_ids if order_id and order_id != "—"]
        if not order_ids:
            self._append_log("Cancel bot orders: 0", kind="ORDERS")
            return
        self._cancel_live_orders(order_ids, reason="cancel_all")

    def _handle_cancel_all(self) -> None:
        count = self._orders_table.rowCount()
        if count == 0:
            self._append_log("Cancel all: 0", kind="ORDERS")
            return
        if self._dry_run_toggle.isChecked():
            self._append_log(f"Cancel all: {count}", kind="ORDERS")
            self._set_orders_snapshot(self._build_orders_snapshot([]), reason="cancel_all_dry_run")
            self._order_age_registry = {}
            return
        if not self._account_client:
            self._append_log("Cancel all: no account client.", kind="WARN")
            return
        removed = self._mark_orders_cancelling(None, reason="all")
        self._append_log(
            f"[ORDERS][CANCEL] ui_marked reason=cancel_all rows={removed}",
            kind="INFO",
        )
        self._set_cancel_buttons_enabled(False)
        self._cancel_all_and_reconcile(reason="cancel_all")

    def _handle_refresh(self) -> None:
        self._append_log("Manual refresh requested.", kind="INFO")
        self._refresh_balances(force=True)
        self._request_orders_refresh("manual_refresh")
        self._refresh_exchange_rules(force=True)
        self._refresh_trade_fees(force=True)

    def _change_state(self, new_state: str) -> None:
        self._state = new_state
        self._state_badge.setText(f"{tr('state')}: {self._state_display_text(self._state)}")
        self._engine_state = self._engine_state_from_status(new_state)
        self._session.runtime.engine_state = self._engine_state
        self._engine_state_label.setText(f"{tr('engine')}: {self._engine_state}")
        self._apply_engine_state_style(self._engine_state)
        self._update_orders_timer_interval()
        self._update_fills_timer()
        self.update_ui_lock_state()
        if new_state in {"RUNNING", "PAUSED", "WAITING_FILLS"} and self._current_action == "start":
            self._current_action = None
        if new_state in {"IDLE", "STOPPED"} and not self._session.runtime.start_in_progress:
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
        self._schedule_settings_save()

    def _apply_settings_from_ui(self) -> None:
        if not hasattr(self, "_budget_input"):
            return
        settings = GridSettingsState(
            budget=float(self._budget_input.value()),
            direction=str(self._direction_combo.currentData() or self._settings_state.direction),
            grid_count=int(self._grid_count_input.value()),
            grid_step_pct=float(self._grid_step_input.value()),
            grid_step_mode=str(self._grid_step_mode_combo.currentData() or self._settings_state.grid_step_mode),
            range_mode=str(self._range_mode_combo.currentData() or self._settings_state.range_mode),
            range_low_pct=float(self._range_low_input.value()),
            range_high_pct=float(self._range_high_input.value()),
            take_profit_pct=float(self._take_profit_input.value()),
            stop_loss_enabled=bool(self._stop_loss_toggle.isChecked()),
            stop_loss_pct=float(self._stop_loss_input.value()),
            max_active_orders=int(self._max_orders_input.value()),
            order_size_mode=str(self._order_size_combo.currentData() or self._settings_state.order_size_mode),
        )
        if settings == self._settings_state:
            return
        self._set_settings_state(settings)
        if settings.grid_step_mode == "MANUAL":
            self._manual_grid_step_pct = settings.grid_step_pct
        self._clear_tp_break_even_helper()
        self._on_strategy_changed()
        self._update_grid_preview()
        self._schedule_settings_save()
        self._append_log(
            (
                "[SETTINGS] applied "
                f"budget={settings.budget:.2f} step={settings.grid_step_pct:.4f} "
                f"range={settings.range_low_pct:.4f}-{settings.range_high_pct:.4f} "
                f"tp={settings.take_profit_pct:.4f} max_active={settings.max_active_orders}"
            ),
            kind="INFO",
        )

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
                klines, _latency_ms = self._net_call_sync(
                    "klines",
                    {"symbol": self._symbol, "interval": "1h", "limit": 120},
                    timeout_ms=5000,
                )
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
        if self._is_micro_profile():
            grid_step_pct = self._clamp(
                grid_step_pct,
                MICRO_STABLECOIN_STEP_PCT_MIN,
                MICRO_STABLECOIN_STEP_PCT_MAX,
            )
            range_pct = self._clamp(range_pct, 0.0, MICRO_STABLECOIN_RANGE_PCT_MAX)
            tp_pct = min(max(grid_step_pct * 2, tp_pct), MICRO_STABLECOIN_TP_PCT_MAX)
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
        best_bid, best_ask, _source = self._resolve_book_bid_ask()
        if best_bid is not None and best_ask is not None:
            self._append_log("PRICE: HTTP_BOOK age=live", kind="INFO")
            return (best_bid + best_ask) / 2
        cached_book = self._get_http_cached("book_ticker")
        if isinstance(cached_book, dict):
            bid, ask = self._update_http_book_snapshot(cached_book)
            if bid is not None and ask is not None:
                self._append_log("PRICE: HTTP_BOOK age=cached", kind="INFO")
                return (bid + ask) / 2
        try:
            book, _latency_ms = self._net_call_sync(
                "book_ticker",
                {"symbol": symbol},
                timeout_ms=3000,
            )
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"PRICE: HTTP_BOOK failed ({exc})", kind="WARN")
            return None
        if isinstance(book, dict):
            bid, ask = self._update_http_book_snapshot(book)
            if bid is not None and ask is not None:
                self._set_http_cache("book_ticker", book)
                self._append_log("PRICE: HTTP_BOOK age=0ms", kind="INFO")
                return (bid + ask) / 2
        return None

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

    def _pilot_get_cached(self, symbol: str, key: str) -> tuple[Any, int] | tuple[None, None]:
        cached = self._pilot_market_cache.get(symbol, key)
        if not cached:
            return None, None
        data, saved_at = cached
        ttl = self._pilot_market_cache_ttls.get(key)
        if ttl is None or not self._pilot_market_cache.is_fresh(saved_at, ttl):
            return None, None
        age_ms = int((time_fn() - saved_at) * 1000)
        return data, age_ms

    def _pilot_set_cache(self, symbol: str, key: str, payload: Any) -> None:
        self._pilot_market_cache.set(symbol, key, payload)

    def _pilot_fetch_book_ticker(self, symbol: str) -> tuple[dict[str, Any] | None, int | None]:
        cached, age_ms = self._pilot_get_cached(symbol, "book_ticker")
        if isinstance(cached, dict):
            return cached, age_ms
        try:
            payload, _latency_ms = self._net_call_sync(
                "book_ticker",
                {"symbol": symbol},
                timeout_ms=2000,
            )
        except Exception:
            return None, None
        if isinstance(payload, dict):
            self._pilot_set_cache(symbol, "book_ticker", payload)
            return payload, 0
        return None, None

    def _pilot_fetch_orderbook_depth(self, symbol: str) -> dict[str, Any] | None:
        cached, _age_ms = self._pilot_get_cached(symbol, "orderbook_depth_50")
        if isinstance(cached, dict):
            return cached
        try:
            payload, _latency_ms = self._net_call_sync(
                "orderbook_depth",
                {"symbol": symbol, "limit": 50},
                timeout_ms=2500,
            )
        except Exception:
            return None
        if isinstance(payload, dict):
            self._pilot_set_cache(symbol, "orderbook_depth_50", payload)
            return payload
        return None

    def _pilot_collect_market_data(self, symbols: set[str]) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            book, age_ms = self._pilot_fetch_book_ticker(symbol)
            bid = self._coerce_float(book.get("bidPrice")) if isinstance(book, dict) else None
            ask = self._coerce_float(book.get("askPrice")) if isinstance(book, dict) else None
            src = "HTTP" if isinstance(book, dict) else "NONE"
            ts_ms = None
            if age_ms is not None:
                ts_ms = int(time_fn() * 1000) - age_ms
            elif isinstance(book, dict):
                ts_ms = int(time_fn() * 1000)
            depth = self._pilot_fetch_orderbook_depth(symbol)
            results[symbol] = {
                "bid": bid,
                "ask": ask,
                "age_ms": age_ms,
                "ts_ms": ts_ms,
                "src": src,
                "depth": depth,
            }
        return results

    def _pilot_exchange_symbols(self) -> set[str] | None:
        if not hasattr(self, "_price_feed_manager") or not self._price_feed_manager:
            return None
        return self._price_feed_manager.get_exchange_symbols()

    def _pilot_alias_summary(self) -> str:
        runtime = self._session.pilot
        aliases = runtime.symbol_aliases
        return ", ".join(
            f"{symbol}→{aliases[symbol]}"
            for symbol in sorted(aliases)
            if aliases[symbol] != symbol
        )

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

    def _apply_grid_clamps(self, settings: GridSettingsState, anchor_price: float) -> None:
        self._apply_auto_clamps(settings, anchor_price)
        self._apply_micro_grid_clamps(settings, anchor_price)
        self._apply_micro_budget_limit(settings, anchor_price)

    def _apply_micro_grid_clamps(self, settings: GridSettingsState, anchor_price: float) -> None:
        if not self._is_micro_profile():
            return
        settings.grid_count = 2
        tick_step_pct = self._micro_step_pct_from_tick(anchor_price)
        if tick_step_pct is not None:
            settings.grid_step_mode = "TICK_BASED"
            settings.grid_step_pct = tick_step_pct
        settings.grid_step_pct = self._clamp(
            settings.grid_step_pct,
            MICRO_STABLECOIN_STEP_PCT_MIN,
            MICRO_STABLECOIN_STEP_PCT_MAX,
        )
        settings.range_low_pct = self._clamp(
            settings.range_low_pct,
            settings.grid_step_pct,
            MICRO_STABLECOIN_RANGE_PCT_MAX,
        )
        settings.range_high_pct = self._clamp(
            settings.range_high_pct,
            settings.grid_step_pct,
            MICRO_STABLECOIN_RANGE_PCT_MAX,
        )
        spread_ticks = self._micro_spread_capture_context().get("spread_ticks")
        tp_ticks = self._micro_tp_ticks(spread_ticks if isinstance(spread_ticks, int) else None)
        if tick_step_pct is not None:
            settings.take_profit_pct = max(tick_step_pct * tp_ticks, tick_step_pct)
        settings.take_profit_pct = min(settings.take_profit_pct, MICRO_STABLECOIN_TP_PCT_MAX)
        settings.order_size_mode = "Equal"
        settings.max_active_orders = min(settings.max_active_orders, settings.grid_count)

    def _apply_micro_budget_limit(self, settings: GridSettingsState, anchor_price: float) -> None:
        if not self._is_micro_profile():
            return
        if anchor_price <= 0:
            return
        quote_asset = self._quote_asset or ""
        if not quote_asset:
            return
        quote_total = self._asset_total(quote_asset)
        base_total = self._asset_total(self._base_asset) if self._base_asset else 0.0
        equity = quote_total + (base_total * anchor_price)
        max_budget = equity * 0.5
        if max_budget <= 0:
            return
        if settings.budget > max_budget:
            now = monotonic()
            reason = "50% equity"
            should_log = False
            if self._pilot_budget_cap_last_value is None:
                should_log = True
            elif self._pilot_budget_cap_last_reason != reason:
                should_log = True
            elif abs(max_budget - self._pilot_budget_cap_last_value) >= 0.01:
                should_log = True
            elif (
                self._pilot_budget_cap_last_log_ts is None
                or now - self._pilot_budget_cap_last_log_ts >= LOG_DEDUP_HEARTBEAT_SEC
            ):
                should_log = True
            if should_log:
                self._append_log(
                    f"[NC_PILOT] budget capped {settings.budget:.2f} -> {max_budget:.2f} ({reason})",
                    kind="INFO",
                )
                self._pilot_budget_cap_last_value = max_budget
                self._pilot_budget_cap_last_reason = reason
                self._pilot_budget_cap_last_log_ts = now
            settings.budget = max_budget

    def _balance_snapshot(self) -> dict[str, Decimal]:
        balances = dict(self._balances)
        base_free = Decimal("0")
        quote_free = Decimal("0")
        if self._base_asset:
            base_free = self.as_decimal(balances.get(self._base_asset, (0.0, 0.0))[0])
        if self._quote_asset:
            quote_free = self.as_decimal(balances.get(self._quote_asset, (0.0, 0.0))[0])
        return {"base_free": base_free, "quote_free": quote_free}

    def _last_good_balance_snapshot(self) -> dict[str, Decimal]:
        base_free = Decimal("0")
        quote_free = Decimal("0")
        if self._base_asset:
            base_free = self.as_decimal(self._last_good_balances.get(self._base_asset, (0.0, 0.0, 0.0))[0])
        if self._quote_asset:
            quote_free = self.as_decimal(self._last_good_balances.get(self._quote_asset, (0.0, 0.0, 0.0))[0])
        return {"base_free": base_free, "quote_free": quote_free}

    def _record_last_good_balances(self, balances: dict[str, tuple[float, float]]) -> None:
        now = monotonic()
        for asset, (free, locked) in balances.items():
            self._last_good_balances[asset] = (free, locked, now)
        self._last_good_ts = now

    def _validate_balance_snapshot(
        self,
        balances: dict[str, tuple[float, float]],
    ) -> tuple[bool, str]:
        if not balances:
            return False, "empty_snapshot"
        if not self._quote_asset or not self._base_asset:
            return True, "ok"
        quote_asset = self._quote_asset
        base_asset = self._base_asset
        quote_present = quote_asset in balances
        base_present = base_asset in balances
        if not quote_present or not base_present:
            missing = []
            if not quote_present:
                missing.append(quote_asset)
            if not base_present:
                missing.append(base_asset)
            return False, f"missing_assets={','.join(missing)}"
        quote_free, quote_locked = balances.get(quote_asset, (0.0, 0.0))
        base_free, base_locked = balances.get(base_asset, (0.0, 0.0))
        quote_total = quote_free + quote_locked
        base_total = base_free + base_locked
        last_quote = self._last_good_balances.get(quote_asset)
        last_base = self._last_good_balances.get(base_asset)
        if last_quote and quote_total <= 0 and (last_quote[0] + last_quote[1]) > 0:
            return False, f"zero_total_quote={quote_asset}"
        if last_base and base_total <= 0 and (last_base[0] + last_base[1]) > 0:
            return False, f"zero_total_base={base_asset}"
        if quote_total <= 0 and base_total <= 0 and (last_quote or last_base):
            return False, "zero_totals"
        return True, "ok"

    def _balances_ready_for_start(self) -> bool:
        if not self._balances_loaded:
            return False
        if not self._quote_asset or not self._base_asset:
            return False
        if self._quote_asset not in self._balances or self._base_asset not in self._balances:
            return False
        balance_age_s = self._balance_age_s()
        if balance_age_s is None:
            return self._open_orders_loaded
        return balance_age_s <= 2.0 or self._open_orders_loaded

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

    def _force_refresh_open_orders_and_wait(self, timeout_ms: int) -> bool:
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
        self._signals.open_orders_refresh.connect(_on_refresh)
        self._refresh_orders(force=True, force_refresh=True, reason="force_wait")
        timer.start(timeout_ms)
        loop.exec()
        self._signals.open_orders_refresh.disconnect(_on_refresh)
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

    def _set_balances_poll_interval(self, interval_ms: int) -> None:
        interval_ms = max(500, int(interval_ms))
        if self._balances_poll_ms == interval_ms:
            return
        self._balances_poll_ms = interval_ms
        self._balances_timer.setInterval(self._balances_poll_ms)
        if self._balances_timer.isActive():
            self._balances_timer.start(self._balances_poll_ms)

    @staticmethod
    def _format_account_error(error: object) -> str:
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code if error.response else "n/a"
            return f"HTTP {status}"
        if isinstance(error, httpx.RequestError):
            return error.__class__.__name__
        if isinstance(error, Exception):
            return error.__class__.__name__
        return str(error)

    def _reset_defaults(self) -> None:
        if self._is_micro_profile():
            defaults = self._micro_grid_defaults()
            if self._settings_state == defaults:
                return
            self._apply_micro_defaults(reason="reset_button")
        else:
            defaults = GridSettingsState()
            if self._settings_state == defaults:
                return
            self._set_settings_state(defaults)
        self._manual_grid_step_pct = defaults.grid_step_pct
        self._refresh_settings_ui_from_state()
        self._append_log("Settings reset to defaults.", kind="INFO")
        self._update_grid_preview()

    def _refresh_balances(self, force: bool = False) -> None:
        if self._closing and not self._stop_in_progress and not force:
            return
        self._session.runtime.last_poll_ts = monotonic()
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
            self._queue_net_request(
                "account_info",
                {},
                on_success=self._handle_account_info,
                on_error=self._handle_account_error,
            )
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _refresh_balances")
            self._notify_crash("_refresh_balances")
            self._pilot_force_hold(reason="exception:_refresh_balances")
            return

    def _handle_account_info(self, result: object, latency_ms: int) -> None:
        self._balances_in_flight = False
        if not isinstance(result, dict):
            self._handle_account_error(ValueError("Unexpected account response"))
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
        valid_snapshot, reason = self._validate_balance_snapshot(balances)
        if valid_snapshot:
            self._balances = balances
            self._balances_loaded = True
            if self._rules_loaded:
                self._balance_ready_ts_monotonic_ms = int(monotonic() * 1000)
            self._record_last_good_balances(balances)
            self._balances_error_streak = 0
            self._set_balances_poll_interval(1000)
            self._set_account_status("ready")
        else:
            self._balances_error_streak += 1
            if self._balances_error_streak >= 3:
                self._set_balances_poll_interval(3000)
            self._set_account_status("error_kept", detail=reason, kept_last=True)
        self._account_api_error = not valid_snapshot
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
        if valid_snapshot:
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
            self._last_account_snapshot = {
                "balances": balances,
                "timestamp": monotonic(),
                "latency_ms": latency_ms,
            }
        else:
            age_text = "n/a"
            if self._last_good_ts is not None:
                age_text = f"{max(monotonic() - self._last_good_ts, 0.0):.1f}s"
            self._append_log_throttled(
                f"[BAL] rejected snapshot reason={reason} keep_last_good age={age_text}",
                kind="WARN",
                key=f"balances_rejected:{reason}",
                cooldown_sec=20.0,
            )
            self._signals.balances_refresh.emit(False)
        if self._balances_ready_for_start() and not self._start_locked_until_change:
            self._last_preflight_blocked = False
            self._last_preflight_hash = None

    def _handle_account_error(self, message: object) -> None:
        self._balances_in_flight = False
        self._balances_error_streak += 1
        if self._balances_error_streak >= 3:
            self._set_balances_poll_interval(3000)
        self._account_can_trade = False
        self._account_api_error = True
        self._account_permissions = []
        self._last_account_trade_snapshot = None
        reason = self._format_account_error(message)
        self._set_account_status("error", detail=reason, kept_last=True)
        self._append_log(f"balances fetch failed: {reason}", kind="WARN")
        self._auto_pause_on_api_error(reason)
        self._auto_pause_on_exception(reason)
        self._apply_trade_gate()
        self._update_runtime_balances()
        self._signals.balances_refresh.emit(False)

    def _request_orders_refresh(self, event: str) -> None:
        now = monotonic()
        last_ts = self._orders_event_refresh_ts.get(event)
        if last_ts is not None and now - last_ts < ORDERS_EVENT_DEDUP_SEC:
            return
        self._orders_event_refresh_ts[event] = now
        self._reset_orders_poll_backoff()
        self._refresh_orders(force=True, reason=event, event_key=event)

    def _schedule_force_open_orders_refresh(self, reason: str) -> None:
        if not self._account_client:
            return

        def _kick() -> None:
            if self._closing:
                return
            self._safe_call(
                lambda: self._refresh_orders(force=True, force_refresh=True, reason=reason),
                label="timer:force_orders_refresh",
            )

        QTimer.singleShot(400, self, _kick)
        QTimer.singleShot(800, self, _kick)

    def _refresh_open_orders(
        self,
        force: bool = False,
        *,
        force_refresh: bool = False,
        reason: str | None = None,
        event_key: str | None = None,
    ) -> None:
        self._refresh_orders(force=force, force_refresh=force_refresh, reason=reason or "poll", event_key=event_key)

    def _refresh_orders(
        self,
        *,
        reason: str,
        force: bool = False,
        force_refresh: bool = False,
        event_key: str | None = None,
    ) -> None:
        critical_reason = self._is_critical_refresh_reason(reason)
        if self._closing and not self._stop_in_progress and not critical_reason and not force:
            return
        self._session.runtime.last_poll_ts = monotonic()
        try:
            if force_refresh:
                force = True
                self._snapshot_refresh_inflight = False
                self._snapshot_refresh_suppressed_log_ts = None
            if self._stop_in_progress:
                force = True
            if self._cancel_inflight and not force and not critical_reason:
                return
            self._orders_tick_count += 1
            if self._orders_tick_count % 30 == 0:
                self._logger.debug("orders refresh tick")
            if not self._account_client:
                self._set_account_status("no_keys")
                self._set_open_orders_all([])
                self._set_open_orders([])
                self._open_orders_loaded = False
                self._snapshot_refresh_inflight = False
                self._snapshot_refresh_suppressed_log_ts = None
                self._set_open_orders_map({})
                self._clear_order_info()
                self._bot_client_ids.clear()
                self._bot_order_keys = set()
                self._active_order_keys.clear()
                self._recent_order_keys.clear()
                self._order_id_to_registry_key.clear()
                self._active_tp_ids.clear()
                self._active_restore_ids.clear()
                self._set_orders_snapshot(self._build_orders_snapshot([]), reason=reason)
                self._apply_trade_gate()
                return
            if not force:
                now_ms = int(monotonic() * 1000)
                interval_ms = self._current_orders_poll_interval_ms()
                if self._orders_poll_last_ts is not None and now_ms - self._orders_poll_last_ts < interval_ms:
                    return
            if not force:
                if self._ensure_open_orders_snapshot_fresh("poll"):
                    return
                snapshot_age_sec = self._snapshot_age_sec()
                if snapshot_age_sec is not None and snapshot_age_sec > STALE_SNAPSHOT_MAX_AGE_SEC:
                    self._maybe_log_open_orders_snapshot_stale(snapshot_age_sec, skip_actions=True)
            if self._orders_in_flight:
                if not force and not self._stop_in_progress and not critical_reason:
                    return
            if not force:
                self._orders_poll_last_ts = int(monotonic() * 1000)
            self._orders_in_flight = True
            self._orders_refresh_reason = reason
            self._queue_net_request(
                "open_orders",
                {"symbol": self._symbol},
                on_success=self._handle_open_orders,
                on_error=self._handle_open_orders_error,
            )
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _refresh_orders")
            self._notify_crash("_refresh_orders")
            self._pilot_force_hold(reason="exception:_refresh_orders")
            self._orders_in_flight = False
            self._snapshot_refresh_inflight = False
            return

    def _prime_bot_registry_from_exchange(self) -> None:
        if not self._account_client:
            return
        try:
            open_orders, _latency_ms = self._net_call_sync(
                "open_orders",
                {"symbol": self._symbol},
                timeout_ms=5000,
            )
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[START] openOrders preload failed: {exc}", kind="WARN")
            return
        if not isinstance(open_orders, list):
            return
        self._set_open_orders_all([item for item in open_orders if isinstance(item, dict)])
        self._set_open_orders(self._filter_bot_orders(self._open_orders_all))
        self._set_open_orders_map({
            str(order.get("orderId", "")): order
            for order in self._open_orders
            if str(order.get("orderId", ""))
        })
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
        self._registry_dirty = False

    def _refresh_fills(self) -> None:
        if self._closing and not self._stop_in_progress:
            return
        self._session.runtime.last_poll_ts = monotonic()
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
            self._queue_net_request(
                "my_trades",
                {"symbol": self._symbol, "limit": 50},
                on_success=self._handle_fill_poll,
                on_error=self._handle_fill_poll_error,
            )
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _refresh_fills")
            self._notify_crash("_refresh_fills")
            self._pilot_force_hold(reason="exception:_refresh_fills")
            return

    def _handle_fill_poll(self, result: object, latency_ms: int) -> None:
        try:
            if self._closing:
                return
            self._fills_in_flight = False
            if not isinstance(result, list):
                self._handle_fill_poll_error("Unexpected trades response")
                return
            for trade in result:
                if not isinstance(trade, dict):
                    continue
                order_id = str(trade.get("orderId", ""))
                if not order_id:
                    continue
                if order_id and order_id not in self._bot_order_ids:
                    continue
                is_buyer = trade.get("isBuyer")
                side = "BUY" if is_buyer else "SELL"
                order_stub = {"side": side, "orderId": order_id}
                fill = self._build_trade_fill(order_stub, trade)
                if fill:
                    self._process_fill(fill)
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _handle_fill_poll")
            self._notify_crash("_handle_fill_poll")
            self._pilot_force_hold(reason="exception:_handle_fill_poll")
            return

    def _handle_fill_poll_error(self, message: object) -> None:
        self._fills_in_flight = False
        reason = self._format_account_error(message)
        self._append_log(f"fills poll failed: {reason}", kind="WARN")
        self._auto_pause_on_api_error(reason)
        self._auto_pause_on_exception(reason)

    def _current_orders_poll_interval_ms(self) -> int:
        if self._state == "STOPPING" and self._cancel_inflight:
            return ORDERS_POLL_STOPPING_MS
        base_interval = ORDERS_POLL_RUNNING_MS if self._state == "RUNNING" else ORDERS_POLL_IDLE_MS
        interval = base_interval + self._orders_poll_backoff_ms
        return int(min(interval, ORDERS_POLL_MAX_MS))

    def _reset_orders_poll_backoff(self) -> None:
        if self._orders_poll_backoff_ms != 0:
            self._orders_poll_backoff_ms = 0
            self._update_orders_timer_interval()

    def _update_orders_timer_interval(self) -> None:
        if not hasattr(self, "_orders_timer"):
            return
        interval = self._current_orders_poll_interval_ms()
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

    @staticmethod
    def _orders_signature(orders: list[dict[str, Any]]) -> tuple[tuple[str, str, str, str], ...]:
        signature: list[tuple[str, str, str, str]] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("orderId", ""))
            status = str(order.get("status", ""))
            price = str(order.get("price", ""))
            qty = str(order.get("origQty", ""))
            if order_id:
                signature.append((order_id, status, price, qty))
        return tuple(sorted(signature))

    @staticmethod
    def _orders_snapshot_hash(orders: list[dict[str, Any]]) -> int:
        signature: list[tuple[str, str, str, str, str, str, str]] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("orderId", ""))
            if not order_id:
                continue
            signature.append(
                (
                    order_id,
                    str(order.get("side", "")),
                    str(order.get("price", "")),
                    str(order.get("origQty", "")),
                    str(order.get("executedQty", "")),
                    str(order.get("status", "")),
                    str(order.get("updateTime", "")),
                )
            )
        return hash(tuple(sorted(signature)))

    def _apply_open_orders_snapshot(self, orders: list[dict[str, Any]], *, reason: str) -> None:
        self._purge_recent_order_keys()
        previous_map = dict(self._open_orders_map)
        previous_signature = self._orders_snapshot_signature
        self._set_open_orders_all([item for item in orders if isinstance(item, dict)])
        self._open_orders_loaded = True
        self._bootstrap_legacy_orders(self._open_orders_all)
        self._set_open_orders(self._filter_bot_orders(self._open_orders_all))
        self._set_open_orders_map({
            str(order.get("orderId", "")): order
            for order in self._open_orders
            if str(order.get("orderId", ""))
        })
        open_order_ids = {
            str(order.get("orderId", ""))
            for order in self._open_orders_all
            if isinstance(order, dict) and str(order.get("orderId", ""))
        }
        if self._legacy_order_ids:
            self._legacy_order_ids.intersection_update(open_order_ids)
        if self._owned_order_ids:
            self._owned_order_ids.intersection_update(open_order_ids)
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
        self._registry_dirty = False
        self._fills_changed_since_refresh = False
        snapshot = self._build_orders_snapshot(self._open_orders)
        self._set_orders_snapshot(snapshot, reason=reason)
        self._grid_engine.sync_open_orders(self._open_orders)
        missing = [
            order
            for order_id, order in previous_map.items()
            if order_id not in self._open_orders_map and self._is_bot_order(order)
        ]
        if missing and self._account_client and self._state in {"RUNNING", "WAITING_FILLS"}:
            self._queue_reconcile_missing_orders(missing[:3])
        count = len(self._open_orders)
        self._orders_snapshot_signature = self._orders_signature(self._open_orders_all)
        new_hash = self._orders_snapshot_hash(self._open_orders_all)
        previous_hash = self._orders_snapshot_hash_value
        self._orders_snapshot_hash_changed = previous_hash is None or new_hash != previous_hash
        if self._orders_snapshot_hash_changed:
            self._orders_snapshot_hash_stable_cycles = 0
        else:
            self._orders_snapshot_hash_stable_cycles += 1
        self._orders_snapshot_hash_value = new_hash
        orders_changed = self._orders_snapshot_signature != previous_signature
        if orders_changed:
            self._orders_last_change_ts = time_fn()
        inflight_actions = bool(self._active_action_keys or self._pilot_action_in_progress)
        if (
            orders_changed
            or inflight_actions
            or self._session.runtime.start_in_progress
            or self._stop_in_progress
        ):
            self._reset_orders_poll_backoff()
        else:
            base_interval = ORDERS_POLL_RUNNING_MS if self._state == "RUNNING" else ORDERS_POLL_IDLE_MS
            max_backoff = max(ORDERS_POLL_MAX_MS - base_interval, 0)
            if self._orders_poll_backoff_ms < max_backoff:
                self._orders_poll_backoff_ms = min(self._orders_poll_backoff_ms + 500, max_backoff)
        if self._orders_last_count is None or count != self._orders_last_count:
            self._append_log(
                f"open orders updated (n={count}).",
                kind="INFO",
            )
            self._orders_last_count = count
        self._update_orders_timer_interval()
        self._signals.open_orders_refresh.emit(True)

    def _handle_open_orders(self, result: object, latency_ms: int) -> None:
        if self._closing:
            return
        try:
            self._orders_in_flight = False
            self._snapshot_refresh_inflight = False
            if not isinstance(result, list):
                self._handle_open_orders_error("Unexpected open orders response")
                return
            reason = self._orders_refresh_reason
            self._apply_open_orders_snapshot(result, reason=reason)
            if reason == "after_cancel_backoff":
                if not self._open_orders:
                    self._cancel_reconcile_pending = 0
                    self._cancel_inflight = False
                    self._update_orders_timer_interval()
                else:
                    if self._cancel_reconcile_pending > 0:
                        self._cancel_reconcile_pending -= 1
                    if self._cancel_reconcile_pending <= 0:
                        self._append_log(
                            f"[ORDERS][DESYNC] still_nonzero after_cancel rows={len(self._open_orders)}",
                            kind="WARN",
                        )
                        self._cancel_inflight = False
                        self._update_orders_timer_interval()
            if self._stop_in_progress and self._stop_finalize_waiting and not self._open_orders:
                self._stop_finalize_waiting = False
                self._finalize_stop(keep_open_orders=False)
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _handle_open_orders")
            self._notify_crash("_handle_open_orders")
            self._pilot_force_hold(reason="exception:_handle_open_orders")
            return

    def _handle_open_orders_error(self, message: object) -> None:
        self._orders_in_flight = False
        self._snapshot_refresh_inflight = False
        self._signals.open_orders_refresh.emit(False)
        reason = self._format_account_error(message)
        if self._is_auth_error(reason):
            self._account_api_error = True
        self._append_log(f"openOrders fetch failed: {reason}", kind="WARN")
        self._auto_pause_on_api_error(reason)
        self._auto_pause_on_exception(reason)
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
        self._registry_dirty = True

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
                self._register_order_key(registry_key, mark_dirty=False)
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

    def _find_open_order_by_client_id(self, client_id: str) -> dict[str, Any] | None:
        if not client_id:
            return None
        for order in self._open_orders_all:
            if not isinstance(order, dict):
                continue
            if str(order.get("clientOrderId", "")) == client_id:
                return order
        return None

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

    def _has_open_order_side_price(self, side: str, price: Decimal, tolerance_ticks: int = 0) -> bool:
        side = side.upper()
        if side not in {"BUY", "SELL"}:
            return False
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        target_price = self.q_price(price, tick)
        tolerance = (tick or Decimal("0")) * Decimal(tolerance_ticks)
        for order in self._open_orders:
            if str(order.get("side", "")).upper() != side:
                continue
            order_price = self._coerce_float(str(order.get("price", ""))) or 0.0
            if order_price <= 0:
                continue
            order_price_dec = self.q_price(self.as_decimal(order_price), tick)
            if tolerance > 0:
                if abs(order_price_dec - target_price) > tolerance:
                    continue
            elif order_price_dec != target_price:
                continue
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

    def _mark_registry_dirty(self) -> None:
        self._registry_dirty = True

    def _register_order_key(self, key: str, ttl_s: float | None = None, *, mark_dirty: bool = True) -> None:
        self._active_order_keys.add(key)
        self._registry_key_last_seen_ts[key] = time_fn()
        self._mark_recent_order_key(key, ttl_s or self._recent_key_ttl_s)
        if mark_dirty:
            self._mark_registry_dirty()

    def _discard_order_registry_key(
        self,
        key: str,
        *,
        drop_recent: bool = False,
        mark_dirty: bool = True,
    ) -> None:
        self._active_order_keys.discard(key)
        self._registry_key_last_seen_ts.pop(key, None)
        if drop_recent:
            self._recent_order_keys.pop(key, None)
        if mark_dirty:
            self._mark_registry_dirty()

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
        if self._closing:
            return
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
            self._logger.exception("[NC_PILOT][CRASH] exception in _gc_registry_keys")
            self._notify_crash("_gc_registry_keys")
            self._pilot_force_hold(reason="exception:_gc_registry_keys")
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
            trades = self._fetch_my_trades_sync(self._symbol, limit=50)
            trades_by_order: dict[str, dict[str, Any]] = {}
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
                    order_info = self._fetch_order_sync(
                        self._symbol,
                        orig_client_order_id=client_order_id,
                    )
                else:
                    order_info = self._fetch_order_sync(self._symbol, order_id=order_id or None)
                status = str(order_info.get("status", "")).upper() if isinstance(order_info, dict) else "UNKNOWN"
                results.append({"status": status, "order": order, "trade": None})
            return {"results": results}

        try:
            result = _reconcile()
            self._handle_reconcile_missing_orders(result, 0)
        except Exception as exc:  # noqa: BLE001
            self._handle_reconcile_error(str(exc))

    def _handle_reconcile_missing_orders(self, result: object, latency_ms: int) -> None:
        if self._closing:
            return
        try:
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
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _handle_reconcile_missing_orders")
            self._notify_crash("_handle_reconcile_missing_orders")
            self._pilot_force_hold(reason="exception:_handle_reconcile_missing_orders")
            return

    def _handle_reconcile_error(self, message: str) -> None:
        self._append_log(f"Reconcile failed: {message}", kind="WARN")

    def _build_trade_fill(
        self,
        order: dict[str, Any],
        trade: dict[str, Any] | None,
    ) -> TradeFill | None:
        is_total = False
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
            is_total = True
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
            is_total=is_total,
        )

    def _log_exec_new(
        self,
        *,
        order_id: str,
        trade_id: str,
        qty_delta: Decimal,
        cum_qty: Decimal,
        price: Decimal,
        side: str,
    ) -> None:
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        trade_token = trade_id or "—"
        self._session.counters.fills += 1
        self._append_log(
            (
                "[EXEC] exec_new "
                f"orderId={order_id or '—'} tradeId={trade_token} "
                f"qty_delta={self.fmt_qty(qty_delta, step)} "
                f"cum_qty={self.fmt_qty(cum_qty, step)} "
                f"price={self.fmt_price(price, tick)} side={side}"
            ),
            kind="INFO",
        )

    def _log_exec_dup(
        self,
        *,
        order_id: str,
        trade_id: str,
    ) -> None:
        key = order_id or trade_id or "—"
        state = self._exec_dup_log_state.get(key)
        if state is None:
            state = ExecDupLogState()
            self._exec_dup_log_state[key] = state
        state.count += 1
        self._session.counters.exec_dup += 1
        now = monotonic()
        should_log = state.count == 1
        if state.last_log_ts is not None and now - state.last_log_ts >= EXEC_DUP_LOG_COOLDOWN_SEC:
            should_log = True
        if not should_log:
            return
        state.last_log_ts = now
        trade_token = trade_id or "—"
        self._append_log(
            (
                "[EXEC] exec_dup suppressed "
                f"orderId={order_id or '—'} tradeId={trade_token} "
                f"dup_count={state.count}"
            ),
            kind="DEBUG",
        )

    def _process_fill(self, fill: TradeFill) -> None:
        if self._closing:
            return
        try:
            trade_id = fill.trade_id or ""
            if trade_id:
                exec_key = (self._symbol, fill.order_id or "", trade_id)
                if self._exec_key_deduper.seen(exec_key, monotonic()):
                    self._log_exec_dup(order_id=fill.order_id, trade_id=trade_id)
                    return
            if trade_id and self._trade_id_deduper.seen(trade_id, monotonic()):
                self._log_exec_dup(order_id=fill.order_id, trade_id=trade_id)
                return
            total_filled = self.as_decimal(fill.qty)
            delta = total_filled
            if fill.order_id:
                total_filled, delta = self._fill_accumulator.record(
                    fill.order_id,
                    self.as_decimal(fill.qty),
                    is_total=fill.is_total,
                )
            elif fill.is_total:
                delta = self._fill_exec_cumulative.update("", total_filled)
            if delta <= 0:
                self._log_exec_dup(order_id=fill.order_id, trade_id=trade_id)
                return
            self._fills_changed_since_refresh = True
            fill_key = self._fill_key(fill)
            if fill.trade_id == "" and fill.order_id == "":
                if fill_key in self._fill_keys:
                    self._log_exec_dup(order_id=fill.order_id, trade_id=trade_id)
                    return
            self._fill_keys.add(fill_key)
            self._log_exec_new(
                order_id=fill.order_id,
                trade_id=trade_id,
                qty_delta=delta,
                cum_qty=total_filled,
                price=self.as_decimal(fill.price),
                side=fill.side,
            )
            client_order_id = ""
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
            self._place_replacement_order(
                fill,
                delta_qty=delta,
                total_qty=total_filled,
                fill_client_order_id=client_order_id,
            )
            if fill.order_id:
                self._fill_accumulator.mark_handled(fill.order_id, total_filled)
                if fill.is_total:
                    self._fill_exec_cumulative.totals[fill.order_id] = total_filled
            self._maybe_enable_bootstrap_sell_side(fill)
            self._request_orders_refresh("fill")
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception in _process_fill")
            self._notify_crash("_process_fill")
            self._pilot_force_hold(reason="exception:_process_fill")
            return

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

    def _fee_override_roundtrip(self, symbol: str | None) -> float | None:
        if not symbol:
            return None
        return FEE_OVERRIDES_ROUNDTRIP.get(symbol.upper())

    def get_effective_fees_pct(self, symbol: str | None = None) -> float:
        override = self._fee_override_roundtrip(symbol or self._symbol)
        if override is not None:
            return override
        maker, taker = self._trade_fees
        maker_pct = (maker or 0.0) * 100
        taker_pct = (taker or 0.0) * 100
        fill_mode = self._runtime_profit_inputs().get("expected_fill_mode") or "MAKER"
        if fill_mode == "TAKER":
            return maker_pct + taker_pct
        return 2 * maker_pct

    def _current_profit_guard_mode(self) -> ProfitGuardMode:
        if not self.pilot_enabled:
            return ProfitGuardMode.WARN_ONLY
        if self._fee_source == "OVERRIDE":
            return ProfitGuardMode.WARN_ONLY
        return ProfitGuardMode.WARN_ONLY if self._fee_unverified else self._profit_guard_mode

    def _current_profit_guard_reason(self) -> str | None:
        if self._fee_source == "OVERRIDE":
            return "fees_override"
        if self._fee_unverified:
            return "fees_unverified"
        return None

    def _fee_display_text(self) -> str:
        if self._fee_source == "OVERRIDE":
            return "0.00%/0.00% (override)"
        maker, taker = self._trade_fees
        unverified = self._fee_unverified
        maker_pct = (maker or 0.0) * 100
        taker_pct = (taker or 0.0) * 100
        suffix = " (unverified)" if unverified else ""
        return f"{maker_pct:.2f}%/{taker_pct:.2f}%{suffix}"

    def _log_fee_source(self, source: str) -> None:
        if source == "OVERRIDE":
            roundtrip = self.get_effective_fees_pct(self._symbol)
            self._append_log(
                (
                    "[FEES] source=OVERRIDE "
                    f"symbol={self._symbol} roundtrip={roundtrip:.4f}%"
                ),
                kind="INFO",
            )
        elif source == "ACCOUNT":
            maker, taker = self._trade_fees
            maker_pct = (maker or 0.0) * 100
            taker_pct = (taker or 0.0) * 100
            roundtrip = self.get_effective_fees_pct(self._symbol)
            self._append_log(
                (
                    "[FEES] source=ACCOUNT "
                    f"maker={maker_pct:.4f}% taker={taker_pct:.4f}% roundtrip={roundtrip:.4f}%"
                ),
                kind="INFO",
            )
        else:
            roundtrip = self.get_effective_fees_pct(self._symbol)
            self._append_log(
                f"[FEES] source=DEFAULT_UNVERIFIED roundtrip={roundtrip:.4f}%",
                kind="INFO",
            )

    def _update_fee_state(self, maker: float | None, taker: float | None, source: str) -> None:
        self._trade_fees = (maker, taker)
        fee_unverified = maker is None or taker is None or source != "ACCOUNT"
        source_value = source if not fee_unverified else "DEFAULT_UNVERIFIED"
        if self._fee_override_roundtrip(self._symbol) is not None:
            source_value = "OVERRIDE"
            fee_unverified = False
        if source_value != self._fee_source or fee_unverified != self._fee_unverified:
            self._fee_source = source_value
            self._fee_unverified = fee_unverified
            self._log_fee_source(source_value)
        else:
            self._fee_unverified = fee_unverified

    def _profit_profile_name(self) -> str:
        return "BALANCED"

    def _runtime_profit_inputs(self) -> dict[str, Any]:
        return {
            "expected_fill_mode": "MAKER",
            "slippage_pct": self._profit_guard_slippage_pct(),
            "safety_edge_pct": self._profit_guard_pad_pct(),
            "fee_discount_pct": None,
        }

    def _profit_guard_fee_inputs(self) -> tuple[float, float, bool]:
        if self._fee_source == "OVERRIDE":
            return 0.0, 0.0, False
        maker, taker = self._trade_fees
        used_default = self._fee_unverified or maker is None or taker is None
        maker_pct = (maker * 100) if maker is not None else PROFIT_GUARD_DEFAULT_MAKER_FEE_PCT
        taker_pct = (taker * 100) if taker is not None else PROFIT_GUARD_DEFAULT_TAKER_FEE_PCT
        return maker_pct, taker_pct, used_default

    def _profit_guard_round_trip_cost_pct(self) -> float:
        fee_cost = self.get_effective_fees_pct(self._symbol)
        slippage_pct = self._profit_guard_slippage_pct()
        return fee_cost + slippage_pct + self._profit_guard_pad_pct()

    def _profit_guard_min_profit_pct(self, settings: GridSettingsState) -> float:
        round_trip_cost = self._profit_guard_round_trip_cost_pct()
        base_min_profit = max(
            round_trip_cost + self._profit_guard_buffer_pct(),
            self._profit_guard_min_floor_pct(),
        )
        return max(base_min_profit, settings.take_profit_pct)

    def _current_spread_pct(self) -> float | None:
        best_bid, best_ask, source = self._resolve_book_bid_ask()
        if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
            book_source = source if source and source != "—" else (self.trade_source or "UNKNOWN")
            self._update_kpi_waiting_book(monotonic(), book_source, self._http_book_age_ms())
            return None
        return self._compute_spread_pct_from_book(best_bid, best_ask)

    def _min_tick_step_pct(self, anchor_price: float) -> float:
        tick = self._exchange_rules.get("tick")
        if not tick or anchor_price <= 0:
            return 0.0
        return tick / anchor_price * 100

    def _micro_step_pct_from_tick(self, anchor_price: float) -> float | None:
        tick = self._exchange_rules.get("tick")
        if not tick or anchor_price <= 0:
            return None
        return tick / anchor_price * 100

    def _micro_spread_capture_context(self) -> dict[str, float | int | bool | None]:
        bid, ask, _source = self._resolve_book_bid_ask()
        tick = self._exchange_rules.get("tick")
        age_ms = self._http_book_age_ms()
        has_bidask = bid is not None and ask is not None and bid > 0 and ask > 0
        if not has_bidask:
            book_source = _source if _source and _source != "—" else (self.trade_source or "UNKNOWN")
            self._update_kpi_waiting_book(monotonic(), book_source, age_ms)
        is_live = bool(has_bidask and age_ms is not None and age_ms < BIDASK_FAIL_SOFT_MS)
        mid = (bid + ask) / 2 if has_bidask else None
        spread_ticks = None
        if has_bidask and tick and tick > 0:
            spread_ticks = int(round((ask - bid) / tick))
        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "tick": tick,
            "spread_ticks": spread_ticks,
            "is_live": is_live,
            "has_bidask": has_bidask,
        }

    def _micro_tp_ticks(self, spread_ticks: int | None) -> int:
        if spread_ticks is None:
            return 2
        if spread_ticks < 1:
            return 2
        return 2

    def _micro_spread_capture_guard(self) -> tuple[bool, str, dict[str, float | int | bool | None]]:
        context = self._micro_spread_capture_context()
        if not context["has_bidask"] or not context["is_live"]:
            return False, "no_live_bidask", context
        spread_ticks = context.get("spread_ticks")
        if not isinstance(spread_ticks, int) or spread_ticks < 1:
            return False, "spread_lt_1_tick", context
        return True, "ok", context

    def _profit_guard_should_hold(
        self,
        min_profit_bps: float,
    ) -> tuple[
        str,
        float | None,
        float | None,
        float | None,
        float | None,
        float | None,
        float | None,
        float,
        bool,
    ]:
        spread_pct = self._current_spread_pct()
        if self._kpi_state != KPI_STATE_OK:
            return "OK", None, None, None, None, None, None, min_profit_bps, False
        if spread_pct is None or spread_pct <= 0:
            return "OK", None, None, None, None, None, None, min_profit_bps, False
        spread_bps = spread_pct * 100
        fee_cost_pct = self.get_effective_fees_pct(self._symbol)
        fees_roundtrip_bps = fee_cost_pct * 100
        slippage_bps = self._profit_guard_slippage_pct() * 100
        safety_pad_bps = self._profit_guard_pad_pct() * 100
        vol_bps = self._market_health.vol_bps if self._market_health else None
        risk_buffer_bps = (vol_bps or 0.0) * MARKET_HEALTH_RISK_BUFFER_MULT
        adaptive_min_profit = False
        min_profit_bps_used = min_profit_bps
        if self._is_micro_profile() and fees_roundtrip_bps <= 0.01 and spread_bps <= 3.0:
            slip_pad_bps = slippage_bps + safety_pad_bps
            min_profit_bps_used = min(max(2.0, spread_bps + slip_pad_bps), 6.0)
            adaptive_min_profit = True
            now = monotonic()
            if self._tp_micro_gate_log_ts is None or now - self._tp_micro_gate_log_ts >= 15.0:
                self._append_log(
                    f"[TP] micro gate min_profit_bps={min_profit_bps_used:.2f} spread_bps={spread_bps:.2f}",
                    kind="INFO",
                )
                self._tp_micro_gate_log_ts = now
        edge = compute_expected_edge_bps(
            spread_bps=spread_bps,
            fees_roundtrip_bps=fees_roundtrip_bps,
            slippage_bps=slippage_bps,
            pad_bps=safety_pad_bps,
            risk_buffer_bps=risk_buffer_bps,
        )
        if edge.expected_profit_bps < min_profit_bps_used:
            return (
                "HOLD",
                spread_bps,
                edge.expected_profit_bps,
                fees_roundtrip_bps,
                slippage_bps,
                safety_pad_bps,
                risk_buffer_bps,
                min_profit_bps_used,
                adaptive_min_profit,
            )
        return (
            "OK",
            spread_bps,
            edge.expected_profit_bps,
            fees_roundtrip_bps,
            slippage_bps,
            safety_pad_bps,
            risk_buffer_bps,
            min_profit_bps_used,
            adaptive_min_profit,
        )

    def _profit_guard_autofix(
        self,
        settings: GridSettingsState,
        anchor_price: float,
        *,
        action_label: str,
        update_ui: bool,
        min_profit_pct: float,
    ) -> bool:
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
        max_step_pct = max(self.pilot_config.max_step_pct, 0.0)
        if max_step_pct > 0:
            settings.grid_step_pct = min(settings.grid_step_pct, max_step_pct)
        min_range_pct = settings.grid_step_pct * max(settings.grid_count, 1) / 2
        if settings.range_low_pct < min_range_pct:
            settings.range_low_pct = min_range_pct
        if settings.range_high_pct < min_range_pct:
            settings.range_high_pct = min_range_pct
        max_range_pct = max(self.pilot_config.max_range_pct, 0.0)
        if max_range_pct > 0:
            settings.range_low_pct = min(settings.range_low_pct, max_range_pct)
            settings.range_high_pct = min(settings.range_high_pct, max_range_pct)
        if self._is_micro_profile():
            settings.take_profit_pct = min(settings.take_profit_pct, MICRO_STABLECOIN_TP_PCT_MAX)
        changed = (
            settings.take_profit_pct != tp_old
            or settings.grid_step_pct != step_old
            or settings.range_low_pct != range_low_old
            or settings.range_high_pct != range_high_old
        )
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
        return changed

    def _apply_profit_guard(
        self,
        settings: GridSettingsState,
        anchor_price: float,
        *,
        action_label: str,
        update_ui: bool,
    ) -> str:
        guard_mode = self._current_profit_guard_mode()
        min_profit_pct = self._profit_guard_min_profit_pct(settings)
        min_profit_bps = min_profit_pct * 100
        (
            decision,
            spread_bps,
            expected_profit_bps,
            fees_roundtrip_bps,
            slippage_bps,
            safety_pad_bps,
            risk_buffer_bps,
            min_profit_bps_used,
            adaptive_min_profit,
        ) = self._profit_guard_should_hold(min_profit_bps)
        maker_pct, taker_pct, used_default = self._profit_guard_fee_inputs()
        if used_default:
            self._append_log(
                (
                    f"[GUARD] fees defaulted maker={maker_pct:.4f}% taker={taker_pct:.4f}% "
                    f"round_trip={self._profit_guard_round_trip_cost_pct():.4f}%"
                ),
                kind="INFO",
            )
        if decision == "HOLD":
            spread_text = f"{spread_bps:.2f}bps" if spread_bps is not None else "—"
            expected_text = f"{expected_profit_bps:.2f}bps" if expected_profit_bps is not None else "—"
            fee_text = f"{fees_roundtrip_bps:.2f}bps" if fees_roundtrip_bps is not None else "—"
            slip_text = f"{slippage_bps:.2f}bps" if slippage_bps is not None else "—"
            pad_text = f"{safety_pad_bps:.2f}bps" if safety_pad_bps is not None else "—"
            risk_text = f"{risk_buffer_bps:.2f}bps" if risk_buffer_bps is not None else "—"
            guard_prefix = "HOLD" if guard_mode == ProfitGuardMode.BLOCK else "WARN_ONLY"
            if not (adaptive_min_profit and guard_mode == ProfitGuardMode.WARN_ONLY):
                self._append_log(
                    (
                        f"[GUARD] {guard_prefix} thin_edge "
                        f"raw_spread={spread_text} expected_profit_bps={expected_text} "
                        f"min_profit_bps={min_profit_bps_used:.2f} fees={fee_text} "
                        f"slip={slip_text} pad={pad_text} risk={risk_text}"
                    ),
                    kind="WARN",
                )
            if self.pilot_enabled:
                self._pilot_hold_reason = "guard"
                self._pilot_hold_until = monotonic() + max(self.pilot_config.hold_timeout_sec, 0)
                self._set_pilot_state(PilotState.HOLD, reason="guard")
            if guard_mode == ProfitGuardMode.BLOCK:
                return "HOLD"
        allow_autofix = self.enable_guard_autofix and self.pilot_enabled
        if (
            self.pilot_enabled
            and settings.grid_step_mode == "MANUAL"
            and not getattr(self.pilot_config, "allow_guard_autofix", False)
        ):
            allow_autofix = False
            self._append_log(
                "[GUARD] autofix skipped: manual mode (allow_guard_autofix=false)",
                kind="WARN",
            )
        if allow_autofix:
            changed = self._profit_guard_autofix(
                settings,
                anchor_price,
                action_label=action_label,
                update_ui=update_ui,
                min_profit_pct=min_profit_pct,
            )
            if changed:
                self._log_pilot_auto_action(action="guard_autofix", reason="thin_edge")
        return "OK"

    def _evaluate_tp_profitability(self, tp_pct: float) -> dict[str, float | bool]:
        profile = get_profile_preset(self._profit_profile_name())
        inputs = self._runtime_profit_inputs()
        maker, taker = self._trade_fees
        if self._fee_source == "OVERRIDE":
            maker_fee_pct = 0.0
            taker_fee_pct = 0.0
        else:
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
        min_profit_bps = max(self.pilot_config.min_profit_bps, 0.0)
        return evaluate_tp_min_profit_bps(entry_price, tp_price, entry_side, min_profit_bps)

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

    def _place_replacement_order(
        self,
        fill: TradeFill,
        *,
        delta_qty: Decimal,
        total_qty: Decimal | None = None,
        fill_client_order_id: str | None = None,
    ) -> None:
        if not self._account_client or not self._bot_session_id:
            return
        if should_block_new_orders(self._state):
            self._append_log("[TP] blocked: stopping", kind="WARN")
            return
        if self._state not in {"RUNNING", "WAITING_FILLS"}:
            return
        settings = self._live_settings or self._settings_state
        is_micro = self._is_micro_profile()
        order_type = self._order_registry_type_from_client_id(fill_client_order_id or "")
        if is_micro and order_type == "TP":
            plan, guard_hold, guard_reason = self._build_spread_capture_plan(settings, action_label="tp_reentry")
            if guard_hold:
                self._append_log(f"[TP] reentry hold reason={guard_reason}", kind="INFO")
                return
            if not plan:
                self._append_log("[TP] reentry skipped: empty plan", kind="WARN")
                return
            self._place_live_orders(plan)
            return
        if not is_micro:
            tp_pct = settings.take_profit_pct or settings.grid_step_pct
            if tp_pct <= 0:
                return
        self._rebuild_registry_from_open_orders(self._open_orders)
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        fill_price = self.as_decimal(fill.price)
        fill_qty = delta_qty
        tp_target_qty = total_qty if (fill.side == "BUY" and total_qty is not None) else fill_qty
        balances_snapshot = self._balance_snapshot()
        base_free = self.as_decimal(balances_snapshot.get("base_free", Decimal("0")))
        quote_free = self.as_decimal(balances_snapshot.get("quote_free", Decimal("0")))
        min_notional = self._rule_decimal(self._exchange_rules.get("min_notional"))
        tp_side = "SELL" if fill.side == "BUY" else "BUY"
        tp_qty = Decimal("0")
        tp_notional = Decimal("0")
        tp_reason = "ok"
        allow_tp = True
        if tp_side == "SELL" and self._pilot_state == PilotState.ACCUMULATE_BASE:
            tp_reason = "skip_accumulate_base"
            allow_tp = False
        tp_min_profit_detail = ""
        tp_qty_cap = Decimal("0")
        tp_required_qty = tp_target_qty
        tp_required_quote = Decimal("0")
        if is_micro:
            if tick is None or tick <= 0:
                return
            spread_ticks = self._micro_spread_capture_context().get("spread_ticks")
            tp_ticks = self._micro_tp_ticks(spread_ticks if isinstance(spread_ticks, int) else None)
            tick_move = tick * Decimal(tp_ticks)
            if tp_side == "SELL":
                tp_price = self.q_price(fill_price + tick_move, tick)
            else:
                tp_price = self.q_price(fill_price - tick_move, tick)
        else:
            tp_dec = self.as_decimal(settings.take_profit_pct or settings.grid_step_pct) / Decimal("100")
            if tp_side == "SELL":
                tp_price = self.q_price(fill_price * (Decimal("1") + tp_dec), tick)
            else:
                tp_price = self.q_price(fill_price * (Decimal("1") - tp_dec), tick)
        if tp_side == "SELL":
            tp_required_qty = tp_target_qty
            if tp_required_qty > base_free:
                tp_reason = "skip_insufficient_base"
                allow_tp = False
            else:
                tp_qty_cap = min(tp_target_qty, base_free)
        else:
            tp_required_quote = tp_price * tp_target_qty
            if tp_required_quote > quote_free:
                tp_reason = "skip_insufficient_quote"
                allow_tp = False
            else:
                quote_cap = quote_free / tp_price if tp_price > 0 else Decimal("0")
                tp_qty_cap = min(tp_target_qty, quote_cap)
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
                        f"profit_bps={profitability.get('profit_bps', 0.0):.3f} "
                        f"profit_pct={profitability.get('profit_pct', 0.0):.5f}% "
                        f"min_profit_bps={profitability.get('min_profit_bps', 0.0):.3f} "
                        f"min_profit_pct={profitability.get('min_profit_pct', 0.0):.5f}%"
                    )
                allow_tp = False
        if allow_tp and (tp_price <= 0 or tp_qty_cap <= 0):
            tp_reason = "skip_unknown"
            allow_tp = False
        intended_tp_price = tp_price
        intended_tp_qty = tp_qty_cap
        tp_replace_order_id = ""
        if allow_tp and tp_client_order_id in self._pending_tp_ids:
            tp_reason = "skip_pending_tp"
            allow_tp = False
        if allow_tp:
            existing_tp = self._find_open_order_by_client_id(tp_client_order_id)
            tp_replace_order_id = str(existing_tp.get("orderId", "")) if existing_tp else ""
            if tp_replace_order_id:
                tp_reason = "replace"
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

        allow_restore = not is_micro
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
        if restore_side == "SELL" and self._pilot_state == PilotState.ACCUMULATE_BASE:
            restore_reason = "skip_accumulate_base"
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
        allow_restore = allow_restore and restore_decision == "ok" and restore_qty > 0
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
            price_str = self.fmt_price(restore_price, tick)
            qty_str = self.fmt_qty(restore_qty, step)
            registry_key = self._order_registry_key(
                restore_side,
                price_str,
                qty_str,
                self._order_registry_type("restore"),
            )
            if registry_key in self._active_order_keys or registry_key in self._recent_order_keys:
                restore_reason = "skip_duplicate_restore"
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
        elif not is_micro:
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
            elif restore_reason == "skip_duplicate_restore":
                restore_key_detail = (
                    f"registry_key={self._order_registry_key(restore_side, self.fmt_price(restore_price, tick), self.fmt_qty(restore_qty, step), self._order_registry_type('restore'))} "
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
            allow_tp_local = allow_tp
            if allow_tp_local and tp_replace_order_id:
                ok, error, _outcome = self._cancel_order_idempotent(tp_replace_order_id)
                if not ok:
                    allow_tp_local = False
                    if error:
                        results["errors"].append(error)
            if allow_tp_local and tp_client_order_id and tp_qty > 0:
                self._pending_tp_ids.add(tp_client_order_id)
                tp_response, tp_error, tp_status = self._place_limit(
                    tp_side,
                    tp_price,
                    tp_qty,
                    tp_client_order_id,
                    reason="tp",
                    skip_open_order_duplicate=True,
                    skip_registry=False,
                    allow_existing_client_id=bool(tp_replace_order_id),
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

        try:
            result = _place()
            self._handle_replacement_order(result, 0)
        except Exception as exc:  # noqa: BLE001
            self._handle_live_order_error(str(exc))

    def _maybe_enable_bootstrap_sell_side(self, fill: TradeFill) -> None:
        if fill.side != "BUY":
            return
        if self._pilot_state == PilotState.ACCUMULATE_BASE:
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
        self._request_orders_refresh("place_tp_restore")

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
        allow_existing_client_id: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None, str]:
        if not self._account_client:
            return None, "[LIVE] place skipped: no account client", "skip_no_account"
        if should_block_new_orders(self._state):
            return None, "[LIVE] place skipped: stopping", "skip_stopping"
        blocked, reason = self._should_block_order_actions()
        if blocked:
            return None, f"[LIVE] place skipped: {reason}", "skip_bidask_stale"
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
        if client_id and self._has_open_order_client_id(client_id) and not allow_existing_client_id:
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
            response = self._place_limit_order_sync(
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
                    QTimer.singleShot(
                        0,
                        self,
                        lambda: self._safe_call(
                            lambda: self._request_orders_refresh("place_duplicate"),
                            label="timer:place_duplicate_refresh",
                        ),
                    )
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
            self._register_owned_order_id(order_id)
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
            remaining_qty = max_sell_qty_total - total_sell_qty
            if remaining_qty <= 0:
                stop_sells = True
                continue
            if order_qty > remaining_qty:
                order_qty = self.q_qty(remaining_qty, step)
            if order_qty <= 0:
                stop_sells = True
                continue
            total_sell_qty += order_qty
            order.qty = float(order_qty)
            trimmed.append(order)
        return trimmed

    @staticmethod
    def _rule_decimal(value: float | None) -> Decimal | None:
        if value is None:
            return None
        return NcPilotTabWidget.as_decimal(value)

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

    def _cancel_order_idempotent(self, order_id: str) -> tuple[bool, str | None, str]:
        if not order_id or not self._account_client:
            return False, None, "skipped"
        if order_id in self._closed_order_ids:
            return False, None, "skipped"
        try:
            self._cancel_order_sync(symbol=self._symbol, order_id=order_id)
        except Exception as exc:  # noqa: BLE001
            _status, code, _message, _response = self._parse_binance_exception(exc)
            if code == -2011:
                self._signals.log_append.emit("[CANCEL] unknown -> verifying on exchange", "INFO")
                present = False
                try:
                    open_orders = self._fetch_open_orders_sync(self._symbol)
                    present = any(
                        str(order.get("orderId", "")) == order_id
                        for order in open_orders
                        if isinstance(order, dict)
                    )
                except Exception as verify_exc:  # noqa: BLE001
                    return False, f"[CANCEL] verify failed: {verify_exc}", "failed"
                self._signals.log_append.emit(
                    f"[CANCEL] verify present={str(present).lower()}",
                    "INFO",
                )
                if present:
                    try:
                        self._cancel_open_orders_sync(self._symbol)
                    except Exception as cancel_exc:  # noqa: BLE001
                        return False, self._format_cancel_exception(cancel_exc, order_id), "failed"
                    try:
                        open_orders_after = self._fetch_open_orders_sync(self._symbol)
                        present_after = any(
                            str(order.get("orderId", "")) == order_id
                            for order in open_orders_after
                            if isinstance(order, dict)
                        )
                    except Exception as verify_exc:  # noqa: BLE001
                        return False, f"[CANCEL] verify failed: {verify_exc}", "failed"
                    if present_after:
                        return False, f"[CANCEL] verify present=true orderId={order_id}", "failed"
                self._pilot_pending_cancel_ids.add(order_id)
                self._closed_order_ids.add(order_id)
                self._discard_registry_for_order_id(order_id)
                self._open_orders_map.pop(order_id, None)
                self._order_info_map.pop(order_id, None)
                self._bot_order_ids.discard(order_id)
                self._owned_order_ids.discard(order_id)
                self._legacy_order_ids.discard(order_id)
                return True, None, "missing"
            return False, self._format_cancel_exception(exc, order_id), "failed"
        self._pilot_pending_cancel_ids.add(order_id)
        self._closed_order_ids.add(order_id)
        self._discard_registry_for_order_id(order_id)
        self._open_orders_map.pop(order_id, None)
        self._order_info_map.pop(order_id, None)
        self._bot_order_ids.discard(order_id)
        self._owned_order_ids.discard(order_id)
        self._legacy_order_ids.discard(order_id)
        return True, None, "canceled"

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
        self._queue_net_request(
            "exchange_info_symbol",
            {"symbol": self._symbol},
            on_success=self._handle_exchange_info,
            on_error=self._handle_exchange_error,
        )

    def _sync_account_time(self) -> None:
        if not self._account_client:
            return
        self._queue_net_request(
            "sync_time_offset",
            {},
            on_success=self._handle_time_sync,
            on_error=self._handle_time_sync_error,
        )

    def _handle_time_sync(self, result: object, latency_ms: int) -> None:
        if not isinstance(result, dict):
            return
        offset = result.get("offset_ms")
        if isinstance(offset, int):
            self._append_log(f"[LIVE] time sync offset={offset}ms ({latency_ms}ms)", kind="INFO")

    def _handle_time_sync_error(self, message: object) -> None:
        reason = self._format_account_error(message)
        self._append_log(f"[LIVE] time sync failed: {reason}", kind="WARN")

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

    def _handle_exchange_error(self, message: object) -> None:
        self._rules_in_flight = False
        self._symbol_tradeable = False
        reason = self._format_account_error(message)
        self._append_log(f"Exchange rules error: {reason}", kind="ERROR")
        self._auto_pause_on_api_error(reason)
        self._auto_pause_on_exception(reason)
        self._apply_trade_gate()
        self._update_rules_label()

    def _refresh_trade_fees(self, force: bool = False) -> None:
        if not self._account_client or self._fees_in_flight:
            return
        now = time_fn()
        if not force and self._fees_last_fetch_ts and now - self._fees_last_fetch_ts < 1800:
            return
        self._fees_in_flight = True
        self._queue_net_request(
            "trade_fees",
            {"symbol": self._symbol},
            on_success=self._handle_trade_fees,
            on_error=self._handle_trade_fees_error,
        )

    def _handle_trade_fees(self, result: object, latency_ms: int) -> None:
        self._fees_in_flight = False
        if not isinstance(result, list):
            self._handle_trade_fees_error("Unexpected trade fee response")
            return
        entry = result[0] if result else {}
        if isinstance(entry, dict):
            maker = self._coerce_float(str(entry.get("makerCommission", "")))
            taker = self._coerce_float(str(entry.get("takerCommission", "")))
            if maker is None or taker is None:
                self._update_fee_state(None, None, "DEFAULT_UNVERIFIED")
            else:
                self._update_fee_state(maker, taker, "ACCOUNT")
        self._fees_last_fetch_ts = time_fn()
        self._update_rules_label()
        self._append_log(f"Trade fees loaded ({latency_ms}ms).", kind="INFO")

    def _handle_trade_fees_error(self, message: object) -> None:
        self._fees_in_flight = False
        reason = self._format_account_error(message)
        self._append_log(f"Trade fees error: {reason}", kind="ERROR")
        self._auto_pause_on_api_error(reason)
        self._auto_pause_on_exception(reason)
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
        self._update_fee_state(maker / 10_000, taker / 10_000, "ACCOUNT")
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
        self._dry_run_toggle.setEnabled(True)
        self._apply_trade_status_label()
        self._update_grid_preview()
        self._update_fills_timer()

    def _apply_trade_status_label(self) -> None:
        if self._trade_gate != TradeGate.TRADE_OK:
            reason = self._trade_gate_reason()
            if self._dry_run_toggle.isChecked():
                self._trade_status_label.setText(f"LIVE unavailable: {reason} (running DRY-RUN)")
                self._trade_status_label.setStyleSheet(
                    "color: #f97316; font-size: 11px; font-weight: 600;"
                )
                self._trade_status_label.setToolTip(f"LIVE unavailable: {reason} (running DRY-RUN)")
            else:
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

    def _set_account_status(self, status: str, *, detail: str | None = None, kept_last: bool = False) -> None:
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
            if kept_last:
                detail_text = f": {detail}" if detail else ""
                text = f"ACCOUNT: ERROR (kept last){detail_text}"
            else:
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
        fee_text = self._fee_display_text()
        rules = (
            f"tick {tick_text} | step {step_text}"
            f" | minQty {min_qty_text} | maxQty {max_qty_text}"
            f" | minNotional {min_text} | maker/taker {fee_text}"
        )
        self._set_rules_label_text(tr("rules_line", rules=rules))
        self._market_fee.setText(fee_text)
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
        self._logger.info("[NC_PILOT] aboutToQuit received")
        self._write_crash_log_line(f"[NC_PILOT] aboutToQuit received ts={datetime.now().isoformat()}")
        if not self._stop_in_progress and not self._close_pending:
            self._close_pending = True
            self._handle_stop(reason="close")

    def _toggle_logs_panel(self) -> None:
        if not hasattr(self, "_right_splitter"):
            return
        sizes = self._right_splitter.sizes()
        if len(sizes) < 3:
            return
        if sizes[1] == 0:
            restored = getattr(self, "_logs_splitter_sizes", None)
            if not restored or len(restored) < 3:
                restored = [520, 280, 240]
            self._right_splitter.setSizes(restored)
        else:
            self._logs_splitter_sizes = sizes
            self._right_splitter.setSizes([max(1, sizes[0] + sizes[1]), 0, sizes[2]])

    @staticmethod
    def _extract_filter_value(filters: object, filter_type: str, key: str) -> float | None:
        if not isinstance(filters, list):
            return None
        for entry in filters:
            if not isinstance(entry, dict):
                continue
            if entry.get("filterType") == filter_type:
                return NcPilotTabWidget._coerce_float(str(entry.get(key, "")))
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

    def _build_orders_snapshot(self, orders: list[dict[str, Any]]) -> OrdersSnapshot:
        now_ms = int(time_fn() * 1000)
        rows: list[OrderRow] = []
        fingerprint_parts: list[tuple[str, str, str, str, str]] = []
        merged_orders = list(orders)
        if self._pilot_virtual_orders:
            merged_orders.extend(self._pilot_virtual_orders)
        for order in merged_orders:
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("orderId", ""))
            side = str(order.get("side", "—"))
            price_raw = str(order.get("price", "—"))
            qty_raw = str(order.get("origQty", "—"))
            filled_raw = str(order.get("executedQty", "—"))
            status = str(order.get("status", ""))
            time_ms = order.get("time")
            age_text = self._format_age(time_ms, now_ms)
            rows.append(
                OrderRow(
                    order_id=order_id or "—",
                    side=side,
                    price=price_raw,
                    qty=qty_raw,
                    filled=filled_raw,
                    age=age_text,
                )
            )
            if order_id:
                fingerprint_parts.append((order_id, status, price_raw, qty_raw, filled_raw))
        fingerprint = str(hash(tuple(sorted(fingerprint_parts))))
        return OrdersSnapshot(orders=rows, fingerprint=fingerprint, ts=time_fn())

    def _set_orders_snapshot(self, snapshot: OrdersSnapshot, *, reason: str) -> None:
        if snapshot.fingerprint == self._last_orders_fingerprint:
            return
        self._last_orders_fingerprint = snapshot.fingerprint
        self._orders_snapshot = snapshot
        self._render_open_orders(snapshot.orders)
        self._append_log(
            f"[ORDERS][UI] updated rows={len(snapshot.orders)} reason={reason}",
            kind="INFO",
        )
        self._refresh_orders_metrics()

    def _render_open_orders(self, rows: list[OrderRow] | None = None) -> None:
        if rows is None:
            rows = self._orders_snapshot.orders if self._orders_snapshot else []
        self._orders_table.setRowCount(len(rows))
        for row, order in enumerate(rows):
            self._set_order_cell(row, 0, "", align=Qt.AlignLeft, user_role=order.order_id)
            self._set_order_cell(row, 1, order.side, align=Qt.AlignLeft, user_role=order.order_id)
            self._set_order_cell(row, 2, order.price, align=Qt.AlignRight)
            self._set_order_cell(row, 3, order.qty, align=Qt.AlignRight)
            self._set_order_cell(row, 4, order.filled, align=Qt.AlignRight)
            self._set_order_cell(row, 5, order.age, align=Qt.AlignRight)
            info = self._order_info_map.get(order.order_id)
            if info is not None:
                self._remember_order_created_ts(order.order_id, info.created_ts)
            elif order.order_id and order.order_id not in self._order_age_registry:
                self._remember_order_created_ts(order.order_id, time_fn())
            self._set_order_row_tooltip(row)

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
            self._remember_order_created_ts(order_id, time_fn())
            self._set_order_row_tooltip(row)
        self._refresh_orders_metrics()

    def _should_block_order_actions(self) -> tuple[bool, str]:
        block, reason = should_block_bidask_actions(self._feed_ok, self._bidask_state)
        if not block:
            self._bidask_block_reason = None
            return False, "ok"
        now = monotonic()
        should_log = False
        if self._bidask_block_log_ts is None or now - self._bidask_block_log_ts >= LOG_DEDUP_HEARTBEAT_SEC:
            should_log = True
        if reason != self._bidask_block_reason:
            should_log = True
        if should_log:
            self._append_log(
                f"[FEED] order actions blocked reason={reason}",
                kind="WARN",
            )
            self._bidask_block_log_ts = now
            self._bidask_block_reason = reason
        return True, reason

    def _place_live_orders(self, planned: list[GridPlannedOrder]) -> None:
        if not self._account_client:
            self._append_log("Live order placement skipped: no account client.", kind="WARN")
            return
        if should_block_new_orders(self._state):
            self._append_log("[LIVE] placement skipped: stopping", kind="WARN")
            return
        blocked, _reason = self._should_block_order_actions()
        if blocked:
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
                if self._has_open_order_side_price(order.side, self.as_decimal(order.price)):
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
                        last_open_orders = self._fetch_open_orders_sync(self._symbol)
                    except Exception:
                        last_open_orders = None
            return {"results": results, "errors": errors, "open_orders": last_open_orders}
        try:
            result = _place()
            self._handle_live_order_placement(result, 0)
        except Exception as exc:  # noqa: BLE001
            self._handle_live_order_error(str(exc))

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
                    self._remember_order_created_ts(order_id, time_fn())
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
            self._orders_refresh_reason = "after_place"
            self._handle_open_orders(open_orders, latency_ms)
            self._append_log(f"[LIVE] OPEN_ORDERS n={len(self._open_orders)}", kind="ORDERS")
        else:
            self._request_orders_refresh("place_orders")
        self._activate_stale_warmup()
        self._change_state("RUNNING")
        if self._pilot_pending_action == PilotAction.RECENTER:
            expected_min = self._pilot_recenter_expected_min or 0
            self._append_log(
                f"[INFO] [NC_PILOT] recenter placed place_n={len(results)} expected_min={expected_min}",
                kind="INFO",
            )
            self._pilot_wait_for_recenter_open_orders(expected_min)

    def _handle_live_order_error(self, message: str) -> None:
        self._append_log(f"[LIVE] order error: {message}", kind="WARN")
        if self._state == "PLACING_GRID":
            self._change_state("RUNNING")
        if self._pilot_pending_action is not None:
            self._set_pilot_action_in_progress(False)
            if self._pilot_pending_anchor is not None:
                self._pilot_pending_anchor = None
                self._pilot_recenter_expected_min = None
            self._pilot_pending_action = None
            self._pilot_pending_plan = None
        self._auto_pause_on_api_error(message)
        self._auto_pause_on_exception(message)

    def _cancel_live_orders(self, order_ids: list[str], *, reason: str) -> None:
        reason_label = reason.replace("cancel_", "")
        if not self._account_client:
            self._append_log(f"Cancel {reason_label}: no account client.", kind="WARN")
            return
        filtered_ids = [order_id for order_id in order_ids if order_id and order_id != "—"]
        if not filtered_ids:
            self._append_log(f"Cancel {reason_label}: no valid order IDs.", kind="WARN")
            return
        self._append_log(
            f"[ORDERS][CANCEL] start reason={reason} planned={len(filtered_ids)}",
            kind="INFO",
        )

        def _cancel() -> dict[str, Any]:
            responses: list[dict[str, Any]] = []
            errors: list[str] = []
            canceled = 0
            missing = 0
            failed = 0
            batch_size = 4
            for idx, order_id in enumerate(filtered_ids, start=1):
                ok, error, outcome = self._cancel_order_idempotent(order_id)
                if not ok and error:
                    self._sleep_ms(120)
                    ok_retry, error_retry, outcome_retry = self._cancel_order_idempotent(order_id)
                    if ok_retry:
                        ok = True
                        outcome = outcome_retry
                        error = None
                    else:
                        error = error_retry or error
                        outcome = outcome_retry
                if ok:
                    responses.append({"orderId": order_id})
                    if outcome == "missing":
                        missing += 1
                    else:
                        canceled += 1
                else:
                    failed += 1
                    if error:
                        errors.append(error)
                        self._signals.api_error.emit(error)
                if idx % batch_size == 0:
                    self._sleep_ms(250)
            return {
                "responses": responses,
                "errors": errors,
                "canceled": canceled,
                "missing": missing,
                "failed": failed,
                "planned": len(filtered_ids),
                "reason": reason,
            }
        try:
            result = _cancel()
            self._handle_cancel_selected_result(result, 0)
        except Exception as exc:  # noqa: BLE001
            self._handle_cancel_error(str(exc))

    def _handle_cancel_selected_result(self, result: object, latency_ms: int) -> None:
        if not isinstance(result, dict):
            self._handle_cancel_error("Unexpected cancel response")
            return
        responses = result.get("responses", [])
        for entry in responses if isinstance(responses, list) else []:
            order_id = str(entry.get("orderId", "—")) if isinstance(entry, dict) else "—"
            if isinstance(entry, dict):
                self._discard_order_key_from_order(entry)
                self._discard_registry_for_order(entry)
            self._append_log(f"[LIVE] cancel orderId={order_id}", kind="ORDERS")
        errors = result.get("errors", [])
        if isinstance(errors, list):
            for message in errors:
                if message:
                    self._append_log(str(message), kind="WARN")
        canceled = int(result.get("canceled", 0) or 0)
        missing = int(result.get("missing", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        planned = int(result.get("planned", 0) or 0)
        reason = str(result.get("reason", "cancel_selected"))
        self._append_log(
            f"[ORDERS][CANCEL] done reason={reason} planned={planned} canceled={canceled} missing={missing} failed={failed}",
            kind="INFO",
        )
        self._set_cancel_buttons_enabled(True)
        self._request_orders_refresh("cancel_selected")
        self._schedule_force_open_orders_refresh("cancel_selected")
        self._append_log("orders refreshed after cancel", kind="INFO")

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
        self._request_orders_refresh("cancel_all")
        self._schedule_force_open_orders_refresh("cancel_all")

    def _record_stop_error(self, message: str) -> None:
        if self._stop_in_progress:
            self._stop_last_error = message

    def _handle_cancel_error(self, message: str) -> None:
        self._record_stop_error(message)
        self._append_log(f"[LIVE] cancel failed: {message}", kind="WARN")
        self._set_cancel_buttons_enabled(True)
        self._request_orders_refresh("cancel_error")
        self._append_log("orders refreshed after cancel", kind="INFO")
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

    def _remember_order_created_ts(self, order_id: str, created_ts: float) -> None:
        if not order_id:
            return
        self._order_age_registry[order_id] = created_ts

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

    def _tick_order_ages(self) -> None:
        if self._closing:
            return
        self._check_inflight_watchdog()
        if not hasattr(self, "_orders_table"):
            return
        if self._orders_table.rowCount() == 0:
            if hasattr(self, "_pilot_orders_oldest_value"):
                self._update_pilot_orders_metrics()
            return
        now = time_fn()
        now_ms = int(now * 1000)
        for row in range(self._orders_table.rowCount()):
            order_id = self._order_id_for_row(row)
            if not order_id or order_id == "—":
                continue
            info = self._order_info_map.get(order_id)
            if info is not None:
                created_ts = info.created_ts
            else:
                created_ts = self._order_age_registry.get(order_id)
                if created_ts is None:
                    created_ts = now
                    self._order_age_registry[order_id] = created_ts
            age_text = self._format_age(int(created_ts * 1000), now_ms)
            age_item = self._orders_table.item(row, 5)
            if age_item and age_item.text() != age_text:
                age_item.setText(age_text)
        self._update_pilot_orders_metrics()

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
        if self._orders_table.rowCount() > 0 and not self._open_orders and self._account_client:
            self._schedule_force_open_orders_refresh("ui_drift")

    def _set_cancel_buttons_enabled(self, enabled: bool) -> None:
        if hasattr(self, "_cancel_selected_button"):
            self._cancel_selected_button.setEnabled(enabled)
        if hasattr(self, "_cancel_all_button"):
            self._cancel_all_button.setEnabled(enabled)

    def _mark_orders_cancelling(self, order_ids: list[str] | None, *, reason: str) -> int:
        if not hasattr(self, "_orders_table"):
            return 0
        ids_set = None if order_ids is None else {order_id for order_id in order_ids if order_id and order_id != "—"}
        removed_ids: set[str] = set()
        for row in reversed(range(self._orders_table.rowCount())):
            row_order_id = self._order_id_for_row(row)
            if ids_set is None or row_order_id in ids_set:
                removed_ids.add(row_order_id)
                self._orders_table.removeRow(row)
        if not removed_ids and ids_set:
            return 0
        if ids_set is None:
            self._set_open_orders([])
            self._set_open_orders_all([])
            self._set_open_orders_map({})
            self._order_info_map = {}
            self._order_age_registry = {}
        else:
            self._set_open_orders([
                order for order in self._open_orders if str(order.get("orderId", "")) not in removed_ids
            ])
            self._set_open_orders_all([
                order for order in self._open_orders_all if str(order.get("orderId", "")) not in removed_ids
            ])
            for order_id in removed_ids:
                self._open_orders_map.pop(order_id, None)
                self._order_info_map.pop(order_id, None)
                self._order_age_registry.pop(order_id, None)
        self._set_orders_snapshot(self._build_orders_snapshot(self._open_orders), reason=f"cancel_{reason}")
        return len(removed_ids)

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
            "WAITING FOR EDGE": "#d97706",
            "PAUSED": "#d97706",
            "STOPPING": "#f97316",
            "STOPPED": "#6b7280",
            "ERROR": "#dc2626",
        }
        color = color_map.get(state, "#6b7280")
        self._engine_state_label.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 600;"
        )

    def _set_engine_state(self, state: str) -> None:
        self._engine_state = state
        self._session.runtime.engine_state = state
        self._engine_state_label.setText(f"{tr('engine')}: {self._engine_state}")
        self._apply_engine_state_style(self._engine_state)
        self.update_ui_lock_state()

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

    def _append_log_throttled(
        self,
        message: str,
        *,
        kind: str,
        key: str,
        cooldown_sec: float,
    ) -> None:
        now = monotonic()
        last_log = self._log_throttle_ts.get(key)
        if last_log is not None and now - last_log < cooldown_sec:
            return
        self._log_throttle_ts[key] = now
        self._append_log(message, kind=kind)

    def _append_log(self, message: str, kind: str = "INFO") -> None:
        formatted = _format_session_log(self._symbol, message)
        entry = (kind.upper(), formatted)
        self._log_entries.append(entry)
        self._apply_log_filter()
        self._logger.info("%s | %s", entry[0], entry[1])

    def _maybe_emit_minute_summary(self, now_ts: float) -> None:
        if not self._session.should_emit_minute_summary(now_ts):
            return
        settings = self._settings_state
        market = self._session.market
        counters = self._session.counters
        health = self._market_health
        spread_bps = health.spread_bps if health else None
        edge_bps = health.expected_profit_bps if health else None
        spread_text = f"{spread_bps:.2f}" if spread_bps is not None else "—"
        edge_text = f"{edge_bps:.2f}" if edge_bps is not None else "—"
        age_text = f"{market.age_ms}" if market.age_ms is not None else "—"
        source = market.source or "—"
        quote_free = float(self._last_good_balance_snapshot().get("quote_free", 0.0))
        budget_used = max(settings.budget - quote_free, 0.0)
        self._append_log(
            (
                f"[1m][{self._symbol}] src={source} age_ms={age_text} "
                f"spread_bps={spread_text} kpi_edge_bps={edge_text} "
                f"engine={self._engine_state} open_orders={len(self._open_orders)} "
                f"fills={len(self._fills)} exec_dup={counters.exec_dup} "
                f"stale_skips={counters.stale_poll_skips} "
                f"kpi_updates={counters.kpi_updates} "
                f"budget={settings.budget:.2f} used={budget_used:.2f} free={quote_free:.2f}"
            ),
            kind="INFO",
        )
        counters.reset()

    def _notify_crash(self, source: str) -> None:
        if not self._crash_log_path:
            return
        if self._crash_notified:
            return
        self._crash_notified = True
        self._append_log(
            f"[NC_PILOT][CRASH] see crash log: {self._crash_log_path} (source={source})",
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
            "WAIT_EDGE": "WAITING FOR EDGE",
            "PAUSED": "PAUSED",
            "STOPPING": "STOPPING",
            "STOPPED": "STOPPED",
            "PLACING_GRID": "PLACING_GRID",
            "ERROR": "ERROR",
        }
        return mapping.get(state, state)

    @staticmethod
    def _state_display_text(state: str) -> str:
        if state == "WAIT_EDGE":
            return "WAITING FOR EDGE"
        return state

    def _profit_guard_hold(self, settings: GridSettingsState) -> bool:
        guard_mode = self._current_profit_guard_mode()
        if guard_mode != ProfitGuardMode.BLOCK:
            return False
        min_profit_pct = self._profit_guard_min_profit_pct(settings)
        decision, _spread_bps, _expected_bps, _fee_bps, _slip_bps, _pad_bps, _risk_bps = (
            self._profit_guard_should_hold(min_profit_pct * 100)
        )
        return decision == "HOLD"

    def _handle_wait_edge_tick(self) -> None:
        if self._state != "WAIT_EDGE":
            return
        if self._session.runtime.start_in_progress:
            return
        anchor_price = self.get_anchor_price(self._symbol)
        if anchor_price is None:
            return
        snapshot = self._collect_strategy_snapshot()
        settings = self._resolve_start_settings(snapshot)
        self._apply_grid_clamps(settings, anchor_price)
        if self._profit_guard_override_pending:
            self._append_log("[EDGE] ok -> placing orders", kind="INFO")
            self._handle_start()
            return
        if self._profit_guard_hold(settings):
            now = monotonic()
            if (
                self._wait_edge_last_log_ts is None
                or now - self._wait_edge_last_log_ts >= WAIT_EDGE_LOG_COOLDOWN_SEC
            ):
                self._append_log("[EDGE] still thin -> waiting", kind="INFO")
                self._wait_edge_last_log_ts = now
            return
        self._append_log("[EDGE] ok -> placing orders", kind="INFO")
        self._handle_start()

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
        tp_pct = float(self._take_profit_input.value())
        max_active = int(self._max_orders_input.value())
        self._grid_preview_label.setText(
            (
                f"Grid: budget {budget:.2f} {quote_ccy} | step {step:.2f}% | "
                f"range {range_low:.2f}-{range_high:.2f}% | TP {tp_pct:.2f}% | "
                f"max active {max_active} | est min/order {min_order:.2f} {quote_ccy}"
            )
        )
        self._update_auto_values_label(self._settings_state.grid_step_mode == "AUTO_ATR")

    def _setup_net_worker(self) -> None:
        self._net_worker = get_net_worker(timeout_s=self._config.http.timeout_s)

    def _shutdown_net_worker(self) -> None:
        parent = self.parent()
        if isinstance(parent, NcPilotMainWindow):
            if parent.has_active_tabs():
                return
        shutdown_net_worker()

    def _next_net_request_id(self, action: str) -> str:
        self._net_request_counter += 1
        return f"{action}:{self._net_request_counter}:{uuid4().hex}"

    def _queue_net_request(
        self,
        action: str,
        params: dict[str, Any],
        *,
        on_success: Callable[[object, int], None] | None,
        on_error: Callable[[object], None] | None,
    ) -> None:
        if self._disposed or self._closing:
            self._logger.info("[NC_PILOT] drop net request (closing) name=%s", action)
            return
        if not self._net_worker:
            if on_error:
                on_error(RuntimeError("net worker not initialized"))
            return
        request_id = self._next_net_request_id(action)
        self._net_pending[request_id] = (on_success, on_error)
        fn = self._build_net_request_fn(action, params)
        dedup_key = None
        if action == "book_ticker":
            symbol = str(params.get("symbol", ""))
            dedup_key = f"{action}:{symbol}"
        self._net_worker.submit(
            action,
            fn,
            on_ok=functools.partial(self._handle_net_success, request_id, action),
            on_err=functools.partial(self._handle_net_error, request_id, action),
            dedup_key=dedup_key,
        )

    def _build_net_request_fn(self, action: str, params: dict[str, Any]) -> Callable[[object], object]:
        def _require_account() -> BinanceAccountClient:
            if not self._account_client:
                raise RuntimeError("Account client not initialized (missing API keys)")
            return self._account_client

        def _require_http() -> BinanceHttpClient:
            if not self._http_client:
                raise RuntimeError("HTTP client not initialized")
            return self._http_client

        def _dispatch(_client: object) -> object:
            if action == "account_info":
                return _require_account().get_account_info()
            if action == "open_orders":
                symbol = str(params.get("symbol", ""))
                return _require_account().get_open_orders(symbol)
            if action == "trade_fees":
                symbol = str(params.get("symbol", ""))
                return _require_account().get_trade_fees(symbol)
            if action == "my_trades":
                symbol = str(params.get("symbol", ""))
                limit = int(params.get("limit", 50))
                return _require_account().get_my_trades(symbol, limit=limit)
            if action == "order":
                symbol = str(params.get("symbol", ""))
                order_id = params.get("order_id")
                orig_client_order_id = params.get("orig_client_order_id")
                return _require_account().get_order(
                    symbol,
                    order_id=str(order_id) if order_id is not None else None,
                    orig_client_order_id=str(orig_client_order_id) if orig_client_order_id else None,
                )
            if action == "place_limit_order":
                return _require_account().place_limit_order(
                    symbol=str(params.get("symbol", "")),
                    side=str(params.get("side", "")),
                    price=str(params.get("price", "")),
                    quantity=str(params.get("quantity", "")),
                    time_in_force=str(params.get("time_in_force", "GTC")),
                    new_client_order_id=params.get("new_client_order_id"),
                )
            if action == "cancel_order":
                return _require_account().cancel_order(
                    symbol=str(params.get("symbol", "")),
                    order_id=str(params.get("order_id")) if params.get("order_id") else None,
                    orig_client_order_id=params.get("orig_client_order_id"),
                )
            if action == "cancel_open_orders":
                symbol = str(params.get("symbol", ""))
                return _require_account().cancel_open_orders(symbol)
            if action == "sync_time_offset":
                return _require_account().sync_time_offset()
            if action == "exchange_info_symbol":
                symbol = str(params.get("symbol", ""))
                return _require_http().get_exchange_info_symbol(symbol)
            if action == "book_ticker":
                symbol = str(params.get("symbol", ""))
                return _require_http().get_book_ticker(symbol)
            if action == "orderbook_depth":
                symbol = str(params.get("symbol", ""))
                limit = int(params.get("limit", 50))
                return _require_http().get_orderbook_depth(symbol, limit=limit)
            if action == "klines":
                symbol = str(params.get("symbol", ""))
                interval = str(params.get("interval", "1h"))
                limit = int(params.get("limit", 120))
                return _require_http().get_klines(symbol, interval=interval, limit=limit)
            raise ValueError(f"Unknown net action: {action}")

        return _dispatch

    def _net_call_sync(
        self,
        action: str,
        params: dict[str, Any],
        *,
        timeout_ms: int = 5000,
    ) -> tuple[object, int]:
        loop = QEventLoop()
        result: dict[str, Any] = {"done": False, "value": None, "error": None, "latency": 0}

        def _on_success(payload: object, latency_ms: int) -> None:
            if result["done"]:
                return
            result["done"] = True
            result["value"] = payload
            result["latency"] = latency_ms
            loop.quit()

        def _on_error(message: object) -> None:
            if result["done"]:
                return
            result["done"] = True
            result["error"] = message
            loop.quit()

        self._queue_net_request(action, params, on_success=_on_success, on_error=_on_error)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        loop.exec()
        timer.stop()
        if not result["done"]:
            raise TimeoutError(f"net request timeout action={action}")
        if result["error"] is not None:
            if isinstance(result["error"], Exception):
                raise result["error"]
            raise RuntimeError(str(result["error"]))
        return result["value"], int(result["latency"] or 0)

    def _fetch_open_orders_sync(self, symbol: str) -> list[dict[str, Any]]:
        payload, _latency_ms = self._net_call_sync(
            "open_orders",
            {"symbol": symbol},
            timeout_ms=5000,
        )
        if not isinstance(payload, list):
            raise ValueError("Unexpected open orders response")
        return [item for item in payload if isinstance(item, dict)]

    def _fetch_my_trades_sync(self, symbol: str, *, limit: int = 50) -> list[dict[str, Any]]:
        payload, _latency_ms = self._net_call_sync(
            "my_trades",
            {"symbol": symbol, "limit": limit},
            timeout_ms=5000,
        )
        if not isinstance(payload, list):
            raise ValueError("Unexpected trades response")
        return [item for item in payload if isinstance(item, dict)]

    def _fetch_order_sync(
        self,
        symbol: str,
        *,
        order_id: str | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        payload, _latency_ms = self._net_call_sync(
            "order",
            {"symbol": symbol, "order_id": order_id, "orig_client_order_id": orig_client_order_id},
            timeout_ms=5000,
        )
        if not isinstance(payload, dict):
            raise ValueError("Unexpected order response")
        return payload

    def _place_limit_order_sync(
        self,
        *,
        symbol: str,
        side: str,
        price: str,
        quantity: str,
        time_in_force: str = "GTC",
        new_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        payload, _latency_ms = self._net_call_sync(
            "place_limit_order",
            {
                "symbol": symbol,
                "side": side,
                "price": price,
                "quantity": quantity,
                "time_in_force": time_in_force,
                "new_client_order_id": new_client_order_id,
            },
            timeout_ms=5000,
        )
        if not isinstance(payload, dict):
            raise ValueError("Unexpected order response format")
        return payload

    def _cancel_order_sync(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        payload, _latency_ms = self._net_call_sync(
            "cancel_order",
            {
                "symbol": symbol,
                "order_id": order_id,
                "orig_client_order_id": orig_client_order_id,
            },
            timeout_ms=5000,
        )
        if not isinstance(payload, dict):
            raise ValueError("Unexpected cancel order response")
        return payload

    def _cancel_open_orders_sync(self, symbol: str) -> list[dict[str, Any]]:
        payload, _latency_ms = self._net_call_sync(
            "cancel_open_orders",
            {"symbol": symbol},
            timeout_ms=5000,
        )
        if not isinstance(payload, list):
            raise ValueError("Unexpected cancel open orders response")
        return [item for item in payload if isinstance(item, dict)]

    def _handle_net_success(self, request_id: str, _action: str, payload: object, latency_ms: int) -> None:
        if not self._can_emit_worker_results():
            return
        handlers = self._net_pending.pop(request_id, None)
        if not handlers:
            return
        on_success, _on_error = handlers
        if on_success:
            on_success(payload, latency_ms)

    def _handle_net_error(self, request_id: str, _action: str, error: object) -> None:
        if not self._can_emit_worker_results():
            return
        handlers = self._net_pending.pop(request_id, None)
        if not handlers:
            return
        _on_success, on_error = handlers
        if on_error:
            on_error(error)

    def _can_emit_worker_results(self) -> bool:
        return not self._closing or self._stop_in_progress

    def _stop_local_timers(self) -> None:
        if self._market_kpi_timer.isActive():
            self._market_kpi_timer.stop()
        if self._pilot_ui_timer is not None and self._pilot_ui_timer.isActive():
            self._pilot_ui_timer.stop()
        if self._registry_gc_timer.isActive():
            self._registry_gc_timer.stop()
        if self._order_age_timer.isActive():
            self._order_age_timer.stop()

    def _stop_runtime_timers(self) -> None:
        if self._balances_timer.isActive():
            self._balances_timer.stop()
        if self._orders_timer.isActive():
            self._orders_timer.stop()
        if self._fills_timer.isActive():
            self._fills_timer.stop()
        if self._order_age_timer.isActive():
            self._order_age_timer.stop()
        self._stop_local_timers()
        if hasattr(self, "_pilot_controller"):
            self._pilot_controller.shutdown()

    def _resume_local_timers(self) -> None:
        if not self._market_kpi_timer.isActive():
            self._market_kpi_timer.start()
        if self._pilot_ui_timer is not None and not self._pilot_ui_timer.isActive():
            self._pilot_ui_timer.start()
        if not self._registry_gc_timer.isActive():
            self._registry_gc_timer.start()
        if not self._order_age_timer.isActive():
            self._order_age_timer.start()

    def _stop_price_feed(self, *, shutdown: bool) -> None:
        if not self._feed_active and not shutdown:
            return
        self._feed_active = False
        try:
            self._price_feed_manager.unsubscribe(self._symbol, self._emit_price_update)
            self._price_feed_manager.unsubscribe_status(self._symbol, self._emit_status_update)
            should_stop = not self._price_feed_manager.has_active_subscribers()
            if should_stop and shutdown and not self._price_feed_manager.is_shutting_down:
                self._price_feed_manager.shutdown()
            elif should_stop and not shutdown and not self._price_feed_manager.is_shutting_down:
                self._price_feed_manager.stop()
        except Exception:
            self._logger.exception("[NC_PILOT][CRASH] exception stopping price feed")
        self._append_log("[STOP] feed stopped", kind="INFO")

    def _start_price_feed_if_needed(self) -> None:
        if self._feed_active or self._price_feed_manager.is_shutting_down:
            return
        self._feed_active = True
        self._price_feed_manager.start()
        self._price_feed_manager.subscribe(self._symbol, self._emit_price_update)
        self._price_feed_manager.subscribe_status(self._symbol, self._emit_status_update)

    def _begin_shutdown(self, *, reason: str) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._closing = True
        self._stop_runtime_timers()
        self._stop_price_feed(shutdown=(reason == "close"))
        self._append_log(f"[STOP] begin reason={reason}", kind="INFO")

    def _complete_close_if_pending(self) -> None:
        if not self._close_pending or self._close_finalizing:
            return
        self._close_finalizing = True
        QTimer.singleShot(0, self, lambda: self._safe_call(self.close, label="timer:close_window"))

    def closeEvent(self, event: object) -> None:  # noqa: N802
        self._closing = True
        if self._close_finalizing:
            self._stop_runtime_timers()
            self._stop_price_feed(shutdown=True)
        elif not self._stop_in_progress:
            self._close_pending = True
            self._handle_stop(reason="close")
            try:
                event.ignore()
            except Exception:
                return
            return
        else:
            self._close_pending = True
            try:
                event.ignore()
            except Exception:
                return
        self._disposed = True
        self._append_log("Lite Grid Terminal closed.", kind="INFO")
        self._shutdown_net_worker()
        close_shared_client()
        super().closeEvent(event)

    def dump_settings(self) -> dict[str, Any]:
        return asdict(self._settings_state)


class NcPilotMainWindow(QMainWindow):
    def __init__(
        self,
        config: Config,
        app_state: AppState,
        price_feed_manager: PriceFeedManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._app_state = app_state
        self._price_feed_manager = price_feed_manager
        self._logger = get_logger("gui.nc_pilot_main_window")
        self._tabs_by_symbol: dict[str, NcPilotTabWidget] = {}
        self.setWindowTitle(f"NC PILOT {NC_PILOT_VERSION}")
        self.resize(1150, 760)

        self._tab_widget = QTabWidget(self)
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.tabCloseRequested.connect(self._close_tab)
        self._tab_widget.currentChanged.connect(self._handle_tab_changed)
        self.setCentralWidget(self._tab_widget)

    def add_or_activate_symbol(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        if not normalized:
            return
        existing = self._tabs_by_symbol.get(normalized)
        if existing is not None:
            index = self._tab_widget.indexOf(existing)
            if index >= 0:
                if self._tab_widget.currentIndex() != index:
                    self._tab_widget.setCurrentIndex(index)
                else:
                    existing.on_tab_activated()
                self._set_window_title(normalized)
            return
        tab = NcPilotTabWidget(
            symbol=normalized,
            config=self._config,
            app_state=self._app_state,
            price_feed_manager=self._price_feed_manager,
            parent=self,
        )
        self._tabs_by_symbol[normalized] = tab
        index = self._tab_widget.addTab(tab, normalized)
        self._tab_widget.setCurrentIndex(index)
        self._set_window_title(normalized)
        self._logger.info("[NC_PILOT][UI] tab_added symbol=%s index=%s", normalized, index)

    def has_active_tabs(self) -> bool:
        return self._tab_widget.count() > 0

    def _close_tab(self, index: int) -> None:
        widget = self._tab_widget.widget(index)
        if widget is None:
            return
        symbol = getattr(widget, "symbol", None)
        self._tab_widget.removeTab(index)
        if isinstance(widget, QWidget):
            widget.close()
            widget.deleteLater()
        if symbol:
            self._tabs_by_symbol.pop(symbol, None)
        self._logger.info("[NC_PILOT][UI] tab_closed symbol=%s index=%s", symbol, index)

    def _handle_tab_changed(self, index: int) -> None:
        widget = self._tab_widget.widget(index)
        if isinstance(widget, NcPilotTabWidget):
            widget.on_tab_activated()
            self._set_window_title(widget.symbol)

    def _set_window_title(self, symbol: str | None) -> None:
        if symbol:
            self.setWindowTitle(f"NC PILOT {NC_PILOT_VERSION} — {symbol}")
        else:
            self.setWindowTitle(f"NC PILOT {NC_PILOT_VERSION}")

    def closeEvent(self, event: object) -> None:  # noqa: N802
        for index in reversed(range(self._tab_widget.count())):
            widget = self._tab_widget.widget(index)
            if widget is not None:
                widget.close()
        shutdown_net_worker()
        super().closeEvent(event)
