from __future__ import annotations

from dataclasses import dataclass

from src.gui.lite_grid_window import GridSettingsState

STRATEGY_ID = "GRID_CLASSIC"
STRATEGY_LABEL = "GRID_CLASSIC"


@dataclass(frozen=True)
class GridClassicDefinition:
    strategy_id: str
    label: str
    settings_model: type[GridSettingsState]


def get_definition() -> GridClassicDefinition:
    return GridClassicDefinition(
        strategy_id=STRATEGY_ID,
        label=STRATEGY_LABEL,
        settings_model=GridSettingsState,
    )
