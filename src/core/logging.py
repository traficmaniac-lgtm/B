from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Hashable


@dataclass
class _DedupEntry:
    last_ts: float = 0.0
    suppressed: int = 0


class LogDeduper:
    def __init__(self, cooldown_s: float = 3.0) -> None:
        self._cooldown_s = max(0.0, cooldown_s)
        self._lock = threading.Lock()
        self._entries: dict[Hashable, _DedupEntry] = {}

    def log(
        self,
        logger: logging.Logger | logging.LoggerAdapter,
        level: int,
        key: Hashable,
        message: str,
        *args: Any,
    ) -> bool:
        now = time.monotonic()
        suppressed = 0
        should_log = False
        with self._lock:
            entry = self._entries.setdefault(key, _DedupEntry())
            if now - entry.last_ts >= self._cooldown_s:
                should_log = True
                suppressed = entry.suppressed
                entry.suppressed = 0
                entry.last_ts = now
            else:
                entry.suppressed += 1
        if not should_log:
            return False
        suffix = f" (suppressed {suppressed})" if suppressed else ""
        logger.log(level, f"{message}{suffix}", *args)
        return True


def configure_logging(level: str = "INFO") -> None:
    numeric_level = logging.getLevelName(level.upper())
    if isinstance(numeric_level, str):
        numeric_level = logging.INFO
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def get_logger(name: str, **extra: Any) -> logging.LoggerAdapter:
    logger = logging.getLogger(name)
    return logging.LoggerAdapter(logger, extra)
