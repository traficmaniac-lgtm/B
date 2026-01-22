from __future__ import annotations

from enum import Enum


class PairMode(Enum):
    LITE = "lite"
    LITE_ALL_STRATEGY = "lite_all_strategy"
    TRADING = "trading"
    TRADE_READY = "trade_ready"
    AI_OPERATOR_GRID = "ai_operator_grid"
    AI_FULL_STRATEG_V2 = "ai_full_strateg_v2"


PAIR_MODE_LITE = PairMode.LITE
PAIR_MODE_LITE_ALL_STRATEGY = PairMode.LITE_ALL_STRATEGY
PAIR_MODE_TRADING = PairMode.TRADING
PAIR_MODE_TRADE_READY = PairMode.TRADE_READY
PAIR_MODE_AI_OPERATOR_GRID = PairMode.AI_OPERATOR_GRID
PAIR_MODE_AI_FULL_STRATEG_V2 = PairMode.AI_FULL_STRATEG_V2
