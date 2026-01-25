# Project Structure

## Project tree (annotated)

```
.
├── CHANGELOG.md — project changelog.
├── AI_PROTOCOL.md — project notes for AI usage and expectations.
├── PROJECT_STRUCTURE.md — this file: layout reference and per-file overview.
├── README.md — quickstart and usage instructions.
├── reports
│   ├── ALGO_PILOT_v1.5.md — detailed description of the ALGO PILOT mode (RU, фактическая версия v1.6.5).
│   ├── NC MICRO.md — detailed NC MICRO mode overview (RU).
│   ├── NC_PILOT_MULTI_PAIR_2LEG_REBAL_LOOP_SPREAD.md — 2-leg rebalancing loop spread playbook (RU).
│   ├── NC_PILOT_MULTI_PAIR_ARCHITECTURE.md — architecture & components for NC Pilot multi-pair (RU).
│   ├── NC_PILOT_MULTI_PAIR_OVERVIEW.md — strategic overview of NC Pilot multi-pair (RU).
│   ├── NC_PILOT_MULTI_PAIR_RISK_CONTROLS.md — risk controls, guards, and kill-switches (RU).
│   ├── NC_PILOT_MULTI_PAIR_RUNTIME.md — runtime loops, scheduling, and state handling (RU).
│   ├── REPORT.md — project report/notes.
│   └── SYSTEM_OVERVIEW_RU.md — detailed system overview in Russian.
├── _link_test.txt — link test artifact.
├── requirements.txt — Python dependencies.
├── requirements.txt.txt — duplicate dependency list (legacy).
├── run_gui.bat — Windows batch launcher for the GUI.
├── src
│   ├── __init__.py — package marker for src.
│   ├── ai
│   │   ├── __init__.py — package marker for AI module.
│   │   ├── models.py — AI response/data models and parsing helpers.
│   │   ├── openai_client.py — OpenAI API client wrapper.
│   │   ├── operator_datapack.py — builds AI operator datapacks.
│   │   ├── operator_math.py — math helpers (fees, break-even, TP checks).
│   │   ├── operator_models.py — operator request/response schemas.
│   │   ├── operator_profiles.py — operator profiles and presets.
│   │   ├── operator_request_loop.py — iterative AI request loop logic.
│   │   ├── operator_runtime.py — runtime helpers for operator actions.
│   │   ├── operator_state_machine.py — operator state machine for decisions.
│   │   └── operator_validation.py — validation of operator patches/actions.
│   ├── app
│   │   ├── __init__.py — package marker for app entrypoint.
│   │   └── main.py — GUI entrypoint (config, logging, MainWindow).
│   ├── binance
│   │   ├── __init__.py — package marker for Binance clients.
│   │   ├── account_client.py — signed account/trading endpoints.
│   │   ├── http_client.py — HTTP API client for public endpoints.
│   │   └── ws_client.py — websocket client for streaming prices.
│   ├── config
│   │   └── fee_overrides.py — fee override tables for guard calculations.
│   ├── core
│   │   ├── __init__.py — package marker for core utilities.
│   │   ├── cancel_reconcile.py — cancel/reconcile helpers for open orders.
│   │   ├── config.py — configuration models and loader.
│   │   ├── logging.py — logging setup and adapters.
│   │   ├── micro_edge.py — edge/expected profit calculation helpers.
│   │   ├── models.py — core domain models (pairs/prices/etc.).
│   │   ├── nc_micro_exec_dedup.py — trade ID dedup + cumulative fill tracking.
│   │   ├── nc_micro_refresh.py — stale refresh gating and log limiting.
│   │   ├── nc_micro_stop.py — shared STOP finalization helpers.
│   │   ├── symbols.py — symbol validation/sanitization helpers.
│   │   ├── timeutil.py — time/TTL/backoff utilities.
│   │   └── strategies
│   │       ├── __init__.py — package marker for strategy helpers.
│   │       ├── manual_runtime.py — manual strategy runtime logic.
│   │       ├── manual_strategies.py — manual strategy definitions.
│   │       └── registry.py — strategy registry utilities.
│   ├── gui
│   │   ├── __init__.py — package marker for GUI.
│   │   ├── ai_operator_grid_window.py — AI operator grid window.
│   │   ├── dialogs
│   │   │   ├── __init__.py — package marker for dialogs.
│   │   │   └── pilot_settings_dialog.py — NC MICRO/ALGO PILOT settings dialog.
│   │   ├── i18n.py — GUI translations/localization helpers.
│   │   ├── lite_all_strategy_algo_pilot_window.py — Lite All Strategy Terminal with ALGO PILOT (v1.6.5).
│   │   ├── lite_all_strategy_nc_micro_window.py — Lite All Strategy Terminal with NC MICRO mode.
│   │   ├── lite_all_strategy_terminal_window.py — Lite All Strategy Terminal (multi-strategy grid UI).
│   │   ├── lite_grid_math.py — math helpers for lite grid UI and fills.
│   │   ├── lite_grid_window.py — lite grid trading window.
│   │   ├── main_window.py — main application window and menu.
│   │   ├── modes
│   │   │   └── ai_full_strateg_v2
│   │   │       ├── __init__.py — package marker for AI full strategy mode v2.
│   │   │       ├── controller.py — controller for AI full strategy mode.
│   │   │       └── window.py — window for AI full strategy mode.
│   │   ├── overview_tab.py — overview tab with market list.
│   │   ├── pair_action_dialog.py — dialog for pair action selection.
│   │   ├── pair_mode_manager.py — opens specific pair mode windows.
│   │   ├── pair_workspace_tab.py — tabbed pair workspace controller.
│   │   ├── pair_workspace_window.py — standalone pair workspace window.
│   │   ├── settings_dialog.py — settings dialog for user config.
│   │   ├── trade_ready_mode_window.py — trade-ready mode window.
│   │   ├── trading_runtime_window.py — runtime execution UI.
│   │   ├── trading_workspace_window.py — trading workspace UI for AI.
│   │   ├── models
│   │   │   ├── __init__.py — package marker for GUI state models.
│   │   │   ├── app_state.py — persisted application settings/state.
│   │   │   ├── market_state.py — market UI state container.
│   │   │   ├── pair_mode.py — pair mode selection model.
│   │   │   ├── pair_state.py — per-pair UI state.
│   │   │   ├── pair_workspace.py — workspace UI model.
│   │   │   └── pair_workspace_state.py — workspace state container.
│   │   └── widgets
│   │       ├── __init__.py — package marker for GUI widgets.
│   │       ├── bot_tab.py — bot tab widget.
│   │       ├── dashboard_tab.py — dashboard tab widget.
│   │       ├── log_dock.py — dockable log viewer widget.
│   │       ├── markets_tab.py — markets tab widget.
│   │       ├── pair_logs_panel.py — panel for pair logs.
│   │       ├── pair_topbar.py — top bar for pair actions.
│   │       └── settings_tab.py — settings tab widget.
│   ├── runtime
│   │   ├── __init__.py — package marker for runtime engine.
│   │   ├── engine.py — runtime engine for strategy execution.
│   │   ├── runtime_state.py — runtime state enum/model.
│   │   ├── strategy_executor.py — strategy execution helpers.
│   │   └── virtual_orders.py — in-memory virtual order book.
│   └── services
│       ├── __init__.py — package marker for services.
│       ├── ai_provider.py — AI provider stub/bridge.
│       ├── data_cache.py — in-memory data cache.
│       ├── markets_service.py — market list loading/filtering.
│       ├── nc_micro
│       │   ├── __init__.py — NC MICRO service helpers package.
│       │   └── session.py — NC MICRO runtime/session state containers.
│       ├── net
│       │   ├── __init__.py — NetWorker lifecycle helpers.
│       │   └── net_worker.py — background HTTP worker with queue/dedup/rate-limit.
│       ├── price_feed_manager.py — price feed orchestration (WS + HTTP fallback).
│       ├── price_feed_service.py — per-symbol feed service used by the manager.
│       ├── price_hub.py — Qt hub for price updates.
│       ├── price_service.py — price caching and TTL checks.
│       └── rate_limiter.py — rate limiter utility.
└── tests
    ├── __init__.py — test package marker.
    ├── manual
    │   └── nc_micro_router_test.md — manual NC MICRO router stability checklist.
    ├── test_ai_models.py — AI response model parsing tests.
    ├── test_ai_operator_state_machine.py — AI operator state machine tests.
    ├── test_ai_protocol_parse.py — AI protocol parsing tests.
    ├── test_app_state.py — tests for AppState persistence.
    ├── test_cancel_on_pause_stop.py — cancel-on-pause/stop tests.
    ├── test_fill_accumulator.py — fill accumulator tests.
    ├── test_imports.py — import smoke tests.
    ├── test_markets_service.py — tests for MarketsService.
    ├── test_pair_state.py — pair state persistence tests.
    ├── test_patch_validation.py — patch validation tests.
    ├── test_price_feed_manager.py — price feed manager tests.
    ├── test_price_feed_router.py — price feed router tests.
    ├── test_profit_math.py — profit math tests.
    ├── test_qty_calc.py — quantity calculation tests.
    ├── test_request_data_loop.py — request loop tests.
    ├── test_restore_affordability.py — restore affordability checks.
    ├── test_start_gating.py — strategy start gating tests.
    ├── test_strategies_build_orders.py — strategy order build tests.
    ├── test_symbols.py — symbol helper tests.
    └── test_transport_test_side_effects.py — transport test side effects.
```

