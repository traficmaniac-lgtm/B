from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Iterable, Literal


FamilyLiteral = Literal["SPREAD", "REBAL", "2LEG", "LOOP"]


@dataclass
class PilotWindow:
    family: FamilyLiteral
    route: str
    symbols: list[str]
    profit_abs: float
    profit_bps: float
    max_notional: float
    ts_start: float
    ts_last: float
    ttl_ms: int
    valid: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def life_s(self) -> float:
        if not self.valid or self.ts_start <= 0:
            return 0.0
        return max(self.ts_last - self.ts_start, 0.0)

    def is_alive(self, now: float | None = None) -> bool:
        if not self.valid:
            return False
        check_now = now if now is not None else monotonic()
        return (check_now - self.ts_last) * 1000 < self.ttl_ms


@dataclass
class PilotMarketQuote:
    bid: float | None
    ask: float | None
    age_ms: int | None
    ts_ms: int | None
    depth: dict[str, Any] | None = None


@dataclass
class PilotMarketSnapshot:
    quotes: dict[str, PilotMarketQuote]
    stale_ms: int

    @classmethod
    def from_market_data(
        cls,
        data: dict[str, dict[str, Any]],
        *,
        stale_ms: int,
    ) -> "PilotMarketSnapshot":
        quotes: dict[str, PilotMarketQuote] = {}
        for symbol, payload in data.items():
            quotes[symbol] = PilotMarketQuote(
                bid=payload.get("bid"),
                ask=payload.get("ask"),
                age_ms=payload.get("age_ms"),
                ts_ms=payload.get("ts_ms"),
                depth=payload.get("depth"),
            )
        return cls(quotes=quotes, stale_ms=stale_ms)

    def quote(self, symbol: str) -> PilotMarketQuote | None:
        return self.quotes.get(symbol)

    def is_stale(self, symbol: str) -> bool:
        quote = self.quotes.get(symbol)
        if not quote or quote.age_ms is None:
            return True
        return quote.age_ms > self.stale_ms


class BaseFamilyScanner:
    family: FamilyLiteral = "SPREAD"

    def scan(self, snapshot: PilotMarketSnapshot, balances: dict[str, float] | None = None) -> PilotWindow:
        raise NotImplementedError


def _apply_cost(amount: float, cost_bps: float) -> float:
    return amount * (1 - cost_bps / 10000)


def _depth_notional(depth: dict[str, Any] | None, mid: float) -> tuple[float | None, str | None]:
    if not depth:
        return None, "no_depth_cap"
    bids = depth.get("bids")
    asks = depth.get("asks")
    if not bids or not asks:
        return None, "no_depth_cap"
    try:
        bid_qty = float(bids[0][1])
        ask_qty = float(asks[0][1])
    except (TypeError, ValueError, IndexError):
        return None, "no_depth_cap"
    cap_qty = min(bid_qty, ask_qty)
    return cap_qty * mid, None


