from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BotState(str, Enum):
    IDLE = "IDLE"
    PREPARING_DATA = "PREPARING_DATA"
    DATA_READY = "DATA_READY"
    ANALYZING = "ANALYZING"
    PLAN_READY = "PLAN_READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


@dataclass
class PairState:
    state: BotState = BotState.IDLE
    last_reason: str = ""

    def set_state(self, new_state: BotState, reason: str = "") -> BotState:
        previous = self.state
        self.state = new_state
        self.last_reason = reason
        return previous
