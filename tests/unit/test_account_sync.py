from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from dynamic_leverage.domain.account import (
    BrokerAccountSnapshot,
    BrokerPosition,
    BrokerSettlement,
)
from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.infrastructure.tables import RawSnapshotRecord
from dynamic_leverage.services.account_sync import mask_binding, sync_account_snapshot


class FakeAccountReader:
    def __init__(self, snapshot: BrokerAccountSnapshot) -> None:
        self.snapshot = snapshot

    def read_account(self, binding: str) -> BrokerAccountSnapshot:
        assert binding == "9A00-1234567"
        return self.snapshot


def _snapshot(*, code: str = "0050") -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        observed_at=datetime(2026, 8, 31, 5, 30, tzinfo=UTC),
        cash=Decimal("80000"),
        positions=(
            BrokerPosition(
                instrument=code,
                quantity=Decimal("1000"),
                average_price=Decimal("48"),
                last_price=Decimal("50"),
                direction="Buy",
                condition="Cash",
            ),
        ),
        settlements=(
            BrokerSettlement(settlement_date=date(2026, 9, 2), amount=Decimal("-10000"), t=2),
        ),
    )


def test_sync_persists_redacted_observed_account_snapshot() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory.begin() as session:
            result = sync_account_snapshot(
                session,
                FakeAccountReader(_snapshot()),
                binding="9A00-1234567",
            )
        with factory() as session:
            row = session.scalar(select(RawSnapshotRecord))
    finally:
        engine.dispose()

    assert result.created is True
    assert result.account_mode == "observed"
    assert result.nav == Decimal("130000")
    assert result.strategy_cash == Decimal("80000")
    assert result.pending_settlement == Decimal("-10000")
    assert result.actual_exposure == Decimal("0.3846153846153846153846153846")
    assert row is not None
    assert row.kind == "broker.account.shioaji"
    assert row.quality == "authoritative"
    assert row.payload["account_masked"] == "9A00-***4567"
    assert "1234567" not in row.source
    assert "1234567" not in str(row.payload)


def test_sync_marks_unknown_position_as_external_without_blocking() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory.begin() as session:
            result = sync_account_snapshot(
                session,
                FakeAccountReader(_snapshot(code="2330")),
                binding="9A00-1234567",
            )
    finally:
        engine.dispose()

    assert result.created is True
    assert result.reconciliation_break is False
    assert result.quality == "observed-with-external-positions"
    assert result.external_instruments == ("2330",)
    assert result.actual_exposure == Decimal("0")


def test_strategy_exposure_uses_configured_strategy_nav_not_external_positions() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    snapshot = BrokerAccountSnapshot(
        observed_at=datetime(2026, 8, 31, 5, 30, tzinfo=UTC),
        cash=Decimal("80000"),
        positions=(
            BrokerPosition(
                instrument="0050",
                quantity=Decimal("1000"),
                average_price=Decimal("50"),
                last_price=Decimal("50"),
                direction="Buy",
                condition="Cash",
            ),
            BrokerPosition(
                instrument="2330",
                quantity=Decimal("1000"),
                average_price=Decimal("50"),
                last_price=Decimal("50"),
                direction="Buy",
                condition="Cash",
            ),
        ),
        settlements=(),
    )
    try:
        with factory.begin() as session:
            result = sync_account_snapshot(
                session,
                FakeAccountReader(snapshot),
                binding="9A00-1234567",
                capital_budget=Decimal("100000"),
            )
    finally:
        engine.dispose()

    assert result.nav == Decimal("180000")
    assert result.strategy_nav == Decimal("100000")
    assert result.actual_exposure == Decimal("0.5")
    assert result.account_exposure == Decimal(50000) / Decimal(180000)