class SpreadFamilyScanner(BaseFamilyScanner):
    family: FamilyLiteral = "SPREAD"

    def __init__(
        self,
        *,
        symbols: Iterable[str],
        min_bps: float,
        fee_bps: float,
        slippage_bps: float,
        safety_bps: float,
        ttl_ms: int,
        max_notional_default: float,
    ) -> None:
        self._symbols = list(symbols)
        self._min_bps = min_bps
        self._fee_bps = fee_bps
        self._slippage_bps = slippage_bps
        self._safety_bps = safety_bps
        self._ttl_ms = ttl_ms
        self._max_notional_default = max_notional_default

    def scan(self, snapshot: PilotMarketSnapshot, balances: dict[str, float] | None = None) -> PilotWindow:
        now = monotonic()
        candidates: list[PilotWindow] = []
        reasons: list[str] = []
        details_top: list[dict[str, Any]] = []
        for symbol in self._symbols:
            quote = snapshot.quote(symbol)
            bid = quote.bid if quote else None
            ask = quote.ask if quote else None
            if quote is None or bid is None or ask is None:
                reasons.append("no_data")
                details_top.append({"symbol": symbol, "reason": "no_data"})
                continue
            if snapshot.is_stale(symbol):
                reasons.append("stale")
                details_top.append({"symbol": symbol, "reason": "stale", "age_ms": quote.age_ms})
                continue
            if bid <= 0 or ask <= 0 or ask <= bid:
                reasons.append("no_bidask")
                details_top.append({"symbol": symbol, "reason": "no_bidask", "bid": bid, "ask": ask})
                continue
            mid = (bid + ask) / 2
            spread_bps = (ask - bid) / mid * 10000 if mid else 0.0
            expected_bps = spread_bps - self._fee_bps - self._slippage_bps - self._safety_bps
            max_notional, cap_reason = _depth_notional(quote.depth, mid)
            if max_notional is None:
                max_notional = self._max_notional_default
            window = PilotWindow(
                family="SPREAD",
                route=f"{symbol} spread",
                symbols=[symbol],
                profit_abs=max_notional * (expected_bps / 10000) if expected_bps >= self._min_bps else 0.0,
                profit_bps=expected_bps,
                max_notional=max_notional,
                ts_start=now,
                ts_last=now,
                ttl_ms=self._ttl_ms,
                valid=expected_bps >= self._min_bps,
                reason="ok" if expected_bps >= self._min_bps else "no_window",
                details={
                    "symbol": symbol,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "spread_bps": spread_bps,
                    "expected_bps": expected_bps,
                    "age_ms": quote.age_ms,
                    "cap_reason": cap_reason or "ok",
                },
            )
            details_top.append(
                {
                    "symbol": symbol,
                    "expected_bps": expected_bps,
                    "spread_bps": spread_bps,
                    "valid": window.valid,
                    "age_ms": quote.age_ms,
                    "cap_reason": cap_reason or "ok",
                }
            )
            if window.valid:
                candidates.append(window)
        if candidates:
            best = max(candidates, key=lambda item: (item.profit_bps, item.profit_abs))
            best.details["top4"] = sorted(details_top, key=lambda entry: entry.get("expected_bps", -1), reverse=True)[:4]
            return best
        reason = "no_window"
        if reasons and all(r in {"no_data", "stale"} for r in reasons):
            reason = "stale" if "stale" in reasons else "no_data"
        return PilotWindow(
            family="SPREAD",
            route="—",
            symbols=[],
            profit_abs=0.0,
            profit_bps=0.0,
            max_notional=self._max_notional_default,
            ts_start=0.0,
            ts_last=now,
            ttl_ms=self._ttl_ms,
            valid=False,
            reason=reason,
            details={"top4": details_top, "reason": reason},
        )


