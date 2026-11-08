from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class PriceBar:
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("bar prices must be positive")
        if self.low > self.high:
            raise ValueError("bar low cannot exceed high")


@dataclass(frozen=True, slots=True)
class PaperOrder:
    order_id: str
    submitted_at: datetime
    instrument: str
    side: OrderSide
    quantity: int
    limit_price: Decimal
    status: OrderStatus = OrderStatus.OPEN
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if self.limit_price <= 0:
            raise ValueError("limit price must be positive")


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: str
    order_id: str
    filled_at: datetime
    instrument: str
    side: OrderSide
    quantity: int
    price: Decimal
    commission: Decimal
    tax: Decimal
    settlement_date: date

    @property
    def gross(self) -> Decimal:
        return self.price * self.quantity

    @property
    def settlement_amount(self) -> Decimal:
        if self.side == OrderSide.BUY:
            return -(self.gross + self.commission)
        return self.gross - self.commission - self.tax


@dataclass(frozen=True, slots=True)
class PendingSettlement:
    settlement_date: date
    amount: Decimal
    fill_id: str


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    commission_rate: Decimal = Decimal("0.001425")
    minimum_commission: Decimal = Decimal("20")
    etf_sell_tax_rate: Decimal = Decimal("0.001")

    @staticmethod
    def _whole_twd(value: Decimal) -> Decimal:
        return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    def commission(self, gross: Decimal) -> Decimal:
        return self._whole_twd(max(gross * self.commission_rate, self.minimum_commission))

    def sell_tax(self, gross: Decimal) -> Decimal:
        return self._whole_twd(gross * self.etf_sell_tax_rate)


@dataclass(slots=True)
class PaperAccount:
    settled_cash: Decimal
    positions: dict[str, int] = field(default_factory=dict)
    pending_settlements: list[PendingSettlement] = field(default_factory=list)
    fills: list[PaperFill] = field(default_factory=list)

    @property
    def available_cash(self) -> Decimal:
        reserved_buys = sum(
            (settlement.amount for settlement in self.pending_settlements if settlement.amount < 0),
            Decimal("0"),
        )
        return self.settled_cash + reserved_buys

    def settle_through(self, on_date: date) -> None:
        due = [item for item in self.pending_settlements if item.settlement_date <= on_date]
        self.settled_cash += sum((item.amount for item in due), Decimal("0"))
        self.pending_settlements = [
            item for item in self.pending_settlements if item.settlement_date > on_date
        ]

    def can_apply(self, fill: PaperFill) -> tuple[bool, str | None]:
        if fill.side == OrderSide.BUY and self.available_cash < -fill.settlement_amount:
            return False, "INSUFFICIENT_SETTLEMENT_CASH"
        if fill.side == OrderSide.SELL and self.positions.get(fill.instrument, 0) < fill.quantity:
            return False, "INSUFFICIENT_POSITION"
        return True, None

    def apply(self, fill: PaperFill) -> None:
        allowed, reason = self.can_apply(fill)
        if not allowed:
            raise ValueError(reason)
        delta = fill.quantity if fill.side == OrderSide.BUY else -fill.quantity
        self.positions[fill.instrument] = self.positions.get(fill.instrument, 0) + delta
        self.pending_settlements.append(
            PendingSettlement(fill.settlement_date, fill.settlement_amount, fill.fill_id)
        )
        self.fills.append(fill)


class PaperBroker:
    def __init__(self, fee_schedule: FeeSchedule | None = None) -> None:
        self.fees = fee_schedule or FeeSchedule()

    @staticmethod
    def _fill_price(order: PaperOrder, bar: PriceBar) -> Decimal | None:
        if order.side == OrderSide.BUY:
            if bar.low > order.limit_price:
                return None
            return min(order.limit_price, bar.open)
        if bar.high < order.limit_price:
            return None
        return max(order.limit_price, bar.open)

    def process(
        self,
        order: PaperOrder,
        bar: PriceBar,
        *,
        filled_at: datetime,
        settlement_date: date,
        fill_id: str,
    ) -> tuple[PaperOrder, PaperFill | None]:
        if order.status != OrderStatus.OPEN:
            return order, None
        price = self._fill_price(order, bar)
        if price is None:
            return order, None
        gross = price * order.quantity
        commission = self.fees.commission(gross)
        tax = self.fees.sell_tax(gross) if order.side == OrderSide.SELL else Decimal("0")
        fill = PaperFill(
            fill_id=fill_id,
            order_id=order.order_id,
            filled_at=filled_at,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            price=price,
            commission=commission,
            tax=tax,
            settlement_date=settlement_date,
        )
        return replace(order, status=OrderStatus.FILLED), fill
