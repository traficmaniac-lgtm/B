## Technical Report

### Step 1 - Skeleton + config + logging + models
- Added core configuration loader with environment overrides and validation.
- Added shared logging setup and logger adapter helper.
- Added core data models for pairs, prices, and exchange info.
- Added time utilities for monotonic timing and exponential backoff.

### Stage 1.1 - Working GUI skeleton
- Added PySide6 desktop GUI with dashboard, markets, bot, and settings tabs plus log dock.
- Implemented GUI log handler, demo market data table, and bot placeholder actions.
- Added user settings persistence and import tests for GUI entry points.

### Stage 1.2 - Pair workspace + AI loop scaffolding
- Expanded settings with exchange/AI keys, default period/quality, and AI data request toggle.
- Added pair workspace window with analysis, strategy form, AI chat, and simulated confirm/start flow.
- Introduced AI provider stub with datapack request/plan logic and added import coverage tests.

### Stage 1.2.1 - Overview + settings dialog + bot analysis tabs
- Merged dashboard/markets into an Overview tab with status cards and demo pair table.
- Replaced settings tab with a modal Settings dialog, persisted to config.user.yaml.
- Added Pair Workspace tabs with a state machine, pipeline controls, AI chat placeholder, and local logs.

### Stage 3.0.1 - Bot workspace state machine + bot-only price updates
- Implemented a strict bot workspace state machine with staged controls and transition logging.
- Added demo data prep/analyze/apply flow and resume-ready confirm start in the bot UI.
- Stopped Overview from streaming prices; live updates now run only for open bot tabs.
- Added a price hub that registers/unregisters symbols on tab open/close.
- Added PairState transition test coverage.

### Stage 2.1.2 - AI protocol JSON + datapack expansion + UI patch flow
- Added strict AI protocol dataclasses, response envelope parsing, and invalid JSON guards with single retry.
- Expanded the analysis datapack with market snapshot, liquidity summary, constraints, and user context.
- Updated Pair Workspace to parse analysis_result/strategy_patch, block unsafe starts, and support Apply Patch.
- Added local quick-intent parsing for budget updates in AI chat.
- Refreshed AI_PROTOCOL.md with new schemas and examples plus parser tests.

### Stage 3.1 - Overview filtering + Trading Workspace + minute periods
- Switched Overview to a proxy-filtered model with search + quote filtering, shown/total counter, and cache status.
- Added minute timeframes to period dropdowns and fallback warning when minute data is unavailable.
- Introduced Trading Workspace with runtime panels, AI Observer, and user-approved action gating.
- Added AI protocol fallback helper and tests for invalid JSON DO_NOT_TRADE handling.

### Stage 4.0 - Trade Ready Mode GUI + AI mock flow
- Replaced the Advanced AI placeholder with the Trade Ready Mode window and state machine.
- Added market context, analysis controls, AI summary, trade variants, chat, and history zones.
- Wired Overview double-click to pass last price into the Trade Ready Mode entry point.

### Stage 4.0.1 - Trading Runtime window scaffolding
- Added Trading Runtime Window with header controls, split layout, and runtime state machine.
- Implemented mock orders, position snapshot, AI observer, recommendations, runtime chat, and tabs.
- Wired Trade Ready Mode to open the runtime window with a strategy snapshot and mode selection.

### Stage 4.1 - Runtime Engine (dry-run)
- Added runtime module with price feed polling, virtual order book, strategy executor, and runtime state machine.
- Wired Trading Runtime Window to the runtime engine with live orders, PnL, exposure, and observer updates.
- Extended strategy snapshots for numeric parameters and refreshed AI observer recommendations using live metrics.

### v7.1 Live check (2–4 orders)
1. Open Lite Grid Terminal for a spot symbol with API keys enabled (e.g., BTCUSDT).
2. Toggle DRY-RUN off, confirm LIVE mode, keep grid_count/max_active_orders at 2–4, set a small budget.
3. Press Start and confirm the LIVE dialog; expect [LIVE] place logs and 2–4 open orders in the table.
4. Wait 10–20 seconds and verify orders remain in Binance Open Orders and the bot table (no auto-cancel).

### v7.1 WS stabilization (BBOT)
- Corrected Binance WS base URL usage and stream formatting; added a one-time DEBUG log to verify the final WS URL.
- Added WS anti-flapping with disabled-404 TTL, switch cooldown, and recovery hysteresis to prevent noisy reconnects.
- Reduced HTTP polling when WS is unhealthy based on active subscribers and tuned GUI close events to stop timers.