class RebalFamilyScanner(BaseFamilyScanner):
    family: FamilyLiteral = "REBAL"

    def __init__(
        self,
        *,
        min_bps: float,
        fee_bps: float,
        slippage_bps: float,
        safety_bps: float,
        ttl_ms: int,
    ) -> None:
        self._min_bps = min_bps
        self._fee_bps = fee_bps
        self._slippage_bps = slippage_bps
        self._safety_bps = safety_bps
        self._ttl_ms = ttl_ms

    def scan(self, snapshot: PilotMarketSnapshot, balances: dict[str, float] | None = None) -> PilotWindow:
        now = monotonic()
        total_cost = self._fee_bps + self._slippage_bps + self._safety_bps
        candidates: list[PilotWindow] = []
        checks = [
            ("USDCUSDT", "USDC", "USDT"),
            ("TUSDUSDT", "TUSD", "USDT"),
            ("EURIUSDT", "EURI", "USDT"),
            ("EUREURI", "EUR", "EURI"),
        ]
        reasons: list[str] = []
        for symbol, base, quote in checks:
            quote_data = snapshot.quote(symbol)
            if quote_data is None or quote_data.bid is None or quote_data.ask is None:
                reasons.append("no_data")
                continue
            if snapshot.is_stale(symbol):
                reasons.append("stale")
                continue
            bid = quote_data.bid
            ask = quote_data.ask
            if bid <= 0 or ask <= 0:
                reasons.append("no_bidask")
                continue
            route = None
            raw_bps = 0.0
            if bid > 1.0:
                route = f"{base}→{quote}"
                raw_bps = (bid - 1.0) * 10000
            if ask < 1.0:
                alt_bps = (1.0 - ask) * 10000
                if alt_bps > raw_bps:
                    route = f"{quote}→{base}"
                    raw_bps = alt_bps
            if route is None:
                reasons.append("no_window")
                continue
            profit_bps = raw_bps - total_cost
            if profit_bps < self._min_bps:
                reasons.append("no_window")
                continue
            candidates.append(
                PilotWindow(
                    family="REBAL",
                    route=route,
                    symbols=[symbol],
                    profit_abs=0.0,
                    profit_bps=profit_bps,
                    max_notional=0.0,
                    ts_start=now,
                    ts_last=now,
                    ttl_ms=self._ttl_ms,
                    valid=True,
                    reason="ok",
                    details={
                        "symbol": symbol,
                        "bid": bid,
                        "ask": ask,
                        "raw_bps": raw_bps,
                        "cost_bps": total_cost,
                    },
                )
            )
        if candidates:
            return max(candidates, key=lambda item: item.profit_bps)
        reason = "no_window"
        if reasons and all(r in {"no_data", "stale"} for r in reasons):
            reason = "stale" if "stale" in reasons else "no_data"
        return PilotWindow(
            family="REBAL",
            route="—",
            symbols=[],
            profit_abs=0.0,
            profit_bps=0.0,
            max_notional=0.0,
            ts_start=0.0,
            ts_last=now,
            ttl_ms=self._ttl_ms,
            valid=False,
            reason=reason,
            details={"reason": reason},
        )


