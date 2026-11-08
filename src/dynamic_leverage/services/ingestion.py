from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from dynamic_leverage.infrastructure.database import make_engine, make_session_factory
from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot
from dynamic_leverage.infrastructure.providers.official_valuation import (
    OfficialValuationSnapshot,
    parse_taiex_index,
)
from dynamic_leverage.infrastructure.providers.twse import TwseClient
from dynamic_leverage.infrastructure.providers.vix import parse_vix_close
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services.valuation_evidence import append_observation


@dataclass(frozen=True, slots=True)
class IngestionResult:
    snapshot_id: str
    created: bool
    item_count: int


def _snapshot_payload(snapshot: HttpSnapshot) -> dict[str, object]:
    """Keep the source response immutable while exposing JSON when available."""
    try:
        decoded = json.loads(snapshot.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "schema": "official-http-raw-v1",
            "content_type": snapshot.content_type,
            "body_sha256": snapshot.checksum,
            "body_text": snapshot.body.decode("utf-8", errors="replace"),
        }
    return {
        "schema": "official-http-json-v1",
        "content_type": snapshot.content_type,
        "body": decoded,
    }


async def ingest_official_http_snapshot(
    *,
    database_url: str,
    kind: str,
    snapshot: HttpSnapshot,
) -> IngestionResult:
    """Persist an official response as an append-only raw snapshot."""
    observed_at = snapshot.received_at
    payload = _snapshot_payload(snapshot)
    if kind == "market.taifex_vix.official":
        trading_date, value = parse_vix_close(snapshot.body, snapshot.source)
        observed_at = datetime.combine(trading_date, time(13, 45), ZoneInfo("Asia/Taipei"))
        payload.update(
            {
                "source_trade_date": trading_date.isoformat(),
                "close_vix": str(value),
                "body_base64": base64.b64encode(snapshot.body).decode("ascii"),
            }
        )
    if kind == "market.taiex_index.official":
        trading_date, _ = parse_taiex_index(snapshot.body, snapshot.content_type)
        observed_at = datetime.combine(trading_date, time(13, 30), ZoneInfo("Asia/Taipei"))
    engine = make_engine(database_url)
    session_factory = make_session_factory(engine)
    try:
        with session_factory.begin() as session:
            row, created = AuditRepository(session).append_raw_snapshot(
                kind=kind,
                source=snapshot.source,
                observed_at=observed_at,
                received_at=snapshot.received_at,
                quality="authoritative",
                checksum=snapshot.checksum,
                payload=payload,
            )
        return IngestionResult(
            snapshot_id=row.snapshot_id,
            created=created,
            item_count=1,
        )
    finally:
        engine.dispose()


async def ingest_official_valuation_snapshot(
    *,
    database_url: str,
    snapshot: OfficialValuationSnapshot,
) -> IngestionResult:
    """Persist the TWSE monthly valuation + MOPS EPS pair as one evidence row."""
    engine = make_engine(database_url)
    session_factory = make_session_factory(engine)
    try:
        with session_factory.begin() as session:
            observations = {
                "valuation": append_observation(session, snapshot.valuation, "valuation"),
                "index": append_observation(session, snapshot.index, "index"),
            }
            # Historical/error placeholders are generated locally, not official EPS responses.
            if not snapshot.eps_error and "#historical-" not in snapshot.eps.source:
                observations["eps"] = append_observation(session, snapshot.eps, "eps")
            payload = snapshot.to_payload()
            payload["source_observation_ids"] = observations
            payload["availability_evidence"] = "source-observations-v1"
            row, created = AuditRepository(session).append_raw_snapshot(
                kind="valuation.taiex.manual",
                source="twse-monthly-report+mops-eps",
                observed_at=snapshot.observed_at,
                received_at=max(
                    snapshot.valuation.received_at,
                    snapshot.eps.received_at,
                    snapshot.index.received_at,
                ),
                quality="authoritative",
                checksum=snapshot.checksum,
                payload=payload,
            )
        return IngestionResult(snapshot_id=row.snapshot_id, created=created, item_count=1)
    finally:
        engine.dispose()


async def ingest_twse_snapshot(
    *,
    database_url: str,
    codes: set[str],
    client: TwseClient | None = None,
) -> IngestionResult:
    quote_snapshot = await (client or TwseClient()).daily_quote_snapshot()
    selected = [quote for quote in quote_snapshot.quotes if quote.code in codes]
    missing = codes - {quote.code for quote in selected}
    if missing:
        raise RuntimeError(f"TWSE response did not contain: {', '.join(sorted(missing))}")

    payload = {
        "schema": "twse-dated-quotes-v2",
        "source_trade_date": (
            quote_snapshot.source_trade_date.isoformat()
            if quote_snapshot.source_trade_date
            else None
        ),
        "published_at": None,
        "publication_evidence": "first_publication_unknown",
        "received_at": quote_snapshot.raw.received_at.isoformat(),
        "raw_body_base64": base64.b64encode(quote_snapshot.raw.body).decode("ascii"),
        "raw_content_type": quote_snapshot.raw.content_type,
        "quotes": [
            {
                "code": quote.code,
                "name": quote.name,
                "close": str(quote.close),
                "volume": quote.volume,
                "trade_value": str(quote.trade_value),
            }
            for quote in selected
        ],
    }
    engine = make_engine(database_url)
    session_factory = make_session_factory(engine)
    try:
        with session_factory.begin() as session:
            row, created = AuditRepository(session).append_raw_snapshot(
                kind="market.twse.daily_quotes",
                source=quote_snapshot.raw.source,
                observed_at=(
                    datetime.combine(
                        quote_snapshot.source_trade_date, time(13, 30), ZoneInfo("Asia/Taipei")
                    )
                    if quote_snapshot.source_trade_date
                    else quote_snapshot.raw.received_at
                ),
                received_at=quote_snapshot.raw.received_at,
                quality="authoritative" if quote_snapshot.source_trade_date else "date_unknown",
                checksum=quote_snapshot.raw.checksum,
                payload=payload,
            )
        return IngestionResult(
            snapshot_id=row.snapshot_id,
            created=created,
            item_count=len(selected),
        )
    finally:
        engine.dispose()
