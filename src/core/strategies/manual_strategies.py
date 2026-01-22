from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from statistics import mean, pstdev
from typing import Any, Protocol


@dataclass(frozen=True)
class OrderIntent:
    side: str
    price: float
    qty: float
    tag: str


@dataclass(frozen=True)
class StrategyContext:
    symbol: str
    budget_usdt: float
    rules: dict[str, float | None]
    current_price: float
    best_bid: float | None
    best_ask: float | None
    fee_maker: float | None
    fee_taker: float | None
    dry_run: bool
    balances: dict[str, float]
    max_active_orders: int
    params: dict[str, Any]
    klines: list[Any] | None = None
    klines_by_tf: dict[str, list[Any]] | None = None
    trade_gate: str | None = None
    spot_enabled: bool | None = None
    rules_loaded: bool | None = None
    balances_ready: bool | None = None
    balance_age_s: float | None = None


class ManualStrategy(Protocol):
    id: str
    label: str

    def build_orders(self, ctx: StrategyContext) -> list[OrderIntent]:
        ...

    def validate(self, ctx: StrategyContext) -> tuple[bool, str]:
        ...

    def on_tick(self, ctx: StrategyContext, price_event: dict[str, Any]) -> None:
        ...

    def on_fill(self, ctx: StrategyContext, fill_event: dict[str, Any]) -> None:
        ...


@dataclass(frozen=True)
class GridParams:
    levels: int
    step_pct: float
    range_down_pct: float
    range_up_pct: float
    tp_pct: float
    max_active_orders: int
    size_mode: str
    bias: str


@dataclass(frozen=True)
class MeanReversionBandsParams:
    lookback: int
    band_k: float
    atr_period: int
    base_step_pct: float
    range_pct: float
    tp_pct: float
    max_active_orders: int


@dataclass(frozen=True)
class BreakoutPullbackParams:
    breakout_window: int
    pullback_pct: float
    confirm_trades_n: int
    tp_pct: float
    sl_pct: float | None
    max_active_orders: int


@dataclass(frozen=True)
class ScalpMicroSpreadParams:
    min_edge_pct: float
    max_edge_pct: float
    refresh_ms: int
    max_position: float
    tp_pct: float
    maker_only: bool
    max_active_orders: int


@dataclass(frozen=True)
class TrendFollowGridParams:
    trend_tf: str
    trend_threshold: float
    bias_strength: float
    adaptive_step: bool
    max_active_orders: int


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_price(price: float, tick: float | None, side: str) -> float:
    if tick is None or tick <= 0:
        return price
    tick_dec = Decimal(str(tick))
    price_dec = Decimal(str(price))
    if side.upper() == "SELL":
        rounded = (price_dec / tick_dec).to_integral_value(rounding=ROUND_CEILING) * tick_dec
    else:
        rounded = (price_dec / tick_dec).to_integral_value(rounding=ROUND_FLOOR) * tick_dec
    return float(rounded)


def _round_qty(qty: float, step: float | None) -> float:
    if step is None or step <= 0:
        return qty
    step_dec = Decimal(str(step))
    qty_dec = Decimal(str(qty))
    rounded = (qty_dec / step_dec).to_integral_value(rounding=ROUND_FLOOR) * step_dec
    return float(rounded)


def _clamp_intent(
    side: str,
    price: float,
    qty: float,
    rules: dict[str, float | None],
    tag: str,
) -> OrderIntent | None:
    tick = rules.get("tick")
    step = rules.get("step")
    min_qty = rules.get("min_qty")
    min_notional = rules.get("min_notional")
    price = _round_price(price, tick, side)
    qty = _round_qty(qty, step)
    if price <= 0 or qty <= 0:
        return None
    if min_qty is not None and qty < min_qty:
        return None
    if min_notional is not None and (price * qty) < min_notional:
        return None
    return OrderIntent(side=side, price=price, qty=qty, tag=tag)


