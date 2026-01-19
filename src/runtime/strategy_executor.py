from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.logging import get_logger
from src.runtime.virtual_orders import VirtualOrder, VirtualOrderBook, VirtualOrderSide


@dataclass
class StrategyConfig:
    strategy_type: str
    budget: float
    grid_step_pct: float
    range_low_pct: float
    range_high_pct: float


class StrategyExecutor:
    def __init__(self, strategy_snapshot: dict[str, Any], order_book: VirtualOrderBook) -> None:
        self._logger = get_logger("runtime.strategy")
        self._order_book = order_book
        self._config = self._parse_snapshot(strategy_snapshot)
        self._initialized = False
        self._anchor_price: float | None = None

    def initialize(self, price: float) -> list[VirtualOrder]:
        if self._initialized:
            return []
        self._initialized = True
        self._anchor_price = price
        if self._config.strategy_type.lower() == "range":
            return self._build_range(price)
        return self._build_grid(price)

    def on_fills(self, filled_orders: list[VirtualOrder]) -> list[VirtualOrder]:
        if not filled_orders:
            return []
        if self._config.strategy_type.lower() == "range":
            return self._rebuild_range(filled_orders)
        return self._rebuild_grid(filled_orders)

    def _build_grid(self, price: float) -> list[VirtualOrder]:
        orders: list[VirtualOrder] = []
        step = max(self._config.grid_step_pct, 0.1) / 100
        levels = 2
        budget_per_order = self._config.budget / (levels * 2) if self._config.budget else 25.0
        qty = max(budget_per_order / price, 0.001)
        for level in range(1, levels + 1):
            buy_price = price * (1 - step * level)
            sell_price = price * (1 + step * level)
            orders.append(self._order_book.create_order(VirtualOrderSide.BUY, buy_price, qty))
            orders.append(self._order_book.create_order(VirtualOrderSide.SELL, sell_price, qty))
        self._logger.info("Grid orders created", extra={"count": len(orders)})
        return orders

    def _build_range(self, price: float) -> list[VirtualOrder]:
        orders: list[VirtualOrder] = []
        low_pct = max(self._config.range_low_pct, 0.5) / 100
        high_pct = max(self._config.range_high_pct, 0.5) / 100
        buy_price = price * (1 - low_pct)
        sell_price = price * (1 + high_pct)
        budget_per_order = self._config.budget / 2 if self._config.budget else 50.0
        qty = max(budget_per_order / price, 0.001)
        orders.append(self._order_book.create_order(VirtualOrderSide.BUY, buy_price, qty))
        orders.append(self._order_book.create_order(VirtualOrderSide.SELL, sell_price, qty))
        self._logger.info("Range orders created", extra={"count": len(orders)})
        return orders

    def _rebuild_grid(self, filled_orders: list[VirtualOrder]) -> list[VirtualOrder]:
        orders: list[VirtualOrder] = []
        step = max(self._config.grid_step_pct, 0.1) / 100
        for order in filled_orders:
            if order.side == VirtualOrderSide.BUY:
                target_price = order.price * (1 + step)
                orders.append(self._order_book.create_order(VirtualOrderSide.SELL, target_price, order.qty))
            else:
                target_price = order.price * (1 - step)
                orders.append(self._order_book.create_order(VirtualOrderSide.BUY, target_price, order.qty))
        return orders

    def _rebuild_range(self, filled_orders: list[VirtualOrder]) -> list[VirtualOrder]:
        orders: list[VirtualOrder] = []
        if self._anchor_price is None:
            return orders
        low_pct = max(self._config.range_low_pct, 0.5) / 100
        high_pct = max(self._config.range_high_pct, 0.5) / 100
        buy_price = self._anchor_price * (1 - low_pct)
        sell_price = self._anchor_price * (1 + high_pct)
        for order in filled_orders:
            if order.side == VirtualOrderSide.BUY:
                orders.append(self._order_book.create_order(VirtualOrderSide.SELL, sell_price, order.qty))
            else:
                orders.append(self._order_book.create_order(VirtualOrderSide.BUY, buy_price, order.qty))
        return orders

    def _parse_snapshot(self, snapshot: dict[str, Any]) -> StrategyConfig:
        strategy_type = snapshot.get("strategy_type", "Grid")
        budget = self._parse_float(snapshot.get("budget"), fallback=100.0)
        grid_step_pct = self._parse_float(snapshot.get("grid_step_pct"), fallback=0.5)
        range_low_pct = self._parse_float(snapshot.get("range_low_pct"), fallback=1.0)
        range_high_pct = self._parse_float(snapshot.get("range_high_pct"), fallback=1.0)
        if not snapshot.get("grid_step_pct"):
            grid_step_pct = self._parse_percent(snapshot.get("grid_step"), grid_step_pct)
        if not snapshot.get("range_low_pct") or not snapshot.get("range_high_pct"):
            range_low_pct, range_high_pct = self._parse_range(snapshot.get("range"), range_low_pct, range_high_pct)
        return StrategyConfig(
            strategy_type=strategy_type,
            budget=budget,
            grid_step_pct=grid_step_pct,
            range_low_pct=range_low_pct,
            range_high_pct=range_high_pct,
        )

    def _parse_percent(self, value: Any, fallback: float) -> float:
        if value is None:
            return fallback
        if isinstance(value, str):
            cleaned = value.replace("%", "").split("/")[0].strip()
            return self._parse_float(cleaned, fallback)
        return self._parse_float(value, fallback)

    def _parse_float(self, value: Any, fallback: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def _parse_range(self, value: Any, low_fallback: float, high_fallback: float) -> tuple[float, float]:
        if value is None:
            return low_fallback, high_fallback
        if isinstance(value, str) and "/" in value:
            parts = [part.strip().replace("%", "") for part in value.split("/")[:2]]
            if len(parts) == 2:
                return self._parse_float(parts[0], low_fallback), self._parse_float(parts[1], high_fallback)
        return low_fallback, high_fallback
