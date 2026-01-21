from __future__ import annotations

from typing import Any

from dataclasses import replace

from src.ai.operator_math import (
    compute_fee_total_pct,
    evaluate_tp_profitability,
    estimate_grid_edge,
    min_step_pct,
)
from src.ai.operator_models import StrategyPatch
from src.ai.operator_profiles import ProfilePreset, get_profile_preset


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
    expected_fill_mode: str | None = None,
    fee_discount_pct: float | None = None,
    safety_edge_pct: float | None = None,
) -> tuple[bool, list[str], dict[str, Any], ProfilePreset]:
    preset = get_profile_preset(profile)
    reasons: list[str] = []

    fee_total_pct = compute_fee_total_pct(
        maker_fee_pct,
        taker_fee_pct,
        fill_mode=expected_fill_mode or "MAKER",
        fee_discount_pct=fee_discount_pct,
    )
    min_step_needed = min_step_pct(
        price,
        _to_float(rules.get("tickSize") or rules.get("tick_size")),
        fee_total_pct=fee_total_pct,
        slippage_pct=slippage_pct,
        safety_edge_pct=safety_edge_pct,
    )
    if patch.step_pct is None:
        reasons.append("step_missing")
    else:
        if min_step_needed is not None and patch.step_pct < min_step_needed:
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
        expected_fill_mode=expected_fill_mode,
        fee_discount_pct=fee_discount_pct,
        safety_edge_pct=safety_edge_pct,
    )
    break_even_tp_pct = profit_estimate.get("break_even_tp_pct")
    min_tp_pct = profit_estimate.get("min_tp_pct")
    if patch.tp_pct is None:
        reasons.append("tp_missing")
    else:
        if break_even_tp_pct is not None and patch.tp_pct < break_even_tp_pct:
            reasons.append("tp_below_break_even")
        if min_tp_pct is not None and patch.tp_pct < min_tp_pct:
            reasons.append("tp_below_target")

    net_edge_pct = profit_estimate.get("net_edge_pct")
    if isinstance(net_edge_pct, (int, float)) and net_edge_pct <= 0:
        reasons.append("net_edge_nonpositive")
    if isinstance(net_edge_pct, (int, float)) and net_edge_pct < preset.target_net_edge_pct:
        reasons.append("net_edge_below_target")

    return (not reasons, reasons, profit_estimate, preset)


def normalize_strategy_patch(
    patch: StrategyPatch,
    *,
    profile: str,
    price: float | None,
    rules: dict[str, Any],
    maker_fee_pct: float | None,
    taker_fee_pct: float | None,
    slippage_pct: float | None,
    expected_fill_mode: str | None = None,
    fee_discount_pct: float | None = None,
    safety_edge_pct: float | None = None,
) -> tuple[StrategyPatch, list[str], dict[str, Any]]:
    preset = get_profile_preset(profile)
    adjustments: list[str] = []
    normalized = replace(patch)

    fee_total_pct = compute_fee_total_pct(
        maker_fee_pct,
        taker_fee_pct,
        fill_mode=expected_fill_mode or "MAKER",
        fee_discount_pct=fee_discount_pct,
    )
    min_step_needed = min_step_pct(
        price,
        _to_float(rules.get("tickSize") or rules.get("tick_size")),
        fee_total_pct=fee_total_pct,
        slippage_pct=slippage_pct,
        safety_edge_pct=safety_edge_pct,
    )
    if normalized.step_pct is None or normalized.step_pct <= 0:
        fallback = min_step_needed or 0.05
        normalized.step_pct = fallback
        adjustments.append(f"step_pct→{fallback:.6f}")
    elif min_step_needed is not None and normalized.step_pct < min_step_needed:
        normalized.step_pct = min_step_needed
        adjustments.append(f"step_pct→{min_step_needed:.6f}")

    if normalized.levels is None:
        normalized.levels = preset.levels_min
        adjustments.append(f"levels→{preset.levels_min}")
    else:
        clamped_levels = min(max(normalized.levels, preset.levels_min), preset.levels_max)
        if clamped_levels != normalized.levels:
            normalized.levels = clamped_levels
            adjustments.append(f"levels→{clamped_levels}")

    if normalized.max_active_orders is None:
        normalized.max_active_orders = preset.max_active_orders
    elif normalized.max_active_orders > preset.max_active_orders:
        normalized.max_active_orders = preset.max_active_orders
        adjustments.append(f"max_active_orders→{preset.max_active_orders}")

    min_range_from_step = None
    if normalized.step_pct is not None and normalized.levels:
        min_range_from_step = normalized.step_pct * (normalized.levels / 2)
    min_range = max(
        preset.min_range_pct,
        min_range_from_step or preset.min_range_pct,
    )
    for attr in ("range_down_pct", "range_up_pct"):
        value = getattr(normalized, attr)
        if value is None or value <= 0:
            setattr(normalized, attr, min_range)
            adjustments.append(f"{attr}→{min_range:.6f}")
            continue
        if value < min_range:
            setattr(normalized, attr, min_range)
            adjustments.append(f"{attr}→{min_range:.6f}")
            continue
        if value > preset.max_range_pct:
            setattr(normalized, attr, preset.max_range_pct)
            adjustments.append(f"{attr}→{preset.max_range_pct:.6f}")

    if normalized.tp_pct is None or normalized.tp_pct <= 0:
        normalized.tp_pct = max(preset.target_profit_pct + preset.safety_margin_pct, 0.1)
        adjustments.append(f"tp_pct→{normalized.tp_pct:.6f}")

    profit = evaluate_tp_profitability(
        tp_pct=normalized.tp_pct,
        fee_total_pct=fee_total_pct,
        slippage_pct=slippage_pct,
        safety_edge_pct=safety_edge_pct,
        target_profit_pct=preset.target_profit_pct,
    )
    min_tp_pct = profit.get("min_tp_pct")
    if isinstance(min_tp_pct, (int, float)) and normalized.tp_pct < min_tp_pct:
        normalized.tp_pct = float(min_tp_pct)
        adjustments.append(f"tp_pct→{float(min_tp_pct):.6f}")
        profit = evaluate_tp_profitability(
            tp_pct=normalized.tp_pct,
            fee_total_pct=fee_total_pct,
            slippage_pct=slippage_pct,
            safety_edge_pct=safety_edge_pct,
            target_profit_pct=preset.target_profit_pct,
        )
    return normalized, adjustments, profit


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
