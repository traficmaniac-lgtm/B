# NC_MICRO router stability & orders poll throttle (manual test)

## Goal
Validate router anti-flap, WS grace handling, UI source stability, and orders poll throttling for `EURIUSDT`.

## Debug mode (optional)
Enable router debug summaries:

```bash
export NC_MICRO_ROUTER_DEBUG=1
```

This logs `[ROUTER][DEBUG]` every ~10s with source, WS age, and switch counts.

## Steps
1. Launch the NC_MICRO window for `EURIUSDT` with an intentionally unstable WS (e.g., flaky network, or WS endpoint blocked).
2. Let it run for 60 seconds.
3. Observe logs and UI.

## Expected results
* Router switches are limited (≤ 6 per minute, ideally ≤ 3), with compact router logs:
  * `[ROUTER] EURIUSDT src=HTTP reason=... ws_age_ms=... cooldown=...`
* No constant `WS recovered -> switching source` spam.
* Orders poll refresh logs appear at most:
  * ~1–2 seconds in `RUNNING`
  * ~2.5 seconds or slower in `IDLE`
* Manual refresh still triggers an immediate refresh once per action.
* UI source label changes only when the router source changes and is debounced (~300ms).
