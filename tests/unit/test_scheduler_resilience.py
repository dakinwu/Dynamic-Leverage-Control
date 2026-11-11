"""The scheduler loop must survive database errors and must not skip a day's account sync."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import OperationalError

from dynamic_leverage.infrastructure.database import Base, make_engine
from dynamic_leverage.services import scheduler

TAIPEI = ZoneInfo("Asia/Taipei")


class CycleFinished(Exception):
    """Raised from the end-of-cycle sleep to stop the endless loop in a test."""


class _UnavailableSources:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __getattr__(self, _name):
        async def unavailable(*_args, **_kwargs):
            raise OSError("network disabled in test")

        return unavailable


@pytest.fixture
def isolated_scheduler(monkeypatch, tmp_path):
    """Run one scheduler cycle with every external source stubbed out."""

    async def no_backfill(**_kwargs):
        return {"valuation_remaining": 0, "valuation_created": 0, "valuation_failed": 0}

    async def unavailable(**_kwargs):
        raise OSError("network disabled in test")

    async def stop(_seconds):
        raise CycleFinished

    monkeypatch.setattr(scheduler, "run_official_backfill", no_backfill)
    monkeypatch.setattr(scheduler, "ingest_twse_snapshot", unavailable)
    monkeypatch.setattr(scheduler, "OfficialIndicesClient", _UnavailableSources)
    monkeypatch.setattr(scheduler, "OfficialValuationClient", _UnavailableSources)
    monkeypatch.setattr(scheduler, "load_credentials", lambda _path: None)
    monkeypatch.setattr(scheduler, "ShioajiAccountSnapshotReader", lambda **_kw: object())
    monkeypatch.setattr(scheduler.asyncio, "sleep", stop)
    secret = tmp_path / "runtime.env"
    secret.write_text("", encoding="utf-8")
    database_url = f"sqlite:///{(tmp_path / 'sched.db').as_posix()}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()

    def run_one_cycle() -> None:
        with pytest.raises(CycleFinished):
            asyncio.run(
                scheduler.run_snapshot_scheduler(
                    database_url=database_url,
                    interval_seconds=900,
                    shioaji_mode="readonly",
                    shioaji_secret_file=secret,
                    shioaji_account_id="9A00-1234567",
                )
            )

    run_one_cycle.database_url = database_url
    return run_one_cycle


def test_database_error_during_account_sync_does_not_stop_the_loop(
    monkeypatch, isolated_scheduler
) -> None:
    attempts = []

    def database_down(*_args, **_kwargs):
        attempts.append("sync")
        raise OperationalError("UPDATE strategy_ledger_state", {}, Exception("server closed"))

    monkeypatch.setattr(scheduler, "sync_account_snapshot", database_down)
    monkeypatch.setattr(scheduler, "_now", lambda: datetime(2026, 11, 10, 9, 7, tzinfo=TAIPEI))

    isolated_scheduler()  # Reaching the end-of-cycle sleep means the loop survived.

    assert attempts == ["sync"]


def test_account_sync_missed_in_its_window_is_caught_up_later_that_day(
    monkeypatch, isolated_scheduler
) -> None:
    attempts = []

    def sync(*_args, **_kwargs):
        attempts.append("sync")
        raise OSError("broker unavailable in test")

    monkeypatch.setattr(scheduler, "sync_account_snapshot", sync)
    # 09:20: the 09:05-09:15 account-sync slot was missed by a 900 s interval.
    monkeypatch.setattr(scheduler, "_now", lambda: datetime(2026, 11, 10, 9, 20, tzinfo=TAIPEI))

    isolated_scheduler()

    assert attempts == ["sync"]


@pytest.mark.parametrize(
    ("now", "synced_on", "due"),
    [
        (datetime(2026, 11, 10, 9, 4, tzinfo=TAIPEI), None, False),  # before the window
        (datetime(2026, 11, 10, 9, 5, tzinfo=TAIPEI), None, True),
        (datetime(2026, 11, 10, 13, 0, tzinfo=TAIPEI), None, True),  # window missed
        (datetime(2026, 11, 10, 13, 0, tzinfo=TAIPEI), "2026-11-10", False),
        (datetime(2026, 11, 11, 9, 30, tzinfo=TAIPEI), "2026-11-10", True),  # new day
        (datetime(2026, 11, 14, 10, 0, tzinfo=TAIPEI), None, False),  # Saturday
    ],
)
def test_account_sync_due_once_per_trading_day(now, synced_on, due) -> None:
    from datetime import date

    from dynamic_leverage.services.calendar import account_sync_due

    last = date.fromisoformat(synced_on) if synced_on else None
    assert account_sync_due(now, last) is due


def test_cycle_captures_the_official_distribution_table_for_each_instrument(
    monkeypatch, isolated_scheduler
) -> None:
    from sqlalchemy import select

    from dynamic_leverage.infrastructure.database import make_session_factory
    from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot
    from dynamic_leverage.infrastructure.tables import RawSnapshotRecord
    from dynamic_leverage.services.corporate_actions import DISTRIBUTION_KIND, distribution_url

    requested = []

    class DistributionsOnly(_UnavailableSources):
        async def twse_distributions_snapshot(self, symbol, year):
            requested.append((symbol, year))
            url = distribution_url(symbol, year)
            return HttpSnapshot(url, datetime.now(TAIPEI), b"<table></table>", "text/html", url)

    monkeypatch.setattr(scheduler, "OfficialIndicesClient", DistributionsOnly)
    monkeypatch.setattr(scheduler, "sync_account_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "_now", lambda: datetime(2026, 11, 10, 15, 30, tzinfo=TAIPEI))

    isolated_scheduler()

    engine = make_engine(isolated_scheduler.database_url)
    with make_session_factory(engine)() as session:
        sources = session.scalars(
            select(RawSnapshotRecord.source).where(RawSnapshotRecord.kind == DISTRIBUTION_KIND)
        ).all()
    engine.dispose()
    assert sorted(requested) == [("0050", 2026), ("00631L", 2026), ("00685L", 2026)]
    assert sorted(sources) == sorted(distribution_url(s, 2026) for s, _ in requested)
