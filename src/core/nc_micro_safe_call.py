from __future__ import annotations

import traceback
from typing import Any, Callable


def safe_call(
    fn: Callable[[], Any],
    *,
    label: str,
    on_error: Callable[[], Any] | None = None,
    logger: Any | None = None,
    crash_writer: Callable[[str], Any] | None = None,
) -> None:
    try:
        fn()
    except Exception:
        if logger is not None:
            try:
                logger.exception(f"[SAFE] {label} crashed")
            except Exception:
                pass
        if crash_writer is not None:
            try:
                crash_writer(f"[SAFE] {label} crashed")
                crash_writer(traceback.format_exc().rstrip())
            except Exception:
                pass
        if on_error is not None:
            try:
                on_error()
            except Exception:
                if logger is not None:
                    try:
                        logger.exception(f"[SAFE] {label} on_error crashed")
                    except Exception:
                        pass
        return
