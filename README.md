# BBOT Desktop Terminal (Stage 3.0.1)

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
3. Click **Load Pairs** to fetch real Binance Spot symbols (cache is used if fresh).
4. Use the search input + quote filter to narrow the list (Shown/Total counter updates).
5. Click **Force refresh from exchange** if the cache is partial or stale.
6. Prices are updated only for the selected row; live streaming still happens in Bot tabs.

Overview prices are now static by design.
Overview acts as a catalog of symbols, not a live ticker.
Live price streaming is enabled only inside open Bot tabs.
Open a Bot tab to receive ticks and see the current price there.
Closing the Bot tab stops live price updates for that symbol.

### Settings

Open Settings from the toolbar or File → Settings menu. Changes are saved to `config.user.yaml`.

### OpenAI (AI Analyze)

1. Open **Settings** and paste your OpenAI API key.
2. Select the **OpenAI model** (default: gpt-4o-mini).
3. Click **Save**, then return to Overview.
4. Press **Run Self-check** to verify the OpenAI connection.
5. Open a Bot tab, click **Prepare Data**, then **AI Analyze**.
6. The response appears in **AI Chat** and is logged in **Local logs**.

### Pair Workspace

Double click any row in the Overview tab to open a Pair Workspace bot tab for that symbol.

### Trading Workspace

Use the **Trading** button in the Pair Workspace to open the Trading Workspace window with runtime status,
orders, risk summary, and the AI Observer panel. AI suggestions never execute automatically.

## Run tests

```powershell
pytest -q
```
