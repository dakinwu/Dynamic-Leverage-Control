"""Run inside API container: capture valuation only, inspect evidence and safety settings."""

import asyncio
import hashlib
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

import dynamic_leverage
from dynamic_leverage.config import get_settings
from dynamic_leverage.infrastructure.database import make_engine, make_session_factory
from dynamic_leverage.infrastructure.providers.official_valuation import OfficialValuationClient
from dynamic_leverage.infrastructure.tables import RawSnapshotRecord
from dynamic_leverage.services.ingestion import ingest_official_valuation_snapshot
from dynamic_leverage.services.valuation_evidence import observation_inventory


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/" + path, timeout=20) as response:
        return json.load(response)


async def main():
    settings = get_settings()
    system = get("system")
    assert system["production_order_submission"] == "disabled"
    assert system["shioaji_mode"] == "readonly"
    selected = get("strategies/selection")
    assert selected["strategy_version"] == "dvl-tw-v2-shadow"
    client = OfficialValuationClient(
        market_report_url=settings.twse_market_valuation_url,
        market_eps_url=settings.mops_market_eps_url,
    )
    snapshot = await client.snapshot()
    first = await ingest_official_valuation_snapshot(
        database_url=settings.database_url, snapshot=snapshot
    )
    second = await ingest_official_valuation_snapshot(
        database_url=settings.database_url, snapshot=snapshot
    )
    assert not second.created and first.snapshot_id == second.snapshot_id
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            before = session.scalar(select(func.count()).select_from(RawSnapshotRecord))
            inventory = observation_inventory(session, cutoff=datetime.now(UTC))
            observation_inventory(session, cutoff=datetime.now(UTC))
            after = session.scalar(select(func.count()).select_from(RawSnapshotRecord))
        assert before == after
        assert inventory["observation_count"] >= 2
        acceptance = get("data/acceptance")
        assert acceptance["profitability_demonstrated"] is False
        print(
            json.dumps(
                {
                    "checked_at": datetime.now(UTC).isoformat(),
                    "valuation_as_of": str(snapshot.as_of),
                    "system": {
                        k: system[k]
                        for k in (
                            "status",
                            "database",
                            "production_order_submission",
                            "shioaji_mode",
                        )
                    },
                    "selected_strategy": selected["strategy_version"],
                    "duplicate_ingestion_idempotent": True,
                    "read_queries_changed_counts": False,
                    "inventory": inventory,
                    "profitability_demonstrated": False,
                    "runtime_source_sha256": {
                        name: hashlib.sha256(
                            (Path(dynamic_leverage.__file__).parent / name).read_bytes()
                        ).hexdigest()
                        for name in ("services/ingestion.py", "services/valuation_evidence.py")
                    },
                },
                indent=2,
            )
        )
    finally:
        engine.dispose()


asyncio.run(main())
