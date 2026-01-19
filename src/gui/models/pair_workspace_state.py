from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PairWorkspaceState:
    symbol: str
    plan_levels: list[dict[str, str]] = field(default_factory=list)
    open_orders: list[dict[str, str]] = field(default_factory=list)
    recent_fills: list[dict[str, str]] = field(default_factory=list)
    plan_source: str = "none"
    demo_source_symbol: str | None = None
    position_status: str = "FLAT"
    position_qty: float = 0.0
    entry_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    error_count: int = 0
