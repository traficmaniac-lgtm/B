from decimal import Decimal

from src.gui.lite_grid_math import compute_order_qty


def test_buy_qty_respects_quote_buffer_and_step() -> None:
    balances = {"quote_free": Decimal("100"), "base_free": Decimal("0")}
    rules = {"step": 0.01, "min_notional": 5.0, "min_qty": None, "max_qty": None}
    qty, notional, reason = compute_order_qty(
        "BUY",
        Decimal("10"),
        Decimal("100"),
        balances,
        rules,
        Decimal("0"),
    )
    assert reason == "ok"
    assert qty == Decimal("9.9")
    assert notional == Decimal("99.0")


def test_sell_qty_allows_sell_when_free_slightly_above_qty() -> None:
    balances = {"quote_free": Decimal("0"), "base_free": Decimal("0.000110")}
    rules = {"step": 0.000001, "min_notional": 1.0, "min_qty": None, "max_qty": None}
    price = Decimal("30000")
    desired_qty = Decimal("0.00010")
    qty, _, reason = compute_order_qty(
        "SELL",
        price,
        price * desired_qty,
        balances,
        rules,
        Decimal("0"),
    )
    assert reason == "ok"
    assert qty == desired_qty


def test_minNotional_enforced_after_rounding() -> None:
    balances = {"quote_free": Decimal("100"), "base_free": Decimal("0")}
    rules = {"step": 0.1, "min_notional": 10.0, "min_qty": None, "max_qty": None}
    qty, _, reason = compute_order_qty(
        "BUY",
        Decimal("10"),
        Decimal("9.9"),
        balances,
        rules,
        Decimal("0"),
    )
    assert qty == Decimal("0")
    assert reason == "min_notional"
