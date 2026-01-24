from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class StopFinalizeRecord:
    state: str
    inflight: bool
    start_enabled: bool
    done_with_errors: bool
    remaining: int


def finalize_stop_state(
    *,
    remaining: int,
    done_with_errors: bool,
    set_inflight: Callable[[bool], None],
    set_state: Callable[[str], None],
    set_start_enabled: Callable[[bool], None],
    log_fn: Callable[[str, str], None],
    state: str = "IDLE",
) -> StopFinalizeRecord:
    set_inflight(False)
    set_state(state)
    set_start_enabled(True)
    log_fn(f"[STOP] finalize state={state} inflight=false remaining={remaining}", "INFO")
    if done_with_errors:
        log_fn("[STOP] STOP_DONE_WITH_ERRORS", "WARN")
    return StopFinalizeRecord(
        state=state,
        inflight=False,
        start_enabled=True,
        done_with_errors=done_with_errors,
        remaining=remaining,
    )