def _validate_common(ctx: StrategyContext, total_orders: int) -> tuple[bool, str]:
    if ctx.budget_usdt <= 0:
        return False, "budget_usdt<=0"
    if total_orders <= 0:
        return False, "total_orders<=0"
    if ctx.max_active_orders and total_orders > ctx.max_active_orders:
        return False, "max_active_orders"
    tick = ctx.rules.get("tick")
    step = ctx.rules.get("step")
    if tick is None or step is None:
        return False, "missing_exchange_rules"
    if ctx.current_price <= 0:
        return False, "price_invalid"
    min_notional = ctx.rules.get("min_notional")
    min_qty = ctx.rules.get("min_qty")
    per_order_notional = ctx.budget_usdt / total_orders
    if min_notional is not None and per_order_notional < min_notional:
        return False, "min_notional"
    per_order_qty = per_order_notional / ctx.current_price
    if min_qty is not None and per_order_qty < min_qty:
        return False, "min_qty"
    return True, "ok"


def _extract_ohlc(klines: list[Any]) -> tuple[list[float], list[float], list[float]]:
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for entry in klines:
        if isinstance(entry, dict):
            close = _safe_float(entry.get("close") or entry.get("c"))
            high = _safe_float(entry.get("high") or entry.get("h"))
            low = _safe_float(entry.get("low") or entry.get("l"))
        elif isinstance(entry, (list, tuple)) and len(entry) >= 5:
            high = _safe_float(entry[2])
            low = _safe_float(entry[3])
            close = _safe_float(entry[4])
        else:
            continue
        if close is None or high is None or low is None:
            continue
        closes.append(close)
        highs.append(high)
        lows.append(low)
    return closes, highs, lows


def _compute_atr(highs: list[float], lows: list[float], period: int) -> float | None:
    if len(highs) < period or len(lows) < period:
        return None
    ranges = [(high - low) for high, low in zip(highs[-period:], lows[-period:], strict=False)]
    if not ranges:
        return None
    return mean(ranges)


def _compute_ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = alpha * value + (1 - alpha) * ema
    return ema


def _split_levels(total_levels: int, bias_ratio: float) -> tuple[int, int]:
    bias_ratio = max(0.1, min(0.9, bias_ratio))
    buy_levels = max(1, int(round(total_levels * bias_ratio)))
    sell_levels = max(1, total_levels - buy_levels)
    if buy_levels + sell_levels < total_levels:
        sell_levels += total_levels - (buy_levels + sell_levels)
    return buy_levels, sell_levels


class BaseManualStrategy:
    id: str = ""
    label: str = ""

    def on_tick(self, ctx: StrategyContext, price_event: dict[str, Any]) -> None:
        _ = ctx, price_event

    def on_fill(self, ctx: StrategyContext, fill_event: dict[str, Any]) -> None:
        _ = ctx, fill_event


class GridClassicStrategy(BaseManualStrategy):
    id = "GRID_CLASSIC"
    label = "GRID_CLASSIC"

    def validate(self, ctx: StrategyContext) -> tuple[bool, str]:
        params = GridParams(**ctx.params)
        total_orders = params.levels * 2
        return _validate_common(ctx, total_orders)

    def build_orders(self, ctx: StrategyContext) -> list[OrderIntent]:
        params = GridParams(**ctx.params)
        levels = max(1, params.levels)
        down_pct = params.range_down_pct or (params.step_pct * levels)
        up_pct = params.range_up_pct or (params.step_pct * levels)
        down_step = down_pct / levels
        up_step = up_pct / levels
        total_orders = levels * 2
        per_order = ctx.budget_usdt / total_orders
        intents: list[OrderIntent] = []
        for idx in range(1, levels + 1):
            buy_price = ctx.current_price * (1 - (down_step * idx) / 100)
            sell_price = ctx.current_price * (1 + (up_step * idx) / 100)
            qty_buy = per_order / buy_price
            qty_sell = per_order / sell_price
            buy_intent = _clamp_intent("BUY", buy_price, qty_buy, ctx.rules, f"grid_buy_{idx}")
            sell_intent = _clamp_intent("SELL", sell_price, qty_sell, ctx.rules, f"grid_sell_{idx}")
            if buy_intent:
                intents.append(buy_intent)
            if sell_intent:
                intents.append(sell_intent)
        return intents


