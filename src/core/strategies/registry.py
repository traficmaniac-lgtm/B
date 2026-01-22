from __future__ import annotations

from dataclasses import dataclass

from src.core.strategies.manual_strategies import BUILTIN_STRATEGIES, ManualStrategy


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    label: str


_STRATEGY_REGISTRY: dict[str, ManualStrategy] = {
    strategy.id: strategy for strategy in BUILTIN_STRATEGIES
}


def register_strategy(strategy: ManualStrategy) -> None:
    _STRATEGY_REGISTRY[strategy.id] = strategy


def get_strategy(strategy_id: str) -> ManualStrategy | None:
    return _STRATEGY_REGISTRY.get(strategy_id)


def get_strategy_definitions() -> tuple[StrategyDefinition, ...]:
    return tuple(
        StrategyDefinition(strategy_id=strategy.id, label=strategy.label)
        for strategy in _STRATEGY_REGISTRY.values()
    )
