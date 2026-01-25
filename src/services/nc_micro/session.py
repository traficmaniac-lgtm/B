from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
class NcMicroMinuteCounters:
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
class NcMicroRuntimeState:
    engine_state: str = "WAITING"
    stop_inflight: bool = False
    start_in_progress: bool = False
    last_poll_ts: float | None = None
    last_ui_update_ts: float | None = None
    next_summary_ts: float | None = None
    last_summary_ts: float | None = None


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
class NcMicroSession:
    symbol: str
    config: GridSettingsState = field(default_factory=GridSettingsState)
    runtime: NcMicroRuntimeState = field(default_factory=NcMicroRuntimeState)
    market: MarketDataCache = field(default_factory=MarketDataCache)
    order_tracking: OrderTrackingState = field(default_factory=OrderTrackingState)
    counters: NcMicroMinuteCounters = field(default_factory=NcMicroMinuteCounters)

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
