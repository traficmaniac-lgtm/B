from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import QTimer

from src.gui.lite_grid_math import compute_order_qty
from src.services.nc_pilot.arb_hub import ArbHub
from src.services.nc_pilot.session import NcPilotSession


@dataclass(frozen=True)
class PilotPlan:
    side: str
    leg_index: int
    leg_type: str
    price: Decimal
    qty: Decimal
    reason: str


class PilotController:
    ANALYSIS_INTERVAL_MS = 500
    ORDER_COOLDOWN_SEC = 1.5
    SKIP_LOG_COOLDOWN_SEC = 2.0

    def __init__(self, window: object, session: NcPilotSession) -> None:
        self._window = window
        self._session = session
        self._arb_hub = ArbHub()
        self._analysis_timer = QTimer(window)
        self._analysis_timer.setInterval(self.ANALYSIS_INTERVAL_MS)
        self._analysis_timer.timeout.connect(self._on_analysis_tick)

    def start_analysis(self) -> None:
        pilot = self._session.pilot
        if pilot.analysis_on:
            return
        pilot.analysis_on = True
        pilot.state = "ANALYZE"
        self._analysis_timer.start()
        self._window._append_log("[PILOT] ANALYSIS_START", kind="INFO")
        self._window._set_pilot_controls(analysis_on=True, trading_on=pilot.trading_on)
        self._window._update_pilot_status_line()

    def start_trading(self) -> None:
        pilot = self._session.pilot
        if not pilot.analysis_on:
            self._log_skip("analysis_off")
            return
        if pilot.trading_on:
            return
        pilot.trading_on = True
        pilot.state = "TRADING"
        self._window._append_log("[PILOT] TRADING_START", kind="INFO")
        self._window._set_pilot_controls(analysis_on=True, trading_on=True)
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
            self._window._append_log(f"[CANCEL] n={canceled} reason=stop", kind="INFO")
        self._window._append_log("[PILOT] STOP", kind="INFO")
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
        selected_symbols = {symbol.upper() for symbol in pilot.selected_symbols}
        if not selected_symbols:
            pilot.last_best_route = None
            pilot.last_best_edge_bps = None
            self._log_skip("no_pairs_selected")
            return
        current_symbol = self._window._symbol.replace("/", "").upper()
        if current_symbol not in selected_symbols:
            pilot.last_best_route = None
            pilot.last_best_edge_bps = None
            self._log_skip("pair_not_selected")
            return
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
        pilot.last_best_route = best.route
        pilot.last_best_edge_bps = best.edge_bps
        pilot.last_decision = best.reason
        now = monotonic()
        if best.side and (pilot.last_route_log_ts is None or now - pilot.last_route_log_ts >= 2.0):
            pilot.last_route_log_ts = now
            edge_text = f"{best.edge_bps:.3f}" if best.edge_bps is not None else "—"
            self._window._append_log(
                f"[ROUTE] best side={best.side} edge={edge_text}bps reason={best.reason}",
                kind="INFO",
            )
        self._window._update_pilot_status_line()
        if not pilot.trading_on:
            pilot.state = "ANALYZE"
            return
        pilot.state = "TRADING"
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

    def _log_skip(self, reason: str) -> None:
        pilot = self._session.pilot
        now = monotonic()
        pilot.last_decision = reason
        if pilot.last_skip_log_ts and now - pilot.last_skip_log_ts < self.SKIP_LOG_COOLDOWN_SEC:
            return
        pilot.last_skip_log_ts = now
        pilot.counters.skips += 1
        self._window._append_log(f"[SKIP] reason={reason}", kind="WARN")