## Runtime/Generated artifacts

These files are created at runtime and are not tracked in the repo:

- `config.json` — main application configuration (env, endpoints, limits). Expected by `src/app/main.py` and `core/config.py`.
- `config.user.yaml` — user state (auto-created at first run) for GUI settings and API keys.
- `data/exchange_info_*.json` — cached Binance exchange info payloads (MarketsService cache).
- `logs/crash/NC_MICRO_*.log` — crash‑catcher logs for NC MICRO window sessions.

## Modules overview (expanded)

- **core**: configuration loading/validation, logging setup, shared models, time utilities, cancel/reconcile helpers, and NC MICRO support helpers (refresh gating, dedup, stop finalize).
- **core/strategies**: manual strategy definitions and runtime helpers used by advanced/manual modes.
- **config**: fee override data used by profit-guard logic.
- **app**: GUI entry point (`python -m src.app.main`) and startup state wiring.
- **gui**: main window, dialogs, tabs, widgets, and UI state models for the PySide6 UI.
- **gui/dialogs**: shared modal dialogs (pilot settings).
- **gui/modes**: specialized mode packages (AI full strategy v2).
- **gui/lite_all_strategy_terminal_window.py**: Lite All Strategy Terminal (grid experimentation and trade gating).
- **gui/lite_all_strategy_algo_pilot_window.py**: ALGO PILOT v1.6.5 (grid automation, KPI checks, stale handling, profit guard).
- **gui/lite_all_strategy_nc_micro_window.py**: NC MICRO mode (HTTP-only trade source, crash catcher, stale policies, refresh gating, dedup).
- **services**: application services for prices, markets, caching, and rate limiting.
- **services/price_feed_manager.py**: WS/HTTP price orchestration and status reporting.
- **services/price_feed_service.py**: per‑symbol feed used by the manager.
- **services/nc_micro/session.py**: NC MICRO session/runtime state containers (grid settings, market cache, counters).
- **services/net/net_worker.py**: background HTTP task worker with priority/dedup/rate limiting.
- **binance**: HTTP, websocket, and account client wrappers for Binance endpoints.
- **ai**: AI operator schemas, datapack building, validation, and OpenAI client wrapper.
- **runtime**: local runtime engine and virtual order simulation.
- **tests**: unit tests for config loading, service behavior, and GUI import smoke checks.

