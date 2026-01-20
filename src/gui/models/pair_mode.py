from __future__ import annotations

from enum import Enum


class PairMode(Enum):
    TRADING = "trading"
    TRADE_READY = "trade_ready"
    AI_OPERATOR_GRID = "ai_operator_grid"


PAIR_MODE_TRADING = PairMode.TRADING
PAIR_MODE_TRADE_READY = PairMode.TRADE_READY
PAIR_MODE_AI_OPERATOR_GRID = PairMode.AI_OPERATOR_GRID
