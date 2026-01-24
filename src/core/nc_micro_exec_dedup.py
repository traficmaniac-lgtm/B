from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class TradeIdDeduper:
    max_size: int = 10_000
    ttl_sec: float = 4 * 60 * 60
    _trade_ids: OrderedDict[str, float] = field(default_factory=OrderedDict)

    def seen(self, trade_id: str, now: float) -> bool:
        if not trade_id:
            return False
        self._purge(now)
        if trade_id in self._trade_ids:
            self._trade_ids.move_to_end(trade_id)
            return True
        self._trade_ids[trade_id] = now
        self._trim()
        return False

    def clear(self) -> None:
        self._trade_ids.clear()

    def _purge(self, now: float) -> None:
        if not self._trade_ids:
            return
        ttl_cutoff = now - self.ttl_sec
        expired = [trade_id for trade_id, ts in self._trade_ids.items() if ts <= ttl_cutoff]
        for trade_id in expired:
            self._trade_ids.pop(trade_id, None)

    def _trim(self) -> None:
        while len(self._trade_ids) > self.max_size:
            self._trade_ids.popitem(last=False)


@dataclass
class CumulativeFillTracker:
    totals: dict[str, Decimal] = field(default_factory=dict)

    def update(self, order_id: str, cumulative_qty: Decimal) -> Decimal:
        if not order_id:
            return cumulative_qty
        previous = self.totals.get(order_id, Decimal("0"))
        if cumulative_qty <= previous:
            return Decimal("0")
        self.totals[order_id] = cumulative_qty
        return cumulative_qty - previous


def should_block_new_orders(state: str) -> bool:
    return state in {"STOPPING", "STOPPED"}
