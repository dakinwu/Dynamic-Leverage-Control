import asyncio
import base64
import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot
from dynamic_leverage.infrastructure.providers.official_valuation import OfficialValuationSnapshot
from dynamic_leverage.infrastructure.tables import RawSnapshotRecord
from dynamic_leverage.services import valuation_evidence
from dynamic_leverage.services.ingestion import ingest_official_valuation_snapshot
from dynamic_leverage.services.valuation_evidence import (
    KIND,
    append_observation,
    observation_inventory,
)


@pytest.fixture
def db(tmp_path):
    url = f"sqlite+pysqlite:///{(tmp_path / 'evidence.db').as_posix()}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    yield url, make_session_factory(engine)
    engine.dispose()


def response(body=b"PK\x00\xff", received=None, source="https://www.twse.com.tw/report.zip"):
    return HttpSnapshot(
        source,
        received or datetime.now(UTC),
        body,
        "application/zip",
        hashlib.sha256(body).hexdigest(),
    )


def valuation():
    return OfficialValuationSnapshot(
        valuation=response(),
        index=response(b"index", source="https://www.twse.com.tw/index"),
        eps=response(b"[]", source="https://openapi.twse.com.tw/eps#historical-2026-08-31"),
        rows=({"as_of": "2026-08-31", "pe_ttm": "20", "market_eps": "1000"},),
        as_of=date(2026, 8, 31),
        eps_methodology="derived-taiex-close-divided-by-twse-market-pe",
        eps_error="historical EPS unavailable",
    )


def ingest(url, snap):
    return asyncio.run(ingest_official_valuation_snapshot(database_url=url, snapshot=snap))


def set_clock(monkeypatch, value):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return value.astimezone(tz)

    monkeypatch.setattr(valuation_evidence, "datetime", Clock)


def test_raw_zip_preserved_and_synthetic_eps_not_archived(db):
    url, factory = db
    snap = valuation()
    result = ingest(url, snap)
    with factory() as session:
        parsed = session.get(RawSnapshotRecord, result.snapshot_id)
        ids = parsed.payload["source_observation_ids"]
        assert set(ids) == {"valuation", "index"}
        raw = session.get(RawSnapshotRecord, ids["valuation"])
        assert base64.b64decode(raw.payload["body_base64"]) == snap.valuation.body
        assert raw.published_at is None
        assert raw.payload["body_sha256"] == snap.valuation.checksum
        assert parsed.received_at.replace(tzinfo=UTC) == snap.index.received_at


def test_repeated_fetches_preserve_observations_without_duplicating_parsed_value(db):
    url, factory = db
    first = valuation()
    a, b = ingest(url, first), ingest(url, first)
    assert a.created and not b.created and a.snapshot_id == b.snapshot_id
    later = replace(
        first,
        valuation=replace(
            first.valuation, received_at=first.valuation.received_at + timedelta(seconds=1)
        ),
        index=replace(first.index, received_at=first.index.received_at + timedelta(seconds=1)),
    )
    assert not ingest(url, later).created
    with factory() as session:
        result = observation_inventory(session, cutoff=datetime.now(UTC) + timedelta(seconds=2))
        assert result["observation_count"] == 4
        assert len(result["sources"]) == 2
        assert all(len(s["versions"]) == 1 and s["observations"] == 2 for s in result["sources"])


def test_revision_and_reversion_select_only_versions_known_at_cutoff(db, monkeypatch):
    _, factory = db
    start = datetime(2026, 10, 1, tzinfo=UTC)
    set_clock(monkeypatch, start)
    with factory.begin() as session:
        append_observation(session, response(b"original", received=start), "valuation")
    before_revision = start + timedelta(hours=1)
    set_clock(monkeypatch, start + timedelta(days=1))
    with factory.begin() as session:
        append_observation(session, response(b"corrected", received=start), "valuation")
    after_revision = start + timedelta(days=1, hours=1)
    set_clock(monkeypatch, start + timedelta(days=2))
    with factory.begin() as session:
        append_observation(
            session, response(b"original", received=start + timedelta(days=2)), "valuation"
        )
    with factory() as session:
        early = observation_inventory(session, cutoff=before_revision)
        revised = observation_inventory(session, cutoff=after_revision)
        current = observation_inventory(session, cutoff=start + timedelta(days=3))
        assert early["observation_count"] == 1
        assert early["sources"][0]["latest_body_sha256"] == response(b"original").checksum
        assert revised["sources"][0]["latest_body_sha256"] == response(b"corrected").checksum
        assert current["sources"][0]["latest_body_sha256"] == response(b"original").checksum
        assert current["sources"][0]["content_changed"]
        assert len(current["sources"][0]["versions"]) == 2
        assert current["historical_research_ready"] is False


