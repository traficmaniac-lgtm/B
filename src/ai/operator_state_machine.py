from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AiOperatorState(str, Enum):
    IDLE = "IDLE"
    DATA_READY = "DATA_READY"
    ANALYZING = "ANALYZING"
    PLAN_READY = "PLAN_READY"
    PLAN_APPLIED = "PLAN_APPLIED"
    RUNNING = "RUNNING"
    WATCHING = "WATCHING"
    INVALID = "INVALID"


@dataclass
class AiOperatorStateMachine:
    state: AiOperatorState = AiOperatorState.IDLE
    ai_confidence_locked: bool = False

    def can_refresh(self) -> bool:
        return self.state != AiOperatorState.ANALYZING

    def can_analyze(self) -> bool:
        return self.state in {AiOperatorState.DATA_READY, AiOperatorState.INVALID}

    def can_apply_plan(self) -> bool:
        return self.state == AiOperatorState.PLAN_READY

    def can_start(self) -> bool:
        return self.state == AiOperatorState.PLAN_APPLIED

    def can_pause(self) -> bool:
        return self.state == AiOperatorState.RUNNING

    def can_stop(self) -> bool:
        return self.state in {
            AiOperatorState.RUNNING,
            AiOperatorState.WATCHING,
            AiOperatorState.PLAN_APPLIED,
        }

    def set_data_ready(self) -> bool:
        self.state = AiOperatorState.DATA_READY
        return True

    def start_analyzing(self) -> bool:
        if not self.can_analyze():
            return False
        self.state = AiOperatorState.ANALYZING
        return True

    def set_plan_ready(self) -> bool:
        if self.state != AiOperatorState.ANALYZING:
            return False
        self.state = AiOperatorState.PLAN_READY
        return True

    def set_invalid(self) -> bool:
        self.state = AiOperatorState.INVALID
        self.ai_confidence_locked = False
        return True

    def apply_plan(self) -> bool:
        if self.state != AiOperatorState.PLAN_READY:
            return False
        self.state = AiOperatorState.PLAN_APPLIED
        self.ai_confidence_locked = True
        return True

    def start(self) -> bool:
        if self.state != AiOperatorState.PLAN_APPLIED:
            return False
        self.state = AiOperatorState.RUNNING
        return True

    def pause(self) -> bool:
        if self.state != AiOperatorState.RUNNING:
            return False
        self.state = AiOperatorState.WATCHING
        return True

    def stop(self) -> bool:
        if not self.can_stop():
            return False
        self.state = AiOperatorState.IDLE
        self.ai_confidence_locked = False
        return True

    def invalidate_manual(self) -> bool:
        self.state = AiOperatorState.INVALID
        self.ai_confidence_locked = False
        return True
