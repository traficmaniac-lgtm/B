from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal


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

    @dataclass
    class MarketQuote:
        bid: float | None
        ask: float | None
        age_ms: int | None
        ts_ms: int | None
        src: str
        depth: dict[str, Any] | None

    @dataclass
    class RouteSnapshot:
        family: Literal["SPREAD", "REBAL", "2LEG", "LOOP"]
        algo_id: str
        route_text: str
        symbol: str | None
        profit_abs_usdt: float
        profit_bps: float
        life_ms: int
        valid: bool
        reason_invalid: str | None
        age_ms: int | None
        depth_ok: bool
        suggested_action: str
        details: dict[str, Any]

    def compute_spread_family_best(
        self,
        *,
        market_by_symbol: dict[str, MarketQuote],
        selected_symbols: set[str],
        max_notional: float,
        min_edge_bps: float,
        max_age_ms: int,
    ) -> RouteSnapshot:
        candidates: list[ArbHub.RouteSnapshot] = []
        for symbol in sorted(selected_symbols):
            quote = market_by_symbol.get(symbol)
            snapshot = self._compute_spread_snapshot(
                symbol=symbol,
                quote=quote,
                max_notional=max_notional,
                min_edge_bps=min_edge_bps,
                max_age_ms=max_age_ms,
            )
            if snapshot.valid:
                candidates.append(snapshot)
        if not candidates:
            return self._invalid_family_snapshot(
                family="SPREAD",
                algo_id="SPREAD_BEST",
                reason="no_window",
                details={"reason": "no_window"},
            )
        return max(
            candidates,
            key=lambda snap: (snap.profit_abs_usdt, snap.profit_bps),
        )

    @staticmethod
    def _compute_spread_snapshot(
        *,
        symbol: str,
        quote: MarketQuote | None,
        max_notional: float,
        min_edge_bps: float,
        max_age_ms: int,
    ) -> RouteSnapshot:
        bid = quote.bid if quote else None
        ask = quote.ask if quote else None
        age_ms = quote.age_ms if quote else None
        age_ok = age_ms is not None and age_ms <= max_age_ms
        details: dict[str, Any] = {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "mid": None,
            "edge_bps": None,
            "age_ms": age_ms,
            "min_edge_bps": min_edge_bps,
            "max_notional": max_notional,
        }
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return ArbHub.RouteSnapshot(
                family="SPREAD",
                algo_id="SPREAD_BEST",
                route_text=f"{symbol} spread-capture",
                symbol=symbol,
                profit_abs_usdt=0.0,
                profit_bps=0.0,
                life_ms=0,
                valid=False,
                reason_invalid="no_data",
                age_ms=age_ms,
                depth_ok=False,
                suggested_action="BUY @bid+tick then SELL @ask-tick",
                details=details,
            )
        mid = (ask + bid) / 2
        edge_bps = (ask - bid) / mid * 10000 if mid else 0.0
        details["mid"] = mid
        details["edge_bps"] = edge_bps
        profit_abs_usdt = max_notional * (edge_bps / 10000)
        reason_invalid = None
        if not age_ok:
            reason_invalid = "age"
        elif edge_bps < min_edge_bps:
            reason_invalid = "edge"
        valid = reason_invalid is None
        return ArbHub.RouteSnapshot(
            family="SPREAD",
            algo_id="SPREAD_BEST",
            route_text=f"{symbol} spread-capture",
            symbol=symbol,
            profit_abs_usdt=profit_abs_usdt if valid else 0.0,
            profit_bps=edge_bps if valid else edge_bps,
            life_ms=0,
            valid=valid,
            reason_invalid=reason_invalid,
            age_ms=age_ms,
            depth_ok=True,
            suggested_action="BUY @bid+tick then SELL @ask-tick",
            details=details,
        )

    @staticmethod
    def _invalid_family_snapshot(
        *,
        family: Literal["SPREAD", "REBAL", "2LEG", "LOOP"],
        algo_id: str,
        reason: str,
        details: dict[str, Any],
    ) -> RouteSnapshot:
        return ArbHub.RouteSnapshot(
            family=family,
            algo_id=algo_id,
            route_text="—",
            symbol=None,
            profit_abs_usdt=0.0,
            profit_bps=0.0,
            life_ms=0,
            valid=False,
            reason_invalid=reason,
            age_ms=None,
            depth_ok=False,
            suggested_action="—",
            details=details,
        )
