from __future__ import annotations

from typing import Any

from src.ai.operator_profiles import get_profile_preset

DEFAULT_SLIPPAGE_PCT = 0.02
DEFAULT_SAFETY_EDGE_PCT = 0.02


def estimate_grid_edge(
    step_pct: float | None,
    tp_pct: float | None,
    *,
    maker_fee_pct: float | None,
    taker_fee_pct: float | None,
    slippage_pct: float | None,
    spread_pct: float | None,
    profile: str,
    expected_fill_mode: str | None = None,
    fee_discount_pct: float | None = None,
    safety_edge_pct: float | None = None,
) -> dict[str, Any]:
    gross_edge_pct = tp_pct if tp_pct is not None else (step_pct or 0.0)
    fill_mode = _normalize_fill_mode(expected_fill_mode)
    fee_total_pct = compute_fee_total_pct(
        maker_fee_pct,
        taker_fee_pct,
        fill_mode=fill_mode,
        fee_discount_pct=fee_discount_pct,
    )
    assumed_slippage = _coerce_pct(slippage_pct, DEFAULT_SLIPPAGE_PCT)
    safety_edge = _coerce_pct(safety_edge_pct, DEFAULT_SAFETY_EDGE_PCT)
    break_even_tp_pct = compute_break_even_tp_pct(
        fee_total_pct=fee_total_pct,
        slippage_pct=assumed_slippage,
        safety_edge_pct=safety_edge,
    )
    net_edge_pct = compute_net_edge_pct(
        tp_pct=gross_edge_pct,
        fee_total_pct=fee_total_pct,
        slippage_pct=assumed_slippage,
    )
    preset = get_profile_preset(profile)
    target_profit_pct = preset.target_profit_pct
    min_tp_pct = break_even_tp_pct + target_profit_pct
    return {
        "gross_edge_pct": round(gross_edge_pct, 6),
        "fee_total_pct": round(fee_total_pct, 6),
        "net_edge_pct": round(net_edge_pct, 6),
        "break_even_tp_pct": round(break_even_tp_pct, 6),
        "min_tp_pct": round(min_tp_pct, 6),
        "assumptions": {
            "maker_fee_pct": _coerce_pct(maker_fee_pct, 0.0),
            "taker_fee_pct": _coerce_pct(taker_fee_pct, 0.0),
            "fee_discount_pct": fee_discount_pct,
            "assumed_slippage_pct": assumed_slippage,
            "safety_edge_pct": safety_edge,
            "expected_fill_mode": fill_mode,
            "profile_target_profit_pct": target_profit_pct,
            "spread_pct": spread_pct,
        },
    }


def compute_fee_total_pct(
    maker_fee_pct: float | None,
    taker_fee_pct: float | None,
    *,
    fill_mode: str = "maker",
    fee_discount_pct: float | None = None,
) -> float:
    maker = _coerce_pct(maker_fee_pct, 0.0)
    taker = _coerce_pct(taker_fee_pct, maker)
    if fill_mode in {"MAKER", "MAKER_MAKER"}:
        total = maker * 2
    elif fill_mode in {"MIXED", "MAKER_TAKER"}:
        total = maker + taker
    elif fill_mode in {"TAKER", "TAKER_TAKER"}:
        total = taker * 2
    else:
        total = taker * 2
    if fee_discount_pct is not None:
        discount_factor = max(0.0, 1 - (fee_discount_pct / 100))
        total *= discount_factor
    return round(total, 6)


def compute_break_even_tp_pct(
    *,
    fee_total_pct: float,
    slippage_pct: float | None = None,
    safety_edge_pct: float | None = None,
) -> float:
    slippage = _coerce_pct(slippage_pct, DEFAULT_SLIPPAGE_PCT)
    safety = _coerce_pct(safety_edge_pct, DEFAULT_SAFETY_EDGE_PCT)
    return round(fee_total_pct + slippage + safety, 6)


def compute_net_edge_pct(
    *,
    tp_pct: float,
    fee_total_pct: float,
    slippage_pct: float | None = None,
) -> float:
    slippage = _coerce_pct(slippage_pct, DEFAULT_SLIPPAGE_PCT)
    return round(tp_pct - fee_total_pct - slippage, 6)


def min_step_pct_from_rules(price: float | None, tick_size: float | None) -> float | None:
    if price is None or tick_size is None:
        return None
    if price <= 0 or tick_size <= 0:
        return None
    min_step_abs = tick_size * 2
    return (min_step_abs / price) * 100


def min_step_pct_from_fees(
    *,
    fee_total_pct: float,
    slippage_pct: float | None = None,
    safety_edge_pct: float | None = None,
) -> float:
    return compute_break_even_tp_pct(
        fee_total_pct=fee_total_pct,
        slippage_pct=slippage_pct,
        safety_edge_pct=safety_edge_pct,
    )


def min_step_pct(
    price: float | None,
    tick_size: float | None,
    *,
    fee_total_pct: float,
    slippage_pct: float | None = None,
    safety_edge_pct: float | None = None,
) -> float | None:
    tick_min = min_step_pct_from_rules(price, tick_size)
    fee_min = min_step_pct_from_fees(
        fee_total_pct=fee_total_pct,
        slippage_pct=slippage_pct,
        safety_edge_pct=safety_edge_pct,
    )
    if tick_min is None:
        return fee_min
    return max(tick_min, fee_min)


def evaluate_tp_profitability(
    *,
    tp_pct: float,
    fee_total_pct: float,
    slippage_pct: float | None,
    safety_edge_pct: float | None,
    target_profit_pct: float,
) -> dict[str, float | bool]:
    break_even_tp_pct = compute_break_even_tp_pct(
        fee_total_pct=fee_total_pct,
        slippage_pct=slippage_pct,
        safety_edge_pct=safety_edge_pct,
    )
    min_tp_pct = break_even_tp_pct + target_profit_pct
    net_edge_pct = compute_net_edge_pct(
        tp_pct=tp_pct,
        fee_total_pct=fee_total_pct,
        slippage_pct=slippage_pct,
    )
    return {
        "break_even_tp_pct": round(break_even_tp_pct, 6),
        "min_tp_pct": round(min_tp_pct, 6),
        "net_edge_pct": round(net_edge_pct, 6),
        "is_profitable": tp_pct >= min_tp_pct,
    }


def _normalize_fill_mode(mode: str | None) -> str:
    if not mode:
        return "MAKER"
    normalized = mode.strip().upper().replace("-", "_")
    mapping = {
        "MAKER": "MAKER",
        "MAKER_MAKER": "MAKER_MAKER",
        "MAKER_GRID": "MAKER_MAKER",
        "MIXED": "MIXED",
        "MAKER_TAKER": "MAKER_TAKER",
        "TAKER": "TAKER",
        "TAKER_TAKER": "TAKER_TAKER",
        "UNKNOWN": "UNKNOWN",
    }
    return mapping.get(normalized, "UNKNOWN")


def _coerce_pct(value: float | None, default: float) -> float:
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default
