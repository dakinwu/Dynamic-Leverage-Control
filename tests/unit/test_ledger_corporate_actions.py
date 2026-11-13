"""Account sync applies registered splits and official dividends to the strategy ledger."""

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from dynamic_leverage.domain.account import BrokerAccountSnapshot, BrokerPosition
from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.infrastructure.tables import StrategyLedgerEventRecord
from dynamic_leverage.services.account_sync import sync_account_snapshot
from dynamic_leverage.services.corporate_actions import DISTRIBUTION_KIND, distribution_url
from dynamic_leverage.services.ledger import record_strategy_fill

TAIPEI = ZoneInfo("Asia/Taipei")
BINDING = "9A00-1234567"
DIVIDEND_ID = "0050:DIVIDEND:2026-07-21"


class Reader:
    def __init__(self, snapshot: BrokerAccountSnapshot) -> None:
        self.snapshot = snapshot

    def read_account(self, binding: str) -> BrokerAccountSnapshot:
        return self.snapshot


def _snapshot(day: date, quantity: str, price: str) -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        observed_at=datetime(day.year, day.month, day.day, 13, 30, tzinfo=TAIPEI),
        cash=Decimal("80000"),
        positions=(
            BrokerPosition(
                instrument="0050",
                quantity=Decimal(quantity),
                average_price=Decimal(price),
                last_price=Decimal(price),
                direction="Buy",
                condition="Cash",
            ),
        ),
        settlements=(),
    )


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'ca.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _sync(factory, day: date, quantity: str, price: str, **kwargs):
    with factory.begin() as session:
        return sync_account_snapshot(
            session,
            Reader(_snapshot(day, quantity, price)),
            binding=BINDING,
            capital_budget=Decimal("500000"),
            **kwargs,
        )


def _ledger(factory):
    with factory() as session:
        return AuditRepository(session).get_strategy_ledger_state()


def _events(factory, prefix: str = "CA:") -> list[str]:
    with factory() as session:
        return sorted(
            session.scalars(
                select(StrategyLedgerEventRecord.event_key).where(
                    StrategyLedgerEventRecord.event_key.startswith(prefix)
                )
            )
        )


def _capture_distribution(factory, amount: str, *, checksum: str = "d1") -> None:
    row = (
        "<tr><td>0050</td><td>Yuanta Taiwan 50</td><td>2026/07/21</td>"
        f"<td>2026/08/10</td><td>{amount}</td><td>2026</td></tr>"
    )
    body = (
        "<table><tr><th>ETF Code</th><th>ETF Name</th><th>Ex-dividend Date</th>"
        "<th>Dividend Payment Date</th><th>Cash Dividend (NT$/Per beneficiary unit)</th>"
        f"<th>Year Announced</th></tr>{row}</table>"
    )
    with factory.begin() as session:
        AuditRepository(session).append_raw_snapshot(
            kind=DISTRIBUTION_KIND,
            source=distribution_url("0050", 2026),
            observed_at=datetime.now(UTC),
            received_at=datetime(2026, 7, 20, 1, 0, tzinfo=UTC),
            quality="authoritative",
            checksum=checksum,
            payload={"body_text": body},
        )


def _fill(factory, execution_id: str, day: date, quantity: str = "100", price: str = "49.5"):
    with factory.begin() as session:
        return record_strategy_fill(
            session,
            execution_id=execution_id,
            instrument="0050",
            side="BUY",
            quantity=Decimal(quantity),
            price=Decimal(price),
            commission=Decimal("0"),
            tax=Decimal("0"),
            occurred_at=datetime(day.year, day.month, day.day, 10, 0, tzinfo=TAIPEI),
            note="",
        )


# --- Splits (0050: 1 -> 4, last trade 2025-06-10, halt 06-11..06-17, resume 06-18) ---


def test_registered_split_seen_at_the_broker_is_applied(factory) -> None:
    _sync(factory, date(2025, 6, 9), "1000", "188.65")

    result = _sync(factory, date(2025, 6, 18), "4000", "47.16")

    ledger = _ledger(factory)
    assert result.reconciliation_break is False
    assert ledger.positions == {"0050": Decimal("4000")}
    assert ledger.current_nav == ledger.cash + Decimal("4000") * Decimal("47.16")
    assert _events(factory) == ["CA:0050:SPLIT:2025-06-18:SPLIT"]


def test_rerunning_the_sync_does_not_apply_the_split_twice(factory) -> None:
    _sync(factory, date(2025, 6, 9), "1000", "188.65")
    _sync(factory, date(2025, 6, 18), "4000", "47.16")

    _sync(factory, date(2025, 6, 18), "4000", "47.16")

    assert _ledger(factory).positions == {"0050": Decimal("4000")}
    assert _events(factory) == ["CA:0050:SPLIT:2025-06-18:SPLIT"]


def test_unregistered_whole_multiple_is_a_reconciliation_break(factory) -> None:
    _sync(factory, date(2025, 6, 9), "1000", "188.65", splits=())

    result = _sync(factory, date(2025, 6, 18), "4000", "47.16", splits=())

    assert result.reconciliation_break is True
    assert "unregistered-quantity-ratio (0050 x4)" in result.reconciliation_reason
    assert _events(factory) == []


def test_broker_still_showing_old_units_during_the_halt_is_marked_normally(factory) -> None:
    _sync(factory, date(2025, 6, 9), "1000", "188.65")

    result = _sync(factory, date(2025, 6, 12), "1000", "188.65")

    assert result.reconciliation_break is False
    assert _events(factory) == []


