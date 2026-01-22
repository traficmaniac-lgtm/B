from __future__ import annotations

from dataclasses import dataclass

from src.core.strategies.defs.grid_classic import get_definition as grid_classic_definition


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    label: str


def get_strategy_definitions() -> tuple[StrategyDefinition, ...]:
    grid_classic = grid_classic_definition()
    return (
        StrategyDefinition(strategy_id=grid_classic.strategy_id, label=grid_classic.label),
    )
