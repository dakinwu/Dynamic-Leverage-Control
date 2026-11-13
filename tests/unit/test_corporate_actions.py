"""Splits change units, not wealth; dividends are owed at ex-date and paid later."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from dynamic_leverage.domain.corporate_actions import (
    DividendAction,
    FillRecord,
    SplitAction,
    split_units,
    units_held_at_open,
)
from dynamic_leverage.domain.ledger import (
    accrue_dividend,
    apply_split,
    bootstrap_strategy_ledger,
    confirm_dividend,
    mark_strategy_ledger,
    pay_dividend,
)

AT = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)
SPLIT_0050 = SplitAction(
    symbol="0050",
    ratio=4,
    last_trade_date=date(2025, 6, 10),
    halt_start=date(2025, 6, 11),
    halt_end=date(2025, 6, 17),
    resume_date=date(2025, 6, 18),
)
DIVIDEND_0050 = DividendAction(
    symbol="0050",
    ex_date=date(2026, 7, 21),
    payment_date=date(2026, 8, 10),
    amount_per_unit=Decimal("0.6"),
)


def _ledger(units="1000", price="50"):
    return bootstrap_strategy_ledger(
        capital_budget=Decimal("500000"),
        positions={"0050": Decimal(units)},
        prices={"0050": Decimal(price)},
        observed_at=AT,
        account_snapshot_id="bootstrap",
    )


def test_action_ids_are_derived_from_official_facts() -> None:
    assert SPLIT_0050.action_id == "0050:SPLIT:2025-06-18"
    assert DIVIDEND_0050.action_id == "0050:DIVIDEND:2026-07-21"


def test_split_units_requires_a_whole_number_of_units() -> None:
    assert split_units(Decimal("1000"), 4) == Decimal("4000")
    with pytest.raises(ValueError, match="fractional"):
        split_units(Decimal("1001"), Decimal("0.5"))


def test_split_scales_units_and_price_without_changing_nav() -> None:
    before = _ledger(units="1000", price="188.65")

    after = apply_split(before, symbol="0050", ratio=4, at=AT)

    assert after.positions["0050"] == Decimal("4000")
    assert after.last_prices["0050"] == Decimal("188.65") / 4
    assert after.current_nav == before.current_nav
    assert after.position_revision == before.position_revision + 1
    assert after.principal_loss_ratio == before.principal_loss_ratio


def test_ex_date_price_drop_is_offset_by_the_dividend_receivable() -> None:
    before = _ledger(units="1000", price="50")
    owed = accrue_dividend(before, action_id=DIVIDEND_0050.action_id, amount=Decimal("600"), at=AT)
    # The ex-date mark: price falls by the dividend per unit.
    marked = mark_strategy_ledger(
        owed,
        positions={"0050": Decimal("1000")},
        prices={"0050": Decimal("49.4")},
        observed_at=AT,
        account_snapshot_id="ex-date",
    )

    assert marked.receivables == {DIVIDEND_0050.action_id: Decimal("600")}
    assert marked.current_nav == before.current_nav
    assert marked.principal_loss_ratio == Decimal("0")


def test_payment_moves_the_receivable_to_cash_pending_confirmation() -> None:
    owed = accrue_dividend(_ledger(), action_id="a", amount=Decimal("600"), at=AT)

    paid = pay_dividend(owed, action_id="a", at=AT)

    assert paid.cash == owed.cash + Decimal("600")
    assert paid.receivables == {}
    assert paid.unconfirmed_dividends == {"a": Decimal("600")}
    assert paid.current_nav == owed.current_nav


def test_confirmation_books_the_amount_actually_received() -> None:
    paid = pay_dividend(
        accrue_dividend(_ledger(), action_id="a", amount=Decimal("600"), at=AT),
        action_id="a",
        at=AT,
    )

    confirmed = confirm_dividend(paid, action_id="a", received=Decimal("590"), at=AT)

    assert confirmed.cash == paid.cash - Decimal("10")
    assert confirmed.unconfirmed_dividends == {}
    assert confirmed.current_nav == paid.current_nav - Decimal("10")


def test_units_at_ex_date_open_ignore_fills_on_or_after_the_ex_date() -> None:
    fills = [
        FillRecord("0050", "BUY", Decimal("100"), date(2026, 7, 20)),  # cum-dividend
        FillRecord("0050", "BUY", Decimal("50"), date(2026, 7, 21)),  # ex-date: no entitlement
        FillRecord("0050", "SELL", Decimal("30"), date(2026, 7, 25)),
    ]

    units = units_held_at_open(
        date(2026, 7, 21), symbol="0050", current_units=Decimal("1120"), fills=fills, splits=[]
    )

    assert units == Decimal("1100")


def test_units_at_open_undo_a_later_split() -> None:
    units = units_held_at_open(
        date(2025, 6, 10),
        symbol="0050",
        current_units=Decimal("4000"),
        fills=[],
        splits=[SPLIT_0050],
    )

    assert units == Decimal("1000")


def test_units_at_open_handle_fills_on_both_sides_of_a_split() -> None:
    fills = [
        FillRecord("0050", "BUY", Decimal("100"), date(2025, 6, 5)),  # pre-split units
        FillRecord("0050", "BUY", Decimal("400"), date(2025, 6, 20)),  # post-split units
    ]
    # (1000 + 100) x 4 + 400
    units = units_held_at_open(
        date(2025, 6, 1),
        symbol="0050",
        current_units=Decimal("4800"),
        fills=fills,
        splits=[SPLIT_0050],
    )

    assert units == Decimal("1000")