def test_identical_broker_state_is_idempotent() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    reader = FakeAccountReader(_snapshot())
    try:
        with factory.begin() as session:
            first = sync_account_snapshot(session, reader, binding="9A00-1234567")
        with factory.begin() as session:
            second = sync_account_snapshot(session, reader, binding="9A00-1234567")
    finally:
        engine.dispose()

    assert first.created is True
    assert second.created is False
    assert second.snapshot_id == first.snapshot_id


def test_binding_mask_rejects_output_contamination() -> None:
    try:
        mask_binding("9A00-1234567\nResponse Code: 0")
    except ValueError:
        return
    raise AssertionError("contaminated account binding must be rejected")


def test_leveraged_etf_exposure_uses_official_pcf_snapshot() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    leveraged = _snapshot(code="00631L")
    try:
        with factory.begin() as session:
            repo = AuditRepository(session)
            repo.append_raw_snapshot(
                kind="market.yuanta_00631l_pcf.official",
                source="https://issuer.example/00631L.pcf",
                observed_at=leveraged.observed_at,
                received_at=leveraged.observed_at,
                quality="authoritative",
                checksum="b" * 64,
                payload={"body_text": "交易日期: 2026/08/31 實際曝險 200%"},
            )
            result = sync_account_snapshot(
                session,
                FakeAccountReader(leveraged),
                binding="9A00-1234567",
            )
    finally:
        engine.dispose()
    assert result.exposure_quality == "authoritative-pcf"
    assert result.actual_exposure == Decimal("100000") / Decimal("130000")


def test_market_move_updates_strategy_loss_and_drawdown_metrics() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    first = _snapshot()
    second = BrokerAccountSnapshot(
        observed_at=datetime(2026, 9, 1, 5, 30, tzinfo=UTC),
        cash=Decimal("80000"),
        positions=(
            BrokerPosition(
                instrument="0050",
                quantity=Decimal("1000"),
                average_price=Decimal("48"),
                last_price=Decimal("40"),
                direction="Buy",
                condition="Cash",
            ),
        ),
        settlements=(),
    )
    try:
        with factory.begin() as session:
            sync_account_snapshot(
                session,
                FakeAccountReader(first),
                binding="9A00-1234567",
                capital_budget=Decimal("100000"),
            )
        with factory.begin() as session:
            result = sync_account_snapshot(
                session,
                FakeAccountReader(second),
                binding="9A00-1234567",
                capital_budget=Decimal("100000"),
            )
    finally:
        engine.dispose()

    assert result.strategy_cash == Decimal("50000")
    assert result.strategy_nav == Decimal("90000")
    assert result.principal_loss_amount == Decimal("10000")
    assert result.principal_loss_ratio == Decimal("0.1")
    assert result.current_drawdown == Decimal("0.1")
    assert result.maximum_drawdown == Decimal("0.1")


def test_unrecorded_strategy_position_change_breaks_reconciliation() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    changed = BrokerAccountSnapshot(
        observed_at=datetime(2026, 9, 1, 5, 30, tzinfo=UTC),
        cash=Decimal("85000"),
        positions=(
            BrokerPosition(
                instrument="0050",
                quantity=Decimal("900"),
                average_price=Decimal("48"),
                last_price=Decimal("50"),
                direction="Buy",
                condition="Cash",
            ),
        ),
        settlements=(),
    )
    try:
        with factory.begin() as session:
            sync_account_snapshot(
                session,
                FakeAccountReader(_snapshot()),
                binding="9A00-1234567",
                capital_budget=Decimal("100000"),
            )
        with factory.begin() as session:
            result = sync_account_snapshot(
                session,
                FakeAccountReader(changed),
                binding="9A00-1234567",
                capital_budget=Decimal("100000"),
            )
    finally:
        engine.dispose()

    assert result.reconciliation_break is True
    assert result.actual_exposure is None
    assert result.strategy_nav == Decimal("100000")
    assert result.reconciliation_reason == (
        "unrecorded-position-change (0050:ledger=1000,broker=900)"
    )
