from __future__ import annotations

from math import isfinite


def has_valid_bidask(bid: float | None, ask: float | None) -> bool:
    if bid is None or ask is None:
        return False
    if not isfinite(bid) or not isfinite(ask):
        return False
    if bid <= 0 or ask <= 0:
        return False
    if ask <= bid:
        return False
    return True
