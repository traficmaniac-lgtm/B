## Technical Report

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