class TwoLegFamilyScanner(BaseFamilyScanner):
    family: FamilyLiteral = "2LEG"

    def __init__(
        self,
        *,
        min_bps: float,
        fee_bps: float,
        slippage_bps: float,
        safety_bps: float,
        ttl_ms: int,
    ) -> None:
        self._min_bps = min_bps
        self._cost_bps = fee_bps + slippage_bps + safety_bps
        self._ttl_ms = ttl_ms

    def scan(self, snapshot: PilotMarketSnapshot, balances: dict[str, float] | None = None) -> PilotWindow:
        now = monotonic()
        candidates: list[PilotWindow] = []
        reasons: list[str] = []
        start_usdt = 100.0
        euri_quote = snapshot.quote("EURIUSDT")
        euri_mid = None
        if euri_quote and euri_quote.bid and euri_quote.ask:
            euri_mid = (euri_quote.bid + euri_quote.ask) / 2

        def _needs(symbol: str) -> PilotMarketQuote | None:
            quote = snapshot.quote(symbol)
            if not quote or quote.bid is None or quote.ask is None:
                reasons.append("no_data")
                return None
            if snapshot.is_stale(symbol):
                reasons.append("stale")
                return None
            if quote.bid <= 0 or quote.ask <= 0:
                reasons.append("no_bidask")
                return None
            return quote

        usdc = _needs("USDCUSDT")
        tusd = _needs("TUSDUSDT")
        euri = _needs("EURIUSDT")
        eureuri = _needs("EUREURI")

        if usdc and tusd:
            amount_usdc = start_usdt
            usdt = _apply_cost(amount_usdc * usdc.bid, self._cost_bps)
            tusd_out = _apply_cost(usdt / tusd.ask, self._cost_bps)
            end_usdt = tusd_out
            profit_bps = (end_usdt - start_usdt) / start_usdt * 10000
            candidates.append(
                PilotWindow(
                    family="2LEG",
                    route="USDC→USDT→TUSD",
                    symbols=["USDCUSDT", "TUSDUSDT"],
                    profit_abs=end_usdt - start_usdt,
                    profit_bps=profit_bps,
                    max_notional=start_usdt,
                    ts_start=now,
                    ts_last=now,
                    ttl_ms=self._ttl_ms,
                    valid=profit_bps >= self._min_bps,
                    reason="ok" if profit_bps >= self._min_bps else "profit_below_min",
                    details={"start_usdt": start_usdt, "end_usdt": end_usdt},
                )
            )
        if usdc and tusd:
            amount_tusd = start_usdt
            usdt = _apply_cost(amount_tusd * tusd.bid, self._cost_bps)
            usdc_out = _apply_cost(usdt / usdc.ask, self._cost_bps)
            end_usdt = usdc_out
            profit_bps = (end_usdt - start_usdt) / start_usdt * 10000
            candidates.append(
                PilotWindow(
                    family="2LEG",
                    route="TUSD→USDT→USDC",
                    symbols=["TUSDUSDT", "USDCUSDT"],
                    profit_abs=end_usdt - start_usdt,
                    profit_bps=profit_bps,
                    max_notional=start_usdt,
                    ts_start=now,
                    ts_last=now,
                    ttl_ms=self._ttl_ms,
                    valid=profit_bps >= self._min_bps,
                    reason="ok" if profit_bps >= self._min_bps else "profit_below_min",
                    details={"start_usdt": start_usdt, "end_usdt": end_usdt},
                )
            )
        if euri and eureuri and euri_mid:
            start_eur = 100.0
            euri_out = _apply_cost(start_eur * eureuri.bid, self._cost_bps)
            end_usdt = _apply_cost(euri_out * euri.bid, self._cost_bps)
            start_usdt_value = start_eur * euri_mid
            profit_bps = (end_usdt - start_usdt_value) / start_usdt_value * 10000
            candidates.append(
                PilotWindow(
                    family="2LEG",
                    route="EUR→EURI→USDT",
                    symbols=["EUREURI", "EURIUSDT"],
                    profit_abs=end_usdt - start_usdt_value,
                    profit_bps=profit_bps,
                    max_notional=start_usdt_value,
                    ts_start=now,
                    ts_last=now,
                    ttl_ms=self._ttl_ms,
                    valid=profit_bps >= self._min_bps,
                    reason="ok" if profit_bps >= self._min_bps else "profit_below_min",
                    details={"start_usdt": start_usdt_value, "end_usdt": end_usdt},
                )
            )
        if euri and eureuri and euri_mid:
            start_usdt_value = start_usdt
            euri_out = _apply_cost(start_usdt_value / euri.ask, self._cost_bps)
            eur_out = _apply_cost(euri_out / eureuri.ask, self._cost_bps)
            end_usdt = eur_out * euri_mid
            profit_bps = (end_usdt - start_usdt_value) / start_usdt_value * 10000
            candidates.append(
                PilotWindow(
                    family="2LEG",
                    route="USDT→EURI→EUR",
                    symbols=["EURIUSDT", "EUREURI"],
                    profit_abs=end_usdt - start_usdt_value,
                    profit_bps=profit_bps,
                    max_notional=start_usdt_value,
                    ts_start=now,
                    ts_last=now,
                    ttl_ms=self._ttl_ms,
                    valid=profit_bps >= self._min_bps,
                    reason="ok" if profit_bps >= self._min_bps else "profit_below_min",
                    details={"start_usdt": start_usdt_value, "end_usdt": end_usdt},
                )
            )
        if candidates:
            return max(candidates, key=lambda item: item.profit_bps)
        reason = "no_window"
        if reasons and all(r in {"no_data", "stale"} for r in reasons):
            reason = "stale" if "stale" in reasons else "no_data"
        return PilotWindow(
            family="2LEG",
            route="—",
            symbols=[],
            profit_abs=0.0,
            profit_bps=0.0,
            max_notional=0.0,
            ts_start=0.0,
            ts_last=now,
            ttl_ms=self._ttl_ms,
            valid=False,
            reason=reason,
            details={"reason": reason},
        )


