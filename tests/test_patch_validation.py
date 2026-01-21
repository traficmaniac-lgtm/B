from src.ai.operator_models import StrategyPatch
from src.ai.operator_validation import validate_strategy_patch


def test_rejects_step_below_min_step() -> None:
    patch = StrategyPatch(
        step_pct=0.05,
        range_down_pct=2.0,
        range_up_pct=2.0,
        levels=4,
        tp_pct=0.2,
        bias="FLAT",
        max_active_orders=8,
    )
    is_valid, reasons, _, _ = validate_strategy_patch(
        patch,
        profile="BALANCED",
        price=100.0,
        rules={"tickSize": 0.1},
        maker_fee_pct=0.01,
        taker_fee_pct=0.01,
        slippage_pct=0.01,
        spread_pct=0.02,
    )
    assert not is_valid
    assert "step_below_min" in reasons


def test_rejects_tp_below_break_even_margin() -> None:
    patch = StrategyPatch(
        step_pct=0.3,
        range_down_pct=2.0,
        range_up_pct=2.0,
        levels=4,
        tp_pct=0.02,
        bias="FLAT",
        max_active_orders=8,
    )
    is_valid, reasons, _, _ = validate_strategy_patch(
        patch,
        profile="CONSERVATIVE",
        price=100.0,
        rules={"tickSize": 0.01},
        maker_fee_pct=0.05,
        taker_fee_pct=0.05,
        slippage_pct=0.02,
        spread_pct=0.02,
    )
    assert not is_valid
    assert "tp_below_break_even" in reasons


def test_rejects_insane_range_even_if_clamped() -> None:
    patch = StrategyPatch(
        step_pct=0.2,
        range_down_pct=0.5,
        range_up_pct=0.5,
        levels=4,
        tp_pct=0.2,
        bias="FLAT",
        max_active_orders=8,
    )
    is_valid, reasons, _, _ = validate_strategy_patch(
        patch,
        profile="BALANCED",
        price=100.0,
        rules={"tickSize": 0.01},
        maker_fee_pct=0.01,
        taker_fee_pct=0.01,
        slippage_pct=0.01,
        spread_pct=0.02,
    )
    assert not is_valid
    assert "range_down_pct_profile_bounds" in reasons
    assert "range_up_pct_profile_bounds" in reasons


def test_accepts_sane_patch_within_profile() -> None:
    patch = StrategyPatch(
        step_pct=0.4,
        range_down_pct=2.5,
        range_up_pct=2.8,
        levels=4,
        tp_pct=0.3,
        bias="FLAT",
        max_active_orders=8,
    )
    is_valid, reasons, _, _ = validate_strategy_patch(
        patch,
        profile="BALANCED",
        price=100.0,
        rules={"tickSize": 0.01},
        maker_fee_pct=0.01,
        taker_fee_pct=0.01,
        slippage_pct=0.005,
        spread_pct=0.01,
    )
    assert is_valid
    assert reasons == []
