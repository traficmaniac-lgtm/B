from __future__ import annotations

from dataclasses import dataclass


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