def test_old_receipt_cannot_backdate_newly_archived_evidence(db, monkeypatch):
    _, factory = db
    before_archive = datetime.now(UTC)
    set_clock(monkeypatch, before_archive + timedelta(seconds=1))
    with factory.begin() as session:
        append_observation(
            session, response(received=datetime(2016, 1, 1, tzinfo=UTC)), "valuation"
        )
    with factory() as session:
        assert observation_inventory(session, cutoff=before_archive)["observation_count"] == 0
        assert (
            observation_inventory(session, cutoff=before_archive + timedelta(seconds=2))[
                "observation_count"
            ]
            == 1
        )


def test_source_failure_rolls_back_all_observations_and_parsed_snapshot(db):
    url, factory = db
    snap = valuation()
    with pytest.raises(ValueError, match="checksum"):
        ingest(url, replace(snap, index=replace(snap.index, checksum="bad")))
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(RawSnapshotRecord)) == 0


def test_actual_eps_response_archived(db):
    url, factory = db
    snap = valuation()
    ingest(
        url,
        replace(
            snap,
            eps_error=None,
            eps=response(b"real eps", source="https://openapi.twse.com.tw/eps"),
        ),
    )
    with factory() as session:
        result = observation_inventory(session, cutoff=datetime.now(UTC))
        assert {s["role"] for s in result["sources"]} == {"valuation", "index", "eps"}


def test_unknown_legacy_evidence_and_inventory_are_read_only(db):
    url, factory = db
    ingest(url, valuation())
    with factory.begin() as session:
        before = session.scalar(select(func.count()).select_from(RawSnapshotRecord))
        for _ in range(2):
            result = observation_inventory(session, cutoff=datetime(2000, 1, 1, tzinfo=UTC))
            assert result["observation_count"] == 0
        assert session.scalar(select(func.count()).select_from(RawSnapshotRecord)) == before


@pytest.mark.parametrize("tamper", ["body_base64", "body_sha256", "available_at"])
def test_corrupted_evidence_fails_closed(db, tamper):
    url, factory = db
    ingest(url, valuation())
    with factory.begin() as session:
        row = session.scalars(
            select(RawSnapshotRecord).where(RawSnapshotRecord.kind == KIND)
        ).first()
        row.payload = {
            **row.payload,
            tamper: (
                datetime(2000, 1, 1, tzinfo=UTC).isoformat() if tamper == "available_at" else "YmFk"
            ),
        }
    with factory() as session, pytest.raises(ValueError):
        observation_inventory(session, cutoff=datetime.now(UTC))


def test_naive_timestamps_rejected_and_future_receipts_not_available(db):
    _, factory = db
    with factory.begin() as session, pytest.raises(ValueError, match="timezone"):
        append_observation(session, response(received=datetime(2026, 1, 1)), "valuation")
    with factory.begin() as session:
        append_observation(
            session, response(received=datetime.now(UTC) + timedelta(days=1)), "index"
        )
    with factory() as session:
        assert observation_inventory(session, cutoff=datetime.now(UTC))["observation_count"] == 0
        with pytest.raises(ValueError, match="timezone"):
            observation_inventory(session, cutoff=datetime(2026, 1, 1))


def test_conflicting_versions_at_identical_timestamp_fail_closed(db, monkeypatch):
    _, factory = db
    now = datetime(2026, 10, 1, tzinfo=UTC)
    set_clock(monkeypatch, now)
    with factory.begin() as session:
        append_observation(session, response(b"a", received=now), "valuation")
        append_observation(session, response(b"b", received=now), "valuation")
    with factory() as session, pytest.raises(ValueError, match="ambiguous"):
        observation_inventory(session, cutoff=now)
