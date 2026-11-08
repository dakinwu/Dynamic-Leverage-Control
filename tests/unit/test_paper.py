from datetime import date, datetime
from decimal import Decimal

import pytest

from dynamic_leverage.domain.paper import (
    FeeSchedule,
    OrderSide,
    OrderStatus,
    PaperAccount,
    PaperBroker,
    PaperOrder,
    PriceBar,
)


def test_buy_limit_fills_at_better_open_and_reserves_t_plus_two_cash() -> None:
    broker = PaperBroker(FeeSchedule(minimum_commission=Decimal("0")))
    order = PaperOrder(
        order_id="o1",
        submitted_at=datetime(2026, 8, 28, 9, 15),
        instrument="0050",
        side=OrderSide.BUY,
        quantity=100,
        limit_price=Decimal("50"),
    )
    bar = PriceBar(
        date(2026, 8, 28),
        Decimal("49"),
        Decimal("51"),
        Decimal("48"),
        Decimal("50"),
        10000,
    )
    updated, fill = broker.process(
        order,
        bar,
        filled_at=datetime(2026, 8, 28, 9, 16),
        settlement_date=date(2026, 9, 1),
        fill_id="f1",
    )
    assert updated.status == OrderStatus.FILLED
    assert fill is not None
    assert fill.price == Decimal("49")

    account = PaperAccount(settled_cash=Decimal("200000"))
    account.apply(fill)
    assert account.positions["0050"] == 100
    assert account.settled_cash == Decimal("200000")
    assert account.available_cash < account.settled_cash
    account.settle_through(date(2026, 9, 1))
    assert account.settled_cash < Decimal("200000")


def test_sell_rejects_position_that_is_not_owned() -> None:
    broker = PaperBroker()
    order = PaperOrder(
        order_id="o2",
        submitted_at=datetime(2026, 8, 28, 9, 15),
        instrument="00631L",
        side=OrderSide.SELL,
        quantity=1,
        limit_price=Decimal("100"),
    )
    bar = PriceBar(
        date(2026, 8, 28),
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
        10000,
    )
    _, fill = broker.process(
        order,
        bar,
        filled_at=datetime(2026, 8, 28, 9, 16),
        settlement_date=date(2026, 9, 1),
        fill_id="f2",
    )
    assert fill is not None
    with pytest.raises(ValueError, match="INSUFFICIENT_POSITION"):
        PaperAccount(settled_cash=Decimal("200000")).apply(fill)


def test_limit_outside_bar_does_not_fill() -> None:
    order = PaperOrder(
        order_id="o3",
        submitted_at=datetime(2026, 8, 28, 9, 15),
        instrument="0050",
        side=OrderSide.BUY,
        quantity=1,
        limit_price=Decimal("45"),
    )
    bar = PriceBar(
        date(2026, 8, 28),
        Decimal("50"),
        Decimal("51"),
        Decimal("49"),
        Decimal("50"),
        10000,
    )
    updated, fill = PaperBroker().process(
        order,
        bar,
        filled_at=datetime(2026, 8, 28, 9, 16),
        settlement_date=date(2026, 9, 1),
        fill_id="f3",
    )
    assert updated.status == OrderStatus.OPEN
    assert fill is None
