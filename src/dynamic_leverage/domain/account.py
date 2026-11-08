from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    instrument: str
    quantity: Decimal
    average_price: Decimal
    last_price: Decimal
    direction: str
    condition: str

    @property
    def market_value(self) -> Decimal:
        sign = Decimal("-1") if self.direction.lower() == "sell" else Decimal("1")
        return sign * self.quantity * self.last_price


@dataclass(frozen=True, slots=True)
class BrokerSettlement:
    settlement_date: date
    amount: Decimal
    t: int


@dataclass(frozen=True, slots=True)
class BrokerAccountSnapshot:
    observed_at: datetime
    cash: Decimal
    positions: tuple[BrokerPosition, ...]
    settlements: tuple[BrokerSettlement, ...]

    @property
    def nav(self) -> Decimal:
        return self.cash + sum((position.market_value for position in self.positions), Decimal("0"))

    @property
    def pending_settlement(self) -> Decimal:
        return sum(
            (settlement.amount for settlement in self.settlements if settlement.t > 0),
            Decimal("0"),
        )


class AccountSnapshotReader(Protocol):
    def read_account(self, binding: str) -> BrokerAccountSnapshot: ...