## Runtime data flow (high-level)

1. `src/app/main.py` loads runtime config (expected `config.json`) and `config.user.yaml` via `gui/models/app_state.py`.
2. `MainWindow` creates `PriceFeedManager`, `PairModeManager`, then initializes UI tabs.
3. `OverviewTab` loads markets through `services/markets_service.py` and uses `PriceFeedManager` for live prices.
4. Selecting a pair opens one of:
   - `LiteGridWindow` (legacy grid terminal),
   - `LiteAllStrategyTerminalWindow` (multi-strategy grid experiments),
   - `LiteAllStrategyAlgoPilotWindow` (ALGO PILOT automation + grid runtime),
   - `LiteAllStrategyNcMicroWindow` (NC MICRO).
5. ALGO PILOT pulls KPIs, validates market state, builds grid plans, and applies stale policies before placing/refreshing orders.
6. NC MICRO relies on HTTP‑priority pricing, guard rails (trade gate, profit/break‑even/thin‑edge), refresh gating, and deduplication before trading.
7. Optional AI modes build datapacks in `ai/operator_datapack.py` and validate patches in `ai/operator_validation.py`.

## Run

PowerShell:

```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m src.app.main
```

Batch (Windows):

```
run_gui.bat
```

## User config (runtime)

- File: `<project_root>/config.user.yaml`
- Created automatically on first run (if missing).
- Keys stored:
  - `env`, `log_level`, `config_path`, `show_logs`
  - `binance_api_key`, `binance_api_secret`, `openai_api_key`, `openai_model`
  - `default_period`, `default_quality`, `allow_ai_more_data`
  - `price_ttl_ms`, `price_refresh_ms`
  - `default_quote`, `pnl_period`

## Cached data (runtime)

- `data/exchange_info_*.json` caches Binance `exchangeInfo` payloads for faster startup and offline reuse.
- Cache freshness is evaluated by `MarketsService` and can be invalidated by TTL or explicit refresh.

## Known limitations

- `services/ai_provider.py` uses rule-based placeholder logic and does not call external AI APIs.
- Websocket and HTTP clients are thin wrappers and require valid credentials/endpoints to fetch live data.
