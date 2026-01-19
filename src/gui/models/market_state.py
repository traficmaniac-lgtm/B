from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MarketState:
    zero_fee_symbols: set[str] = field(default_factory=set)