Manual checks:
1. Open Overview → select BTCUSDT → confirm WS stream connects without HTTP 404s.
2. Disconnect network for ~10s → observe clean fallback without rapid log spam.
3. Close a pair window → ensure symbol polling stops within a few seconds.

### v10 Router warmup + transport-test hardening
- Added deterministic WS warmup routing with per-symbol grace windows, stale detection, and clearer router logs.
- Made TRANSPORT-TEST non-invasive by keeping subscriptions alive with keepalive cleanup and waiting for first ticks.
- Added refcounted subscription checks, self-check transport-test logging, and router decision tests.

Repro before:
1. Open Overview → load pairs.
2. Open AI Operator Grid for BTCUSDT; trigger TRANSPORT-TEST.
3. Observe immediate WS fallback to HTTP and WS=NONE even while WS is recoverable.

Verify after:
1. Launch app.
2. Load pairs.
3. Click BTCUSDT → open AI Operator Grid.
4. Press “Self-check”.

Expected:
- WS shows WARMUP then OK within ~3s, without immediate HTTP fallback.
- TRANSPORT-TEST does not force WS=NONE or remove subscriptions.
- AI snapshot shows ws=OK (or WARMUP briefly) when WS is connected.

### Engine hotfix balances/fills
- Added strict engine readiness gating to require rules, fresh balances, and trade gate before start.
- Centralized BUY/SELL qty calculation with buffers, step rounding, and minNotional enforcement.
- Added fill accumulation with delta processing and idempotent TP/RESTORE actions on partial fills.
- Ensured TP/RESTORE placement re-checks balances and skips when unaffordable (no retry loops).
- Improved fill/placement logging with compact delta and plan summaries.

Repro (before):
1. Open AI Operator Grid with low USDT free balance and press Start.
2. Trigger a partial fill for an order (simulated or via small size).
3. Observe repeated INSUFFICIENT_BALANCE logs and multiple TP/RESTORE placements.

Verify (after):
1. Launch AI Operator Grid, confirm Start is blocked until rules + fresh balances are loaded.
2. Place grid, then trigger partial fills; verify logs show single FILLED with total/delta.
3. Confirm only one TP/RESTORE per delta is placed, and restore is skipped when quote free is insufficient.

### v10.0.7 AI Operator Grid datapack + validation + cancel-on-pause/stop
- Added deterministic AI datapack builder with exchange rules, fees, account snapshot, WS/HTTP data quality, and multi-window kline summaries.
- Added explicit profit math (fees + slippage + spread) and profile presets for validation.
- Enforced strict JSON AI responses with action-driven REQUEST_DATA loop and patch validation.
- Implemented reliable Pause/Stop cancellation using bot order registry + prefix matching.

Verify in GUI:
1. Open AI Operator Grid → press “AI Analyze”; confirm snapshot shows data quality line with WS/HTTP ages.
2. Trigger a response with REQUEST_DATA; choose action REQUEST_DATA and approve; confirm AI request loop logs.
3. Press Pause/Stop; confirm log shows “Canceled n orders” and bot orders are cleared.

### v10.0.3 AI Operator Grid fee-aware AI + always-tradable patch + data quality + lookback loop
- Updated AI Operator Grid protocol to require analysis_result + strategy_patch + diagnostics with always-tradable patches.
- Added fee-aware math (maker/taker, slippage, safety edge) used for validation and live TP/restore safety checks.
- Introduced lookback selector (1/7/30/365) and trades_window aggregation; datapack now reports missing data, pack_source, and timings.
- Implemented optional request-more-data loop (single pass) and clear missing-data reporting when lookback fetch fails.

Tests:
- `pytest -q tests/test_profit_math.py tests/test_patch_validation.py tests/test_request_data_loop.py`

### v10.x AI Operator Grid state machine + safety gates
- Added strict AI Operator Grid state machine with gated controls (refresh/analyze/apply/start/pause/stop).
- Enforced safety checks before PLAN_READY (fees/step, micro-volatility range, minQty/minNotional) and DO_NOT_TRADE logging.
- Locked AI confidence on Apply Plan, invalidated on manual parameter edits, and reset on Stop.
- Added state machine unit tests covering transitions and gating.
