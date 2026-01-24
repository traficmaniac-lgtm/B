from decimal import Decimal

import pytest

from src.gui.lite_grid_math import (
    align_tick_based_qty,
    bps_to_frac,
    evaluate_tp_min_profit_bps,
    should_block_bidask_actions,
)


def test_min_profit_units() -> None:
    assert bps_to_frac(0.02) == pytest.approx(0.000002)


def test_tp_gate_allows_1tick_when_min_profit_small() -> None:
    result = evaluate_tp_min_profit_bps(Decimal("1.1835"), Decimal("1.1836"), "BUY", 0.02)
    assert result["is_profitable"] is True
    assert result["profit_bps"] == pytest.approx(0.845, abs=0.01)


def test_qty_symmetry_tick_based() -> None:
    buy_qty, sell_qty, _fixed = align_tick_based_qty(Decimal("104.8"), Decimal("211.2"), Decimal("0.1"))
    assert buy_qty == sell_qty


def test_block_on_stale_bidask() -> None:
    block, reason = should_block_bidask_actions(False, "fresh")
    assert block is True
    assert reason == "feed_not_ok"
