# BBOT Desktop Terminal (Stage 2.0.1)

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
```

## Run GUI

```powershell
python -m src.app.main
```

## Overview (real Binance pairs)

1. Open the **Overview** tab.
2. Pick the desired Quote (e.g., USDT).
3. Click **Load Pairs** to fetch real Binance Spot symbols.
4. Prices will refresh automatically via HTTP every ~2 seconds.

### Settings

Open Settings from the toolbar or File → Settings menu. Changes are saved to `config.user.yaml`.

### Pair Workspace

Double click any row in the Overview tab to open a Pair Workspace bot tab for that symbol.

## Run tests

```powershell
pytest -q
```
