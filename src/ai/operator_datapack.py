from __future__ import annotations

from typing import Any

from src.ai.operator_math import estimate_grid_edge
from src.ai.operator_profiles import PROFILE_PRESETS, get_profile_preset


def build_ai_datapack(
    symbol: str,
    profile: str,
    user_budget: float | None,
    user_constraints: dict[str, Any],
    timeframe_cfg: dict[str, Any],
) -> dict[str, Any]:
    base_asset = str(user_constraints.get("base_asset") or "")
    quote_asset = str(user_constraints.get("quote_asset") or "")
    timestamp_ms = int(user_constraints.get("timestamp_ms") or 0)
    timezone = str(user_constraints.get("timezone") or "UTC")
    mode = str(user_constraints.get("mode") or "SPOT")
    rules = user_constraints.get("rules") or {}
    fees = user_constraints.get("fees") or {}
    balances = user_constraints.get("balances") or {}
    permissions = user_constraints.get("permissions") or []
    trade_gate_state = user_constraints.get("trade_gate_state") or ""
    can_trade = user_constraints.get("can_trade")
    price_snapshot = user_constraints.get("price_snapshot") or {}
    orderbook = user_constraints.get("orderbook_depth_50")
    trades = user_constraints.get("trades_1m")
    trades_window = user_constraints.get("trades_window") or {}
    klines = user_constraints.get("klines_by_window") or {}
    data_quality = user_constraints.get("data_quality") or {}
    lookback_days = user_constraints.get("lookback_days") or 1
    allowed_sides = user_constraints.get("allowed_sides") or "BOTH"
    risk_constraints = user_constraints.get("risk_constraints") or {}
    max_active_orders = user_constraints.get("max_active_orders")
    profit_inputs = user_constraints.get("profit_model_inputs") or {}
    extra_data = user_constraints.get("extra_data")

    orderbook_metrics = _compute_orderbook_metrics(orderbook)
    trades_metrics = _compute_trades_1m_metrics(trades)
    kline_summary = _compute_kline_summary(klines, timeframe_cfg, price_snapshot.get("mid"))

    maker_fee_pct = _to_float(fees.get("makerFeePct"))
    taker_fee_pct = _to_float(fees.get("takerFeePct"))
    assumed_slippage_pct = _to_float(profit_inputs.get("assumed_slippage_pct"))
    safety_edge_pct = _to_float(profit_inputs.get("safety_edge_pct"))
    fee_discount_pct = _to_float(profit_inputs.get("fee_discount_pct"))
    expected_fill_mode = profit_inputs.get("expected_fill_mode") or "maker-grid"
    spread_pct = _to_float(price_snapshot.get("spread_pct"))
    step_pct = _to_float(profit_inputs.get("step_pct"))
    tp_pct = _to_float(profit_inputs.get("tp_pct"))
    profit_estimate = estimate_grid_edge(
        step_pct,
        tp_pct,
        maker_fee_pct=maker_fee_pct,
        taker_fee_pct=taker_fee_pct,
        slippage_pct=assumed_slippage_pct,
        spread_pct=spread_pct,
        profile=profile,
        expected_fill_mode=expected_fill_mode,
        fee_discount_pct=fee_discount_pct,
        safety_edge_pct=safety_edge_pct,
    )
    preset = get_profile_preset(profile)

    datapack = {
        "meta": {
            "symbol": symbol,
            "base": base_asset,
            "quote": quote_asset,
            "timestamp_ms": timestamp_ms,
            "timezone": timezone,
            "mode": mode,
            "profile": profile,
            "lookback_days": lookback_days,
        },
        "exchange_rules": {
            "tickSize": rules.get("tickSize"),
            "stepSize": rules.get("stepSize"),
            "minQty": rules.get("minQty"),
            "minNotional": rules.get("minNotional"),
            "maxQty": rules.get("maxQty"),
        },
        "fees": {
            "makerFeePct": fees.get("makerFeePct"),
            "takerFeePct": fees.get("takerFeePct"),
            "is_zero_fee": fees.get("is_zero_fee", False),
            "fee_asset": fees.get("fee_asset"),
        },
        "account_snapshot": {
            "balances_free": balances.get("free") or {},
            "balances_locked": balances.get("locked") or {},
            "canTrade": can_trade,
            "permissions": permissions,
            "trade_gate_state": trade_gate_state,
        },
        "price_snapshot": {
            "best_bid": price_snapshot.get("best_bid"),
            "best_ask": price_snapshot.get("best_ask"),
            "mid": price_snapshot.get("mid"),
            "spread_pct": price_snapshot.get("spread_pct"),
            "source": price_snapshot.get("source"),
            "ws_age_ms": price_snapshot.get("ws_age_ms"),
            "http_age_ms": price_snapshot.get("http_age_ms"),
            "stale": price_snapshot.get("stale"),
        },
        "orderbook_microstructure": {
            "depth_50": orderbook_metrics.get("depth_50"),
            "book_imbalance": orderbook_metrics.get("book_imbalance"),
            "topN_notional_bid": orderbook_metrics.get("topN_notional_bid"),
            "topN_notional_ask": orderbook_metrics.get("topN_notional_ask"),
            "impact_estimates": orderbook_metrics.get("impact_estimates"),
        },
        "trades_1m": trades_metrics,
        "trades_window": trades_window,
        "klines_summary": {
            "windows": kline_summary,
            "config": timeframe_cfg,
        },
        "constraints": {
            "user_budget_quote": user_budget,
            "max_active_orders": max_active_orders,
            "allowed_sides": allowed_sides,
            "risk_constraints": risk_constraints,
        },
        "data_quality": data_quality,
        "profit_model_inputs": {
            "maker_fee_pct": maker_fee_pct,
            "taker_fee_pct": taker_fee_pct,
            "assumed_slippage_pct": assumed_slippage_pct,
            "safety_edge_pct": safety_edge_pct,
            "fee_discount_pct": fee_discount_pct,
            "tickSize": rules.get("tickSize"),
            "stepSize": rules.get("stepSize"),
            "minNotional": rules.get("minNotional"),
            "expected_fill_mode": expected_fill_mode,
        },
        "profit_estimate": profit_estimate,
        "profile_presets": {
            key: {
                "target_net_edge_pct": preset_item.target_net_edge_pct,
                "target_profit_pct": preset_item.target_profit_pct,
                "safety_margin_pct": preset_item.safety_margin_pct,
                "max_active_orders": preset_item.max_active_orders,
                "levels_min": preset_item.levels_min,
                "levels_max": preset_item.levels_max,
                "min_range_pct": preset_item.min_range_pct,
                "max_range_pct": preset_item.max_range_pct,
            }
            for key, preset_item in PROFILE_PRESETS.items()
        },
        "selected_profile": {
            "target_net_edge_pct": preset.target_net_edge_pct,
            "target_profit_pct": preset.target_profit_pct,
            "safety_margin_pct": preset.safety_margin_pct,
        },
    }
    if extra_data:
        datapack["extra_data"] = extra_data
    return datapack


