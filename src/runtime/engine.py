from __future__ import annotations

import statistics
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.core.logging import get_logger
from src.services.price_feed_manager import MicrostructureSnapshot, PriceFeedManager, PriceUpdate
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
    def __init__(
        self,
        symbol: str,
        strategy_snapshot: dict[str, Any],
        price_feed_manager: PriceFeedManager,
    ) -> None:
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
        self._price_feed_manager = price_feed_manager
        self._last_price: float | None = None
        self._last_price_timestamp: datetime | None = None
        self._last_latency: float | None = None
        self._price_history: deque[tuple[int, float]] = deque(maxlen=240)
        self._fills: list[VirtualOrder] = []
        self._realized_pnl = 0.0
        self._avg_price = 0.0
        self._position_qty = 0.0
        self._max_total_pnl = 0.0
        self._ws_status: str = "LOST"
        self._microstructure: MicrostructureSnapshot | None = None

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
            self._price_feed_manager.subscribe(self._symbol, self._on_price_tick)
            self._price_feed_manager.subscribe_status(self._symbol, self._on_ws_status)
            self._price_feed_manager.start()

    def pause(self) -> None:
        with self._state_lock:
            if self._state != RuntimeState.RUNNING:
                return
            previous = self._state
            self._state = RuntimeState.PAUSED
            self._log_transition(previous, self._state, reason="pause")
            self._emit_state()
            self._price_feed_manager.unsubscribe(self._symbol, self._on_price_tick)
            self._price_feed_manager.unsubscribe_status(self._symbol, self._on_ws_status)

    def stop(self, cancel_orders: bool = False) -> None:
        with self._state_lock:
            if self._state in {RuntimeState.IDLE, RuntimeState.STOPPED}:
                return
            previous = self._state
            self._state = RuntimeState.STOPPED
            self._log_transition(previous, self._state, reason="stop")
            self._emit_state()
            self._price_feed_manager.unsubscribe(self._symbol, self._on_price_tick)
            self._price_feed_manager.unsubscribe_status(self._symbol, self._on_ws_status)
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
        micro_volatility = self._calculate_micro_volatility(window_ms=10_000)
        impulse_pct = self._calculate_impulse(window_ms=5_000)
        total_pnl = pnl.realized + pnl.unrealized
        self._max_total_pnl = max(self._max_total_pnl, total_pnl)
        drawdown = self._max_total_pnl - total_pnl
        micro = self._microstructure
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
            "micro_volatility": micro_volatility,
            "impulse_pct": impulse_pct,
            "spread_estimate": micro.spread_pct if micro else None,
            "best_bid": micro.best_bid if micro else None,
            "best_ask": micro.best_ask if micro else None,
            "mid_price": micro.mid_price if micro else None,
            "spread_abs": micro.spread_abs if micro else None,
            "spread_pct": micro.spread_pct if micro else None,
            "tick_size": micro.tick_size if micro else None,
            "step_size": micro.step_size if micro else None,
            "price_age_ms": micro.price_age_ms if micro else None,
            "ws_latency_ms": micro.ws_latency_ms if micro else None,
            "price_source": micro.source if micro else None,
            "drawdown": drawdown,
            "state": self._state.value,
            "latency_ms": self._last_latency * 1000 if self._last_latency is not None else None,
            "last_price_age_ms": self._last_price_age_ms(),
            "ws_status": self._ws_status,
        }

    def get_recommendations(self) -> list[dict[str, Any]]:
        snapshot = self.get_observer_snapshot()
        volatility = snapshot["volatility"]
        micro_volatility = snapshot["micro_volatility"]
        impulse_pct = snapshot["impulse_pct"]
        drawdown = snapshot["drawdown"]
        recommendations: list[dict[str, Any]] = []
        if impulse_pct is not None and impulse_pct >= 0.6:
            recommendations.append(
                {
                    "rec_type": "REBUILD_GRID",
                    "reason": "Зафиксирован резкий импульс цены, рекомендуется пересчитать сетку.",
                    "confidence": min(0.95, 0.6 + impulse_pct / 10),
                    "expected_effect": "Смещение ордеров ближе к текущему диапазону.",
                    "can_apply_patch": False,
                }
            )
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
        if micro_volatility is not None and micro_volatility >= 0.35:
            recommendations.append(
                {
                    "rec_type": "PAUSE_TRADING",
                    "reason": "Микроволатильность повышена, возможна турбулентность.",
                    "confidence": min(0.85, 0.45 + micro_volatility / 5),
                    "expected_effect": "Защитит от быстрых рыночных рывков.",
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
        self._ws_status = "LOST"
        self._microstructure = None

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

    def _on_price_tick(self, tick: PriceUpdate) -> None:
        if self._state != RuntimeState.RUNNING:
            return
        if tick.last_price is None:
            return
        exchange_ts = tick.exchange_timestamp_ms or tick.local_timestamp_ms
        exchange_dt = datetime.fromtimestamp(exchange_ts / 1000, tz=timezone.utc)
        self._last_price = tick.last_price
        self._last_price_timestamp = exchange_dt
        if tick.latency_ms is not None:
            self._last_latency = tick.latency_ms / 1000
        self._price_history.append((exchange_ts, tick.last_price))
        self._microstructure = tick.microstructure

        created_orders = self._strategy.initialize(tick.last_price)
        for order in created_orders:
            self._emit("order_created", order)

        filled_orders = self._order_book.check_fills(tick.last_price)
        if filled_orders:
            for order in filled_orders:
                self._apply_fill(order)
                self._fills.append(order)
                self._emit("order_filled", order)
            new_orders = self._strategy.on_fills(filled_orders)
            for order in new_orders:
                self._emit("order_created", order)

        self._emit("pnl_updated", self.get_pnl())

    def _on_ws_status(self, status: str, details: str) -> None:
        self._ws_status = status

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
        prices = self._prices_in_window(window_ms=60_000)
        if len(prices) < 2:
            return 0.0
        return statistics.pstdev(prices)

    def _calculate_micro_volatility(self, window_ms: int) -> float | None:
        prices = self._prices_in_window(window_ms)
        if len(prices) < 2:
            return None
        last_price = prices[-1]
        if last_price <= 0:
            return None
        return statistics.pstdev(prices) / last_price * 100

    def _calculate_impulse(self, window_ms: int) -> float | None:
        prices = self._prices_in_window(window_ms)
        if len(prices) < 2:
            return None
        start_price = prices[0]
        end_price = prices[-1]
        if start_price <= 0:
            return None
        return abs(end_price - start_price) / start_price * 100

    def _prices_in_window(self, window_ms: int) -> list[float]:
        if not self._price_history:
            return []
        now_ms = self._price_history[-1][0]
        return [price for ts, price in self._price_history if now_ms - ts <= window_ms]

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
