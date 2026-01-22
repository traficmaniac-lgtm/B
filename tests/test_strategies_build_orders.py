from __future__ import annotations

from typing import Any

from src.core.strategies.manual_strategies import StrategyContext
from src.core.strategies.registry import get_strategy


def _sample_klines(count: int, start: float = 100.0, step: float = 0.2) -> list[list[float]]:
    klines: list[list[float]] = []
    price = start
    for _ in range(count):
        high = price + 1.5
        low = price - 1.0
        close = price + step
        klines.append([0, price, high, low, close, 0])
        price = close
    return klines


def _base_rules() -> dict[str, float]:
    return {
        "tick": 0.01,
        "step": 0.001,
        "min_qty": 0.001,
        "min_notional": 10.0,
        "max_qty": None,
    }


def _build_ctx(
    *,
    strategy_id: str,
    params: dict[str, Any],
    current_price: float = 110.0,
    best_bid: float = 109.9,
    best_ask: float = 110.1,
    klines: list[Any] | None = None,
    klines_by_tf: dict[str, list[Any]] | None = None,
) -> StrategyContext:
    return StrategyContext(
        symbol="BTCUSDT",
        budget_usdt=1000.0,
        rules=_base_rules(),
        current_price=current_price,
        best_bid=best_bid,
        best_ask=best_ask,
        fee_maker=0.0002,
        fee_taker=0.0005,
        dry_run=True,
        balances={"base_free": 0.0, "quote_free": 1000.0},
        max_active_orders=12,
        params=params,
        klines=klines,
        klines_by_tf=klines_by_tf,
    )


def test_build_orders_for_each_strategy() -> None:
    grid_params = {
        "levels": 4,
        "step_pct": 0.5,
        "range_down_pct": 1.0,
        "range_up_pct": 1.0,
        "tp_pct": 0.5,
        "max_active_orders": 12,
        "size_mode": "Equal",
        "bias": "Neutral",
    }
    mr_params = {
        "lookback": 50,
        "band_k": 2.0,
        "atr_period": 14,
        "base_step_pct": 0.3,
        "range_pct": 1.5,
        "tp_pct": 0.8,
        "max_active_orders": 8,
    }
    breakout_params = {
        "breakout_window": 20,
        "pullback_pct": 0.3,
        "confirm_trades_n": 3,
        "tp_pct": 1.0,
        "sl_pct": None,
        "max_active_orders": 6,
    }
    scalp_params = {
        "min_edge_pct": 0.3,
        "max_edge_pct": 0.5,
        "refresh_ms": 1500,
        "max_position": 0.0,
        "tp_pct": 0.2,
        "maker_only": True,
        "max_active_orders": 4,
    }
    trend_params = {
        "trend_tf": "1m",
        "trend_threshold": 0.01,
        "bias_strength": 0.3,
        "adaptive_step": True,
        "max_active_orders": 6,
    }
    klines = _sample_klines(120)
    trending_klines = _sample_klines(120, start=90.0, step=0.4)
    klines_by_tf = {"1m": trending_klines}

    strategy_map = {
        "GRID_CLASSIC": _build_ctx(strategy_id="GRID_CLASSIC", params=grid_params),
        "GRID_BIASED_LONG": _build_ctx(strategy_id="GRID_BIASED_LONG", params=grid_params),
        "GRID_BIASED_SHORT": _build_ctx(strategy_id="GRID_BIASED_SHORT", params=grid_params),
        "MEAN_REVERSION_BANDS": _build_ctx(
            strategy_id="MEAN_REVERSION_BANDS",
            params=mr_params,
            klines=klines,
        ),
        "BREAKOUT_PULLBACK": _build_ctx(
            strategy_id="BREAKOUT_PULLBACK",
            params=breakout_params,
            current_price=130.0,
            klines=klines,
        ),
        "SCALP_MICRO_SPREAD": _build_ctx(strategy_id="SCALP_MICRO_SPREAD", params=scalp_params),
        "TREND_FOLLOW_GRID": _build_ctx(
            strategy_id="TREND_FOLLOW_GRID",
            params=trend_params,
            klines=trending_klines,
            klines_by_tf=klines_by_tf,
        ),
    }

    for strategy_id, ctx in strategy_map.items():
        strategy = get_strategy(strategy_id)
        assert strategy is not None
        ok, _ = strategy.validate(ctx)
        assert ok
        intents = strategy.build_orders(ctx)
        assert intents


def test_strategies_require_klines() -> None:
    mr_params = {
        "lookback": 50,
        "band_k": 2.0,
        "atr_period": 14,
        "base_step_pct": 0.3,
        "range_pct": 1.5,
        "tp_pct": 0.8,
        "max_active_orders": 8,
    }
    breakout_params = {
        "breakout_window": 20,
        "pullback_pct": 0.3,
        "confirm_trades_n": 3,
        "tp_pct": 1.0,
        "sl_pct": None,
        "max_active_orders": 6,
    }
    trend_params = {
        "trend_tf": "1m",
        "trend_threshold": 0.01,
        "bias_strength": 0.3,
        "adaptive_step": True,
        "max_active_orders": 6,
    }
    for strategy_id, params in [
        ("MEAN_REVERSION_BANDS", mr_params),
        ("BREAKOUT_PULLBACK", breakout_params),
        ("TREND_FOLLOW_GRID", trend_params),
    ]:
        strategy = get_strategy(strategy_id)
        assert strategy is not None
        ctx = _build_ctx(strategy_id=strategy_id, params=params, klines=None, klines_by_tf=None)
        ok, reason = strategy.validate(ctx)
        assert not ok
        assert reason == "нет данных klines для стратегии"
