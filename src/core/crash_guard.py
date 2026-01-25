from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
import weakref
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QtMsgType, qInstallMessageHandler


_CRASH_LOG_PATHS: list[Path] = []
_PANIC_HOLD_REF: weakref.ReferenceType[Callable[[str], None]] | None = None


def clear_crash_log_paths() -> None:
    _CRASH_LOG_PATHS.clear()


def register_crash_log_path(path: Path | str | None) -> None:
    if not path:
        return
    try:
        crash_path = Path(path)
    except Exception:
        return
    if crash_path not in _CRASH_LOG_PATHS:
        _CRASH_LOG_PATHS.append(crash_path)


def register_panic_hold_callback(callback: Callable[[str], None] | None) -> None:
    global _PANIC_HOLD_REF
    if callback is None:
        _PANIC_HOLD_REF = None
        return
    try:
        _PANIC_HOLD_REF = weakref.WeakMethod(callback)  # type: ignore[arg-type]
    except TypeError:
        _PANIC_HOLD_REF = weakref.ref(callback)


def _emit_panic_hold(reason: str) -> None:
    if _PANIC_HOLD_REF is None:
        return
    callback = _PANIC_HOLD_REF()
    if callback is None:
        return
    try:
        callback(reason)
    except Exception:
        logging.getLogger("crash_guard").exception("panic_hold callback failed")


def _write_crash_log(
    prefix: str,
    exc_type: type[BaseException],
    exc: BaseException,
    tb: object,
) -> None:
    if not _CRASH_LOG_PATHS:
        return
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = traceback.format_exception(exc_type, exc, tb)
    for path in _CRASH_LOG_PATHS:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n[CRASH] uncaught {prefix} ts={timestamp}\n")
                handle.writelines(lines)
        except Exception:
            continue


def global_excepthook(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
    logger = logging.getLogger("crash_guard")
    logger.exception("[CRASH] uncaught", exc_info=(exc_type, exc, tb))
    _write_crash_log("SYS", exc_type, exc, tb)
    _emit_panic_hold(f"sys:{exc_type.__name__}")


def global_thread_excepthook(args: threading.ExceptHookArgs) -> None:
    logger = logging.getLogger("crash_guard")
    logger.exception("[CRASH] uncaught", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    _write_crash_log(
        f"THREAD:{args.thread.name}",
        args.exc_type,
        args.exc_value,
        args.exc_traceback,
    )
    _emit_panic_hold(f"thread:{args.thread.name}")


def safe_qt(fn: Callable[[], Any], *, label: str, logger: logging.Logger | None = None) -> Any:
    active_logger = logger or logging.getLogger("crash_guard")
    try:
        return fn()
    except Exception:
        active_logger.exception("[CRASH] uncaught", exc_info=True)
        exc_type, exc, tb = sys.exc_info()
        if exc_type and exc and tb:
            _write_crash_log(f"QT:{label}", exc_type, exc, tb)
            _emit_panic_hold(f"qt:{label}")
        return None


def _qt_message_handler(mode: QtMsgType, context: Any, message: str) -> None:
    logger = logging.getLogger("qt")
    details = ""
    try:
        if context is not None and context.file:
            details = f"{context.file}:{context.line}"
    except Exception:
        details = ""
    text = f"{message}"
    if details:
        text = f"{text} ({details})"
    if mode == QtMsgType.QtCriticalMsg:
        logger.error("%s", text)
    elif mode == QtMsgType.QtWarningMsg:
        logger.warning("%s", text)
    elif mode == QtMsgType.QtFatalMsg:
        logger.critical("%s", text)
    else:
        logger.info("%s", text)


def install_crash_guard(logger: logging.Logger, *, crash_log_path: Path | str | None = None) -> None:
    if crash_log_path:
        register_crash_log_path(crash_log_path)
    sys.excepthook = global_excepthook
    logger.info("[CRASH_GUARD] sys.excepthook installed")
    if hasattr(threading, "excepthook"):
        threading.excepthook = global_thread_excepthook
        logger.info("[CRASH_GUARD] threading.excepthook installed")
    qInstallMessageHandler(_qt_message_handler)
    logger.info("[CRASH_GUARD] qt message handler installed")
