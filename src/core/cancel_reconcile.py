from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CancelResult:
    cancelled_ids: list[str]
    remaining_ids: list[str]
    errors: list[str]
    open_orders: list[dict[str, Any]]
    passes: int
    log_entries: list[str] = field(default_factory=list)


def cancel_all_with_reconcile(
    *,
    account_client: Any,
    symbol: str,
    reason: str,
    timeout_s: float = 4.0,
    max_passes: int = 3,
    sleep_s: float = 0.25,
) -> CancelResult:
    start_ts = time.monotonic()
    cancelled_ids: set[str] = set()
    remaining_ids: list[str] = []
    open_orders: list[dict[str, Any]] = []
    errors: list[str] = []
    log_entries: list[str] = []

    def _log(message: str) -> None:
        log_entries.append(message)

    passes = 0
    while passes < max_passes and time.monotonic() - start_ts < timeout_s:
        passes += 1
        try:
            fetched = account_client.get_open_orders(symbol)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"[CANCEL] reconcile failed: {exc}")
            fetched = []
        open_orders = fetched if isinstance(fetched, list) else []
        remaining_ids = [
            str(order.get("orderId", ""))
            for order in open_orders
            if isinstance(order, dict) and str(order.get("orderId", ""))
        ]
        if passes == 1:
            _log(f"[CANCEL] reconcile start symbol={symbol} found={len(remaining_ids)} reason={reason}")
        for order_id in remaining_ids:
            _log(f"[CANCEL] cancel try order_id={order_id}")
            try:
                account_client.cancel_order(symbol, order_id)
                cancelled_ids.add(order_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"[CANCEL] cancel failed order_id={order_id} error={exc}")
        _log(f"[CANCEL] reconcile pass={passes} remaining={len(remaining_ids)}")
        if not remaining_ids:
            break
        remaining = timeout_s - (time.monotonic() - start_ts)
        if remaining <= 0:
            break
        time.sleep(min(sleep_s, remaining))

    _log(
        f"[CANCEL] done cancelled={len(cancelled_ids)} "
        f"remaining={len(remaining_ids)} errors={len(errors)}"
    )
    return CancelResult(
        cancelled_ids=sorted(cancelled_ids),
        remaining_ids=remaining_ids,
        errors=errors,
        open_orders=[order for order in open_orders if isinstance(order, dict)],
        passes=passes,
        log_entries=log_entries,
    )
