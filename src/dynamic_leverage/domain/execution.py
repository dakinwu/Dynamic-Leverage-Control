from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from dynamic_leverage.domain.models import ONE, TWO


@dataclass(frozen=True, slots=True)
class Allocation:
    instrument: str
    weight: Decimal
    assumed_exposure: Decimal


class ExecutionRecipe(Protocol):
    version: str

    def allocate(self, target_exposure: Decimal) -> tuple[Allocation, ...]: ...


def _accepted_target(target_exposure: Decimal) -> Decimal:
    if not Decimal("0") <= target_exposure <= TWO:
        raise ValueError("target exposure must be between 0.0x and 2.0x")
    return target_exposure


class TwoXCashRecipe:
    version = "2x-cash-v1"

    def __init__(self, leveraged_instrument: str = "00631L") -> None:
        self.leveraged_instrument = leveraged_instrument

    def allocate(self, target_exposure: Decimal) -> tuple[Allocation, ...]:
        target = _accepted_target(target_exposure)
        leveraged_weight = target / TWO
        return (
            Allocation(self.leveraged_instrument, leveraged_weight, TWO),
            Allocation("TWD", ONE - leveraged_weight, Decimal("0")),
        )


class OneXTwoXRecipe:
    version = "1x-2x-v1"

    def __init__(
        self,
        base_instrument: str = "0050",
        leveraged_instrument: str = "00631L",
    ) -> None:
        self.base_instrument = base_instrument
        self.leveraged_instrument = leveraged_instrument

    def allocate(self, target_exposure: Decimal) -> tuple[Allocation, ...]:
        target = _accepted_target(target_exposure)
        base_weight = target if target <= ONE else TWO - target
        leveraged_weight = Decimal("0") if target <= ONE else target - ONE
        cash_weight = ONE - base_weight - leveraged_weight
        return (
            Allocation(self.base_instrument, base_weight, ONE),
            Allocation(self.leveraged_instrument, leveraged_weight, TWO),
            Allocation("TWD", cash_weight, Decimal("0")),
        )


def allocation_exposure(allocations: tuple[Allocation, ...]) -> Decimal:
    return sum((item.weight * item.assumed_exposure for item in allocations), Decimal("0"))
