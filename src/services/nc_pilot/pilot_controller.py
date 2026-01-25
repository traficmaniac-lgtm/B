from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import QTimer

from src.gui.lite_grid_math import compute_order_qty
from src.services.nc_pilot.arb_hub import ArbHub
from src.services.nc_pilot.pilot_analysis import (
    LoopFamilyScanner,
    PilotMarketSnapshot,
    PilotWindow,
    RebalFamilyScanner,
    resolve_leg_market,
    SpreadFamilyScanner,
    TwoLegFamilyScanner,
)
from src.services.nc_pilot.session import NcPilotSession
from src.services.nc_pilot.symbol_aliases import format_alias_summary, resolve_symbol


@dataclass(frozen=True)
class PilotPlan:
    side: str
    leg_index: int
    leg_type: str
    price: Decimal
    qty: Decimal
    reason: str


@dataclass(frozen=True)
class TwoLegOrderPlan:
    symbol: str
    side: str
    price: Decimal
    qty: Decimal
    required_asset: str
    required_amount: Decimal


class PilotController:
    SCAN_INTERVAL_MS = 1000
    STALE_MS = 2000
    TTL_MS = 5000
    MIN_BPS_SPREAD = 0.5
    MIN_BPS_REBAL = 0.5
    MIN_BPS_2LEG = 0.8
    MIN_BPS_LOOP = 1.0
    BEST_DELTA_BPS = 0.3
    ORDER_COOLDOWN_SEC = 1.5
    TRADE_BLOCK_COOLDOWN_SEC = 2.0
    HOLD_LOG_COOLDOWN_SEC = 2.0
    COOLDOWN_AFTER_TRADE_S = 2.0
    DEFAULT_MIN_PROFIT_BPS_2LEG = 6.0
    DEFAULT_MAX_WINDOW_LIFE_S = 10.0
    DEFAULT_MAX_PRICE_AGE_MS = 1500
    DEFAULT_COOLDOWN_AFTER_TRADE_S = 2.0
    PILOT_SYMBOLS = ("EURIUSDT", "EUREURI", "USDCUSDT", "TUSDUSDT")

    TWO_LEG_ROUTE_MAP = {
        "USDC→USDT→TUSD": [
            ("USDC", "USDT"),
            ("USDT", "TUSD"),
        ],
        "TUSD→USDT→USDC": [
            ("TUSD", "USDT"),
            ("USDT", "USDC"),
        ],
    }

    def __init__(self, window: object, session: NcPilotSession) -> None:
        self._window = window
        self._session = session
        self._arb_hub = ArbHub()
        self._analysis_timer = QTimer(window)
        self._analysis_timer.setInterval(self.SCAN_INTERVAL_MS)
        self._analysis_timer.timeout.connect(self._on_analysis_tick)
        self._last_windows: dict[str, PilotWindow] = {}

    def start_analysis(self) -> None:
        pilot = self._session.pilot
        if pilot.analysis_on:
            return
        pilot.analysis_on = True
        pilot.state = "ANALYZE"
        self._analysis_timer.start()
        self._window._append_pilot_log("[PILOT] ANALYSIS_START")
        self._refresh_symbol_resolution(log_aliases=True)
        self._window._set_pilot_controls(analysis_on=True, trading_on=pilot.trading_on)
        self._window._update_pilot_status_line()

    def start_trading(self) -> None:
        pilot = self._session.pilot
        if not pilot.analysis_on:
            self.start_analysis()
        if pilot.trading_on:
            return
        pilot.trading_on = True
        pilot.state = "TRADING"
        self._window._append_pilot_log("[PILOT] TRADING_START")
        if not pilot.trade_enabled_families:
            self._window._append_pilot_log("[START] no trade families enabled")
        self._window._set_pilot_controls(analysis_on=pilot.analysis_on, trading_on=True)
        self._window._update_pilot_status_line()

    def stop(self, *, cancel_orders: bool) -> None:
        pilot = self._session.pilot
        if self._analysis_timer.isActive():
            self._analysis_timer.stop()
        pilot.analysis_on = False
        pilot.trading_on = False
        pilot.state = "IDLE"
        pilot.arb_id_active = None
        if cancel_orders:
            canceled = self._window._cancel_pilot_orders()
            pilot.counters.cancels += canceled
        self._window._append_pilot_log("[PILOT] ANALYSIS_STOP")
        self._window._set_pilot_controls(analysis_on=False, trading_on=False)
        self._window._update_pilot_status_line()

    def shutdown(self) -> None:
        if self._analysis_timer.isActive():
            self._analysis_timer.stop()
        pilot = self._session.pilot
        pilot.analysis_on = False
        pilot.trading_on = False
        pilot.state = "IDLE"

    def _on_analysis_tick(self) -> None:
        pilot = self._session.pilot
        if not pilot.analysis_on:
            return
        effective_symbols, invalid_symbols = self._refresh_symbol_resolution()
        now = monotonic()
        snapshot: PilotMarketSnapshot | None = None
        windows: list[PilotWindow] = []
        if invalid_symbols:
            reason = next(iter(invalid_symbols.values()))
            for family in ("SPREAD", "REBAL", "2LEG", "LOOP"):
                windows.append(self._invalid_window(family=family, reason=reason, ttl_ms=self.TTL_MS))
        else:
            market_data = self._window._pilot_collect_market_data(set(effective_symbols))
            snapshot = PilotMarketSnapshot.from_market_data(market_data, stale_ms=self.STALE_MS)
            fee_bps = self._window.get_effective_fees_pct(self._session.symbol) * 100
            slippage_bps = self._window._profit_guard_slippage_pct() * 100
            safety_bps = self._window._profit_guard_pad_pct() * 100
            config_2leg = self._get_2leg_config()
            scanners = [
                SpreadFamilyScanner(
                    symbols=effective_symbols,
                    min_bps=self.MIN_BPS_SPREAD,
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                    safety_bps=safety_bps,
                    ttl_ms=self.TTL_MS,
                    max_notional_default=pilot.max_notional,
                ),
                RebalFamilyScanner(
                    min_bps=self.MIN_BPS_REBAL,
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                    safety_bps=safety_bps,
                    ttl_ms=self.TTL_MS,
                ),
                TwoLegFamilyScanner(
                    min_bps=config_2leg["min_profit_bps"],
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                    safety_bps=safety_bps,
                    ttl_ms=self.TTL_MS,
                ),
                LoopFamilyScanner(
                    min_bps=self.MIN_BPS_LOOP,
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                    safety_bps=safety_bps,
                    ttl_ms=self.TTL_MS,
                ),
            ]
            for scanner in scanners:
                windows.append(scanner.scan(snapshot))
        ui_snapshots: list[dict[str, object]] = []
        for window in windows:
            prev = self._last_windows.get(window.family)
            if window.family == "2LEG":
                window = self._apply_2leg_window_state(window, prev, snapshot, now)
            else:
                window = self._apply_window_lifecycle(window, prev, now)
            self._last_windows[window.family] = window
            self._maybe_log_best_changed(window, prev)
            if window.valid and (not prev or not prev.valid):
                pilot.counters.windows_found_today += 1
            ui_snapshots.append(self._window_to_dict(window, now))
        best_window = self._select_best_window(windows, now)
        pilot.last_best_route = best_window.route if best_window else None
        pilot.last_best_edge_bps = best_window.profit_bps if best_window else None
        if not pilot.trading_on:
            pilot.last_decision = "analysis_only"
        pilot.state = "TRADING" if pilot.trading_on else "ANALYZE"
        self._window._update_pilot_routes_table(ui_snapshots)
        self._window._update_arb_signals()
        self._window._update_pilot_status_line()
        self._window._update_pilot_dashboard_counters()
        if pilot.trading_on and pilot.trade_enabled_families:
            allowed_families = {family.upper() for family in pilot.trade_enabled_families}
            if best_window and best_window.family not in allowed_families:
                self._log_trade_block(best_window.family, now)
            if "2LEG" in allowed_families:
                trade_window = next((item for item in windows if item.family == "2LEG"), None)
                self._maybe_execute_window_trade(trade_window, snapshot, now)
            else:
                trade_window = self._select_best_window(
                    [item for item in windows if item.family in allowed_families],
                    now,
                )
                self._maybe_execute_window_trade(trade_window, snapshot, now)

    def _maybe_execute_window_trade(
        self,
        window: PilotWindow | None,
        snapshot: PilotMarketSnapshot | None,
        now: float,
    ) -> None:
        pilot = self._session.pilot
        if window is None:
            ok, reason, details = self._validate_2leg_window(window, snapshot, now, life_s=0.0)
            pilot.last_decision = reason
            self._log_2leg_hold(reason, window, details)
            return
        if window.family != "2LEG":
            return
        config_2leg = self._get_2leg_config()
        cooldown_after_trade_s = config_2leg["cooldown_after_trade_s"]
        open_orders = getattr(self._window, "_open_orders", [])
        virtual_orders = getattr(self._window, "_pilot_virtual_orders", [])
        if (
            pilot.arb_id_active
            and pilot.last_exec_ts
            and now - pilot.last_exec_ts >= cooldown_after_trade_s
            and len(open_orders) + len(virtual_orders) == 0
        ):
            pilot.arb_id_active = None
        ok, reason, details = self._validate_2leg_window(window, snapshot, now, life_s=window.life_s())
        if not ok:
            pilot.last_decision = reason
            self._log_2leg_hold(reason, window, details)
            return
        plan, balance_issue = self._build_2leg_plan(window, snapshot)
        if plan is None:
            if balance_issue:
                asset, need, free = balance_issue
                self._log_2leg_hold(
                    "insufficient_balance",
                    window,
                    {"asset": asset, "need": need, "free": free},
                )
                pilot.last_decision = "insufficient_balance"
            else:
                self._log_2leg_hold("missing_bidask", window, {})
                pilot.last_decision = "missing_bidask"
            return
        self._window._append_pilot_log(
            (
                "[2LEG] start "
                f"route={window.route} profit_bps={window.profit_bps:+.2f} "
                f"life_s={window.life_s():.1f}"
            )
        )
        arb_id = uuid4().hex[:8]
        dry_run = not self._window._live_enabled()
        sent_any = False
        for idx, leg in enumerate(plan, start=1):
            client_id = self._window._pilot_client_order_id(
                arb_id=arb_id,
                leg_index=idx,
                leg_type="2LEG",
                side=leg.side,
            )
            sent = self._window._send_pilot_order_for_symbol(
                symbol=leg.symbol,
                side=leg.side,
                price=leg.price,
                qty=leg.qty,
                client_order_id=client_id,
                arb_id=arb_id,
                leg_index=idx,
                leg_type="2LEG",
                dry_run=dry_run,
            )
            sent_any = sent_any or sent
        if sent_any:
            pilot.last_decision = "trade_allowed"
            pilot.last_exec_ts = now
            pilot.arb_id_active = f"2LEG:{window.route}"

    def _log_trade_block(self, family: str, now: float) -> None:
        pilot = self._session.pilot
        last_log = pilot.trade_block_log_ts.get(family)
        if last_log is not None and now - last_log < self.TRADE_BLOCK_COOLDOWN_SEC:
            return
        pilot.trade_block_log_ts[family] = now
        self._window._append_pilot_log(f"[TRADE_BLOCK] family={family} reason=family_disabled")

    def _log_2leg_hold(
        self,
        reason: str,
        window: PilotWindow | None,
        details: dict[str, object] | None = None,
    ) -> None:
        pilot = self._session.pilot
        now = monotonic()
        if pilot.last_hold_log_ts and now - pilot.last_hold_log_ts < self.HOLD_LOG_COOLDOWN_SEC:
            return
        info = details or {}
        message = f"[2LEG] HOLD reason={reason}"
        if reason == "profit_below_min":
            profit = window.profit_bps if window else 0.0
            min_profit = info.get("min_profit_bps", 0.0)
            message += f" profit={profit:.2f} min={float(min_profit):.2f}"
        elif reason == "window_expired":
            life_s = info.get("life_s", window.life_s() if window else 0.0)
            max_life = info.get("max_window_life_s", 0.0)
            message += f" life_s={float(life_s):.1f} max={float(max_life):.1f}"
        elif reason == "stale_price":
            age_ms = info.get("age_ms")
            max_age = info.get("max_price_age_ms", 0)
            if age_ms is not None:
                message += f" age_ms={int(age_ms)} max={int(max_age)}"
        elif reason == "missing_bidask":
            symbol = info.get("symbol")
            if symbol:
                message += f" symbol={symbol}"
        elif reason == "insufficient_balance":
            asset = info.get("asset")
            need = info.get("need")
            free = info.get("free")
            if asset and need is not None and free is not None:
                message += f" need={float(need):.2f} {asset} free={float(free):.2f}"
            min_qty = info.get("min_qty")
            min_notional = info.get("min_notional")
            if min_qty is not None:
                message += f" min_qty={float(min_qty):.6f}"
            if min_notional is not None:
                message += f" min_notional={float(min_notional):.2f}"
        elif reason == "cooldown_active":
            remaining = info.get("remaining_s")
            block = info.get("block")
            if remaining is not None:
                message += f" remaining_s={float(remaining):.2f}"
            if block:
                message += f" block={block}"
        self._window._append_pilot_log(message)
        pilot.last_hold_log_ts = now

    def _build_2leg_plan(
        self,
        window: PilotWindow,
        snapshot: PilotMarketSnapshot,
    ) -> tuple[list[TwoLegOrderPlan] | None, tuple[str, Decimal, Decimal] | None]:
        route = window.route
        legs = self.TWO_LEG_ROUTE_MAP.get(route)
        if not legs:
            return None, None
        required_notional = Decimal(str(max(window.max_notional, 0.0)))
        if required_notional <= 0:
            return None, None
        available_symbols = snapshot.quotes.keys()
        resolved_legs: list[tuple[str, str, str, str, bool]] = []
        for from_asset, to_asset in legs:
            symbol, side, invert = resolve_leg_market(from_asset, to_asset, available_symbols)
            if not symbol:
                return None, None
            resolved_legs.append((symbol, side, from_asset, to_asset, invert))
        for _symbol, _side, require_asset, _output_asset, _invert in resolved_legs:
            free = self._window._pilot_balance_free(require_asset)
            if free < required_notional:
                return None, (require_asset, required_notional, free)
        plan: list[TwoLegOrderPlan] = []
        for idx, (symbol, side, require_asset, _output_asset, invert) in enumerate(resolved_legs, start=1):
            quote = snapshot.quote(symbol)
            if quote is None or quote.bid is None or quote.ask is None:
                return None, None
            if snapshot.is_stale(symbol):
                return None, None
            price_raw = quote.bid if side == "SELL" else quote.ask
            if price_raw is None or price_raw <= 0:
                return None, None
            if invert:
                eff_bid = 1 / quote.ask if quote.ask and quote.ask > 0 else None
                eff_ask = 1 / quote.bid if quote.bid and quote.bid > 0 else None
                self._window._append_pilot_log(
                    "[2LEG] "
                    f"leg{idx} symbol={symbol} invert=true "
                    f"bid={quote.bid:.8f} ask={quote.ask:.8f} "
                    f"eff_bid={eff_bid:.8f} eff_ask={eff_ask:.8f}"
                )
            else:
                self._window._append_pilot_log(
                    "[2LEG] "
                    f"leg{idx} symbol={symbol} invert=false bid={quote.bid:.8f} ask={quote.ask:.8f}"
                )
            price = Decimal(str(price_raw))
            if side == "SELL":
                qty = required_notional
            else:
                qty = required_notional / price
            plan.append(
                TwoLegOrderPlan(
                    symbol=symbol,
                    side=side,
                    price=price,
                    qty=qty,
                    required_asset=require_asset,
                    required_amount=required_notional,
                )
            )
        return plan, None

    def _get_2leg_config(self) -> dict[str, float]:
        config = getattr(self._window, "pilot_config", None)
        if config is None:
            return {
                "min_profit_bps": self.DEFAULT_MIN_PROFIT_BPS_2LEG,
                "max_window_life_s": self.DEFAULT_MAX_WINDOW_LIFE_S,
                "max_price_age_ms": self.DEFAULT_MAX_PRICE_AGE_MS,
                "cooldown_after_trade_s": self.DEFAULT_COOLDOWN_AFTER_TRADE_S,
            }
        return {
            "min_profit_bps": getattr(config, "min_profit_bps_2leg", self.DEFAULT_MIN_PROFIT_BPS_2LEG),
            "max_window_life_s": getattr(config, "max_window_life_s", self.DEFAULT_MAX_WINDOW_LIFE_S),
            "max_price_age_ms": getattr(config, "max_price_age_ms", self.DEFAULT_MAX_PRICE_AGE_MS),
            "cooldown_after_trade_s": getattr(
                config,
                "cooldown_after_trade_s",
                self.DEFAULT_COOLDOWN_AFTER_TRADE_S,
            ),
        }

    def _apply_2leg_window_state(
        self,
        window: PilotWindow,
        prev: PilotWindow | None,
        snapshot: PilotMarketSnapshot | None,
        now: float,
    ) -> PilotWindow:
        candidate_start = now
        if prev and prev.valid and prev.route == window.route:
            candidate_start = prev.ts_start
        life_s = max(now - candidate_start, 0.0)
        ok, reason, details = self._validate_2leg_window(window, snapshot, now, life_s=life_s)
        if ok:
            window.valid = True
            window.reason = "ok"
            window.ts_start = candidate_start
        else:
            window.valid = False
            window.reason = reason
            window.ts_start = 0.0
        window.ts_last = now
        if details:
            window.details = {
                **window.details,
                "validation": details,
                "validation_reason": reason,
            }
        return window

    def _validate_2leg_window(
        self,
        window: PilotWindow | None,
        snapshot: PilotMarketSnapshot | None,
        now: float,
        *,
        life_s: float,
    ) -> tuple[bool, str, dict[str, object]]:
        if window is None:
            return False, "missing_bidask", {}
        pilot = self._session.pilot
        config = self._get_2leg_config()
        min_profit_bps = float(config["min_profit_bps"])
        max_window_life_s = float(config["max_window_life_s"])
        max_price_age_ms = int(config["max_price_age_ms"])
        cooldown_after_trade_s = float(config["cooldown_after_trade_s"])
        allowed_families = {family.upper() for family in pilot.trade_enabled_families}
        if "2LEG" not in allowed_families:
            return False, "family_disabled", {}
        if snapshot is None:
            return False, "missing_bidask", {}
        if not window.symbols:
            return False, "missing_bidask", {"route": window.route}
        legs = self.TWO_LEG_ROUTE_MAP.get(window.route)
        if not legs:
            return False, "missing_bidask", {"route": window.route}
        available_symbols = snapshot.quotes.keys()
        resolved_legs: list[tuple[str, str, str, str]] = []
        for from_asset, to_asset in legs:
            symbol, side, _invert = resolve_leg_market(from_asset, to_asset, available_symbols)
            if not symbol:
                return False, "missing_bidask", {"route": window.route}
            resolved_legs.append((symbol, side, from_asset, to_asset))
        for symbol, _side, _from_asset, _to_asset in resolved_legs:
            quote = snapshot.quote(symbol)
            if quote is None or quote.bid is None or quote.ask is None:
                return False, "missing_bidask", {"symbol": symbol}
            if quote.bid <= 0 or quote.ask <= 0:
                return False, "missing_bidask", {"symbol": symbol}
            if quote.age_ms is None or quote.age_ms > max_price_age_ms:
                return (
                    False,
                    "stale_price",
                    {"symbol": symbol, "age_ms": quote.age_ms, "max_price_age_ms": max_price_age_ms},
                )
        if window.profit_bps < min_profit_bps:
            return (
                False,
                "profit_below_min",
                {"profit_bps": window.profit_bps, "min_profit_bps": min_profit_bps},
            )
        if life_s > max_window_life_s:
            return (
                False,
                "window_expired",
                {"life_s": life_s, "max_window_life_s": max_window_life_s},
            )
        required_notional = Decimal(str(max(window.max_notional, 0.0)))
        if required_notional <= 0:
            return False, "insufficient_balance", {"required_notional": float(required_notional)}
        for symbol, side, require_asset, _output_asset in resolved_legs:
            quote = snapshot.quote(symbol)
            if quote is None or quote.bid is None or quote.ask is None:
                return False, "missing_bidask", {"symbol": symbol}
            price_raw = quote.bid if side == "SELL" else quote.ask
            if price_raw is None or price_raw <= 0:
                return False, "missing_bidask", {"symbol": symbol}
            price = Decimal(str(price_raw))
            qty = required_notional if side == "SELL" else required_notional / price
            free = self._window._pilot_balance_free(require_asset)
            if free < required_notional:
                return (
                    False,
                    "insufficient_balance",
                    {"asset": require_asset, "need": required_notional, "free": free},
                )
            rules = self._window._pilot_exchange_rules(symbol)
            if rules:
                min_qty_raw = rules.get("min_qty")
                min_notional_raw = rules.get("min_notional")
                if min_qty_raw is not None:
                    min_qty = Decimal(str(min_qty_raw))
                    if qty < min_qty:
                        return (
                            False,
                            "insufficient_balance",
                            {"asset": require_asset, "need": min_qty, "free": free, "min_qty": min_qty},
                        )
                if min_notional_raw is not None:
                    min_notional = Decimal(str(min_notional_raw))
                    if qty * price < min_notional:
                        return (
                            False,
                            "insufficient_balance",
                            {
                                "asset": require_asset,
                                "need": min_notional,
                                "free": free,
                                "min_notional": min_notional,
                            },
                        )
        if pilot.last_exec_ts and now - pilot.last_exec_ts < cooldown_after_trade_s:
            elapsed = now - pilot.last_exec_ts
            return (
                False,
                "cooldown_active",
                {
                    "elapsed_s": elapsed,
                    "cooldown_after_trade_s": cooldown_after_trade_s,
                    "remaining_s": max(cooldown_after_trade_s - elapsed, 0.0),
                },
            )
        if not self._window._pilot_trade_gate_ready():
            return False, "cooldown_active", {"block": "trade_gate"}
        open_orders = getattr(self._window, "_open_orders", [])
        virtual_orders = getattr(self._window, "_pilot_virtual_orders", [])
        open_total = len(open_orders) + len(virtual_orders)
        if pilot.arb_id_active:
            return False, "cooldown_active", {"block": "active_route", "open_orders": open_total}
        if open_total > 2:
            return False, "cooldown_active", {"block": "open_orders", "open_orders": open_total}
        return True, "ok", {}

    def _panic_hold(self, reason: str) -> None:
        self._window._append_pilot_log(f"[PANIC] hold reason={reason} -> cancel_all")
        canceled = self._window._cancel_pilot_orders()
        if canceled:
            self._window._append_pilot_log(f"[PANIC] cancel_all count={canceled}")
        self._window.panic_hold(reason)

    def _execute_trading(self, now: float) -> None:
        pilot = self._session.pilot
        market = self._session.market
        balances = self._window._balance_snapshot()
        base_free = self._window.as_decimal(balances.get("base_free", Decimal("0")))
        quote_free = self._window.as_decimal(balances.get("quote_free", Decimal("0")))
        best = self._arb_hub.compute_best(
            bid=market.bid,
            ask=market.ask,
            base_free=base_free,
            quote_free=quote_free,
        )
        if best.side is None:
            self._log_skip(best.reason)
            return
        if pilot.last_exec_ts and now - pilot.last_exec_ts < self.ORDER_COOLDOWN_SEC:
            self._log_skip("cooldown")
            return
        if pilot.arb_id_active and pilot.last_exec_ts and now - pilot.last_exec_ts < self.ORDER_COOLDOWN_SEC:
            self._log_skip("cooldown")
            return
        if not self._window._pilot_trade_gate_ready():
            self._log_skip("trade_gate")
            return
        plan = self._build_plan(best.side, base_free=base_free, quote_free=quote_free)
        if plan is None:
            return
        arb_id = uuid4().hex[:8]
        client_id = self._window._pilot_client_order_id(
            arb_id=arb_id,
            leg_index=plan.leg_index,
            leg_type=plan.leg_type,
            side=plan.side,
        )
        dry_run = not self._window._live_enabled()
        sent = self._window._send_pilot_order(
            side=plan.side,
            price=plan.price,
            qty=plan.qty,
            client_order_id=client_id,
            arb_id=arb_id,
            leg_index=plan.leg_index,
            leg_type=plan.leg_type,
            dry_run=dry_run,
        )
        if sent:
            pilot.counters.orders_sent += 1
            pilot.last_exec_ts = now
            pilot.arb_id_active = arb_id

    def _build_plan(self, side: str, *, base_free: Decimal, quote_free: Decimal) -> PilotPlan | None:
        rules = self._window._exchange_rules
        tick = self._window._rule_decimal(rules.get("tick"))
        step = self._window._rule_decimal(rules.get("step"))
        price_raw = self._window.as_decimal(self._session.market.ask if side == "BUY" else self._session.market.bid)
        if price_raw <= 0:
            self._log_skip("no_bidask")
            return None
        price = self._window.q_price(price_raw, tick)
        budget = self._window.as_decimal(self._window._settings_state.budget)
        desired_notional = self._desired_notional(
            side,
            budget=budget,
            price=price,
            base_free=base_free,
            quote_free=quote_free,
        )
        fee_rate = self._window._effective_fee_rate()
        qty, _notional, reason = compute_order_qty(
            side,
            price,
            desired_notional,
            {"base_free": base_free, "quote_free": quote_free},
            rules,
            fee_rate,
            None,
        )
        needs_prep, prep_side = self._arb_hub.balance_acquire_plan(
            side,
            base_free=base_free,
            quote_free=quote_free,
        )
        if reason.startswith("insufficient") and prep_side:
            needs_prep = True
        if needs_prep and prep_side:
            prep_price_raw = self._window.as_decimal(
                self._session.market.ask if prep_side == "BUY" else self._session.market.bid
            )
            if prep_price_raw <= 0:
                self._log_skip("no_bidask")
                return None
            prep_price = self._window.q_price(prep_price_raw, tick)
            prep_notional = self._desired_notional(
                prep_side,
                budget=budget,
                price=prep_price,
                base_free=base_free,
                quote_free=quote_free,
            )
            prep_qty, _prep_total, prep_reason = compute_order_qty(
                prep_side,
                prep_price,
                prep_notional,
                {"base_free": base_free, "quote_free": quote_free},
                rules,
                fee_rate,
                None,
            )
            if prep_reason != "ok":
                self._log_skip("no_balance")
                return None
            return PilotPlan(
                side=prep_side,
                leg_index=0,
                leg_type="PREP",
                price=prep_price,
                qty=self._window.q_qty(prep_qty, step),
                reason="balance_prep",
            )
        if reason != "ok":
            if reason in {"min_qty", "min_notional"}:
                self._log_skip(reason)
            elif reason.startswith("insufficient"):
                self._log_skip("no_balance")
            else:
                self._log_skip("no_balance")
            return None
        return PilotPlan(
            side=side,
            leg_index=1,
            leg_type="MAIN",
            price=price,
            qty=self._window.q_qty(qty, step),
            reason="main",
        )

    @staticmethod
    def _desired_notional(
        side: str,
        *,
        budget: Decimal,
        price: Decimal,
        base_free: Decimal,
        quote_free: Decimal,
    ) -> Decimal:
        if side == "BUY":
            if quote_free > 0:
                return min(budget, quote_free * Decimal("0.30"))
            return budget
        base_value = base_free * price
        if base_value > 0:
            return min(budget, base_value * Decimal("0.30"))
        return budget

    def _apply_window_lifecycle(
        self,
        window: PilotWindow,
        prev: PilotWindow | None,
        now: float,
    ) -> PilotWindow:
        if not window.valid:
            window.ts_start = 0.0
        elif prev and prev.valid and prev.route == window.route:
            window.ts_start = prev.ts_start
        else:
            window.ts_start = now
        window.ts_last = now
        return window

    def _maybe_log_best_changed(self, window: PilotWindow, prev: PilotWindow | None) -> None:
        if prev is None and not window.valid:
            return
        prev_profit = prev.profit_bps if prev else None
        changed = False
        if prev is None:
            changed = True
        elif prev.route != window.route:
            changed = True
        elif prev.valid != window.valid:
            changed = True
        elif prev_profit is not None and abs(prev_profit - window.profit_bps) > self.BEST_DELTA_BPS:
            changed = True
        if not changed:
            return
        self._window._append_pilot_log(
            (
                "[PILOT] BEST_CHANGED "
                f"family={window.family} route={window.route} "
                f"profit_bps={window.profit_bps:+.2f} valid={str(window.valid).lower()} "
                f"reason={window.reason}"
            )
        )

    @staticmethod
    def _window_to_dict(window: PilotWindow, now: float) -> dict[str, object]:
        return {
            "family": window.family,
            "route_text": window.route,
            "symbols": window.symbols,
            "profit_abs_usdt": window.profit_abs,
            "profit_bps": window.profit_bps,
            "life_s": window.life_s(),
            "ttl_ms": window.ttl_ms,
            "valid": window.valid,
            "reason": window.reason,
            "details": window.details,
            "is_alive": window.is_alive(now),
        }

    @staticmethod
    def _invalid_window(*, family: str, reason: str, ttl_ms: int) -> PilotWindow:
        now = monotonic()
        return PilotWindow(
            family=family,
            route="—",
            symbols=[],
            profit_abs=0.0,
            profit_bps=0.0,
            max_notional=0.0,
            ts_start=0.0,
            ts_last=now,
            ttl_ms=ttl_ms,
            valid=False,
            reason=reason,
            details={"reason": reason},
        )

    @staticmethod
    def _select_best_window(windows: list[PilotWindow], now: float) -> PilotWindow | None:
        valid = [window for window in windows if window.valid and window.is_alive(now)]
        if not valid:
            return None
        return max(valid, key=lambda item: item.profit_bps)

    def _log_skip(self, reason: str) -> None:
        pilot = self._session.pilot
        pilot.last_decision = reason
        pilot.counters.skips += 1

    def _refresh_symbol_resolution(self, *, log_aliases: bool = False) -> tuple[set[str], dict[str, str]]:
        pilot = self._session.pilot
        selected_symbols = {symbol.upper() for symbol in pilot.selected_symbols}
        exchange_symbols = self._window._pilot_exchange_symbols()
        aliases: dict[str, str] = {}
        invalid_symbols: dict[str, str] = {}
        effective_symbols: set[str] = set()
        for symbol in sorted(selected_symbols):
            effective, reason = resolve_symbol(symbol, exchange_symbols)
            if effective is None:
                invalid_symbols[symbol] = reason
                continue
            aliases[symbol] = effective
            effective_symbols.add(effective)
        pilot.symbol_aliases = aliases
        pilot.invalid_symbols = invalid_symbols
        if log_aliases:
            summary = format_alias_summary(sorted(selected_symbols), aliases)
            if summary:
                for entry in summary.split(", "):
                    self._window._append_pilot_log(f"[PILOT] symbol alias: {entry}")
            for symbol, reason in invalid_symbols.items():
                self._window._append_pilot_log(f"[PILOT] symbol invalid: {symbol} ({reason})")
        return effective_symbols, invalid_symbols
