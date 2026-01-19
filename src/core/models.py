from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class Pair:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str = "TRADING"
    filters: Mapping[str, object] = field(default_factory=dict)
    tick_size: str | None = None
    step_size: str | None = None


@dataclass(frozen=True)
class PriceTick:
    symbol: str
    price: float
    timestamp_ms: int
    source: str = "WS"


@dataclass(frozen=True)
class ExchangeInfo:
    server_time_ms: int
    symbols: list[Pair]
