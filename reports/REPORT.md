## Technical Report

### NC_MICRO v1.0.30 — Exec dup dedupe + rate limit
- Added exec-key LRU/TTL cache for (symbol, orderId, tradeId) to skip duplicate fills.
- Rate-limited exec_dup INFO logs to first dup + once per 10s with suppression count.
- Reset exec dedupe/log state on start/stop to avoid stale suppression.
- Manual check: LIVE fill shows one exec_new; exec_dup suppressed (≤1 per 10s).

### NC_MICRO v1.0.26 — Net worker, DRY-RUN fallback, bid/ask polling, GUI cleanup
- Routed NC_MICRO Binance HTTP calls through a dedicated net worker with a single thread-bound httpx.Client to prevent SSL/thread crashes.
- Added account snapshot preservation with error banners + backoff (1s→3s after 3 failures) so balances never reset on transient errors.
- Enabled DRY-RUN by default with LIVE fallback messaging; added start-mode logging and tightened bid/ask polling log noise.
- Simplified the NC_MICRO UI to only the essential controls and refreshed the grid summary line.

Manual check (NC_MICRO v1.0.26):
1. Open NC_MICRO EURIUSDT: confirm DRY-RUN ON by default and logs show `[NC_MICRO] opened. version=NC MICRO v1.0.26`.
2. Disconnect internet / force API error: balances remain, banner reads `ACCOUNT: ERROR (kept last)`, no crash.
3. Toggle LIVE: if trade_gate not OK, UI stays in DRY-RUN with `LIVE unavailable` banner; Start still allowed in DRY-RUN.
4. Verify bid/ask via HTTP book polling within ~2s; `no bid/ask` logs appear at most every 5s with reason.
5. Close window: app remains running; feed manager stays alive when other subscribers exist.

### NC_MICRO v1.0.23 — Global crash guard + Qt handler + panic HOLD
- Added global crash guard hooks (sys/thread/Qt) that log `[CRASH] uncaught` and append tracebacks to APP/NC_MICRO crash logs.
- Registered NC_MICRO window panic_hold callback to force Pilot HOLD and cancel pending actions on fatal exceptions.
- Added SafeQt wrapper for Qt signal handlers and hooked NC_MICRO crash log paths into the global guard.

Manual check (NC_MICRO):
1. Launch the app and open an NC_MICRO window; ensure startup logs show `[CRASH_GUARD] ... installed`.
2. Wait until the window reaches WAITING_BOOK; if an exception occurs in background/Qt, verify the app stays alive.
3. Confirm crash logs include `[CRASH] uncaught` and NC_MICRO switches to HOLD without trading actions.

### NC_MICRO v1.0.23 — Safe-call wrappers + bid/ask guards + closing watchdog safety
- Added NC_MICRO-safe call wrapper that logs exceptions with stack traces, mirrors to crash log, and forces Pilot HOLD on handler failures.
- Wrapped NC_MICRO QTimer timeouts and key Qt signal handlers with safe_call; added closing guards to timers/slots and watchdog paths.
- Hardened bid/ask math paths to gate spread/edge calculations and push KPI into WAITING_BOOK when bid/ask is invalid.

Manual check (NC_MICRO):
1. Open NC_MICRO for a symbol without immediate bid/ask (or disconnect feed temporarily).
2. Wait for `[KPI] state=WAITING_BOOK reason=no_bidask_yet` logs to appear.
3. Confirm the window remains running (no crash) and Pilot transitions to HOLD on any injected handler exception.

### NC_MICRO v1.0.18 — Fix partial fills + TP aggregation + Stop/Cancel hardening + stale spam suppression
- Added tradeId-based execution dedup with TTL/LRU and cumulative-fill fallback; exec logs now distinguish new vs duplicate events.
- Switched TP to ONE-TP cancel/replace with stable clientOrderId and cumulative BUY sizing, keeping TP volume aligned with partial fills.
- Hardened Stop flow to hold STOPPING until open orders clear (or watchdog timeout) and blocked new orders during STOPPING/STOPPED.
- Reduced stale refresh log spam with per-reason rate limiting and updated snapshot sync/change timestamps.

Why crash happened:
- Partial fills were treated as final FILLED events, and TP dedup keys locked on the first fill; subsequent cumulative executions retriggered paths that hit inconsistent registry/GUI calls.

### NC MICRO v1.0.22
- Fixed min profit bps conversions and TP gate logging in spread-capture mode.
- Ensured tick-based spread-capture plans align BUY/SELL quantities and clamp sell sizing to inventory.
- Blocked order placement/reprice when bid/ask feed is stale; added coverage tests.

### NC MICRO v1.0.21
- Safe WS shutdown with closing/loop-closed enqueue guards.
- Time sync fix for signed requests with -1021 resync retry and recvWindow bump.
- Micro TP gate calibration for low spreads and break-even unwind support.

### NC MICRO v1.0.20
- Added STOP watchdog timeout to force finalize and re-enable Start after cancellation hangs.
- Deduplicated PILOT/KPI/ORDERS stale logs with heartbeat suppression.
- Added Binance signed 400 response logging with code/msg and timestamp context.

