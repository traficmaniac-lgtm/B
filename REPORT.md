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
