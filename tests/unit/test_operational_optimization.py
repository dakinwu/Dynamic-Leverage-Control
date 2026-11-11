import asyncio
import base64
import hashlib
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from dynamic_leverage.api.app import create_app
from dynamic_leverage.config import Settings
from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot
from dynamic_leverage.infrastructure.providers.twse import TwseClient
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.infrastructure.tables import NotificationOutboxRecord
from dynamic_leverage.services.calendar import expected_close_date, next_trading_day, schedule_slot
from dynamic_leverage.services.freshness import freshness_rows
from dynamic_leverage.services.ingestion import ingest_twse_snapshot
from dynamic_leverage.services.operations_projection import (
    dispatch_notifications,
    reconcile_operations,
)
from dynamic_leverage.services.research_evidence import REFERENCE, research_evidence

TZ = ZoneInfo("Asia/Taipei")


CONSOLE = {"base_url": "http://localhost", "headers": {"X-DVL-Client": "console"}}


@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2026, 9, 24, 14, 59, tzinfo=TZ), date(2026, 9, 23)),
        (datetime(2026, 9, 24, 15, 0, tzinfo=TZ), date(2026, 9, 24)),
        (datetime(2026, 9, 25, 16, 0, tzinfo=TZ), date(2026, 9, 24)),
        (datetime(2026, 9, 28, 16, 0, tzinfo=TZ), date(2026, 9, 24)),
        (datetime(2026, 9, 29, 9, 5, tzinfo=TZ), date(2026, 9, 24)),
    ],
)
def test_official_holidays_and_publication_cutoff(now, expected):
    assert expected_close_date(now) == expected
    assert next_trading_day(date(2026, 9, 24)) == date(2026, 9, 29)
    assert schedule_slot(datetime(2026, 9, 28, 16, tzinfo=TZ)) is None


@pytest.mark.parametrize(
    "trade_date,now,status",
    [
        ("2026-09-23", datetime(2026, 9, 24, 16, tzinfo=TZ), "STALE"),
        ("2026-09-24", datetime(2026, 9, 28, 16, tzinfo=TZ), "FRESH"),
        (None, datetime(2026, 9, 28, 16, tzinfo=TZ), "UNKNOWN"),
        ("2026-09-29", datetime(2026, 9, 28, 16, tzinfo=TZ), "STALE"),
        ("2027-01-05", datetime(2027, 1, 5, 16, tzinfo=TZ), "UNKNOWN"),
    ],
)
def test_download_time_never_substitutes_for_trade_date(trade_date, now, status):
    snapshot = SimpleNamespace(
        observed_at=now, received_at=now, snapshot_id="q", payload={"source_trade_date": trade_date}
    )
    contract = SimpleNamespace(
        contract_key="twse_quotes",
        kind="market.twse.daily_quotes",
        source="TWSE",
        owner="TWSE",
        max_age_minutes=1440,
    )
    repo = SimpleNamespace(
        list_freshness_contracts=lambda: [contract], latest_raw_snapshot=lambda **kwargs: snapshot
    )
    row = freshness_rows(repo, now=now)[0]
    assert row.status == status
    assert row.received_at == now
    if status == "UNKNOWN":
        assert row.effective_at is None


def quote_response(day="20260924"):
    return json.dumps(
        {
            "stat": "OK",
            "date": day,
            "tables": [
                {
                    "fields": ["證券代號", "證券名稱", "收盤價", "成交股數", "成交金額"],
                    "data": [["0050", "Taiwan 50", "112.40", "1,000", "112,400"]],
                }
            ],
        }
    ).encode()


def test_dated_quote_ingestion_preserves_original_response(tmp_path):
    body = quote_response()

    class Http:
        async def fetch(self, url):
            return HttpSnapshot(
                url,
                datetime(2026, 9, 28, tzinfo=UTC),
                body,
                "application/json",
                hashlib.sha256(body).hexdigest(),
            )

    class Client(TwseClient):
        async def daily_quote_snapshot(self):
            return await super().daily_quote_snapshot(as_of=date(2026, 9, 24))

    url = f"sqlite:///{(tmp_path / 'quotes.db').as_posix()}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    result = asyncio.run(
        ingest_twse_snapshot(database_url=url, codes={"0050"}, client=Client(Http()))
    )
    with make_session_factory(engine).begin() as session:
        row = AuditRepository(session).latest_raw_snapshot(kind="market.twse.daily_quotes")
        assert row.snapshot_id == result.snapshot_id
        assert row.payload["source_trade_date"] == "2026-09-24"
        assert row.observed_at.date() == date(2026, 9, 24)
        assert row.received_at.date() == date(2026, 9, 28)
        assert row.published_at is None
        assert base64.b64decode(row.payload["raw_body_base64"]) == body
    engine.dispose()


