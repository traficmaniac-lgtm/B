# Changelog

## v1.0.7
- Add bid/ask readiness flag, faster warmup exit on fresh HTTP book data, and fix GRID duplicate-local checks to allow full BUY-side placement.

## v1.0.6
- Enforce HTTP-only trade source with safe WS handling and bid/ask validation to prevent NC_MICRO crashes during WS flapping.
