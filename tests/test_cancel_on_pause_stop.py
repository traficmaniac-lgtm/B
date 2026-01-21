from __future__ import annotations

from typing import Any

from src.ai.operator_runtime import cancel_all_bot_orders, pause_state, stop_state


class FakeAccountClient:
    def __init__(self, orders: list[dict[str, Any]]) -> None:
        self._orders = orders

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        self._orders = [order for order in self._orders if str(order.get("orderId")) != order_id]
        return {"symbol": symbol, "orderId": order_id}

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return list(self._orders)


def test_cancel_on_pause_stop() -> None:
    prefix = "BBOT_LITE_BTCUSDT_1234_"
    orders = [
        {"orderId": "1", "clientOrderId": f"{prefix}BUY_1"},
        {"orderId": "2", "clientOrderId": "OTHER"},
    ]
    client = FakeAccountClient(orders)

    def cancel_fn() -> Any:
        return cancel_all_bot_orders(
            symbol="BTCUSDT",
            account_client=client,
            order_ids=set(),
            client_order_id_prefix=prefix,
            timeout_s=0.1,
        )

    pause_state_value, pause_result = pause_state(cancel_fn)
    assert pause_state_value == "PAUSED"
    assert pause_result.canceled_count == 1

    stop_state_value, stop_result = stop_state(cancel_fn)
    assert stop_state_value == "STOPPED"
    assert stop_result.canceled_count == 0