def test_source_cannot_silently_return_previous_session():
    class Http:
        async def fetch(self, url):
            return HttpSnapshot(
                url, datetime.now(UTC), quote_response("20260923"), "application/json", "x"
            )

    with pytest.raises(ValueError, match="differs"):
        asyncio.run(TwseClient(Http()).daily_quote_snapshot(as_of=date(2026, 9, 24)))


def test_gets_are_read_only_even_on_empty_database(monkeypatch):
    def unexpected_notification(*args, **kwargs):
        raise AssertionError("read requests must not notify")

    monkeypatch.setattr(
        "dynamic_leverage.services.notifications.TelegramNotifier.notify_state",
        unexpected_notification,
    )
    settings = Settings(app_env="test", database_url="sqlite:///:memory:", auto_create_schema=True)
    app = create_app(settings)
    writes = []
    with TestClient(app, **CONSOLE) as client:

        def inspect(_conn, _cursor, statement, _params, _context, _many):
            if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                writes.append(statement)

        event.listen(app.state.engine, "before_cursor_execute", inspect)
        for _ in range(2):
            for endpoint in [
                "operations/summary",
                "operations/queue",
                "data/freshness",
                "data/acceptance",
                "strategies/selection",
                "dashboard",
            ]:
                assert client.get(f"/api/v1/{endpoint}").status_code == 200
        assert writes == []


def test_reconciliation_is_durable_and_sending_happens_only_after_commit(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'ops.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    settings = Settings(app_env="test")
    with factory.begin() as session:
        reconcile_operations(session, settings)
    with factory.begin() as session:
        first = session.scalars(select(NotificationOutboxRecord)).all()
        ids = {r.event_id for r in first}
        assert ids
        assert AuditRepository(session).queue_items()
    # A new session models a worker restart with no in-memory deduplicator.
    with factory.begin() as session:
        reconcile_operations(session, settings)
        assert {r.event_id for r in session.scalars(select(NotificationOutboxRecord))} == ids
    delivered = []

    def fail(_path, message):
        return False

    def send(_path, message):
        delivered.append(message)
        return True

    assert dispatch_notifications(factory, Path("fake"), sender=fail) == 0
    assert dispatch_notifications(factory, Path("fake"), sender=send) == len(ids)
    assert dispatch_notifications(factory, Path("fake"), sender=send) == 0
    assert len(delivered) == len(ids)
    engine.dispose()


def test_archived_failure_is_not_runtime_qualification_and_tampering_invalidates(tmp_path):
    evidence = research_evidence("dvl-tw-v2-shadow")
    assert evidence["evidence_status"] == "verified-archive"
    assert evidence["selected_status"] == "unverified"
    assert evidence["profitability_demonstrated"] is False
    assert next(r for r in evidence["records"] if r["id"] == "rearm")["q7_pass"] is False
    for p in REFERENCE.glob("*"):
        shutil.copy2(p, tmp_path / p.name)
    (tmp_path / "rearm-results.json").write_text("{}", encoding="utf-8")
    assert research_evidence("drawdown-budget:rearm", root=tmp_path)["records"] == []


def test_changed_configuration_never_inherits_research_qualification(tmp_path):
    for p in REFERENCE.glob("*"):
        shutil.copy2(p, tmp_path / p.name)
    path = tmp_path / "research-registry.json"
    registry = json.loads(path.read_bytes())
    registry["records"][0]["identity"]["policy_config"]["cash_trigger"] = 0.39
    path.write_text(json.dumps(registry), encoding="utf-8")
    assert (
        research_evidence("drawdown-budget:rearm", root=tmp_path)["evidence_status"]
        == "invalid-or-missing"
    )
