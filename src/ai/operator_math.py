from __future__ import annotations

from typing import Any

from src.ai.operator_profiles import get_profile_preset


def estimate_grid_edge(
    step_pct: float | None,
    tp_pct: float | None,
    *,
    maker_fee_pct: float | None,
    taker_fee_pct: float | None,
    slippage_pct: float | None,
    spread_pct: float | None,
    profile: str,
) -> dict[str, Any]:
    gross_edge_pct = tp_pct if tp_pct is not None else (step_pct or 0.0)
    maker_fee = maker_fee_pct or 0.0
    taker_fee = taker_fee_pct or 0.0
    assumed_slippage = slippage_pct or 0.0
    spread_component = (spread_pct or 0.0) / 2
    total_cost_pct = (maker_fee * 2) + (assumed_slippage * 2) + spread_component
    preset = get_profile_preset(profile)
    break_even_tp_pct = total_cost_pct + preset.safety_margin_pct
    net_edge_pct = gross_edge_pct - total_cost_pct
    return {
        "gross_edge_pct": round(gross_edge_pct, 6),
        "total_cost_pct": round(total_cost_pct, 6),
        "net_edge_pct": round(net_edge_pct, 6),
        "break_even_tp_pct": round(break_even_tp_pct, 6),
        "assumptions": {
            "maker_fee_pct": maker_fee,
            "taker_fee_pct": taker_fee,
            "assumed_slippage_pct": assumed_slippage,
            "spread_component_pct": spread_component,
            "profile_safety_margin_pct": preset.safety_margin_pct,
        },
    }
