from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PilotBestRoute:
    side: str | None
    edge_bps: float | None
    spread_bps: float | None
    route: str | None
    reason: str
    needs_prep: bool
    prep_side: str | None


class ArbHub:
    MIN_EDGE_BPS = 0.2

    def compute_best(
        self,
        *,
        bid: float | None,
        ask: float | None,
        base_free: Decimal,
        quote_free: Decimal,
        min_edge_bps: float | None = None,
    ) -> PilotBestRoute:
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask <= bid:
            return PilotBestRoute(
                side=None,
                edge_bps=None,
                spread_bps=None,
                route=None,
                reason="no_bidask",
                needs_prep=False,
                prep_side=None,
            )
        mid = (ask + bid) / 2
        spread_bps = (ask - bid) / mid * 10000 if mid else 0.0
        edge_threshold = self.MIN_EDGE_BPS if min_edge_bps is None else min_edge_bps
        if spread_bps < edge_threshold:
            return PilotBestRoute(
                side=None,
                edge_bps=spread_bps,
                spread_bps=spread_bps,
                route=None,
                reason="no_edge",
                needs_prep=False,
                prep_side=None,
            )
        if quote_free > 0:
            return PilotBestRoute(
                side="BUY",
                edge_bps=spread_bps,
                spread_bps=spread_bps,
                route="BUY_SPREAD",
                reason="quote_balance",
                needs_prep=False,
                prep_side=None,
            )
        if base_free > 0:
            return PilotBestRoute(
                side="SELL",
                edge_bps=spread_bps,
                spread_bps=spread_bps,
                route="SELL_SPREAD",
                reason="base_balance",
                needs_prep=False,
                prep_side=None,
            )
        return PilotBestRoute(
            side=None,
            edge_bps=spread_bps,
            spread_bps=spread_bps,
            route=None,
            reason="no_balance",
            needs_prep=False,
            prep_side=None,
        )

    @staticmethod
    def balance_acquire_plan(side: str, *, base_free: Decimal, quote_free: Decimal) -> tuple[bool, str | None]:
        if side == "BUY" and quote_free <= 0 and base_free > 0:
            return True, "SELL"
        if side == "SELL" and base_free <= 0 and quote_free > 0:
            return True, "BUY"
        return False, None
