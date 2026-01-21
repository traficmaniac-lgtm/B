from src.ai.operator_math import compute_break_even_tp_pct, compute_fee_total_pct, evaluate_tp_profitability


def test_break_even_includes_fees_and_slippage() -> None:
    fee_total = compute_fee_total_pct(0.05, 0.1, fill_mode="MAKER")
    break_even = compute_break_even_tp_pct(fee_total_pct=fee_total, slippage_pct=0.02, safety_edge_pct=0.02)
    assert break_even == round(0.05 * 2 + 0.02 + 0.02, 6)


def test_fee_total_maker_maker() -> None:
    assert compute_fee_total_pct(0.04, 0.06, fill_mode="MAKER") == 0.08


def test_fee_total_taker_taker() -> None:
    assert compute_fee_total_pct(0.04, 0.06, fill_mode="TAKER") == 0.12


def test_fee_total_zero_fee_pair() -> None:
    assert compute_fee_total_pct(0.0, 0.0, fill_mode="UNKNOWN") == 0.0


def test_tp_below_break_even_is_not_profitable() -> None:
    result = evaluate_tp_profitability(
        tp_pct=0.05,
        fee_total_pct=0.08,
        slippage_pct=0.02,
        safety_edge_pct=0.02,
        target_profit_pct=0.05,
    )
    assert result["is_profitable"] is False
