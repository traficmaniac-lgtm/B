# Changelog

## v1.0.22
- Fixed min profit bps units and TP gating for spread-capture mode.
- Aligned tick-based BUY/SELL quantities and clamped sell sizing to inventory.
- Blocked order placement on stale bid/ask; added coverage tests.

## v1.0.21
- Safe WS shutdown (skip enqueue on closing/loop closed).
- Time sync fix for signed requests with -1021 retry + recvWindow bump.
- Micro TP gate calibration for low spreads.

## v1.0.20
- Add STOP watchdog timeout to force finalize and re-enable Start after cancellation hangs.
- Deduplicate noisy PILOT/KPI/ORDERS stale logs with heartbeat logging.
- Log Binance signed 400 response body details (code/msg) with timestamp context.

## v1.0.19
- Fix NC_MICRO Stop/CANCEL deadlock with forced REST reconcile and guaranteed finalization.
- Soften WS heartbeat handling to prevent reconnect storms on quiet symbols.

## v1.0.8
- Add NC_MICRO bootstrap legacy-order tracking, owned-order stop handling, and snapshot stale refresh throttling.
- Unblock Start after balances/open-orders readiness and legacy cleanup flow.

## v1.0.7
- Add bid/ask readiness flag, faster warmup exit on fresh HTTP book data, and fix GRID duplicate-local checks to allow full BUY-side placement.

## v1.0.6
- Enforce HTTP-only trade source with safe WS handling and bid/ask validation to prevent NC_MICRO crashes during WS flapping.
