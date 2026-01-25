from __future__ import annotations

from collections.abc import Iterable
from typing import Final

SYMBOL_ALIAS_MAP: Final[dict[str, str]] = {
    "USDTUSDC": "USDCUSDT",
    "USDCUSDT": "USDCUSDT",
    "EURIEUR": "EUREURI",
    "EUREURI": "EUREURI",
    "EUR_EURI": "EUREURI",
    "USDC_USDT": "USDCUSDT",
    "TUSD_USDT": "TUSDUSDT",
    "EURI_USDT": "EURIUSDT",
}


def resolve_symbol(user_symbol: str, exchange_symbols: set[str] | None) -> tuple[str | None, str]:
    cleaned = str(user_symbol).upper()
    effective = SYMBOL_ALIAS_MAP.get(cleaned, cleaned)
    if exchange_symbols and effective not in exchange_symbols:
        return None, f"invalid_symbol:{effective}"
    return effective, "ok"


def format_alias_summary(symbols: Iterable[str], aliases: dict[str, str]) -> str:
    mapped = []
    for symbol in symbols:
        effective = aliases.get(symbol)
        if effective and effective != symbol:
            mapped.append(f"{symbol}→{effective}")
    return ", ".join(mapped)
