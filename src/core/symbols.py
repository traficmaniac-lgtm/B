from __future__ import annotations

import re

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")


def sanitize_symbol(symbol: object) -> str | None:
    if not isinstance(symbol, str):
        return None
    cleaned = symbol.strip().upper()
    if not cleaned:
        return None
    if not _SYMBOL_RE.fullmatch(cleaned):
        return None
    return cleaned
