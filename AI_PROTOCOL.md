# AI Protocol (Operator Grid v10.0.3)

## Safety rules
- AI must never place trades, open positions, or execute orders.
- AI only analyzes, suggests, and provides patches to the local strategy form.
- AI must return **ONLY valid JSON** (no markdown, no extra prose).

## Analysis DataPack (Strict v2)

AI Operator Grid sends a deterministic datapack built from WS + HTTP. The response logic **must not** assume any hidden fields.

```json
{
  "meta": {
    "symbol": "BTCUSDT",
    "base": "BTC",
    "quote": "USDT",
    "timestamp_ms": 1716038400000,
    "timezone": "UTC",
    "mode": "SPOT",
    "profile": "BALANCED",
    "lookback_days": 7
  },
  "exchange_rules": {
    "tickSize": 0.01,
    "stepSize": 0.0001,
    "minQty": 0.0001,
    "minNotional": 5,
    "maxQty": 100
  },
  "fees": {
    "makerFeePct": 0.02,
    "takerFeePct": 0.04,
    "is_zero_fee": false,
    "fee_asset": null
  },
  "account_snapshot": {
    "balances_free": {"base": 0.1, "quote": 250},
    "balances_locked": {"base": 0, "quote": 0},
    "canTrade": true,
    "permissions": ["SPOT"],
    "trade_gate_state": "TRADE_OK"
  },
  "price_snapshot": {
    "best_bid": 62000.1,
    "best_ask": 62000.8,
    "mid": 62000.45,
    "spread_pct": 0.0011,
    "source": "WS",
    "ws_age_ms": 120,
    "http_age_ms": 430,
    "stale": false
  },
  "orderbook_microstructure": {
    "depth_50": {"bids": [[62000.1, 0.1]], "asks": [[62000.8, 0.2]]},
    "book_imbalance": 0.12,
    "topN_notional_bid": 420000,
    "topN_notional_ask": 390000,
    "impact_estimates": {"slippage_10bp": 0.02, "slippage_25bp": 0.05}
  },
  "trades_1m": {
    "n_trades_1m": 112,
    "buy_vol": 15000,
    "sell_vol": 12500,
    "vwap_1m": 62000.4,
    "last_price": 62000.7
  },
  "trades_window": {
    "window": "4h",
    "lookback_days": 7,
    "bars": 42,
    "quote_volume": 1240000,
    "trade_count": 4800,
    "notes": ""
  },
  "klines_summary": {
    "windows": {
      "1m": {"return_pct": 0.1, "atr_pct": 0.05, "realized_vol_pct": 0.2, "trend_slope": 0.01, "drawdown_pct": 0.2, "range_pct": 0.6},
      "1h": {"return_pct": 1.1, "atr_pct": 0.8, "realized_vol_pct": 1.5, "trend_slope": 0.05, "drawdown_pct": 0.6, "range_pct": 2.4}
    },
    "config": {"windows": ["1m", "5m", "15m", "1h", "4h", "1d"]}
  },
  "constraints": {
    "user_budget_quote": 250,
    "max_active_orders": 12,
    "allowed_sides": "BOTH",
    "risk_constraints": {"stop_loss_enabled": false}
  },
  "data_quality": {
    "ws_ok": true,
    "http_ok": true,
    "ws_age_ms": 120,
    "http_age_ms": 430,
    "book_latency_ms": 180,
    "klines_latency_ms": 640,
    "balances_age_s": 0.8,
    "warnings": [],
    "pack_source": {
      "ws_ok": true,
      "http_ok": true,
      "ws_age_ms": 120,
      "http_age_ms": 430,
      "stale_flags": []
    },
    "missing": [],
    "timings_ms": {"orderbook_depth_50": 180, "klines": 640},
    "notes": ""
  },
  "profit_model_inputs": {
    "maker_fee_pct": 0.02,
    "taker_fee_pct": 0.04,
    "assumed_slippage_pct": 0.02,
    "safety_edge_pct": 0.02,
    "fee_discount_pct": null,
    "tickSize": 0.01,
    "stepSize": 0.0001,
    "minNotional": 5,
    "expected_fill_mode": "maker-grid"
  },
  "profit_estimate": {
    "gross_edge_pct": 0.2,
    "fee_total_pct": 0.04,
    "net_edge_pct": 0.14,
    "break_even_tp_pct": 0.08,
    "min_tp_pct": 0.13,
    "assumptions": {"expected_fill_mode": "MAKER_MAKER", "assumed_slippage_pct": 0.02}
  },
  "profile_presets": {
    "BALANCED": {"target_net_edge_pct": 0.05, "target_profit_pct": 0.05, "safety_margin_pct": 0.03, "max_active_orders": 12}
  }
}
```

## Response Schema (strict JSON only)

Every AI response must be a JSON object with the following schema:

```json
{
  "analysis_result": {
    "state": "OK|WARNING|DANGER",
    "summary": "string",
    "trend": "UP|DOWN|MIXED",
    "volatility": "LOW|MED|HIGH",
    "confidence": 0.0,
    "risks": ["..."],
    "data_quality": {
      "ws": "OK|WARMUP|LOST|NO",
      "http": "OK|NO",
      "klines": "OK|NO",
      "trades": "OK|NO",
      "orderbook": "OK|NO",
      "notes": "string"
    }
  },
  "strategy_patch": {
    "profile": "CONSERVATIVE|BALANCED|AGGRESSIVE",
    "bias": "UP|DOWN|FLAT",
    "range_mode": "MANUAL|AUTO_ATR",
    "step_pct": 0.3,
    "range_down_pct": 2.0,
    "range_up_pct": 2.5,
    "levels": 6,
    "tp_pct": 0.2,
    "max_active_orders": 8,
    "notes": "string"
  },
  "diagnostics": {
    "lookback_days_used": 1|7|30|365,
    "requested_more_data": false,
    "request": { "lookback_days": 7|30|365, "why": "string" } | null
  }
}
```
