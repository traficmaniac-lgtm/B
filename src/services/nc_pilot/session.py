from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from src.core.nc_micro_exec_dedup import CumulativeFillTracker, ExecKeyDeduper, TradeIdDeduper


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


@dataclass
class MarketDataCache:
    bid: float | None = None
    ask: float | None = None
    last_price: float | None = None
    source: str | None = None
    age_ms: int | None = None


@dataclass
class NcPilotMinuteCounters:
    exec_dup: int = 0
    stale_poll_skips: int = 0
    kpi_updates: int = 0
    fills: int = 0

    def reset(self) -> None:
        self.exec_dup = 0
        self.stale_poll_skips = 0
        self.kpi_updates = 0
        self.fills = 0


@dataclass
class NcPilotRuntimeState:
    engine_state: str = "WAITING"
    stop_inflight: bool = False
    start_in_progress: bool = False
    last_poll_ts: float | None = None
    last_ui_update_ts: float | None = None
    next_summary_ts: float | None = None
    last_summary_ts: float | None = None


@dataclass
class PilotRuntimeCounters:
    windows_found_today: int = 0
    orders_sent: int = 0
    cancels: int = 0
    skips: int = 0


@dataclass
class PilotAlgoState:
    last_route_text: str | None = None
    last_valid: bool = False
    last_profit_bps: float | None = None
    last_suggested_action: str | None = None
    life_ms: int = 0
    last_ts: float | None = None


PILOT_DEFAULT_SYMBOLS = ("EURIUSDT", "EURIEUR", "USDTUSDC", "TUSDUSDT")


@dataclass
class PilotRuntimeState:
    state: Literal["IDLE", "ANALYZE", "TRADING", "ERROR"] = "IDLE"
    analysis_on: bool = False
    trading_on: bool = False
    arb_id_active: str | None = None
    last_best_route: str | None = None
    last_best_edge_bps: float | None = None
    last_decision: str = "—"
    counters: PilotRuntimeCounters = field(default_factory=PilotRuntimeCounters)
    last_skip_log_ts: float | None = None
    last_route_log_ts: float | None = None
    last_exec_ts: float | None = None
    order_client_ids: set[str] = field(default_factory=set)
    selected_symbols: set[str] = field(default_factory=lambda: set(PILOT_DEFAULT_SYMBOLS))
    indicator_states: dict[str, bool] = field(default_factory=dict)
    indicator_tooltips: dict[str, str] = field(default_factory=dict)
    algo_states: dict[str, PilotAlgoState] = field(default_factory=dict)


@dataclass
class OrderTrackingState:
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    open_orders_all: list[dict[str, Any]] = field(default_factory=list)
    open_orders_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    fill_keys: set[str] = field(default_factory=set)
    trade_id_deduper: TradeIdDeduper = field(default_factory=TradeIdDeduper)
    exec_key_deduper: ExecKeyDeduper = field(default_factory=ExecKeyDeduper)
    fill_exec_cumulative: CumulativeFillTracker = field(default_factory=CumulativeFillTracker)


@dataclass
class NcPilotSession:
    symbol: str
    config: GridSettingsState = field(default_factory=GridSettingsState)
    runtime: NcPilotRuntimeState = field(default_factory=NcPilotRuntimeState)
    market: MarketDataCache = field(default_factory=MarketDataCache)
    order_tracking: OrderTrackingState = field(default_factory=OrderTrackingState)
    counters: NcPilotMinuteCounters = field(default_factory=NcPilotMinuteCounters)
    pilot: PilotRuntimeState = field(default_factory=PilotRuntimeState)

    def should_emit_minute_summary(self, now: float, interval_s: float = 60.0) -> bool:
        if self.runtime.next_summary_ts is None:
            self.runtime.next_summary_ts = now + interval_s
            return False
        if now < self.runtime.next_summary_ts:
            return False
        while now >= self.runtime.next_summary_ts:
            self.runtime.next_summary_ts += interval_s
        self.runtime.last_summary_ts = now
        return True
