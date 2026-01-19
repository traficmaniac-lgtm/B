from __future__ import annotations

from enum import Enum


class PairMode(Enum):
    TRADING = "trading"
    ADVANCED = "advanced"


PAIR_MODE_TRADING = PairMode.TRADING
PAIR_MODE_ADVANCED = PairMode.ADVANCED