class GridBiasedLongStrategy(GridClassicStrategy):
    id = "GRID_BIASED_LONG"
    label = "GRID_BIASED_LONG"

    def build_orders(self, ctx: StrategyContext) -> list[OrderIntent]:
        params = GridParams(**ctx.params)
        total_levels = max(2, params.levels)
        buy_levels, sell_levels = _split_levels(total_levels, 0.7)
        down_pct = params.range_down_pct or (params.step_pct * buy_levels)
        up_pct = params.range_up_pct or (params.step_pct * sell_levels)
        down_step = down_pct / buy_levels
        up_step = up_pct / sell_levels
        total_orders = buy_levels + sell_levels
        per_order = ctx.budget_usdt / total_orders
        intents: list[OrderIntent] = []
        for idx in range(1, buy_levels + 1):
            buy_price = ctx.current_price * (1 - (down_step * idx) / 100)
            qty = per_order / buy_price
            intent = _clamp_intent("BUY", buy_price, qty, ctx.rules, f"grid_buy_{idx}")
            if intent:
                intents.append(intent)
        for idx in range(1, sell_levels + 1):
            sell_price = ctx.current_price * (1 + (up_step * idx) / 100)
            qty = per_order / sell_price
            intent = _clamp_intent("SELL", sell_price, qty, ctx.rules, f"grid_sell_{idx}")
            if intent:
                intents.append(intent)
        return intents


class GridBiasedShortStrategy(GridClassicStrategy):
    id = "GRID_BIASED_SHORT"
    label = "GRID_BIASED_SHORT"

    def build_orders(self, ctx: StrategyContext) -> list[OrderIntent]:
        params = GridParams(**ctx.params)
        total_levels = max(2, params.levels)
        buy_levels, sell_levels = _split_levels(total_levels, 0.3)
        down_pct = params.range_down_pct or (params.step_pct * buy_levels)
        up_pct = params.range_up_pct or (params.step_pct * sell_levels)
        down_step = down_pct / buy_levels
        up_step = up_pct / sell_levels
        total_orders = buy_levels + sell_levels
        per_order = ctx.budget_usdt / total_orders
        intents: list[OrderIntent] = []
        for idx in range(1, buy_levels + 1):
            buy_price = ctx.current_price * (1 - (down_step * idx) / 100)
            qty = per_order / buy_price
            intent = _clamp_intent("BUY", buy_price, qty, ctx.rules, f"grid_buy_{idx}")
            if intent:
                intents.append(intent)
        for idx in range(1, sell_levels + 1):
            sell_price = ctx.current_price * (1 + (up_step * idx) / 100)
            qty = per_order / sell_price
            intent = _clamp_intent("SELL", sell_price, qty, ctx.rules, f"grid_sell_{idx}")
            if intent:
                intents.append(intent)
        return intents


class MeanReversionBandsStrategy(BaseManualStrategy):
    id = "MEAN_REVERSION_BANDS"
    label = "MEAN_REVERSION_BANDS"

    def validate(self, ctx: StrategyContext) -> tuple[bool, str]:
        if not ctx.klines:
            return False, "нет klines"
        params = MeanReversionBandsParams(**ctx.params)
        levels = max(2, min(6, ctx.max_active_orders))
        return _validate_common(ctx, levels)

    def build_orders(self, ctx: StrategyContext) -> list[OrderIntent]:
        params = MeanReversionBandsParams(**ctx.params)
        levels = max(2, min(6, ctx.max_active_orders))
        closes, highs, lows = _extract_ohlc(ctx.klines or [])
        recent_closes = closes[-params.lookback :] if params.lookback > 0 else closes
        if len(recent_closes) < 2:
            return []
        avg = mean(recent_closes)
        deviation = pstdev(recent_closes) if len(recent_closes) > 1 else 0.0
        atr = _compute_atr(highs, lows, params.atr_period) or 0.0
        band_width = max(deviation, atr)
        lower = avg - params.band_k * band_width
        upper = avg + params.band_k * band_width
        bias_ratio = 0.5
        if ctx.current_price <= lower:
            bias_ratio = 0.7
        elif ctx.current_price >= upper:
            bias_ratio = 0.3
        buy_levels, sell_levels = _split_levels(levels, bias_ratio)
        total_orders = buy_levels + sell_levels
        per_order = ctx.budget_usdt / total_orders
        step_pct = params.base_step_pct or (params.range_pct / levels)
        intents: list[OrderIntent] = []
        for idx in range(1, buy_levels + 1):
            buy_price = ctx.current_price * (1 - (step_pct * idx) / 100)
            qty = per_order / buy_price
            intent = _clamp_intent("BUY", buy_price, qty, ctx.rules, f"mr_buy_{idx}")
            if intent:
                intents.append(intent)
        for idx in range(1, sell_levels + 1):
            sell_price = ctx.current_price * (1 + (step_pct * idx) / 100)
            qty = per_order / sell_price
            intent = _clamp_intent("SELL", sell_price, qty, ctx.rules, f"mr_sell_{idx}")
            if intent:
                intents.append(intent)
        return intents