### NC MICRO v1.0.19
- Fixed Stop/CANCEL deadlock by forcing REST reconcile, overriding inflight suppression, and guaranteeing Stop finalization.
- Softened WS heartbeat handling with a higher idle threshold and symbol-stale HTTP fallback logging (no restart flaps).

### NC MICRO v1.6.6
- Added stale refresh limiter with per-reason rate limits, poll change detection, and deduped logging counters.
- Introduced per-symbol router hysteresis with deterministic source selection and compact router status fields for UI.
- Guarded WS shutdown paths to skip closed-loop enqueues with rate-limited logs.
- Hardened NC MICRO order registry to skip duplicate RESTORE placements via active registry keys.
- Added MarketHealth KPI with edge/expected-profit calculations and thin-edge logging.

Manual check (EURIUSDT, ~10 min):
1. Open NC MICRO for EURIUSDT and leave WS in flapping state (toggle network or simulate degraded WS).
2. Observe router status: `src` stays stable (usually HTTP) and does not switch every second.
3. Let the app run for a few minutes with no order changes; confirm `[ORDERS] stale -> refresh` logs appear
   no more than once every ~5s and refreshes are spaced out.
4. Trigger a fill (or simulate) and verify RESTORE is not duplicated; logs show `skip_duplicate_restore` if
   a duplicate is attempted.
5. Stop the window/app; confirm no `RuntimeError: Event loop is closed` and shutdown logs are orderly.

### NC MICRO v1.0.5
- Hardened NC_MICRO HTTP book success handlers with bound callbacks, self guards, and payload safety to prevent runtime crashes.

### NC MICRO v1.0
- Added NC MICRO mode with a cloned Lite All Strategy window and routing entry in the pair dialog.

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

### Hotfix v10.x.x: WS safe stop, request loop safe, AI Operator Grid UI normalized
- Guarded WS loop enqueues on shutdown and ensured stop ordering avoids closed-loop calls.
- Made AI request-more-data loop UI-safe and kept lookback/UI fields consistent.
- Normalized AI Operator Grid state/data quality labels and balance freshness checks.

### Hotfix v10.x.x: apply/approve sync + state badge + manual-guard
- Unified Apply Plan/Approve flow to apply once, clear pending actions, and avoid re-apply warnings.
- Added response logging for AI analyze/chat/lookback to capture state + pending_action transitions.
- Guarded programmatic updates (apply patch/reset defaults) from triggering manual invalidation.
- Logged user-driven parameter changes and kept invalidation only on real edits.
- Cleared pending actions/confidence on snapshot refresh to return INVALID -> DATA_READY cleanly.
- Made reset defaults idempotent to avoid duplicate "Settings reset to defaults." events.

### NC_MICRO v1.0.27 — Multi-tab parallel sessions + 1m logs
- Added per-symbol NcMicroSession container (config, runtime flags, market cache, order tracking, counters).
- Stored NC_MICRO settings per symbol (with legacy fallback) and added Apply button + Start config snapshot.
- Implemented per-symbol 1-minute summary heartbeat with reduced exec dup spam.
- Switched price feed subscriptions to refcounted subscribe/unsubscribe and improved feed logging for multi-symbol mode.

Acceptance checklist:
1. ✅ Session isolation: per-symbol config + runtime + trackers stored on NcMicroSession (no shared mutable state).
2. ✅ Settings: per-symbol persistence with Apply button; Start logs config snapshot; reset defaults affects only current session.
3. ✅ Price feed: subscribe/unsubscribe refcounts per symbol; open tabs drive subscriptions without overview global state.
4. ✅ Exec dedup: per-symbol caches; exec_dup logs are DEBUG + counted in 1m summary.
5. ✅ 1-minute summary log per symbol using monotonic time (heartbeat even on no bid/ask).
6. ✅ Log tagging: session logs prefixed with [NC_MICRO][symbol].

### NC_PILOT v1.0.13 — Desktop multi-pair module
- Moved NC_PILOT to Desktop Terminal launcher (multi-pair controller) and removed pair-dialog entry.
- Rebuilt NC_PILOT UI with multi-pair status, family budgets, filtered balances, and pilot-centric panels.
- Enforced 2LEG-only execution with deterministic HOLD/TRADE_BLOCK reasons and 2LEG budget sizing.

---

## Reporting conventions

- Каждый пункт обновления должен включать: **контекст**, **изменения**, **эффект**, **проверку**.
- Для критических фиксов добавляется блок **Risk/Regression** и **Rollback plan**.
- Логи ключевых проверок указывать в секции Manual check.

## Release checklist (сокращённый)

1. Проверить запуск GUI и загрузку конфигурации.
2. Открыть минимум одно окно NC MICRO и ALGO PILOT.
3. Проверить обработку ошибок API (live‑gate).
4. Убедиться, что crash‑guard создаёт crash‑лог.
5. Обновить заметки о версии и дату в данном отчёте.
