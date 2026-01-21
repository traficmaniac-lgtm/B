from decimal import Decimal

from src.gui.lite_grid_math import FillAccumulator


def test_fill_accumulator_tracks_partial_deltas() -> None:
    accumulator = FillAccumulator()
    calls: list[Decimal] = []

    total, delta = accumulator.record("order-1", Decimal("0.00006"), is_total=True)
    if delta > 0:
        calls.append(delta)
        accumulator.mark_handled("order-1", total)

    total, delta = accumulator.record("order-1", Decimal("0.00011"), is_total=True)
    if delta > 0:
        calls.append(delta)
        accumulator.mark_handled("order-1", total)

    total, delta = accumulator.record("order-1", Decimal("0.00011"), is_total=True)
    if delta > 0:
        calls.append(delta)
        accumulator.mark_handled("order-1", total)

    assert calls == [Decimal("0.00006"), Decimal("0.00005")]
