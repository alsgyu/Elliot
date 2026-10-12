from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, getcontext

getcontext().prec = 34


def D(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


def quantize_up(value: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return units * step


def quantize_price(value: Decimal, tick_size: Decimal) -> Decimal:
    return quantize_down(value, tick_size)


def quantize_quantity(value: Decimal, step_size: Decimal) -> Decimal:
    return quantize_down(value, step_size)


def bps_to_rate(bps: Decimal) -> Decimal:
    return bps / Decimal("10000")
