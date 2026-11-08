from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from math import sqrt
from statistics import stdev

from dynamic_leverage.domain.models import decimal


def simple_returns(closes: Sequence[Decimal]) -> list[Decimal]:
    if len(closes) < 2:
        return []
    if any(close <= 0 for close in closes):
        raise ValueError("closes must be positive")
    return [
        (current / previous) - Decimal("1")
        for previous, current in zip(closes, closes[1:], strict=False)
    ]


def realized_volatility_20(closes: Sequence[Decimal]) -> Decimal:
    if len(closes) != 21:
        raise ValueError("RV20 requires exactly 21 closes for 20 returns")
    returns = simple_returns(closes)
    value = stdev(float(item) for item in returns) * sqrt(252)
    return decimal(value)


def percentile_midrank(current: Decimal, history: Sequence[Decimal]) -> Decimal:
    if len(history) != 120:
        raise ValueError("P/E percentile requires exactly 120 prior month-end values")
    below = sum(value < current for value in history)
    equal = sum(value == current for value in history)
    return (Decimal(below) + Decimal(equal) / Decimal("2")) / Decimal(len(history))
