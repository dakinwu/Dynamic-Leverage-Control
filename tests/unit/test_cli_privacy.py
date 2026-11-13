"""Snapshot provenance must not carry the operator's local filesystem layout."""

import json

from sqlalchemy import select

from dynamic_leverage import cli
from dynamic_leverage.config import get_settings
from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.tables import RawSnapshotRecord


def test_manual_valuation_source_records_only_the_file_name(monkeypatch, tmp_path) -> None:
    private_dir = tmp_path / "Users" / "operator-name" / "private"
    private_dir.mkdir(parents=True)
    csv_file = private_dir / "valuation-2026-11.csv"
    csv_file.write_text(
        "as_of,pe_ttm,market_eps,source,source_version\n2026-10-31,20.5,1100,twse,1\n",
        encoding="utf-8",
    )
    database = tmp_path / "cli.db"
    engine = make_engine(f"sqlite:///{database.as_posix()}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    try:
        cli._ingest_valuation(csv_file)
        with make_session_factory(engine)() as session:
            row = session.scalar(select(RawSnapshotRecord))
    finally:
        get_settings.cache_clear()
        engine.dispose()

    assert row.source == "manual-file:valuation-2026-11.csv"
    stored = row.source + json.dumps(row.payload, ensure_ascii=False)
    assert "operator-name" not in stored
    assert str(tmp_path) not in stored
