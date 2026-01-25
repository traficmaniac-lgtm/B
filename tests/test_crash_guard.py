from __future__ import annotations

import sys
import threading

from src.core import crash_guard


def test_global_excepthook_writes_crash(tmp_path) -> None:
    crash_path = tmp_path / "APP_test.log"
    crash_guard.clear_crash_log_paths()
    crash_guard.register_crash_log_path(crash_path)

    try:
        raise ValueError("boom")
    except ValueError as exc:
        exc_type, exc_value, tb = sys.exc_info()
        assert exc_type is ValueError
        crash_guard.global_excepthook(exc_type, exc_value, tb)

    contents = crash_path.read_text(encoding="utf-8")
    assert "[CRASH] uncaught" in contents


def test_global_thread_excepthook_no_raise(tmp_path) -> None:
    crash_path = tmp_path / "APP_thread.log"
    crash_guard.clear_crash_log_paths()
    crash_guard.register_crash_log_path(crash_path)

    try:
        raise RuntimeError("thread boom")
    except RuntimeError as exc:
        exc_type, exc_value, tb = sys.exc_info()
        args = threading.ExceptHookArgs(
            (exc_type, exc_value, tb, threading.current_thread())
        )
        crash_guard.global_thread_excepthook(args)
