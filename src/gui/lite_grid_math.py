from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR


@dataclass
class FillAccumulator:
    total_by_order_id: dict[str, Decimal] = field(default_factory=dict)
    handled_by_order_id: dict[str, Decimal] = field(default_factory=dict)

    def record(self, order_id: str, filled_qty: Decimal, *, is_total: bool = False) -> tuple[Decimal, Decimal]:
        if is_total:
            total = filled_qty
        else:
            total = self.total_by_order_id.get(order_id, Decimal("0")) + filled_qty
        self.total_by_order_id[order_id] = total
        handled = self.handled_by_order_id.get(order_id, Decimal("0"))
        delta = total - handled
        return total, delta

    def mark_handled(self, order_id: str, total: Decimal) -> None:
        self.handled_by_order_id[order_id] = total


def floor_to_step(value: Decimal, step: Decimal | None) -> Decimal:
    if step is None or step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _decimal_places(value: Decimal) -> int:
    if value == 0:
        return 0
    exponent = value.normalize().as_tuple().exponent
    return max(-exponent, 0)


def _format_decimal(value: Decimal, step: Decimal | None) -> str:
    if step is None or step <= 0:
        return format(value, "f")
    decimals = _decimal_places(step)
    quant = Decimal("1").scaleb(-decimals)
    return format(value.quantize(quant), "f")


def build_action_key(action: str, order_id: str, price: Decimal, qty: Decimal, step: Decimal | None) -> str:
    qty_key = _format_decimal(qty, step)
    return f"{action}:{order_id}:{price}:{qty_key}"


def compute_order_qty(
    side: str,
    price: Decimal,
    desired_notional: Decimal,
    balances: dict[str, Decimal],
    rules: dict[str, float | None],
    fee_rate: Decimal,
    cfg_buffers: dict[str, Decimal] | None = None,
) -> tuple[Decimal, Decimal, str]:
    if price <= 0:
        return Decimal("0"), Decimal("0"), "invalid_price"
    step = rules.get("step")
    min_notional = rules.get("min_notional")
    min_qty = rules.get("min_qty")
    max_qty = rules.get("max_qty")
    step_dec = Decimal(str(step)) if step is not None else None
    min_notional_dec = Decimal(str(min_notional)) if min_notional is not None else None
    min_qty_dec = Decimal(str(min_qty)) if min_qty is not None else None
    max_qty_dec = Decimal(str(max_qty)) if max_qty is not None else None
    quote_free = balances.get("quote_free", Decimal("0"))
    base_free = balances.get("base_free", Decimal("0"))
    quote_buffer_default = max(Decimal("1"), quote_free * Decimal("0.005"))
    base_step = step_dec or Decimal("0")
    base_buffer_default = max(base_step, base_free * Decimal("0.005"))
    if base_free > 0 and base_step > 0:
        base_buffer_default = min(base_buffer_default, max(base_free - base_step, Decimal("0")))
    buffers = cfg_buffers or {}
    quote_buffer = buffers.get("quote_buffer", quote_buffer_default)
    base_buffer = buffers.get("base_buffer", base_buffer_default)
    qty_desired = desired_notional / price
    qty_desired = floor_to_step(qty_desired, step_dec)
    if side == "BUY":
        max_spend = quote_free - quote_buffer
        if max_spend <= 0:
            return (
                Decimal("0"),
                Decimal("0"),
                f"insufficient_quote usable={max_spend} required={desired_notional}",
            )
        qty_max = floor_to_step(max_spend / price, step_dec)
        qty = min(qty_desired, qty_max)
    else:
        usable_base = base_free - base_buffer
        if usable_base <= 0:
            return (
                Decimal("0"),
                Decimal("0"),
                f"insufficient_base usable={usable_base} required={qty_desired}",
            )
        qty_max = floor_to_step(usable_base, step_dec)
        qty = min(qty_desired, qty_max)
    qty = floor_to_step(qty, step_dec)
    if max_qty_dec is not None and qty > max_qty_dec:
        qty = floor_to_step(max_qty_dec, step_dec)
    if min_qty_dec is not None and qty < min_qty_dec:
        return Decimal("0"), Decimal("0"), "min_qty"
    notional = price * qty
    if min_notional_dec is not None and notional < min_notional_dec:
        return Decimal("0"), Decimal("0"), "min_notional"
    if qty <= 0:
        return Decimal("0"), Decimal("0"), "qty_zero"
    _ = fee_rate
    return qty, notional, "ok"
