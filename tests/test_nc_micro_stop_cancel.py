from __future__ import annotations

from typing import Any

from src.core.cancel_reconcile import cancel_all_with_reconcile
from src.core.nc_micro_stop import finalize_stop_state


class FakeAccountClient:
    def __init__(self, open_orders: list[dict[str, Any]]) -> None:
        self._open_orders = open_orders
        self.get_calls = 0

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        self.get_calls += 1
        return list(self._open_orders)

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        self._open_orders = [
            order for order in self._open_orders if str(order.get("orderId")) != order_id
        ]
        return {"symbol": symbol, "orderId": order_id}


def test_stop_always_finalizes_when_cancel_times_out() -> None:
    state = {"state": "STOPPING", "inflight": True, "start_enabled": False}
    logs: list[tuple[str, str]] = []

    def set_inflight(value: bool) -> None:
        state["inflight"] = value

    def set_state(value: str) -> None:
        state["state"] = value

    def set_start_enabled(value: bool) -> None:
        state["start_enabled"] = value

    def log_fn(message: str, kind: str) -> None:
        logs.append((kind, message))

    def cancel_fn() -> None:
        raise TimeoutError("cancel timeout")

    done_with_errors = False
    try:
        cancel_fn()
    except TimeoutError:
        done_with_errors = True
    finally:
        finalize_stop_state(
            remaining=0,
            done_with_errors=done_with_errors,
            set_inflight=set_inflight,
            set_state=set_state,
            set_start_enabled=set_start_enabled,
            log_fn=log_fn,
            state="IDLE",
        )

    assert state["inflight"] is False
    assert state["state"] == "IDLE"
    assert state["start_enabled"] is True
    assert any("STOP_DONE_WITH_ERRORS" in message for _kind, message in logs)


def test_cancel_reconcile_uses_rest_not_snapshot() -> None:
    client = FakeAccountClient([{"orderId": "1"}])

    result = cancel_all_with_reconcile(
        account_client=client,
        symbol="EURIUSDT",
        reason="stop",
        timeout_s=0.5,
        max_passes=2,
        sleep_s=0.01,
    )

    assert client.get_calls >= 1
    assert result.cancelled_ids == ["1"]
