from __future__ import annotations

import re

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")


def _normalize_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().upper()
    if not cleaned:
        return None
    return cleaned


def sanitize_symbol(symbol: object) -> str | None:
    cleaned = _normalize_token(symbol)
    if cleaned is None:
        return None
    if not _SYMBOL_RE.fullmatch(cleaned):
        return None
    return cleaned


def validate_trade_symbol(symbol: str, exchange_symbols_set: set[str]) -> bool:
    cleaned = _normalize_token(symbol)
    if cleaned is None:
        return False
    return cleaned in exchange_symbols_set


def validate_asset(asset: str) -> bool:
    return _normalize_token(asset) is not None
