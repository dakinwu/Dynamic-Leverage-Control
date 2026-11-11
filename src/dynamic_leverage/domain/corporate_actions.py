"""Corporate actions as official facts: splits change units, dividends are owed then paid.

Pure value types and arithmetic shared by the strategy ledger. They never infer
an action from prices or quantities; callers supply officially sourced facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class SplitAction:
    """``ratio`` is new units per old unit (0050 in 2025: 4)."""

    symbol: str
    ratio: int
    last_trade_date: date
    halt_start: date
    halt_end: date
    resume_date: date

    def __post_init__(self) -> None:
        if isinstance(self.ratio, bool) or not isinstance(self.ratio, int) or self.ratio < 2:
            raise ValueError("split ratio must be a whole number of new units per old unit")
        if not self.last_trade_date < self.halt_start <= self.halt_end < self.resume_date:
            raise ValueError("split dates must be last trade < halt start <= halt end < resume")

    @property
    def action_id(self) -> str:
        return f"{self.symbol}:SPLIT:{self.resume_date.isoformat()}"

    def facts(self) -> dict[str, str]:
        return {
            "ratio": str(self.ratio),
            "last_trade_date": self.last_trade_date.isoformat(),
            "halt_start": self.halt_start.isoformat(),
            "halt_end": self.halt_end.isoformat(),
            "resume_date": self.resume_date.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class DividendAction:
    symbol: str
    ex_date: date
    payment_date: date
    amount_per_unit: Decimal
    # Evidence reference (e.g. the raw snapshot id); not part of the facts.
    source: str = ""

    def __post_init__(self) -> None:
        if not self.amount_per_unit.is_finite() or self.amount_per_unit <= 0:
            raise ValueError("dividend per unit must be positive")
        if self.payment_date < self.ex_date:
            raise ValueError("dividend payment cannot precede the ex-date")

    @property
    def action_id(self) -> str:
        return f"{self.symbol}:DIVIDEND:{self.ex_date.isoformat()}"

    def facts(self) -> dict[str, str]:
        return {
            "ex_date": self.ex_date.isoformat(),
            "payment_date": self.payment_date.isoformat(),
            "amount_per_unit": str(self.amount_per_unit),
        }


@dataclass(frozen=True, slots=True)
class FillRecord:
    symbol: str
    side: str
    quantity: Decimal
    occurred_on: date


def split_units(units: Decimal, ratio: int | Decimal) -> Decimal:
    """Units after a split; a fractional entitlement needs an explicit cash rule."""
    result = Decimal(units) * Decimal(ratio)
    if result != result.to_integral_value():
        raise ValueError("fractional split entitlement needs an explicit cash-in-lieu rule")
    return result


def units_held_at_open(
    day: date,
    *,
    symbol: str,
    current_units: Decimal,
    fills: list[FillRecord],
    splits: list[SplitAction],
) -> Decimal:
    """Units owned at the open of ``day``, walking back from the current position.

    Fills on or after ``day`` are undone, and a split whose last cum-split
    trading day is on or after ``day`` is reversed, so the result is in the
    units that existed on that day (the dividend record holding at ex-date).
    """
    # Step back through time so each fill is undone in the units it was booked
    # in: fills from a split's halt start onward are post-split quantities.
    timeline: list[tuple[date, int, FillRecord | SplitAction]] = [
        (fill.occurred_on, 1, fill)
        for fill in fills
        if fill.symbol == symbol and fill.occurred_on >= day
    ]
    timeline += [
        (split.halt_start, 0, split)
        for split in splits
        if split.symbol == symbol and split.last_trade_date >= day
    ]
    units = Decimal(current_units)
    for _when, _order, item in sorted(timeline, key=lambda entry: entry[:2], reverse=True):
        if isinstance(item, SplitAction):
            units /= item.ratio
        else:
            units += -item.quantity if item.side.upper() == "BUY" else item.quantity
    if units < 0 or units != units.to_integral_value():
        raise ValueError("fill history does not reconstruct a whole position")
    return units
