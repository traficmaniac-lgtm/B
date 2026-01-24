from __future__ import annotations

from decimal import Decimal

from src.core.nc_micro_exec_dedup import TradeIdDeduper, should_block_new_orders
from src.core.nc_micro_refresh import StaleRefreshLogLimiter
from src.gui.lite_grid_math import FillAccumulator


def test_exec_dedup_tradeid() -> None:
    deduper = TradeIdDeduper(max_size=10, ttl_sec=3600)
    now = 0.0
    assert deduper.seen("42", now) is False
    assert deduper.seen("42", now + 1) is True


def test_exec_partial_fill_sequence() -> None:
    accumulator = FillAccumulator()
    order_id = "BUY-1"
    deltas: list[Decimal] = []
    totals = [Decimal("10"), Decimal("12"), Decimal("15")]
    for total in totals:
        cum, delta = accumulator.record(order_id, total, is_total=True)
        accumulator.mark_handled(order_id, cum)
        deltas.append(delta)
    assert deltas == [Decimal("10"), Decimal("2"), Decimal("3")]
    assert cum == Decimal("15")


def test_stop_blocks_new_orders() -> None:
    assert should_block_new_orders("STOPPING") is True
    assert should_block_new_orders("STOPPED") is True
    assert should_block_new_orders("RUNNING") is False


def test_stale_refresh_rate_limit() -> None:
    limiter = StaleRefreshLogLimiter(min_interval_sec=10.0)
    now = 0.0
    logged = 0
    for _ in range(20):
        if limiter.should_log("poll", state=(True, "age"), now=now):
            logged += 1
        now += 1.0
    assert logged <= 3
