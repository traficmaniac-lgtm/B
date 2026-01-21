from src.ai.operator_models import StrategyPatch
from src.ai.operator_validation import normalize_strategy_patch, validate_strategy_patch


def test_rejects_step_below_min_step() -> None:
    patch = StrategyPatch(
        profile="BALANCED",
        bias="FLAT",
        range_mode="MANUAL",
        step_pct=0.05,
        range_down_pct=2.0,
        range_up_pct=2.0,
        levels=4,
        tp_pct=0.2,
        max_active_orders=8,
        notes="",
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
        expected_fill_mode="MAKER",
        safety_edge_pct=0.02,
    )
    assert not is_valid
    assert "step_below_min" in reasons


def test_rejects_tp_below_break_even_margin() -> None:
    patch = StrategyPatch(
        profile="CONSERVATIVE",
        bias="FLAT",
        range_mode="MANUAL",
        step_pct=0.3,
        range_down_pct=2.0,
        range_up_pct=2.0,
        levels=4,
        tp_pct=0.02,
        max_active_orders=8,
        notes="",
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
        expected_fill_mode="MAKER",
        safety_edge_pct=0.02,
    )
    assert not is_valid
    assert "tp_below_break_even" in reasons


def test_rejects_insane_range_even_if_clamped() -> None:
    patch = StrategyPatch(
        profile="BALANCED",
        bias="FLAT",
        range_mode="MANUAL",
        step_pct=0.2,
        range_down_pct=0.5,
        range_up_pct=0.5,
        levels=4,
        tp_pct=0.2,
        max_active_orders=8,
        notes="",
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
        expected_fill_mode="MAKER",
        safety_edge_pct=0.02,
    )
    assert not is_valid
    assert "range_down_pct_profile_bounds" in reasons
    assert "range_up_pct_profile_bounds" in reasons


def test_accepts_sane_patch_within_profile() -> None:
    patch = StrategyPatch(
        profile="BALANCED",
        bias="FLAT",
        range_mode="MANUAL",
        step_pct=0.4,
        range_down_pct=2.5,
        range_up_pct=2.8,
        levels=4,
        tp_pct=0.3,
        max_active_orders=8,
        notes="",
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
        expected_fill_mode="MAKER",
        safety_edge_pct=0.02,
    )
    assert is_valid
    assert reasons == []


def test_normalize_patch_clamps_non_positive_values() -> None:
    patch = StrategyPatch(
        profile="BALANCED",
        bias="FLAT",
        range_mode="MANUAL",
        step_pct=0.0,
        range_down_pct=-1.0,
        range_up_pct=0.0,
        levels=0,
        tp_pct=0.0,
        max_active_orders=50,
        notes="",
    )
    normalized, adjustments, profit = normalize_strategy_patch(
        patch,
        profile="BALANCED",
        price=100.0,
        rules={"tickSize": 0.01},
        maker_fee_pct=0.01,
        taker_fee_pct=0.01,
        slippage_pct=0.01,
        expected_fill_mode="MAKER",
        safety_edge_pct=0.02,
    )
    assert normalized.step_pct > 0
    assert normalized.levels >= 2
    assert normalized.range_down_pct > 0
    assert normalized.range_up_pct > 0
    assert normalized.tp_pct > 0
    assert normalized.max_active_orders <= 12
    assert profit["min_tp_pct"] is not None
    assert adjustments
