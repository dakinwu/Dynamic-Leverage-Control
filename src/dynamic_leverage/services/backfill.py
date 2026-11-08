from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, text

from dynamic_leverage.domain.valuation import validated_valuation_row
from dynamic_leverage.infrastructure.database import make_engine, make_session_factory
from dynamic_leverage.infrastructure.providers.official_valuation import (
    OfficialValuationClient,
    parse_taiex_index,
)
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.infrastructure.tables import RawSnapshotRecord
from dynamic_leverage.services.ingestion import (
    ingest_official_http_snapshot,
    ingest_official_valuation_snapshot,
)


def _requested_months(months: int, end: date) -> set[tuple[int, int]]:
    last_month = end.year * 12 + end.month
    return {
        (year, zero_month + 1)
        for ordinal in range(last_month - max(months - 1, 0), last_month + 1)
        for year, zero_month in [divmod(ordinal - 1, 12)]
    }


def _valuation_month(payload: dict[str, object]) -> tuple[int, int] | None:
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    validated = validated_valuation_row(rows[0])
    return (validated[0].year, validated[0].month) if validated else None


def _existing_valuation_months(database_url: str) -> set[tuple[int, int]]:
    engine = make_engine(database_url)
    try:
        with make_session_factory(engine)() as session:
            records = session.scalars(
                select(RawSnapshotRecord).where(RawSnapshotRecord.kind == "valuation.taiex.manual")
            )
            return {
                (value[0].year, value[0].month)
                for record in records
                for row in record.payload.get("rows", [])
                if (value := validated_valuation_row(row)) is not None and value[0] <= date.today()
            }
    finally:
        engine.dispose()


def _pending_months(database_url: str, missing: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """Least recently attempted first, including after process restarts."""
    engine = make_engine(database_url)
    attempted: dict[tuple[int, int], str] = {}
    try:
        with make_session_factory(engine)() as session:
            records = session.scalars(
                select(RawSnapshotRecord)
                .where(RawSnapshotRecord.kind == "backfill.valuation.attempt")
                .order_by(RawSnapshotRecord.received_at)
            )
            for record in records:
                key = (int(record.payload["year"]), int(record.payload["month"]))
                attempted[key] = record.received_at.isoformat()
    finally:
        engine.dispose()
    return sorted(missing, key=lambda key: (attempted.get(key, ""), key))


def _record_attempt(database_url: str, year: int, month: int) -> None:
    engine = make_engine(database_url)
    try:
        with make_session_factory(engine).begin() as session:
            now = datetime.now(UTC)
            AuditRepository(session).append_raw_snapshot(
                kind="backfill.valuation.attempt",
                source="official-backfill-v2",
                observed_at=now,
                received_at=now,
                quality="operational",
                checksum=uuid4().hex,
                payload={"year": year, "month": month},
            )
    finally:
        engine.dispose()


async def run_official_backfill(
    *,
    database_url: str,
    months: int = 121,
    index_days: int = 90,
    max_month_attempts: int | None = None,
    twse_market_valuation_url: str | None = None,
    mops_market_eps_url: str | None = None,
) -> dict[str, object]:
    """Serialize workers, persist each success, and retain coverage evidence."""
    if not 1 <= months <= 240 or not 0 <= index_days <= 400:
        raise ValueError("invalid backfill range")
    engine = make_engine(database_url)
    try:
        with engine.connect() as lock:
            postgres = engine.dialect.name == "postgresql"
            if postgres and not lock.scalar(text("SELECT pg_try_advisory_lock(734612001)")):
                raise RuntimeError("another official backfill is running")
            try:
                return await _run_batch(
                    database_url,
                    months,
                    index_days,
                    max_month_attempts,
                    twse_market_valuation_url,
                    mops_market_eps_url,
                )
            finally:
                if postgres:
                    lock.execute(text("SELECT pg_advisory_unlock(734612001)"))
    finally:
        engine.dispose()


async def _run_batch(
    database_url: str,
    months: int,
    index_days: int,
    max_month_attempts: int | None,
    report_url: str | None,
    eps_url: str | None,
) -> dict[str, object]:
    client = OfficialValuationClient(
        market_report_url=report_url
        or "https://www.twse.com.tw/zh/trading/statistics/index04.html",
        market_eps_url=eps_url or "https://openapi.twse.com.tw/v1/opendata/t187ap14_L",
    )
    end = await client.latest_published_month()
    if end > date.today().replace(day=1):
        raise ValueError("published report is in the future")
    requested = _requested_months(months, end)
    existing = _existing_valuation_months(database_url) & requested
    pending = _pending_months(database_url, requested - existing)
    if max_month_attempts is not None:
        pending = pending[: max(max_month_attempts, 0)]
    created = seen = index_created = index_seen = 0
    failures: dict[str, str] = {}
    for year, month in pending:
        _record_attempt(database_url, year, month)
        key = f"{year:04d}-{month:02d}"
        try:
            snapshots = await client.backfill_months(months=1, end=date(year, month, 1))
            if not snapshots:
                failures[key] = client.backfill_failures.get(key, "no_valid_snapshot")
                continue
            snapshot = snapshots[0]
            if _valuation_month(snapshot.to_payload()) != (year, month):
                raise ValueError("invalid valuation values or observation period")
            result = await ingest_official_valuation_snapshot(
                database_url=database_url,
                snapshot=snapshot,
            )
            seen += 1
            created += int(result.created)
            # Historical downloads cannot reset the current market freshness clock.
            await ingest_official_http_snapshot(
                database_url=database_url,
                kind="history.taiex_index.official",
                snapshot=snapshot.index,
            )
        except Exception as exc:  # noqa: BLE001 - months are independent
            failures[key] = f"{type(exc).__name__}: {exc}"
        finally:
            print(
                json.dumps(
                    {
                        "event": "backfill_month",
                        "month": key,
                        "success": key not in failures,
                        "reason": failures.get(key),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    for offset in range(index_days):
        requested_date = date.today() - timedelta(days=offset)
        if requested_date.weekday() >= 5:
            continue
        try:
            snapshot = await client.historical_taiex_index(requested_date)
            observed, _ = parse_taiex_index(snapshot.body, snapshot.content_type)
            if observed != requested_date:
                raise ValueError("index response date differs from request")
            result = await ingest_official_http_snapshot(
                database_url=database_url,
                kind="history.taiex_index.official",
                snapshot=snapshot,
            )
            index_seen += 1
            index_created += int(result.created)
        except Exception as exc:  # noqa: BLE001 - holidays/failures remain visible
            failures[requested_date.isoformat()] = type(exc).__name__
        finally:
            await asyncio.sleep(1)
    completed = _existing_valuation_months(database_url) & requested
    return {
        "valuation_requested": len(requested),
        "valuation_existing": len(existing),
        "valuation_attempted": len(pending),
        "valuation_seen": seen,
        "valuation_created": created,
        "valuation_failed": len(pending) - seen,
        "valuation_remaining": len(requested - completed),
        "anchor_month": end.strftime("%Y-%m"),
        "missing_months": [f"{y:04d}-{m:02d}" for y, m in sorted(requested - completed)],
        "failures": failures,
        "index_seen": index_seen,
        "index_created": index_created,
    }
