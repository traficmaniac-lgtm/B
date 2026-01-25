# NC Pilot log analysis (2026-01-25)

## Commands used
- `ls`
- `rg --files -g 'AGENTS.md'`

## Key symptoms in the log
- Data source repeatedly falls back from WebSocket to HTTP with periods of `WS_DEGRADED`, `WS_LOST`, and `symbol_stale`, which can leave KPI/price data stale or missing.
- The feed/router drops an invalid symbol named `MULTI`, and `bookTicker` for `MULTI` is marked unsupported.
- KPI remains in a `WAITING_BOOK` state early in the session because bid/ask data is missing.
- During shutdown, `NcPilotMainWindow` raises an `AttributeError` on `has_active_tabs`, which crashes the close handler.

## Likely root causes
1. **Invalid symbol configuration (`MULTI`)**
   - The price feed/router logs `invalid symbol dropped: MULTI`, and the NC Pilot module logs `unsupported bookTicker symbol=MULTI -> skip`. This suggests the UI or feed is trying to subscribe to a non-exchange symbol, which can block or delay bid/ask availability for the strategy and lead to missing UI data.

2. **Unstable WebSocket feed leading to HTTP-only mode**
   - Multiple symbols transition to `WS_DEGRADED` and `WS_LOST`, and the router repeatedly falls back to HTTP for `EURIUSDT`, `EUREURI`, and `TUSDUSDT`. This causes the KPI to stay in `WAITING_BOOK` and later shows `src=HTTP_ONLY` with empty spread/edge metrics, which matches the “no data” symptom.

3. **Shutdown exception masking clean cleanup**
   - The close event handler crashes because `NcPilotMainWindow` lacks `has_active_tabs`, which can cause timers or background state to remain active after closing and may interfere with subsequent sessions.

## Recommended checks/fixes
- **Fix invalid symbol usage**: Ensure no feed subscription uses `MULTI` as a symbol. If `MULTI` is a mode flag, it should not be passed to bookTicker or symbol validation.
- **Investigate WS connectivity**: Check WS connection stability (network, firewall, rate limits). If stable WS is required for bid/ask, add reconnect/backoff and surface warnings in UI when in HTTP-only mode.
- **Handle missing bid/ask data**: If HTTP fallback is used, ensure it populates bid/ask or adjust KPI logic to avoid reporting `WAITING_BOOK` indefinitely.
- **Fix close handler**: Add or guard `has_active_tabs` in `NcPilotMainWindow` to avoid `AttributeError` during shutdown.

## Evidence summary

- Multiple `WS_DEGRADED`/`WS_LOST` transitions for the same symbols within short intervals.
- `symbol_stale` messages followed by `src=HTTP_ONLY` updates.
- Explicit logs showing `invalid symbol dropped: MULTI`.

## Proposed fix plan

1. **Sanitize symbol list** before feeding `PriceFeedManager` (drop non‑exchange tokens such as `MULTI`).
2. **Add UI guard** to prevent user‑created pseudo‑symbols from entering the feed.
3. **Improve fallback path** so HTTP mode still populates bid/ask for KPI.
4. **Fix close handler** to avoid `has_active_tabs` attribute errors.

## Validation plan

- Reproduce with the same symbols and ensure no `invalid symbol` errors.
- Force WS disconnect for 10–20 seconds and confirm fallback maintains bid/ask.
- Close NC Pilot window and verify clean shutdown (no exceptions).

## Open questions

- Is `MULTI` a legacy mode flag or a UI alias? If so, where is it injected?
- Do any other pseudo‑symbols exist in saved user settings?
