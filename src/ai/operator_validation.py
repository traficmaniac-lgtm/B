from __future__ import annotations

from typing import Any

from src.ai.operator_math import estimate_grid_edge
from src.ai.operator_models import StrategyPatch
from src.ai.operator_profiles import ProfilePreset, get_profile_preset


def min_step_pct_from_rules(price: float | None, tick_size: float | None) -> float | None:
    if price is None or tick_size is None:
        return None
    if price <= 0 or tick_size <= 0:
        return None
    min_step_abs = tick_size * 2
    return (min_step_abs / price) * 100


def validate_strategy_patch(
    patch: StrategyPatch,
    *,
    profile: str,
    price: float | None,
    rules: dict[str, Any],
    maker_fee_pct: float | None,
    taker_fee_pct: float | None,
    slippage_pct: float | None,
    spread_pct: float | None,
) -> tuple[bool, list[str], dict[str, Any], ProfilePreset]:
    preset = get_profile_preset(profile)
    reasons: list[str] = []

    min_step_pct = min_step_pct_from_rules(price, _to_float(rules.get("tickSize") or rules.get("tick_size")))
    if patch.step_pct is None:
        reasons.append("step_missing")
    else:
        if min_step_pct is not None and patch.step_pct < min_step_pct:
            reasons.append("step_below_min")
        if patch.step_pct <= 0:
            reasons.append("step_non_positive")

    if patch.levels is None:
        reasons.append("levels_missing")
    else:
        if patch.levels < 2 or patch.levels > 20:
            reasons.append("levels_out_of_bounds")
        if patch.levels < preset.levels_min or patch.levels > preset.levels_max:
            reasons.append("levels_profile_bounds")

    if patch.max_active_orders is not None and patch.max_active_orders > preset.max_active_orders:
        reasons.append("max_active_orders_exceeded")

    min_range_from_step = None
    if patch.step_pct is not None and patch.levels is not None and patch.levels > 0:
        min_range_from_step = patch.step_pct * (patch.levels / 2)
    for side_label, value in (("range_down_pct", patch.range_down_pct), ("range_up_pct", patch.range_up_pct)):
        if value is None:
            reasons.append(f"{side_label}_missing")
            continue
        if min_range_from_step is not None and value < min_range_from_step:
            reasons.append(f"{side_label}_below_min_range")
        if value < preset.min_range_pct or value > preset.max_range_pct:
            reasons.append(f"{side_label}_profile_bounds")

    profit_estimate = estimate_grid_edge(
        patch.step_pct,
        patch.tp_pct,
        maker_fee_pct=maker_fee_pct,
        taker_fee_pct=taker_fee_pct,
        slippage_pct=slippage_pct,
        spread_pct=spread_pct,
        profile=profile,
    )
    break_even_tp_pct = profit_estimate.get("break_even_tp_pct")
    if patch.tp_pct is None:
        reasons.append("tp_missing")
    else:
        if break_even_tp_pct is not None and patch.tp_pct < break_even_tp_pct:
            reasons.append("tp_below_break_even")

    net_edge_pct = profit_estimate.get("net_edge_pct")
    if isinstance(net_edge_pct, (int, float)) and net_edge_pct <= 0:
        reasons.append("net_edge_nonpositive")
    if isinstance(net_edge_pct, (int, float)) and net_edge_pct < preset.target_net_edge_pct:
        reasons.append("net_edge_below_target")

    return (not reasons, reasons, profit_estimate, preset)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