class LoopFamilyScanner(BaseFamilyScanner):
    family: FamilyLiteral = "LOOP"

    def __init__(
        self,
        *,
        min_bps: float,
        fee_bps: float,
        slippage_bps: float,
        safety_bps: float,
        ttl_ms: int,
    ) -> None:
        self._min_bps = min_bps
        self._cost_bps = fee_bps + slippage_bps + safety_bps
        self._ttl_ms = ttl_ms

    def scan(self, snapshot: PilotMarketSnapshot, balances: dict[str, float] | None = None) -> PilotWindow:
        now = monotonic()
        reasons: list[str] = []

        def _needs(symbol: str) -> PilotMarketQuote | None:
            quote = snapshot.quote(symbol)
            if not quote or quote.bid is None or quote.ask is None:
                reasons.append("no_data")
                return None
            if snapshot.is_stale(symbol):
                reasons.append("stale")
                return None
            if quote.bid <= 0 or quote.ask <= 0:
                reasons.append("no_bidask")
                return None
            return quote

        euri = _needs("EURIUSDT")
        usdc = _needs("USDCUSDT")
        tusd = _needs("TUSDUSDT")
        if not euri:
            reason = "stale" if "stale" in reasons else "no_data"
            return PilotWindow(
                family="LOOP",
                route="—",
                symbols=[],
                profit_abs=0.0,
                profit_bps=0.0,
                max_notional=0.0,
                ts_start=0.0,
                ts_last=now,
                ttl_ms=self._ttl_ms,
                valid=False,
                reason=reason,
                details={"reason": reason},
            )

        start_euri = 100.0
        candidates: list[PilotWindow] = []
        euri_mid = (euri.bid + euri.ask) / 2

        if usdc:
            usdt = _apply_cost(start_euri * euri.bid, self._cost_bps)
            usdc_out = _apply_cost(usdt / usdc.ask, self._cost_bps)
            usdt_back = _apply_cost(usdc_out * usdc.bid, self._cost_bps)
            euri_back = _apply_cost(usdt_back / euri.ask, self._cost_bps)
            profit_bps = (euri_back - start_euri) / start_euri * 10000
            if profit_bps >= self._min_bps:
                profit_abs = (euri_back - start_euri) * euri_mid
                candidates.append(
                    PilotWindow(
                        family="LOOP",
                        route="EURI→USDT→USDC→USDT→EURI",
                        symbols=["EURIUSDT", "USDCUSDT"],
                        profit_abs=profit_abs,
                        profit_bps=profit_bps,
                        max_notional=start_euri * euri_mid,
                        ts_start=now,
                        ts_last=now,
                        ttl_ms=self._ttl_ms,
                        valid=True,
                        reason="ok",
                        details={"start_euri": start_euri, "end_euri": euri_back},
                    )
                )
        if tusd:
            usdt = _apply_cost(start_euri * euri.bid, self._cost_bps)
            tusd_out = _apply_cost(usdt / tusd.ask, self._cost_bps)
            usdt_back = _apply_cost(tusd_out * tusd.bid, self._cost_bps)
            euri_back = _apply_cost(usdt_back / euri.ask, self._cost_bps)
            profit_bps = (euri_back - start_euri) / start_euri * 10000
            if profit_bps >= self._min_bps:
                profit_abs = (euri_back - start_euri) * euri_mid
                candidates.append(
                    PilotWindow(
                        family="LOOP",
                        route="EURI→USDT→TUSD→USDT→EURI",
                        symbols=["EURIUSDT", "TUSDUSDT"],
                        profit_abs=profit_abs,
                        profit_bps=profit_bps,
                        max_notional=start_euri * euri_mid,
                        ts_start=now,
                        ts_last=now,
                        ttl_ms=self._ttl_ms,
                        valid=True,
                        reason="ok",
                        details={"start_euri": start_euri, "end_euri": euri_back},
                    )
                )
        if candidates:
            return max(candidates, key=lambda item: item.profit_bps)
        reason = "no_window"
        if reasons and all(r in {"no_data", "stale"} for r in reasons):
            reason = "stale" if "stale" in reasons else "no_data"
        return PilotWindow(
            family="LOOP",
            route="—",
            symbols=[],
            profit_abs=0.0,
            profit_bps=0.0,
            max_notional=0.0,
            ts_start=0.0,
            ts_last=now,
            ttl_ms=self._ttl_ms,
            valid=False,
            reason=reason,
            details={"reason": reason},
        )
