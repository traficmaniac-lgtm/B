from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import Lock


class VirtualOrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class VirtualOrderStatus(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"


@dataclass
class VirtualOrder:
    id: str
    side: VirtualOrderSide
    price: float
    qty: float
    status: VirtualOrderStatus
    created_at: datetime
    filled_at: datetime | None = None


class VirtualOrderBook:
    def __init__(self) -> None:
        self._orders: dict[str, VirtualOrder] = {}
        self._lock = Lock()
        self._counter = 0

    def create_order(self, side: VirtualOrderSide, price: float, qty: float) -> VirtualOrder:
        with self._lock:
            self._counter += 1
            order_id = f"VO-{self._counter:04d}"
            order = VirtualOrder(
                id=order_id,
                side=side,
                price=price,
                qty=qty,
                status=VirtualOrderStatus.OPEN,
                created_at=datetime.now(timezone.utc),
            )
            self._orders[order_id] = order
            return order

    def cancel_order(self, order_id: str) -> VirtualOrder | None:
        with self._lock:
            order = self._orders.get(order_id)
            if not order or order.status != VirtualOrderStatus.OPEN:
                return None
            order.status = VirtualOrderStatus.CANCELED
            return order

    def cancel_all(self) -> list[VirtualOrder]:
        canceled: list[VirtualOrder] = []
        with self._lock:
            for order in self._orders.values():
                if order.status == VirtualOrderStatus.OPEN:
                    order.status = VirtualOrderStatus.CANCELED
                    canceled.append(order)
        return canceled

    def list_open_orders(self) -> list[VirtualOrder]:
        with self._lock:
            return [order for order in self._orders.values() if order.status == VirtualOrderStatus.OPEN]

    def list_filled_orders(self) -> list[VirtualOrder]:
        with self._lock:
            return [order for order in self._orders.values() if order.status == VirtualOrderStatus.FILLED]

    def list_all_orders(self) -> list[VirtualOrder]:
        with self._lock:
            return list(self._orders.values())

    def check_fills(self, market_price: float) -> list[VirtualOrder]:
        filled: list[VirtualOrder] = []
        with self._lock:
            for order in self._orders.values():
                if order.status != VirtualOrderStatus.OPEN:
                    continue
                if order.side == VirtualOrderSide.BUY and market_price <= order.price:
                    order.status = VirtualOrderStatus.FILLED
                    order.filled_at = datetime.now(timezone.utc)
                    filled.append(order)
                elif order.side == VirtualOrderSide.SELL and market_price >= order.price:
                    order.status = VirtualOrderStatus.FILLED
                    order.filled_at = datetime.now(timezone.utc)
                    filled.append(order)
        return filled
