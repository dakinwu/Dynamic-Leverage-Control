"""Concurrent writers must not lose ledger updates, and fill replays must be exact."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from dynamic_leverage.domain.ledger import bootstrap_strategy_ledger
from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.infrastructure.tables import StrategyLedgerEventRecord
from dynamic_leverage.services import ledger as ledger_service
from dynamic_leverage.services.ledger import record_strategy_fill


def _fill(execution_id: str, *, instrument="0050", price="50", quantity="100") -> dict:
    return {
        "execution_id": execution_id,
        "instrument": instrument,
        "side": "BUY",
        "quantity": Decimal(quantity),
        "price": Decimal(price),
        "commission": Decimal("0"),
        "tax": Decimal("0"),
        "occurred_at": datetime(2026, 9, 1, 2, 0, tzinfo=UTC),
        "note": "",
    }


@pytest.fixture
def factory(tmp_path):
    # A file database: each session has its own connection, as with two workers.
    engine = make_engine(f"sqlite:///{(tmp_path / 'ledger.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions.begin() as session:
        AuditRepository(session).save_strategy_ledger_state(
            bootstrap_strategy_ledger(
                capital_budget=Decimal("500000"),
                positions={"0050": Decimal("1000"), "00631L": Decimal("0")},
                prices={"0050": Decimal("50"), "00631L": Decimal("20")},
                observed_at=datetime(2026, 8, 31, 5, 30, tzinfo=UTC),
                account_snapshot_id="bootstrap",
            )
        )
    yield sessions
    engine.dispose()


def _interleave(monkeypatch, run_other) -> None:
    """Run another complete writer after this writer has read the ledger."""
    real_apply = ledger_service.apply_strategy_fill
    pending = [run_other]

    def apply_then_let_other_commit(current, **kwargs):
        if pending:
            pending.pop()()
        return real_apply(current, **kwargs)

    monkeypatch.setattr(ledger_service, "apply_strategy_fill", apply_then_let_other_commit)


def _state(factory):
    with factory() as session:
        repo = AuditRepository(session)
        events = session.scalar(select(func.count()).select_from(StrategyLedgerEventRecord))
        return repo.get_strategy_ledger_state(), events


def test_interleaved_different_fills_are_both_booked(factory, monkeypatch) -> None:
    def other_writer():
        with factory.begin() as other:
            record_strategy_fill(other, **_fill("exec-B", instrument="00631L", price="20"))

    _interleave(monkeypatch, other_writer)
    with factory.begin() as session:
        result = record_strategy_fill(session, **_fill("exec-A"))

    state, events = _state(factory)
    assert result.created is True
    assert events == 2
    assert state.position_revision == 2
    assert state.positions["0050"] == Decimal("1100")
    assert state.positions["00631L"] == Decimal("100")
    # Bootstrap cash 450,000 (500,000 budget - 1,000 x 50) minus both fills.
    assert state.cash == Decimal("450000") - Decimal("5000") - Decimal("2000")


def test_concurrent_resend_of_one_fill_books_it_once(factory, monkeypatch) -> None:
    outcomes = []

    def other_writer():
        with factory.begin() as other:
            outcomes.append(record_strategy_fill(other, **_fill("exec-A")).created)

    _interleave(monkeypatch, other_writer)
    with factory.begin() as session:
        outcomes.append(record_strategy_fill(session, **_fill("exec-A")).created)

    state, events = _state(factory)
    assert sorted(outcomes) == [False, True]
    assert events == 1
    assert state.position_revision == 1
    assert state.positions["0050"] == Decimal("1100")


def test_same_execution_id_with_different_fill_is_a_conflict(factory) -> None:
    with factory.begin() as session:
        assert record_strategy_fill(session, **_fill("exec-A")).created is True

    with factory.begin() as session, pytest.raises(ValueError, match="different fill"):
        record_strategy_fill(session, **_fill("exec-A", price="51"))

    state, events = _state(factory)
    assert events == 1
    assert state.position_revision == 1


def test_exact_replay_with_equivalent_decimals_is_idempotent(factory) -> None:
    with factory.begin() as session:
        record_strategy_fill(session, **_fill("exec-A"))
    with factory.begin() as session:
        replay = record_strategy_fill(session, **_fill("exec-A", price="50.00", quantity="100.0"))
    assert replay.created is False


def test_receivables_and_unconfirmed_dividends_survive_a_round_trip(factory) -> None:
    from dynamic_leverage.domain.ledger import accrue_dividend, pay_dividend

    at = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)
    with factory.begin() as session:
        repo = AuditRepository(session)
        current = repo.get_strategy_ledger_state(refresh=True)
        owed = accrue_dividend(
            current, action_id="0050:DIVIDEND:2026-09-02", amount=Decimal("600"), at=at
        )
        paid = pay_dividend(
            accrue_dividend(
                owed, action_id="0050:DIVIDEND:2026-01-20", amount=Decimal("80"), at=at
            ),
            action_id="0050:DIVIDEND:2026-01-20",
            at=at,
        )
        repo.save_strategy_ledger_state(paid, expected=current)

    state, _events = _state(factory)
    assert state.receivables == {"0050:DIVIDEND:2026-09-02": Decimal("600")}
    assert state.unconfirmed_dividends == {"0050:DIVIDEND:2026-01-20": Decimal("80")}
    assert state.current_nav == paid.current_nav
