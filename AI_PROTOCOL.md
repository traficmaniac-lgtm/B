# AI Protocol (Stage 2.1.2)

## Safety rules
- AI must never place trades, open positions, or execute orders.
- AI only analyzes, suggests, and provides patches to the local strategy form.
- AI must return **ONLY valid JSON** (no markdown, no extra prose).

## Analysis DataPack (v2.1.2)

The GUI sends a compact datapack with market snapshot and user context. No large arrays.

```json
{
  "version": "v2.1.2",
  "market_snapshot": {
    "symbol": "BTCUSDT",
    "last_price": 65000.12,
    "bid": 64990.5,
    "ask": 65009.7,
    "spread_pct": 0.03,
    "source_latency_ms": 850,
    "timestamp_utc": "2024-05-18T12:00:00+00:00",
    "candles_summary": [
      {"period": "1m", "atr_pct": 0.4, "stdev_pct": 0.3, "momentum_pct": 0.1},
      {"period": "5m", "atr_pct": 0.6, "stdev_pct": 0.5, "momentum_pct": 0.2},
      {"period": "1h", "atr_pct": 1.1, "stdev_pct": 0.9, "momentum_pct": 0.5},
      {"period": "4h", "atr_pct": 1.6, "stdev_pct": 1.2, "momentum_pct": 0.9}
    ]
  },
  "liquidity_summary": {
    "quote_volume_24h": 0,
    "trade_count_24h": 0,
    "liquidity_label": "OK"
  },
  "exchange_constraints": {
    "tick_size": null,
    "step_size": null,
    "min_notional": null
  },
  "user_context": {
    "budget_usdt": 500,
    "dry_run": true,
    "selected_period": "4h",
    "quality": "Standard"
  },
  "pair_context": {
    "base_asset": "BTC",
    "quote_asset": "USDT",
    "source": "ws"
  }
}
```

## Response Envelope

Every AI response must be wrapped in an envelope:

```json
{
  "type": "analysis_result | strategy_patch",
  "status": "OK | WARN | DANGER | ERROR",
  "confidence": 0.0,
  "reason_codes": ["..."],
  "message": "short message",
  "analysis_result": {"...": "..."},
  "strategy_patch": {"...": "..."}
}
```

## analysis_result schema

```json
{
  "type": "analysis_result",
  "status": "OK",
  "confidence": 0.74,
  "reason_codes": ["RANGE", "LOW_SPREAD"],
  "message": "Range-bound conditions",
  "analysis_result": {
    "summary_lines": [
      "Range-bound market",
      "Volatility moderate",
      "Spread stable"
    ],
    "strategy": {
      "strategy_id": "GRID_CLASSIC",
      "budget_usdt": 500,
      "levels": [
        {"side": "BUY", "price": 64200, "qty": 0.01, "pct_from_mid": -1.2},
        {"side": "SELL", "price": 65800, "qty": 0.01, "pct_from_mid": 1.2}
      ],
      "grid_step_pct": 0.3,
      "range_low_pct": 1.5,
      "range_high_pct": 1.8,
      "bias": {"buy_pct": 50, "sell_pct": 50},
      "order_size_mode": "equal",
      "max_orders": 12,
      "max_exposure_pct": 35
    },
    "risk": {
      "hard_stop_pct": 8,
      "cooldown_minutes": 15,
      "soft_stop_rules": ["cooldown if spread widens"],
      "kill_switch_rules": ["halt on drawdown"],
      "volatility_mode": "medium"
    },
    "control": {
      "recheck_interval_sec": 60,
      "ai_reanalyze_interval_sec": 300,
      "min_change_to_rebuild_pct": 0.3
    },
    "actions": [
      {"action": "note", "detail": "Watch for breakout", "severity": "info"}
    ]
  }
}
```

## strategy_patch schema

```json
{
  "type": "strategy_patch",
  "status": "OK",
  "confidence": 0.62,
  "reason_codes": ["USER_REQUEST"],
  "message": "Conservative tweak",
  "strategy_patch": {
    "parameters_patch": {
      "budget_usdt": 400,
      "grid_step_pct": 0.4,
      "max_exposure_pct": 25,
      "hard_stop_pct": 6,
      "volatility_mode": "low"
    },
    "recommended_actions": [
      {"action": "note", "detail": "Keep orders smaller", "severity": "info"}
    ],
    "message": "Adjusted risk lower."
  }
}
```
