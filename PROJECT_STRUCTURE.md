# Project Structure

## Project tree (annotated)

```
.
├── AI_PROTOCOL.md — project notes for AI usage and expectations.
├── PROJECT_STRUCTURE.md — this file: layout reference and per-file overview.
├── README.md — quickstart and usage instructions.
├── REPORT.md — project report/notes.
├── SYSTEM_OVERVIEW_RU.md — detailed system overview in Russian.
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
│   │   ├── operator_math.py — math helpers for operator calculations.
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
│   ├── core
│   │   ├── __init__.py — package marker for core utilities.
│   │   ├── config.py — configuration models and loader.
│   │   ├── logging.py — logging setup and adapters.
│   │   ├── models.py — core domain models (pairs/prices/etc.).
│   │   ├── symbols.py — symbol validation/sanitization helpers.
│   │   ├── timeutil.py — time/TTL/backoff utilities.
│   │   └── strategies
│   │       ├── __init__.py — package marker for strategy helpers.
│   │       ├── defs
│   │       │   ├── __init__.py — built-in strategy definition helpers.
│   │       │   └── grid_classic.py — classic grid strategy definition.
│   │       ├── manual_runtime.py — manual strategy runtime logic.
│   │       ├── manual_strategies.py — manual strategy definitions.
│   │       └── registry.py — strategy registry utilities.
│   ├── gui
│   │   ├── __init__.py — package marker for GUI.
│   │   ├── ai_operator_grid_window.py — AI operator grid window.
│   │   ├── i18n.py — GUI translations/localization helpers.
│   │   ├── lite_all_strategy_terminal_window.py — Lite All Strategy Terminal (multi-strategy grid UI).
│   │   ├── lite_grid_math.py — math helpers for lite grid UI.
│   │   ├── lite_grid_window.py — lite grid trading window.
│   │   ├── main_window.py — main application window and menu.
│   │   ├── overview_tab.py — overview tab with market list.
│   │   ├── pair_action_dialog.py — dialog for pair action selection.
│   │   ├── pair_mode_manager.py — opens specific pair mode windows.
│   │   ├── pair_workspace_tab.py — tabbed pair workspace controller.
│   │   ├── pair_workspace_window.py — standalone pair workspace window.
│   │   ├── settings_dialog.py — settings dialog for user config.
│   │   ├── trade_ready_mode_window.py — trade-ready mode window.
│   │   ├── trading_runtime_window.py — runtime execution UI.
│   │   ├── trading_workspace_window.py — trading workspace UI for AI.
│   │   ├── modes
│   │   │   ├── ai_full_strateg_v2
│   │   │   │   ├── __init__.py — package marker for AI full strategy mode.
│   │   │   │   ├── controller.py — controller helpers for the mode.
│   │   │   │   └── window.py — AI full strategy V2 window.
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
│       ├── price_feed_manager.py — price feed orchestration.
│       ├── price_feed_service.py — per-symbol price feed client.
│       ├── price_hub.py — Qt hub for price updates.
│       ├── price_service.py — price caching and TTL checks.
│       └── rate_limiter.py — rate limiter utility.
└── tests
    ├── __init__.py — test package marker.
    ├── test_app_state.py — tests for AppState persistence.
    ├── test_imports.py — import smoke tests.
    └── test_markets_service.py — tests for MarketsService.
```

## Modules overview

- **core**: configuration loading/validation, logging setup, shared models, time utilities, and strategy registry helpers.
- **core/strategies/defs**: built-in strategy metadata used by advanced manual/AI modes.
- **app**: GUI entry point (`python -m src.app.main`) and startup state wiring.
- **gui**: main window, dialogs, tabs, widgets, and UI state models for the PySide6 UI.
- **gui/models**: UI state containers such as `AppState` and pair/workspace models.
- **gui/modes**: experimental/advanced UI modes (e.g., AI full strategy V2).
- **gui/lite_all_strategy_terminal_window.py**: enhanced Lite terminal focused on multi-strategy grid experimentation.
- **services**: application services for prices, markets, caching, and rate limiting.
- **binance**: HTTP, websocket, and account client wrappers for Binance endpoints.
- **ai**: AI operator schemas, datapack building, validation, and OpenAI client wrapper.
- **runtime**: local runtime engine and virtual order simulation.
- **tests**: unit tests for config loading, service behavior, and GUI import smoke checks.

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

## User config

- File: `<project_root>/config.user.yaml`
- Created automatically on first run (if missing).
- Keys stored:
  - `env`, `log_level`, `config_path`, `show_logs`
  - `binance_api_key`, `binance_api_secret`, `openai_api_key`, `openai_model`
  - `default_period`, `default_quality`, `allow_ai_more_data`
  - `price_ttl_ms`, `price_refresh_ms`
  - `default_quote`, `pnl_period`

## Known limitations

- `services/ai_provider.py` uses rule-based placeholder logic and does not call external AI APIs.
- Websocket and HTTP clients are thin wrappers and require valid credentials/endpoints to fetch live data.
