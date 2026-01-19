# Project Structure

## Project tree

```
.
├── README.md
├── REPORT.md
├── requirements.txt
├── run_gui.bat
├── src
│   ├── app
│   │   ├── __init__.py
│   │   └── main.py
│   ├── binance
│   │   ├── __init__.py
│   │   ├── http_client.py
│   │   └── ws_client.py
│   ├── core
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── logging.py
│   │   ├── models.py
│   │   └── timeutil.py
│   ├── gui
│   │   ├── __init__.py
│   │   ├── main_window.py
│   │   ├── overview_tab.py
│   │   ├── pair_workspace_tab.py
│   │   ├── pair_workspace_window.py
│   │   ├── settings_dialog.py
│   │   ├── models
│   │   │   ├── __init__.py
│   │   │   ├── app_state.py
│   │   │   ├── pair_state.py
│   │   │   └── pair_workspace.py
│   │   └── widgets
│   │       ├── __init__.py
│   │       ├── bot_tab.py
│   │       ├── dashboard_tab.py
│   │       ├── log_dock.py
│   │       ├── markets_tab.py
│   │       ├── pair_logs_panel.py
│   │       ├── pair_topbar.py
│   │       └── settings_tab.py
│   ├── services
│   │   ├── __init__.py
│   │   ├── ai_provider.py
│   │   ├── markets_service.py
│   │   └── price_service.py
│   └── __init__.py
└── tests
    ├── __init__.py
    ├── test_app_state.py
    ├── test_imports.py
    └── test_markets_service.py
```

## Modules overview

- **core**: configuration loading/validation, logging setup, shared models, and time utilities.
- **app**: GUI entry point (`python -m src.app.main`) and startup state wiring.
- **gui**: main window and tabs/widgets for the PySide6 UI.
- **gui/models**: UI state containers such as `AppState` and pair workspace models.
- **services**: application services for prices, markets, and AI strategy suggestions.
- **binance**: HTTP and websocket client wrappers for Binance endpoints.
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
  - `binance_api_key`, `binance_api_secret`, `openai_api_key`
  - `default_period`, `default_quality`, `allow_ai_more_data`
  - `price_ttl_ms`, `price_refresh_ms`
  - `default_quote`, `pnl_period`

## Known limitations

- `services/ai_provider.py` uses rule-based placeholder logic and does not call external AI APIs.
- Websocket and HTTP clients are thin wrappers and require valid credentials/endpoints to fetch live data.
