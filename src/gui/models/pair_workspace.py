from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DataPackSummary:
    symbol: str
    period: str
    quality: str
    candles_count: int
    last_price: float
    volatility_est: float


@dataclass
class DataRequest:
    reason: str
    required_candles: int
    target_period: str
    target_quality: str


@dataclass
class StrategyPlan:
    budget: float
    mode: str
    grid_count: int
    grid_step_pct: float
    range_low_pct: float
    range_high_pct: float
    refresh_interval_s: int
    notes: str = ""


@dataclass
class StrategyPatch:
    budget: float | None = None
    mode: str | None = None
    grid_count: int | None = None
    grid_step_pct: float | None = None
    range_low_pct: float | None = None
    range_high_pct: float | None = None
    refresh_interval_s: int | None = None
    notes: str | None = None
