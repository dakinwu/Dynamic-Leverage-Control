"""Capture one official month in an isolated acceptance database, without broker access."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from dynamic_leverage.infrastructure.database import (
    Base,
    make_engine,
    make_session_factory,
)
from dynamic_leverage.infrastructure.providers.official_valuation import (
    OfficialValuationClient,
)
from dynamic_leverage.services.ingestion import ingest_official_valuation_snapshot
from dynamic_leverage.services.valuation_evidence import observation_inventory


async def capture(output: Path, month: date):
    output.mkdir(parents=True, exist_ok=False)
    before = datetime.now(UTC) - timedelta(seconds=1)
    client = OfficialValuationClient()
    values = await client.backfill_months(months=1, end=month, max_month_attempts=1)
    if len(values) != 1:
        raise ValueError(f"official month capture failed: {client.backfill_failures}")
    snapshot = values[0]
    for role, source in (("valuation", snapshot.valuation), ("index", snapshot.index)):
        (output / f"{role}.raw").write_bytes(source.body)
        (output / f"{role}.provenance.json").write_text(
            json.dumps(
                {
                    "source": source.source,
                    "received_at": source.received_at.isoformat(),
                    "sha256": source.checksum,
                    "published_at": None,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    url = f"sqlite+pysqlite:///{(output / 'evidence.db').resolve().as_posix()}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    try:
        first = await ingest_official_valuation_snapshot(database_url=url, snapshot=snapshot)
        repeated = await ingest_official_valuation_snapshot(database_url=url, snapshot=snapshot)
        with make_session_factory(engine)() as session:
            past = observation_inventory(session, cutoff=before)
            now = observation_inventory(session, cutoff=datetime.now(UTC))
        assert first.created and not repeated.created
        assert first.snapshot_id == repeated.snapshot_id
        assert past["observation_count"] == 0 and now["observation_count"] == 2
        assert all(len(s["versions"]) == 1 for s in now["sources"])
        result = {
            "month": str(month)[:7],
            "captured_at": datetime.now(UTC).isoformat(),
            "database_scope": "isolated acceptance only, not scheduler database",
            "historical_cutoff": past,
            "current_inventory": now,
            "duplicate_ingestion_idempotent": True,
            "raw_files_sha256": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.glob("*.raw")
            },
            "strategy_inputs_changed": False,
            "profitability_demonstrated": False,
        }
        (output / "acceptance.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {"observations": 2, "past_observations": 0, "duplicate_ingestion_idempotent": True}
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(capture(args.output, args.month))
