from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


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
        algo_id: str
        route_text: str
        profit_abs_usdt: float
        profit_bps: float
        life_ms: int
        depth_ok: bool
        age_ok: bool
        suggested_action: str
        details: dict[str, Any]
        valid: bool = False
        reason: str = "—"

    def compute_snapshots(
        self,
        *,
        market_by_symbol: dict[str, MarketQuote],
        selected_symbols: set[str],
        min_edge_bps: float,
        max_notional: float,
        max_age_ms: int,
    ) -> list[RouteSnapshot]:
        return [
            self._algo1_usdt_usdc(
                market_by_symbol=market_by_symbol,
                selected_symbols=selected_symbols,
                min_edge_bps=min_edge_bps,
                max_notional=max_notional,
                max_age_ms=max_age_ms,
            ),
            self._algo2_usdt_tusd(
                market_by_symbol=market_by_symbol,
                selected_symbols=selected_symbols,
                min_edge_bps=min_edge_bps,
                max_notional=max_notional,
                max_age_ms=max_age_ms,
            ),
            self._algo3_euri_anchor(
                market_by_symbol=market_by_symbol,
                selected_symbols=selected_symbols,
                min_edge_bps=min_edge_bps,
                max_notional=max_notional,
                max_age_ms=max_age_ms,
            ),
        ]

    def _algo1_usdt_usdc(
        self,
        *,
        market_by_symbol: dict[str, MarketQuote],
        selected_symbols: set[str],
        min_edge_bps: float,
        max_notional: float,
        max_age_ms: int,
    ) -> RouteSnapshot:
        symbol = "USDTUSDC"
        if symbol not in selected_symbols:
            return self._disabled_snapshot("ALGO1", "USDT↔USDC", "pair_disabled")
        quote = market_by_symbol.get(symbol)
        bid, ask, age_ok = self._extract_bid_ask(quote, max_age_ms)
        effective_bid = self._effective_price(quote, side="SELL", max_notional=max_notional)
        effective_ask = self._effective_price(quote, side="BUY", max_notional=max_notional)
        details = self._build_details(
            symbol,
            bid,
            ask,
            effective_bid,
            effective_ask,
            max_notional=max_notional,
            price_age_ms=quote.age_ms if quote else None,
            src=quote.src if quote else "NONE",
        )
        if bid is None or ask is None:
            details["reason"] = "no_bidask"
            return self._invalid_snapshot("ALGO1", "USDT↔USDC", "no_bidask", details)
        price_bid = effective_bid if effective_bid is not None else bid
        price_ask = effective_ask if effective_ask is not None else ask
        profit_buy = (price_bid - 1.0) * 10000
        profit_sell = (1.0 - price_ask) * 10000
        if profit_buy >= profit_sell:
            route_text = "USDT→USDC"
            profit_bps = profit_buy if profit_buy != -float("inf") else 0.0
            depth_ok = effective_bid is not None
            suggested_action = "SELL USDTUSDC"
        else:
            route_text = "USDC→USDT"
            profit_bps = profit_sell if profit_sell != -float("inf") else 0.0
            depth_ok = effective_ask is not None
            suggested_action = "BUY USDTUSDC"
        if effective_bid is None and effective_ask is None:
            details["reason"] = "depth"
        return self._finalize_snapshot(
            "ALGO1",
            route_text,
            profit_bps,
            max_notional=max_notional,
            depth_ok=depth_ok,
            age_ok=age_ok,
            min_edge_bps=min_edge_bps,
            suggested_action=suggested_action,
            details=details,
        )

    def _algo2_usdt_tusd(
        self,
        *,
        market_by_symbol: dict[str, MarketQuote],
        selected_symbols: set[str],
        min_edge_bps: float,
        max_notional: float,
        max_age_ms: int,
    ) -> RouteSnapshot:
        symbol = "TUSDUSDT"
        if symbol not in selected_symbols:
            return self._disabled_snapshot("ALGO2", "USDT↔TUSD", "pair_disabled")
        quote = market_by_symbol.get(symbol)
        bid, ask, age_ok = self._extract_bid_ask(quote, max_age_ms)
        effective_bid = self._effective_price(quote, side="SELL", max_notional=max_notional)
        effective_ask = self._effective_price(quote, side="BUY", max_notional=max_notional)
        details = self._build_details(
            symbol,
            bid,
            ask,
            effective_bid,
            effective_ask,
            max_notional=max_notional,
            price_age_ms=quote.age_ms if quote else None,
            src=quote.src if quote else "NONE",
        )
        if bid is None or ask is None:
            details["reason"] = "no_bidask"
            return self._invalid_snapshot("ALGO2", "USDT↔TUSD", "no_bidask", details)
        price_bid = effective_bid if effective_bid is not None else bid
        price_ask = effective_ask if effective_ask is not None else ask
        profit_buy = (1.0 - price_ask) * 10000
        profit_sell = (price_bid - 1.0) * 10000
        if profit_sell >= profit_buy:
            route_text = "TUSD→USDT"
            profit_bps = profit_sell if profit_sell != -float("inf") else 0.0
            depth_ok = effective_bid is not None
            suggested_action = "SELL TUSDUSDT"
        else:
            route_text = "USDT→TUSD"
            profit_bps = profit_buy if profit_buy != -float("inf") else 0.0
            depth_ok = effective_ask is not None
            suggested_action = "BUY TUSDUSDT"
        if effective_bid is None and effective_ask is None:
            details["reason"] = "depth"
        return self._finalize_snapshot(
            "ALGO2",
            route_text,
            profit_bps,
            max_notional=max_notional,
            depth_ok=depth_ok,
            age_ok=age_ok,
            min_edge_bps=min_edge_bps,
            suggested_action=suggested_action,
            details=details,
        )

    def _algo3_euri_anchor(
        self,
        *,
        market_by_symbol: dict[str, MarketQuote],
        selected_symbols: set[str],
        min_edge_bps: float,
        max_notional: float,
        max_age_ms: int,
    ) -> RouteSnapshot:
        symbol_usdt = "EURIUSDT"
        symbol_eur = "EURIEUR"
        if symbol_usdt not in selected_symbols or symbol_eur not in selected_symbols:
            return self._disabled_snapshot("ALGO3", "EURI ANCHOR", "pair_disabled")
        quote_usdt = market_by_symbol.get(symbol_usdt)
        quote_eur = market_by_symbol.get(symbol_eur)
        bid_usdt, ask_usdt, age_ok_usdt = self._extract_bid_ask(quote_usdt, max_age_ms)
        bid_eur, ask_eur, age_ok_eur = self._extract_bid_ask(quote_eur, max_age_ms)
        effective_bid_usdt = self._effective_price(quote_usdt, side="SELL", max_notional=max_notional)
        effective_ask_usdt = self._effective_price(quote_usdt, side="BUY", max_notional=max_notional)
        effective_bid_eur = self._effective_price(quote_eur, side="SELL", max_notional=max_notional)
        effective_ask_eur = self._effective_price(quote_eur, side="BUY", max_notional=max_notional)
        if bid_usdt is None or ask_usdt is None or bid_eur is None or ask_eur is None:
            details = {
                "legs": [
                    self._build_leg_details(
                        symbol_usdt,
                        bid_usdt,
                        ask_usdt,
                        effective_bid_usdt,
                        effective_ask_usdt,
                        price_age_ms=quote_usdt.age_ms if quote_usdt else None,
                        src=quote_usdt.src if quote_usdt else "NONE",
                    ),
                    self._build_leg_details(
                        symbol_eur,
                        bid_eur,
                        ask_eur,
                        effective_bid_eur,
                        effective_ask_eur,
                        price_age_ms=quote_eur.age_ms if quote_eur else None,
                        src=quote_eur.src if quote_eur else "NONE",
                    ),
                ],
                "max_notional": max_notional,
                "reason": "no_bidask",
            }
            return self._invalid_snapshot("ALGO3", "EURI ANCHOR", "no_bidask", details)
        details = {
            "legs": [
                self._build_leg_details(
                    symbol_usdt,
                    bid_usdt,
                    ask_usdt,
                    effective_bid_usdt,
                    effective_ask_usdt,
                    price_age_ms=quote_usdt.age_ms if quote_usdt else None,
                    src=quote_usdt.src if quote_usdt else "NONE",
                ),
                self._build_leg_details(
                    symbol_eur,
                    bid_eur,
                    ask_eur,
                    effective_bid_eur,
                    effective_ask_eur,
                    price_age_ms=quote_eur.age_ms if quote_eur else None,
                    src=quote_eur.src if quote_eur else "NONE",
                ),
            ],
            "max_notional": max_notional,
        }
        depth_ok = True
        bid_usdt_calc = effective_bid_usdt if effective_bid_usdt is not None else bid_usdt
        ask_usdt_calc = effective_ask_usdt if effective_ask_usdt is not None else ask_usdt
        bid_eur_calc = effective_bid_eur if effective_bid_eur is not None else bid_eur
        ask_eur_calc = effective_ask_eur if effective_ask_eur is not None else ask_eur
        if (
            effective_bid_usdt is None
            or effective_ask_usdt is None
            or effective_bid_eur is None
            or effective_ask_eur is None
        ):
            depth_ok = False
            details["reason"] = "depth"
        mid_usdt = self._mid(bid_usdt, ask_usdt, bid_usdt_calc, ask_usdt_calc)
        if mid_usdt <= 0:
            details["reason"] = "no_mid"
            return self._invalid_snapshot("ALGO3", "EURI ANCHOR", "no_mid", details)
        eur_per_usdt = (1.0 / ask_usdt_calc) * bid_eur_calc
        usdt_equiv = eur_per_usdt * mid_usdt
        profit_route_1 = (usdt_equiv - 1.0) * 10000
        usdt_per_eur = (1.0 / ask_eur_calc) * bid_usdt_calc
        eur_equiv = usdt_per_eur / mid_usdt
        profit_route_2 = (eur_equiv - 1.0) * 10000
        if profit_route_1 >= profit_route_2:
            route_text = "USDT→EURI→EUR"
            profit_bps = profit_route_1
            suggested_action = "BUY EURIUSDT + SELL EURIEUR"
        else:
            route_text = "EUR→EURI→USDT"
            profit_bps = profit_route_2
            suggested_action = "BUY EURIEUR + SELL EURIUSDT"
        age_ok = bool(age_ok_usdt and age_ok_eur)
        return self._finalize_snapshot(
            "ALGO3",
            route_text,
            profit_bps,
            max_notional=max_notional,
            depth_ok=depth_ok,
            age_ok=age_ok,
            min_edge_bps=min_edge_bps,
            suggested_action=suggested_action,
            details=details,
        )

    @staticmethod
    def _disabled_snapshot(algo_id: str, label: str, reason: str) -> RouteSnapshot:
        return ArbHub.RouteSnapshot(
            algo_id=algo_id,
            route_text="—",
            profit_abs_usdt=0.0,
            profit_bps=0.0,
            life_ms=0,
            depth_ok=False,
            age_ok=False,
            suggested_action="—",
            details={"reason": reason},
            valid=False,
            reason=reason,
        )

    @staticmethod
    def _empty_snapshot(
        algo_id: str,
        label: str,
        reason: str,
        details: dict[str, Any],
        age_ok: bool,
    ) -> RouteSnapshot:
        return ArbHub.RouteSnapshot(
            algo_id=algo_id,
            route_text="—",
            profit_abs_usdt=0.0,
            profit_bps=0.0,
            life_ms=0,
            depth_ok=False,
            age_ok=age_ok,
            suggested_action="—",
            details=details,
            valid=False,
            reason=reason,
        )

    @staticmethod
    def _invalid_snapshot(
        algo_id: str,
        label: str,
        reason: str,
        details: dict[str, Any],
    ) -> RouteSnapshot:
        return ArbHub.RouteSnapshot(
            algo_id=algo_id,
            route_text="—",
            profit_abs_usdt=0.0,
            profit_bps=0.0,
            life_ms=0,
            depth_ok=False,
            age_ok=False,
            suggested_action="—",
            details=details,
            valid=False,
            reason=reason,
        )

    @staticmethod
    def _finalize_snapshot(
        algo_id: str,
        route_text: str,
        profit_bps: float,
        *,
        max_notional: float,
        depth_ok: bool,
        age_ok: bool,
        min_edge_bps: float,
        suggested_action: str,
        details: dict[str, Any],
    ) -> RouteSnapshot:
        profit_abs = max_notional * profit_bps / 10000
        valid = bool(depth_ok and age_ok and profit_bps >= min_edge_bps)
        if valid:
            reason = "ok"
        elif not depth_ok:
            reason = "depth"
        elif not age_ok:
            reason = "age"
        else:
            reason = "edge"
        if not valid:
            details["reason"] = reason
        return ArbHub.RouteSnapshot(
            algo_id=algo_id,
            route_text=route_text,
            profit_abs_usdt=profit_abs,
            profit_bps=profit_bps,
            life_ms=0,
            depth_ok=depth_ok,
            age_ok=age_ok,
            suggested_action=suggested_action,
            details=details,
            valid=valid,
            reason=reason,
        )

    @staticmethod
    def _extract_bid_ask(
        quote: MarketQuote | None,
        max_age_ms: int,
    ) -> tuple[float | None, float | None, bool]:
        if not quote or quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask <= 0:
            return None, None, False
        age_ok = quote.age_ms is not None and quote.age_ms <= max_age_ms
        return quote.bid, quote.ask, age_ok

    @staticmethod
    def _mid(
        bid: float | None,
        ask: float | None,
        effective_bid: float | None,
        effective_ask: float | None,
    ) -> float:
        if bid and ask:
            return (bid + ask) / 2
        if effective_bid and effective_ask:
            return (effective_bid + effective_ask) / 2
        return 0.0

    @staticmethod
    def _effective_price(
        quote: MarketQuote | None,
        *,
        side: str,
        max_notional: float,
    ) -> float | None:
        if (
            not quote
            or quote.bid is None
            or quote.ask is None
            or not isinstance(quote.depth, dict)
        ):
            return None
        levels = quote.depth.get("asks" if side == "BUY" else "bids") or []
        total_quote = 0.0
        total_base = 0.0
        remaining = max_notional
        for item in levels:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                price = float(item[0])
                qty = float(item[1])
            except (TypeError, ValueError):
                continue
            if price <= 0 or qty <= 0:
                continue
            level_quote = price * qty
            if remaining <= 0:
                break
            if level_quote >= remaining:
                fill_qty = remaining / price
                total_base += fill_qty
                total_quote += remaining
                remaining = 0.0
                break
            total_base += qty
            total_quote += level_quote
            remaining -= level_quote
        if remaining > 0 or total_base <= 0:
            return None
        return total_quote / total_base

    @staticmethod
    def _build_details(
        symbol: str,
        bid: float | None,
        ask: float | None,
        effective_bid: float | None,
        effective_ask: float | None,
        *,
        max_notional: float,
        price_age_ms: int | None,
        src: str,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "effective_bid": effective_bid,
            "effective_ask": effective_ask,
            "max_notional": max_notional,
            "price_age_ms": price_age_ms,
            "src": src,
        }

    @staticmethod
    def _build_leg_details(
        symbol: str,
        bid: float | None,
        ask: float | None,
        effective_bid: float | None,
        effective_ask: float | None,
        *,
        price_age_ms: int | None,
        src: str,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "effective_bid": effective_bid,
            "effective_ask": effective_ask,
            "price_age_ms": price_age_ms,
            "src": src,
        }
