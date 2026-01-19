from __future__ import annotations

import statistics
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.core.logging import get_logger
from src.runtime.price_feed import PriceFeed
from src.runtime.runtime_state import RuntimeState
from src.runtime.strategy_executor import StrategyExecutor
from src.runtime.virtual_orders import VirtualOrder, VirtualOrderBook, VirtualOrderSide


@dataclass
class PnLSnapshot:
    realized: float
    unrealized: float
    exposure: float
    position_qty: float
    avg_price: float
    last_price: float | None


class RuntimeEngine:
    def __init__(self, symbol: str, strategy_snapshot: dict[str, Any]) -> None:
        self._symbol = symbol
        self._strategy_snapshot = strategy_snapshot
        self._logger = get_logger(f"runtime.engine.{symbol.lower()}")
        self._state = RuntimeState.IDLE
        self._state_lock = threading.Lock()
        self._event_handlers: dict[str, list] = {
            "order_created": [],
            "order_filled": [],
            "pnl_updated": [],
            "state_changed": [],
        }
        self._order_book = VirtualOrderBook()
        self._strategy = StrategyExecutor(strategy_snapshot, self._order_book)
        self._price_feed = PriceFeed(symbol, on_price=self._on_price_update)
        self._last_price: float | None = None
        self._last_price_timestamp: datetime | None = None
        self._last_latency: float | None = None
        self._price_history: deque[float] = deque(maxlen=120)
        self._fills: list[VirtualOrder] = []
        self._realized_pnl = 0.0
        self._avg_price = 0.0
        self._position_qty = 0.0
        self._max_total_pnl = 0.0

    def subscribe(self, event: str, callback: Any) -> None:
        if event in self._event_handlers:
            self._event_handlers[event].append(callback)

    def start(self) -> None:
        with self._state_lock:
            if self._state == RuntimeState.RUNNING:
                return
            if self._state == RuntimeState.IDLE:
                self._reset_runtime()
            previous = self._state
            self._state = RuntimeState.RUNNING
            self._log_transition(previous, self._state, reason="start")
            self._emit_state()
            self._price_feed.start()

    def pause(self) -> None:
        with self._state_lock:
            if self._state != RuntimeState.RUNNING:
                return
            previous = self._state
            self._state = RuntimeState.PAUSED
            self._log_transition(previous, self._state, reason="pause")
            self._emit_state()
            self._price_feed.stop()

    def stop(self, cancel_orders: bool = False) -> None:
        with self._state_lock:
            if self._state in {RuntimeState.IDLE, RuntimeState.STOPPED}:
                return
            previous = self._state
            self._state = RuntimeState.STOPPED
            self._log_transition(previous, self._state, reason="stop")
            self._emit_state()
            self._price_feed.stop()
            if cancel_orders:
                self._order_book.cancel_all()
            previous = self._state
            self._state = RuntimeState.IDLE
            self._log_transition(previous, self._state, reason="idle")
            self._emit_state()

    def get_state(self) -> RuntimeState:
        return self._state

    def get_orders(self) -> list[VirtualOrder]:
        return self._order_book.list_all_orders()

    def get_fills(self) -> list[VirtualOrder]:
        return list(self._fills)

    def get_pnl(self) -> PnLSnapshot:
        unrealized = self._calculate_unrealized(self._last_price)
        exposure = (self._last_price or 0.0) * self._position_qty
        return PnLSnapshot(
            realized=self._realized_pnl,
            unrealized=unrealized,
            exposure=exposure,
            position_qty=self._position_qty,
            avg_price=self._avg_price,
            last_price=self._last_price,
        )

    def get_observer_snapshot(self) -> dict[str, Any]:
        pnl = self.get_pnl()
        volatility = self._calculate_volatility()
        total_pnl = pnl.realized + pnl.unrealized
        self._max_total_pnl = max(self._max_total_pnl, total_pnl)
        drawdown = self._max_total_pnl - total_pnl
        return {
            "open_orders": [self._order_to_dict(o) for o in self._order_book.list_open_orders()],
            "fills": [self._order_to_dict(o) for o in self._fills],
            "pnl": {
                "realized": pnl.realized,
                "unrealized": pnl.unrealized,
                "exposure": pnl.exposure,
                "position_qty": pnl.position_qty,
                "avg_price": pnl.avg_price,
                "last_price": pnl.last_price,
            },
            "volatility": volatility,
            "drawdown": drawdown,
            "state": self._state.value,
            "latency_ms": self._last_latency * 1000 if self._last_latency is not None else None,
            "last_price_age_ms": self._last_price_age_ms(),
        }

    def get_recommendations(self) -> list[dict[str, Any]]:
        snapshot = self.get_observer_snapshot()
        volatility = snapshot["volatility"]
        drawdown = snapshot["drawdown"]
        recommendations: list[dict[str, Any]] = []
        if volatility and volatility > 0:
            recommendations.append(
                {
                    "rec_type": "ADJUST_PARAMS",
                    "reason": "Реальная волатильность повышена, рекомендуется расширить шаг сетки.",
                    "confidence": min(0.9, 0.5 + volatility / 1000),
                    "expected_effect": "Снизит частоту входов и риск резких разворотов.",
                    "can_apply_patch": False,
                }
            )
        if drawdown > 5:
            recommendations.append(
                {
                    "rec_type": "PAUSE_TRADING",
                    "reason": "Фиксируется просадка по PnL, лучше сделать паузу.",
                    "confidence": 0.6,
                    "expected_effect": "Защитит капитал до стабилизации.",
                    "can_apply_patch": False,
                }
            )
        if not recommendations:
            recommendations.append(
                {
                    "rec_type": "KEEP_RUNNING",
                    "reason": "Показатели стабильны, можно продолжать наблюдение.",
                    "confidence": 0.42,
                    "expected_effect": "Поддерживает текущий режим без изменений.",
                    "can_apply_patch": False,
                }
            )
        return recommendations

    def _reset_runtime(self) -> None:
        self._order_book = VirtualOrderBook()
        self._strategy = StrategyExecutor(self._strategy_snapshot, self._order_book)
        self._fills = []
        self._realized_pnl = 0.0
        self._avg_price = 0.0
        self._position_qty = 0.0
        self._price_history.clear()
        self._last_price = None
        self._last_price_timestamp = None
        self._last_latency = None
        self._max_total_pnl = 0.0

    def _emit(self, event: str, payload: Any) -> None:
        for callback in self._event_handlers.get(event, []):
            callback(payload)

    def _emit_state(self) -> None:
        self._emit("state_changed", self._state)

    def _log_transition(self, previous: RuntimeState, current: RuntimeState, reason: str) -> None:
        self._logger.info(
            "Runtime state transition",
            extra={"from": previous.value, "to": current.value, "reason": reason},
        )

    def _last_price_age_ms(self) -> float | None:
        if not self._last_price_timestamp:
            return None
        return max(
            0.0,
            (datetime.now(timezone.utc) - self._last_price_timestamp).total_seconds() * 1000,
        )

    def _on_price_update(self, price: float, timestamp: datetime, latency: float) -> None:
        if self._state != RuntimeState.RUNNING:
            return
        self._last_price = price
        self._last_price_timestamp = timestamp
        self._last_latency = latency
        self._price_history.append(price)

        created_orders = self._strategy.initialize(price)
        for order in created_orders:
            self._emit("order_created", order)

        filled_orders = self._order_book.check_fills(price)
        if filled_orders:
            for order in filled_orders:
                self._apply_fill(order)
                self._fills.append(order)
                self._emit("order_filled", order)
            new_orders = self._strategy.on_fills(filled_orders)
            for order in new_orders:
                self._emit("order_created", order)

        self._emit("pnl_updated", self.get_pnl())

    def _apply_fill(self, order: VirtualOrder) -> None:
        qty = order.qty
        price = order.price
        if order.side == VirtualOrderSide.BUY:
            if self._position_qty >= 0:
                total_qty = self._position_qty + qty
                self._avg_price = (
                    (self._avg_price * self._position_qty + price * qty) / total_qty
                    if total_qty
                    else 0.0
                )
                self._position_qty = total_qty
            else:
                close_qty = min(qty, abs(self._position_qty))
                self._realized_pnl += (self._avg_price - price) * close_qty
                self._position_qty += qty
                if self._position_qty > 0:
                    self._avg_price = price
                elif self._position_qty == 0:
                    self._avg_price = 0.0
        else:
            if self._position_qty <= 0:
                total_qty = abs(self._position_qty) + qty
                self._avg_price = (
                    (self._avg_price * abs(self._position_qty) + price * qty) / total_qty
                    if total_qty
                    else 0.0
                )
                self._position_qty -= qty
            else:
                close_qty = min(qty, self._position_qty)
                self._realized_pnl += (price - self._avg_price) * close_qty
                self._position_qty -= qty
                if self._position_qty < 0:
                    self._avg_price = price
                elif self._position_qty == 0:
                    self._avg_price = 0.0

    def _calculate_unrealized(self, price: float | None) -> float:
        if price is None:
            return 0.0
        if self._position_qty > 0:
            return (price - self._avg_price) * self._position_qty
        if self._position_qty < 0:
            return (self._avg_price - price) * abs(self._position_qty)
        return 0.0

    def _calculate_volatility(self) -> float:
        if len(self._price_history) < 2:
            return 0.0
        return statistics.pstdev(self._price_history)

    def _order_to_dict(self, order: VirtualOrder) -> dict[str, Any]:
        return {
            "id": order.id,
            "side": order.side.value,
            "price": order.price,
            "qty": order.qty,
            "status": order.status.value,
            "created_at": order.created_at.isoformat(),
            "filled_at": order.filled_at.isoformat() if order.filled_at else None,
        }
