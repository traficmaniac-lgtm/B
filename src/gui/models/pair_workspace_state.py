from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PairWorkspaceState:
    symbol: str
    plan_levels: list[dict[str, str]] = field(default_factory=list)
    open_orders: list[dict[str, str]] = field(default_factory=list)
    plan_source: str = "none"
    demo_source_symbol: str | None = None
