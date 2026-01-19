# AI Protocol (Stage 2.1.2)

## Safety rules
- AI must never place trades, open positions, or execute orders.
- AI only analyzes, suggests, and provides patches to the local strategy form.
- AI must return **ONLY valid JSON** (no markdown, no extra prose).
- All AI responses must be **in Russian** and include `"language": "ru"`.

## Analysis DataPack (v2.1.2)

The GUI sends a compact datapack with market snapshot and user context. No large arrays.

```json
{
  "version": "v2.1.2",
  "symbol": "USDTUSDC",
  "analysis_mode": "ZERO_FEE_CONVERT",
  "market": {
    "last_price": 1.0002,
    "bid": 1.0001,
    "ask": 1.0003,
    "spread_pct": 0.02,
    "ranges": {
      "1m": {"low": 0.9999, "high": 1.0004, "pct": 0.15},
      "5m": {"low": 0.9997, "high": 1.0006, "pct": 0.25},
      "1h": {"low": 0.9992, "high": 1.0010, "pct": 0.40},
      "24h": {"low": 0.9985, "high": 1.0016, "pct": 0.60}
    },
    "volatility_est": 0.25,
    "liquidity": 153200000,
    "source_latency_ms": 420,
    "timestamp_utc": "2024-05-18T12:00:00+00:00"
  },
  "constraints": {
    "tick_size": null,
    "step_size": null,
    "min_qty": null,
    "min_notional": null
  },
  "budget": {
    "budget_usdt": 500,
    "mode": "Grid"
  },
  "user_context": {
    "budget_usdt": 500,
    "mode": "Grid",
    "dry_run": true,
    "selected_period": "4h",
    "quality": "Standard",
    "liquidity_summary": {
      "base_volume_24h": 0,
      "quote_volume_24h": 0,
      "trade_count_24h": 0
    }
  },
  "pair_context": {
    "base_asset": "USDT",
    "quote_asset": "USDC",
    "source": "ws"
  }
}
```

## Monitor DataPack (Trading Workspace)

```json
{
  "version": "v2.1.2",
  "symbol": "USDTUSDC",
  "analysis_mode": "ZERO_FEE_CONVERT",
  "runtime": {
    "state": "RUNNING",
    "dry_run": true,
    "uptime_sec": 120,
    "last_reason": "confirm_start"
  },
  "open_orders": [],
  "position": {
    "status": "FLAT",
    "qty": 0,
    "entry_price": 0,
    "realized_pnl": 0,
    "unrealized_pnl": 0
  },
  "pnl": {
    "total": 0,
    "realized": 0,
    "unrealized": 0
  },
  "recent_fills": [],
  "strategy_state": {
    "strategy_id": "GRID_CLASSIC",
    "budget_usdt": 500,
    "mode": "Grid",
    "grid_step_pct": 0.3,
    "range_low_pct": 1.5,
    "range_high_pct": 1.8,
    "bias": {"buy_pct": 50, "sell_pct": 50},
    "order_size_mode": "equal",
    "max_orders": 12,
    "max_exposure_pct": 35,
    "hard_stop_pct": 8,
    "cooldown_minutes": 15,
    "recheck_interval_sec": 60,
    "ai_reanalyze_interval_sec": 300,
    "min_change_to_rebuild_pct": 0.3,
    "volatility_mode": "medium",
    "plan_applied": true,
    "plan_levels": []
  },
  "health": {
    "ws_latency_ms": 420,
    "http_latency_ms": null,
    "errors": 0
  }
}
```

## Response Envelope (strict)

Every AI response must be a JSON object with the following schema:

```json
{
  "analysis_result": {
    "status": "OK | WARN | DANGER | ERROR",
    "confidence": 0.0,
    "reason_codes": ["..."],
    "summary_ru": "Короткое резюме на русском"
  },
  "trade_options": [
    {
      "name_ru": "Вариант 1",
      "entry": 1.0001,
      "exit": 1.0004,
      "tp_pct": 0.2,
      "stop_pct": 0.1,
      "expected_profit_pct": 0.15,
      "expected_profit_usdt": 0.45,
      "eta_min": 20,
      "confidence": 0.6,
      "risk_note_ru": "Риск: низкая ликвидность"
    }
  ],
  "action_suggestions": [
    {"type": "SUGGEST_REBUILD", "note_ru": "Обновить уровни из-за роста спрэда"}
  ],
  "strategy_patch": {
    "parameters_patch": {
      "budget_usdt": 400,
      "grid_step_pct": 0.4,
      "range_low_pct": 1.2,
      "range_high_pct": 1.6,
      "volatility_mode": "low"
    }
  },
  "language": "ru"
}
```