class BreakoutPullbackStrategy(BaseManualStrategy):
    id = "BREAKOUT_PULLBACK"
    label = "BREAKOUT_PULLBACK"

    def validate(self, ctx: StrategyContext) -> tuple[bool, str]:
        if not ctx.klines:
            return False, "нет klines"
        params = BreakoutPullbackParams(**ctx.params)
        if params.breakout_window <= 1:
            return False, "breakout_window"
        ok, reason = _validate_common(ctx, max(1, params.confirm_trades_n))
        if not ok:
            return ok, reason
        closes, highs, lows = _extract_ohlc(ctx.klines)
        if len(highs) < params.breakout_window:
            return False, "нет klines"
        window_high = max(highs[-params.breakout_window :])
        window_low = min(lows[-params.breakout_window :])
        if ctx.current_price > window_high:
            return True, "ok"
        if ctx.current_price < window_low:
            return True, "ok"
        return False, "нет сигнала пробоя"

    def build_orders(self, ctx: StrategyContext) -> list[OrderIntent]:
        params = BreakoutPullbackParams(**ctx.params)
        closes, highs, lows = _extract_ohlc(ctx.klines or [])
        if len(highs) < params.breakout_window:
            return []
        window_high = max(highs[-params.breakout_window :])
        window_low = min(lows[-params.breakout_window :])
        direction: str | None = None
        if ctx.current_price > window_high:
            direction = "LONG"
        elif ctx.current_price < window_low:
            direction = "SHORT"
        if not direction:
            return []
        levels = max(1, min(ctx.max_active_orders, params.confirm_trades_n))
        per_order = ctx.budget_usdt / levels
        intents: list[OrderIntent] = []
        for idx in range(1, levels + 1):
            if direction == "LONG":
                price = ctx.current_price * (1 - (params.pullback_pct * idx) / 100)
                qty = per_order / price
                intent = _clamp_intent("BUY", price, qty, ctx.rules, f"breakout_buy_{idx}")
            else:
                price = ctx.current_price * (1 + (params.pullback_pct * idx) / 100)
                qty = per_order / price
                intent = _clamp_intent("SELL", price, qty, ctx.rules, f"breakout_sell_{idx}")
            if intent:
                intents.append(intent)
        return intents


