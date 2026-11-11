"""Timestamps must survive SQLite as the same instant, whatever zone they were written in."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services.freshness import freshness_rows
from dynamic_leverage.services.strategy_pipeline import build_strategy_inputs

TAIPEI = ZoneInfo("Asia/Taipei")


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'tz.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _write(factory, *, kind, observed_at, checksum="c1", payload=None, received_at=None) -> str:
    with factory.begin() as session:
        row, _ = AuditRepository(session).append_raw_snapshot(
            kind=kind,
            source=f"test:{kind}",
            observed_at=observed_at,
            received_at=received_at or observed_at,
            quality="authoritative",
            checksum=checksum,
            payload=payload or {},
        )
        return row.snapshot_id


def test_taipei_timestamp_round_trips_as_the_same_instant(factory) -> None:
    written = datetime(2026, 11, 9, 15, 30, tzinfo=TAIPEI)
    _write(factory, kind="broker.account.shioaji", observed_at=written)

    with factory() as session:
        read = AuditRepository(session).latest_raw_snapshot(kind="broker.account.shioaji")

    assert read.observed_at.tzinfo is not None
    assert read.observed_at == written


def test_snapshot_written_30_hours_ago_in_taipei_time_is_stale(factory) -> None:
    now = datetime(2026, 11, 9, 15, 0, tzinfo=TAIPEI)
    _write(factory, kind="broker.account.shioaji", observed_at=now - timedelta(hours=30))

    with factory() as session:
        rows = freshness_rows(AuditRepository(session), now=now)

    account = next(row for row in rows if row.contract_key == "account_sync")
    assert account.age_minutes == 30 * 60
    assert account.status == "STALE"


def test_snapshot_observed_in_the_future_is_unknown(factory) -> None:
    now = datetime(2026, 11, 9, 15, 0, tzinfo=TAIPEI)
    _write(factory, kind="broker.account.shioaji", observed_at=now + timedelta(hours=2))

    with factory() as session:
        rows = freshness_rows(AuditRepository(session), now=now)

    account = next(row for row in rows if row.contract_key == "account_sync")
    assert account.status == "UNKNOWN"


def _taiex_payload(day: date) -> dict:
    roc = f"{day.year - 1911:03d}{day.month:02d}{day.day:02d}"
    return {
        "body": [
            {"日期": roc, "指數": "發行量加權股價指數", "收盤指數": "20000", "漲跌百分比": "0"}
        ]
    }


def test_snapshot_received_just_now_is_visible_to_the_pipeline(factory) -> None:
    now = datetime.now(TAIPEI)
    snapshot_id = _write(
        factory,
        kind="market.taiex_index.official",
        observed_at=now - timedelta(minutes=1),
        payload=_taiex_payload(now.date()),
    )

    with factory() as session:
        result = build_strategy_inputs(AuditRepository(session))

    assert snapshot_id in result.snapshot_ids


def test_replay_cutoff_is_end_of_the_taipei_trading_day(factory) -> None:
    day = date(2026, 11, 6)
    # 07:00 the next morning in Taipei is still the same UTC calendar day.
    next_morning = datetime(2026, 11, 7, 7, 0, tzinfo=TAIPEI)
    assert next_morning.astimezone(UTC).date() == day
    _write(
        factory,
        kind="market.taiex_index.official",
        observed_at=datetime(2026, 11, 6, 14, 0, tzinfo=TAIPEI),
        checksum="same-day",
        payload=_taiex_payload(day),
    )
    late_id = _write(
        factory,
        kind="market.taiex_index.official",
        observed_at=next_morning,
        checksum="next-morning",
        payload=_taiex_payload(date(2026, 11, 7)),
    )

    with factory() as session:
        result = build_strategy_inputs(AuditRepository(session), as_of=day)

    assert late_id not in result.snapshot_ids
    assert result.diagnostics["decision_at"].endswith("+08:00")