def _compute_orderbook_metrics(orderbook: Any) -> dict[str, Any]:
    if not isinstance(orderbook, dict):
        return {
            "depth_50": {"bids": [], "asks": []},
            "book_imbalance": None,
            "topN_notional_bid": None,
            "topN_notional_ask": None,
            "impact_estimates": {"slippage_10bp": None, "slippage_25bp": None},
        }
    bids = _normalize_depth_side(orderbook.get("bids"))
    asks = _normalize_depth_side(orderbook.get("asks"))
    bid_notional = sum(price * qty for price, qty in bids if price and qty)
    ask_notional = sum(price * qty for price, qty in asks if price and qty)
    total = bid_notional + ask_notional
    book_imbalance = (bid_notional - ask_notional) / total if total > 0 else None
    top_notional = max(min(bid_notional, ask_notional), 0.0)
    slippage_10bp = _estimate_slippage_pct(top_notional, 10)
    slippage_25bp = _estimate_slippage_pct(top_notional, 25)
    return {
        "depth_50": {"bids": bids, "asks": asks},
        "book_imbalance": book_imbalance,
        "topN_notional_bid": bid_notional if bid_notional else None,
        "topN_notional_ask": ask_notional if ask_notional else None,
        "impact_estimates": {"slippage_10bp": slippage_10bp, "slippage_25bp": slippage_25bp},
    }


