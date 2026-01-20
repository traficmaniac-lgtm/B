from __future__ import annotations

import time
from typing import Any


class DataCache:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[Any, float]] = {}

    def get(self, symbol: str, data_type: str) -> tuple[Any, float] | None:
        key = (symbol.strip().upper(), data_type.strip().lower())
        return self._data.get(key)

    def set(self, symbol: str, data_type: str, data: Any) -> None:
        key = (symbol.strip().upper(), data_type.strip().lower())
        self._data[key] = (data, time.time())

    def is_fresh(self, ts: float, ttl: float) -> bool:
        return (time.time() - ts) <= ttl