def test_split_not_reflected_by_the_broker_soon_after_resume_is_a_break(factory) -> None:
    _sync(factory, date(2025, 6, 9), "1000", "188.65")

    result = _sync(factory, date(2025, 6, 26), "1000", "188.65")

    assert result.reconciliation_break is True
    assert "split-not-reflected-by-broker (0050:SPLIT:2025-06-18)" in result.reconciliation_reason


def test_fill_dated_before_an_applied_split_is_refused(factory) -> None:
    _sync(factory, date(2025, 6, 9), "1000", "188.65")
    _sync(factory, date(2025, 6, 18), "4000", "47.16")

    with pytest.raises(ValueError, match="corporate action"):
        _fill(factory, "late-report", date(2025, 6, 5), price="188")


# --- Cash dividend (0050: ex 2026-07-21, paid 2026-08-10, NT$0.6 per unit) ---


def test_ex_date_drop_is_offset_by_the_receivable(factory) -> None:
    _capture_distribution(factory, "0.6")
    _sync(factory, date(2026, 7, 15), "1000", "50")

    _sync(factory, date(2026, 7, 21), "1000", "49.4")

    ledger = _ledger(factory)
    assert ledger.receivables == {DIVIDEND_ID: Decimal("600.0")}
    assert ledger.current_nav == Decimal("500000")
    assert ledger.principal_loss_ratio == Decimal("0")
    assert _events(factory) == [f"CA:{DIVIDEND_ID}:ENTITLEMENT"]


def test_payment_date_moves_the_receivable_to_cash_once(factory) -> None:
    _capture_distribution(factory, "0.6")
    _sync(factory, date(2026, 7, 15), "1000", "50")
    _sync(factory, date(2026, 7, 21), "1000", "49.4")

    _sync(factory, date(2026, 8, 10), "1000", "50")
    _sync(factory, date(2026, 8, 10), "1000", "50")

    ledger = _ledger(factory)
    assert ledger.cash == Decimal("450600.0")
    assert ledger.receivables == {}
    assert ledger.unconfirmed_dividends == {DIVIDEND_ID: Decimal("600.0")}
    assert _events(factory) == [f"CA:{DIVIDEND_ID}:ENTITLEMENT", f"CA:{DIVIDEND_ID}:PAYMENT"]


def test_buy_on_or_after_the_ex_date_earns_no_entitlement(factory) -> None:
    _capture_distribution(factory, "0.6")
    _sync(factory, date(2026, 7, 15), "1000", "50")
    _fill(factory, "after-ex", date(2026, 7, 22))

    _sync(factory, date(2026, 7, 23), "1100", "49.5")

    assert _ledger(factory).receivables == {DIVIDEND_ID: Decimal("600.0")}


def test_fill_dated_before_a_booked_entitlement_is_refused(factory) -> None:
    _capture_distribution(factory, "0.6")
    _sync(factory, date(2026, 7, 15), "1000", "50")
    _sync(factory, date(2026, 7, 21), "1000", "49.4")

    with pytest.raises(ValueError, match="corporate action"):
        _fill(factory, "cum-dividend-late", date(2026, 7, 20))


def test_changed_official_facts_after_booking_are_a_break(factory) -> None:
    _capture_distribution(factory, "0.6")
    _sync(factory, date(2026, 7, 15), "1000", "50")
    _sync(factory, date(2026, 7, 21), "1000", "49.4")
    _capture_distribution(factory, "0.7", checksum="d2")

    result = _sync(factory, date(2026, 7, 22), "1000", "49.4")

    assert result.reconciliation_break is True
    assert f"corporate-action-facts-changed ({DIVIDEND_ID})" in result.reconciliation_reason
    assert _ledger(factory).receivables == {DIVIDEND_ID: Decimal("600.0")}


# --- Operator Queue ---


def _active_queue(factory) -> dict[str, dict]:
    from dynamic_leverage.config import Settings
    from dynamic_leverage.services.operations_projection import build_operations

    with factory() as session:
        _summary, active = build_operations(AuditRepository(session), Settings(app_env="test"))
    return {item["item_id"]: item for item in active}


def test_queue_explains_an_unregistered_split(factory) -> None:
    _sync(factory, date(2025, 6, 9), "1000", "188.65", splits=())
    _sync(factory, date(2025, 6, 18), "4000", "47.16", splits=())

    item = _active_queue(factory)["ledger:reconciliation"]

    assert item["severity"] == "BLOCKER"
    assert "unregistered-quantity-ratio (0050 x4)" in item["detail"]
    assert "corporate-actions.json" in item["next_action"]


def test_queue_asks_to_confirm_a_paid_dividend(factory) -> None:
    _capture_distribution(factory, "0.6")
    _sync(factory, date(2026, 7, 15), "1000", "50")
    _sync(factory, date(2026, 7, 21), "1000", "49.4")
    _sync(factory, date(2026, 8, 10), "1000", "50")

    queue = _active_queue(factory)

    assert queue[f"ledger:dividend-unconfirmed:{DIVIDEND_ID}"]["severity"] == "ACTION"


def test_queue_reports_missing_distribution_captures(factory) -> None:
    _sync(factory, date(2026, 7, 15), "1000", "50")

    item = _active_queue(factory)["corporate-actions:distributions"]

    assert "0050: no distribution capture" in item["detail"]
