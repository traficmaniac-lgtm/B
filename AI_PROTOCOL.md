# AI Protocol (Operator Grid v10)

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
    "profile": "BALANCED"
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
    "warnings": []
  },
  "profit_model_inputs": {
    "maker_fee_pct": 0.02,
    "taker_fee_pct": 0.04,
    "assumed_slippage_pct": 0.02,
    "tickSize": 0.01,
    "stepSize": 0.0001,
    "minNotional": 5,
    "expected_fill_mode": "maker-grid"
  },
  "profit_estimate": {
    "net_edge_pct": 0.07,
    "break_even_tp_pct": 0.08,
    "total_cost_pct": 0.03,
    "assumptions": {"fees": "maker", "slippage_pct": 0.02}
  },
  "profile_presets": {
    "BALANCED": {"target_net_edge_pct": 0.05, "safety_margin_pct": 0.03, "max_active_orders": 12}
  }
}
```

## Response Schema (strict JSON only)

Every AI response must be a JSON object with the following schema:

```json
{
  "state": "SAFE|WARNING|DANGER",
  "recommendation": "WAIT|DO_NOT_TRADE|START|APPLY_PATCH|REQUEST_DATA|ADJUST_PARAMS|STOP",
  "next_action": "WAIT|DO_NOT_TRADE|START|APPLY_PATCH|REQUEST_DATA|ADJUST_PARAMS|STOP",
  "reason": "short text",
  "profile": "CONSERVATIVE|BALANCED|AGGRESSIVE",
  "actions_allowed": ["WAIT","APPLY_PATCH","REQUEST_DATA","START","STOP","ADJUST_PARAMS"],
  "patch": {
    "strategy_id": "GRID_CLASSIC|GRID_BIASED|RANGE_MR|DO_NOT_TRADE",
    "bias": "UP|DOWN|FLAT",
    "step_pct": 0.3,
    "range_down_pct": 2.0,
    "range_up_pct": 2.5,
    "levels": 6,
    "tp_pct": 0.2,
    "max_active_orders": 8
  },
  "request_data": {
    "need_more": false,
    "items": []
  },
  "math_check": {
    "net_edge_pct": 0.07,
    "break_even_tp_pct": 0.08,
    "assumptions": {"fees": "maker", "slippage_pct": 0.02}
  }
}
```
