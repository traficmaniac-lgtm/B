from decimal import Decimal

from src.gui.lite_grid_math import build_action_key, compute_order_qty


class FakeExchange:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Decimal, Decimal]] = []

    def place_order(self, side: str, price: Decimal, qty: Decimal) -> None:
        self.calls.append((side, price, qty))


def maybe_place_restore(
    client: FakeExchange,
    action_keys: set[str],
    balances: dict[str, Decimal],
    rules: dict[str, float | None],
    price: Decimal,
    desired_qty: Decimal,
) -> tuple[bool, str]:
    desired_notional = price * desired_qty
    qty, _, reason = compute_order_qty(
        "BUY",
        price,
        desired_notional,
        balances,
        rules,
        Decimal("0"),
    )
    step = Decimal(str(rules["step"])) if rules.get("step") is not None else None
    if qty <= 0:
        return False, reason
    action_key = build_action_key("RESTORE", "order-1", price, qty, step)
    if action_key in action_keys:
        return False, "duplicate"
    action_keys.add(action_key)
    client.place_order("BUY", price, qty)
    return True, "ok"


def test_restore_skipped_when_quote_insufficient() -> None:
    client = FakeExchange()
    action_keys: set[str] = set()
    balances = {"quote_free": Decimal("0.5"), "base_free": Decimal("0")}
    rules = {"step": 0.01, "min_notional": 1.0, "min_qty": None, "max_qty": None}
    placed, reason = maybe_place_restore(
        client,
        action_keys,
        balances,
        rules,
        Decimal("10"),
        Decimal("1"),
    )
    assert not placed
    assert "insufficient_quote" in reason
    placed_again, _ = maybe_place_restore(
        client,
        action_keys,
        balances,
        rules,
        Decimal("10"),
        Decimal("1"),
    )
    assert not placed_again
    assert client.calls == []
