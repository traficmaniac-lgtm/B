from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.core.strategies.manual_strategies import StrategyContext


@dataclass(frozen=True)
class ManualStartDecision:
    ok: bool
    reason: str


def check_manual_start(*, dry_run: bool, trade_gate: str) -> ManualStartDecision:
    if dry_run:
        return ManualStartDecision(ok=True, reason="dry_run")
    if trade_gate != "ok":
        return ManualStartDecision(ok=False, reason=f"trade_gate={trade_gate}")
    return ManualStartDecision(ok=True, reason="ok")


def manual_start_gating(ctx: StrategyContext) -> ManualStartDecision:
    if ctx.rules_loaded is False:
        return ManualStartDecision(ok=False, reason="rules_not_loaded")
    if not ctx.dry_run and ctx.balances_ready is False:
        return ManualStartDecision(ok=False, reason="balances_not_ready")
    decision = check_manual_start(dry_run=ctx.dry_run, trade_gate=ctx.trade_gate or "")
    if not decision.ok:
        return decision
    if not ctx.dry_run and ctx.spot_enabled is False:
        return ManualStartDecision(ok=False, reason="spot_trading_disabled")
    return ManualStartDecision(ok=True, reason="ok")


class ManualRuntime:
    def __init__(
        self,
        *,
        on_start: Callable[[str, StrategyContext], None],
        on_pause: Callable[[], None],
        on_stop: Callable[[], None],
        on_cancel_all: Callable[[str], None],
    ) -> None:
        self._on_start = on_start
        self._on_pause = on_pause
        self._on_stop = on_stop
        self._on_cancel_all = on_cancel_all
        self._pending_cancel_state: str | None = None

    def start(self, strategy_id: str, ctx: StrategyContext) -> None:
        self._on_start(strategy_id, ctx)

    def pause(self) -> None:
        self._pending_cancel_state = "PAUSED"
        self._on_pause()

    def stop(self) -> None:
        self._pending_cancel_state = "IDLE"
        self._on_stop()

    def cancel_all(self) -> None:
        if not self._pending_cancel_state:
            return
        self._on_cancel_all(self._pending_cancel_state)
        self._pending_cancel_state = None
