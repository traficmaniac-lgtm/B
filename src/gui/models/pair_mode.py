from __future__ import annotations

from enum import Enum


class PairMode(Enum):
    TRADING = "trading"
    TRADE_READY = "trade_ready"


PAIR_MODE_TRADING = PairMode.TRADING
PAIR_MODE_TRADE_READY = PairMode.TRADE_READY
