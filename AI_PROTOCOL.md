# AI Protocol (Datapack v2 + Structured Responses)

## Datapack v2 (Compact)

The GUI sends a compact datapack without candles. Example shape:

```json
{
  "version": "v2",
  "symbol": "BTCUSDT",
  "base_asset": "BTC",
  "quote_asset": "USDT",
  "period": "4h",
  "quality": "Standard",
  "current_price": {
    "value": 65000.12,
    "source": "ws",
    "age_ms": 850
  },
  "spread_estimate_pct": 0.12,
  "volatility_proxy_pct": -0.4,
  "exchange_limits": {
    "tickSize": null,
    "stepSize": null,
    "minNotional": null
  },
  "user_inputs": {
    "budget": 500,
    "mode": "Grid",
    "grid_count": 12,
    "grid_step_pct": 0.35,
    "range_low_pct": 1.5,
    "range_high_pct": 1.8
  },
  "app_settings": {
    "dry_run": true,
    "allow_request_more_data": true
  }
}
```

## Expected Analyze Response (AiResponse)

OpenAI must return **JSON only** (no extra prose outside JSON).

```json
{
  "summary": {
    "market_type": "range",
    "volatility": "medium",
    "liquidity": "ok",
    "spread": "0.12%",
    "advice": "Grid is reasonable with moderate spacing."
  },
  "plan": {
    "mode": "grid",
    "grid_count": 12,
    "grid_step_pct": 0.3,
    "range_low_pct": 1.5,
    "range_high_pct": 1.8,
    "budget_usdt": 500
  },
  "preview_levels": {
    "levels": [
      {"side": "BUY", "price": 64200, "qty": 0.01, "pct_from_mid": -1.2},
      {"side": "SELL", "price": 65800, "qty": 0.01, "pct_from_mid": 1.2}
    ]
  },
  "risk_notes": [
    "Range could break during macro event releases."
  ],
  "questions": [],
  "tool_requests": []
}
```

## Expected Adjustment Response (AiPatchResponse)

OpenAI must return **JSON only** (no extra prose outside JSON).

```json
{
  "patch": {
    "budget_usdt": 500,
    "grid_count": 20
  },
  "preview_levels": {
    "levels": [
      {"side": "BUY", "price": 64100, "qty": 0.008, "pct_from_mid": -1.3}
    ]
  },
  "message": "Updated budget and increased grid density.",
  "questions": [],
  "tool_requests": []
}
```
