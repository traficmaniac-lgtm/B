from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from src.gui.models.pair_workspace import DataPackSummary, DataRequest, StrategyPatch, StrategyPlan


class AIProvider:
    def analyze(
        self,
        symbol: str,
        datapack: DataPackSummary,
        user_budget: float,
        mode: str,
        chat_context: Iterable[str] | None = None,
    ) -> StrategyPlan | DataRequest:
        _ = (symbol, chat_context)
        period_hours = _period_to_hours(datapack.period)
        if period_hours >= 24 and datapack.quality == "Deep":
            return StrategyPlan(
                budget=max(user_budget, 50.0),
                mode=mode,
                grid_count=12,
                grid_step_pct=0.6,
                range_low_pct=1.8,
                range_high_pct=2.4,
                refresh_interval_s=30,
                notes=f"Prepared for {datapack.period} {datapack.quality} datapack.",
            )
        return DataRequest(
            reason="Need more candles for long-horizon grid calibration.",
            required_candles=2000,
            target_period="24h",
            target_quality="Deep",
        )

    def chat_adjustment(self, plan: StrategyPlan, message: str) -> StrategyPatch | str:
        lowered = message.lower()
        if "tighter" in lowered or "reduce risk" in lowered:
            return StrategyPatch(grid_step_pct=max(plan.grid_step_pct - 0.1, 0.2), notes="Tightened grid step.")
        if "wider" in lowered or "more range" in lowered:
            return StrategyPatch(range_high_pct=plan.range_high_pct + 0.5, notes="Expanded range high.")
        if "increase grid" in lowered:
            return StrategyPatch(grid_count=plan.grid_count + 2, notes="Added more grid levels.")
        if "lower budget" in lowered:
            return StrategyPatch(budget=max(plan.budget * 0.8, 25.0), notes="Reduced budget.")
        return "Noted. No strategy changes suggested from that message."


def apply_patch(plan: StrategyPlan, patch: StrategyPatch) -> StrategyPlan:
    return replace(
        plan,
        budget=patch.budget if patch.budget is not None else plan.budget,
        mode=patch.mode if patch.mode is not None else plan.mode,
        grid_count=patch.grid_count if patch.grid_count is not None else plan.grid_count,
        grid_step_pct=patch.grid_step_pct if patch.grid_step_pct is not None else plan.grid_step_pct,
        range_low_pct=patch.range_low_pct if patch.range_low_pct is not None else plan.range_low_pct,
        range_high_pct=patch.range_high_pct if patch.range_high_pct is not None else plan.range_high_pct,
        refresh_interval_s=patch.refresh_interval_s if patch.refresh_interval_s is not None else plan.refresh_interval_s,
        notes=patch.notes if patch.notes is not None else plan.notes,
    )


def _period_to_hours(period: str) -> int:
    mapping = {
        "1h": 1,
        "4h": 4,
        "12h": 12,
        "24h": 24,
        "7d": 168,
    }
    return mapping.get(period, 4)