def _normalize_depth_side(raw: Any) -> list[list[float]]:
    if not isinstance(raw, list):
        return []
    normalized: list[list[float]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 2:
            continue
        price = _to_float(item[0])
        qty = _to_float(item[1])
        if price is None or qty is None:
            continue
        normalized.append([price, qty])
    return normalized


def _estimate_slippage_pct(top_notional: float, target_bps: int) -> float | None:
    if top_notional <= 0:
        return None
    liquidity_scale = max(1.0, 100_000 / top_notional)
    slippage = (target_bps / 10000) * liquidity_scale * 100
    return round(slippage, 6)


def _compute_trades_1m_metrics(trades: Any) -> dict[str, Any]:
    if isinstance(trades, dict):
        items = trades.get("items")
    else:
        items = trades
    if not isinstance(items, list):
        return {
            "n_trades_1m": None,
            "buy_vol": None,
            "sell_vol": None,
            "vwap_1m": None,
            "last_price": None,
        }
    buy_vol = 0.0
    sell_vol = 0.0
    vwap_num = 0.0
    vwap_den = 0.0
    last_price = None
    for trade in items:
        if not isinstance(trade, dict):
            continue
        price = _to_float(trade.get("price"))
        qty = _to_float(trade.get("qty"))
        if price is None or qty is None:
            continue
        last_price = price
        quote = price * qty
        if trade.get("isBuyerMaker"):
            sell_vol += quote
        else:
            buy_vol += quote
        vwap_num += quote
        vwap_den += qty
    vwap = vwap_num / vwap_den if vwap_den > 0 else None
    return {
        "n_trades_1m": len(items),
        "buy_vol": buy_vol if buy_vol > 0 else None,
        "sell_vol": sell_vol if sell_vol > 0 else None,
        "vwap_1m": vwap,
        "last_price": last_price,
    }


def _compute_kline_summary(klines: dict[str, Any], timeframe_cfg: dict[str, Any], mid_price: float | None) -> dict[str, Any]:
    windows = timeframe_cfg.get("windows") or []
    summary: dict[str, Any] = {}
    for window in windows:
        raw = klines.get(window)
        ohlc = _extract_ohlc(raw)
        summary[window] = _summarize_ohlc(ohlc, mid_price)
    return summary


def _extract_ohlc(klines: Any) -> list[tuple[float, float, float, float]]:
    if not isinstance(klines, list):
        return []
    series: list[tuple[float, float, float, float]] = []
    for item in klines:
        if not isinstance(item, list) or len(item) < 5:
            continue
        open_val = _to_float(item[1])
        high_val = _to_float(item[2])
        low_val = _to_float(item[3])
        close_val = _to_float(item[4])
        if None in (open_val, high_val, low_val, close_val):
            continue
        series.append((open_val, high_val, low_val, close_val))
    return series


def _summarize_ohlc(series: list[tuple[float, float, float, float]], mid_price: float | None) -> dict[str, Any]:
    if len(series) < 2:
        return {
            "return_pct": None,
            "atr_pct": None,
            "realized_vol_pct": None,
            "trend_slope": None,
            "drawdown_pct": None,
            "range_pct": None,
        }
    closes = [entry[3] for entry in series]
    first_close = closes[0]
    last_close = closes[-1]
    return_pct = ((last_close / first_close) - 1) * 100 if first_close > 0 else None
    returns = []
    for idx in range(1, len(closes)):
        prev = closes[idx - 1]
        if prev <= 0:
            continue
        returns.append((closes[idx] / prev) - 1)
    realized_vol_pct = (_stddev(returns) * 100) if returns else None
    atr_pct = _atr_pct(series)
    trend_slope = ((last_close - first_close) / first_close) * 100 / (len(series) - 1) if first_close > 0 else None
    drawdown_pct = _drawdown_pct(closes)
    range_pct = _range_pct(series, mid_price)
    return {
        "return_pct": _round(return_pct),
        "atr_pct": _round(atr_pct),
        "realized_vol_pct": _round(realized_vol_pct),
        "trend_slope": _round(trend_slope),
        "drawdown_pct": _round(drawdown_pct),
        "range_pct": _round(range_pct),
    }


def _atr_pct(series: list[tuple[float, float, float, float]]) -> float | None:
    if len(series) < 2:
        return None
    trs: list[float] = []
    prev_close = series[0][3]
    for _, high, low, close in series[1:]:
        if prev_close <= 0:
            prev_close = close
            continue
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr / prev_close)
        prev_close = close
    if not trs:
        return None
    return sum(trs) / len(trs) * 100


def _drawdown_pct(closes: list[float]) -> float | None:
    peak = None
    max_dd = 0.0
    for close in closes:
        if peak is None or close > peak:
            peak = close
        if peak and peak > 0:
            drawdown = (peak - close) / peak * 100
            if drawdown > max_dd:
                max_dd = drawdown
    return max_dd if max_dd > 0 else None


def _range_pct(series: list[tuple[float, float, float, float]], mid_price: float | None) -> float | None:
    highs = [entry[1] for entry in series]
    lows = [entry[2] for entry in series]
    if not highs or not lows:
        return None
    max_high = max(highs)
    min_low = min(lows)
    if min_low <= 0:
        return None
    mid = mid_price or ((max_high + min_low) / 2)
    if mid <= 0:
        return None
    return (max_high - min_low) / mid * 100


def _stddev(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((val - mean) ** 2 for val in values) / len(values)) ** 0.5


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 6)