class ScalpMicroSpreadStrategy(BaseManualStrategy):
    id = "SCALP_MICRO_SPREAD"
    label = "SCALP_MICRO_SPREAD"

    def validate(self, ctx: StrategyContext) -> tuple[bool, str]:
        params = ScalpMicroSpreadParams(**ctx.params)
        if ctx.best_bid is None or ctx.best_ask is None:
            return False, "best_bid_ask_missing"
        if params.min_edge_pct <= 0 or params.max_edge_pct <= 0:
            return False, "edge_pct"
        maker_fee = ctx.fee_maker or 0.0
        buffer_pct = 0.01
        required_edge = (maker_fee * 2 * 100) + buffer_pct
        if params.min_edge_pct <= required_edge:
            return False, "edge_below_fee"
        total_orders = min(ctx.max_active_orders, 4)
        return _validate_common(ctx, total_orders)

    def build_orders(self, ctx: StrategyContext) -> list[OrderIntent]:
        params = ScalpMicroSpreadParams(**ctx.params)
        if ctx.best_bid is None or ctx.best_ask is None:
            return []
        edges = [params.min_edge_pct, params.max_edge_pct]
        levels = min(len(edges), max(1, ctx.max_active_orders // 2))
        edges = edges[:levels]
        total_orders = levels * 2
        per_order = ctx.budget_usdt / total_orders
        intents: list[OrderIntent] = []
        for idx, edge in enumerate(edges, start=1):
            buy_price = ctx.best_bid * (1 - edge / 100)
            sell_price = ctx.best_ask * (1 + edge / 100)
            buy_qty = per_order / buy_price
            sell_qty = per_order / sell_price
            buy_intent = _clamp_intent("BUY", buy_price, buy_qty, ctx.rules, f"scalp_buy_{idx}")
            sell_intent = _clamp_intent("SELL", sell_price, sell_qty, ctx.rules, f"scalp_sell_{idx}")
            if buy_intent:
                intents.append(buy_intent)
            if sell_intent:
                intents.append(sell_intent)
        return intents


class TrendFollowGridStrategy(BaseManualStrategy):
    id = "TREND_FOLLOW_GRID"
    label = "TREND_FOLLOW_GRID"

    def validate(self, ctx: StrategyContext) -> tuple[bool, str]:
        params = TrendFollowGridParams(**ctx.params)
        klines = (ctx.klines_by_tf or {}).get(params.trend_tf) or ctx.klines
        if not klines:
            return False, "нет klines"
        total_orders = max(2, min(ctx.max_active_orders, 6))
        return _validate_common(ctx, total_orders)

    def build_orders(self, ctx: StrategyContext) -> list[OrderIntent]:
        params = TrendFollowGridParams(**ctx.params)
        klines = (ctx.klines_by_tf or {}).get(params.trend_tf) or ctx.klines or []
        closes, highs, lows = _extract_ohlc(klines)
        if len(closes) < 20:
            return []
        ema_fast = _compute_ema(closes[-20:], 5)
        ema_slow = _compute_ema(closes[-20:], 20)
        if ema_fast is None or ema_slow is None:
            return []
        diff_pct = ((ema_fast - ema_slow) / ctx.current_price) * 100 if ctx.current_price else 0.0
        if abs(diff_pct) < params.trend_threshold:
            return []
        bias_ratio = 0.5 + (params.bias_strength / 2)
        if diff_pct < 0:
            bias_ratio = 0.5 - (params.bias_strength / 2)
        total_levels = max(2, min(ctx.max_active_orders, 6))
        buy_levels, sell_levels = _split_levels(total_levels, bias_ratio)
        base_range_pct = 1.0
        if params.adaptive_step:
            atr = _compute_atr(highs, lows, 14) or 0.0
            if atr > 0 and ctx.current_price:
                base_range_pct = max(base_range_pct, (atr / ctx.current_price) * 100)
        down_step = base_range_pct / max(1, buy_levels)
        up_step = base_range_pct / max(1, sell_levels)
        total_orders = buy_levels + sell_levels
        per_order = ctx.budget_usdt / total_orders
        intents: list[OrderIntent] = []
        for idx in range(1, buy_levels + 1):
            buy_price = ctx.current_price * (1 - (down_step * idx) / 100)
            qty = per_order / buy_price
            intent = _clamp_intent("BUY", buy_price, qty, ctx.rules, f"trend_buy_{idx}")
            if intent:
                intents.append(intent)
        for idx in range(1, sell_levels + 1):
            sell_price = ctx.current_price * (1 + (up_step * idx) / 100)
            qty = per_order / sell_price
            intent = _clamp_intent("SELL", sell_price, qty, ctx.rules, f"trend_sell_{idx}")
            if intent:
                intents.append(intent)
        return intents


BUILTIN_STRATEGIES: tuple[ManualStrategy, ...] = (
    GridClassicStrategy(),
    GridBiasedLongStrategy(),
    GridBiasedShortStrategy(),
    MeanReversionBandsStrategy(),
    BreakoutPullbackStrategy(),
    ScalpMicroSpreadStrategy(),
    TrendFollowGridStrategy(),
)
