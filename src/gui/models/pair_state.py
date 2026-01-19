from __future__ import annotations

from enum import Enum


class PairRunState(str, Enum):
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    DATA_READY = "DATA_READY"
    READY = "DATA_READY"
    ANALYZING = "ANALYZING"
    NEED_MORE_DATA = "NEED_MORE_DATA"
    PLAN_READY = "PLAN_READY"
    APPLIED = "APPLIED"
    WAIT_CONFIRM = "WAIT_CONFIRM"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
