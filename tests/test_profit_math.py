from src.ai.operator_math import estimate_grid_edge


def test_break_even_includes_fees_and_slippage() -> None:
    result = estimate_grid_edge(
        step_pct=0.2,
        tp_pct=0.2,
        maker_fee_pct=0.05,
        taker_fee_pct=0.1,
        slippage_pct=0.02,
        spread_pct=0.04,
        profile="CONSERVATIVE",
    )
    expected_total = (0.05 * 2) + (0.02 * 2) + (0.04 / 2)
    assert result["total_cost_pct"] == round(expected_total, 6)
    assert result["break_even_tp_pct"] == round(expected_total + 0.05, 6)


def test_net_edge_negative_blocks_start() -> None:
    result = estimate_grid_edge(
        step_pct=0.05,
        tp_pct=0.05,
        maker_fee_pct=0.08,
        taker_fee_pct=0.1,
        slippage_pct=0.05,
        spread_pct=0.05,
        profile="BALANCED",
    )
    assert result["net_edge_pct"] < 0
