from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Protocol


class AccountClientProtocol(Protocol):
    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        ...

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        ...


@dataclass
class CancelResult:
    canceled_count: int
    remaining_orders: list[dict[str, Any]]


def cancel_all_bot_orders(
    *,
    symbol: str,
    account_client: AccountClientProtocol,
    order_ids: set[str],
    client_order_id_prefix: str,
    timeout_s: float = 3.0,
    open_orders_provider: Callable[[], list[dict[str, Any]]] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> CancelResult:
    if open_orders_provider is None:
        open_orders_provider = lambda: account_client.get_open_orders(symbol)

    ids_to_cancel = [order_id for order_id in order_ids if order_id]
    if not ids_to_cancel:
        open_orders = open_orders_provider()
        ids_to_cancel = [
            str(order.get("orderId", ""))
            for order in open_orders
            if _matches_prefix(order, client_order_id_prefix)
        ]
    canceled = 0
    for order_id in ids_to_cancel:
        if not order_id:
            continue
        try:
            account_client.cancel_order(symbol, order_id)
            canceled += 1
        except Exception:
            continue

    deadline = time.monotonic() + timeout_s
    remaining_orders: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        open_orders = open_orders_provider()
        remaining_orders = [
            order for order in open_orders if _matches_prefix(order, client_order_id_prefix)
        ]
        if not remaining_orders:
            break
        sleep_fn(0.2)

    return CancelResult(canceled_count=canceled, remaining_orders=remaining_orders)


def pause_state(cancel_fn: Callable[[], CancelResult]) -> tuple[str, CancelResult]:
    return "PAUSED", cancel_fn()


def stop_state(cancel_fn: Callable[[], CancelResult]) -> tuple[str, CancelResult]:
    return "STOPPED", cancel_fn()


def _matches_prefix(order: dict[str, Any], prefix: str) -> bool:
    client_order_id = str(order.get("clientOrderId", ""))
    return bool(prefix and client_order_id.startswith(prefix))
