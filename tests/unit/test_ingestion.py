import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dynamic_leverage.infrastructure.database import Base, make_engine
from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot
from dynamic_leverage.infrastructure.providers.twse import TwseDailyQuote, TwseDailyQuoteSnapshot
from dynamic_leverage.services.ingestion import ingest_twse_snapshot


class FakeTwseClient:
    async def daily_quote_snapshot(self) -> TwseDailyQuoteSnapshot:
        now = datetime.now(UTC)
        return TwseDailyQuoteSnapshot(
            raw=HttpSnapshot(
                source="https://example.test/twse",
                received_at=now,
                body=b"stable-response",
                content_type="application/json",
                checksum="b" * 64,
            ),
            quotes=(
                TwseDailyQuote(
                    code="0050",
                    name="Taiwan 50",
                    close=Decimal("60"),
                    volume=100,
                    trade_value=Decimal("6000"),
                ),
            ),
        )


def test_twse_ingestion_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "ingestion.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    client = FakeTwseClient()

    first = asyncio.run(
        ingest_twse_snapshot(
            database_url=database_url,
            codes={"0050"},
            client=client,  # type: ignore[arg-type]
        )
    )
    second = asyncio.run(
        ingest_twse_snapshot(
            database_url=database_url,
            codes={"0050"},
            client=client,  # type: ignore[arg-type]
        )
    )

    assert first.created is True
    assert second.created is False
    assert first.snapshot_id == second.snapshot_id
    assert first.item_count == 1


def test_twse_ingestion_fails_closed_when_a_code_is_missing(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"

    with pytest.raises(RuntimeError, match="00631L"):
        asyncio.run(
            ingest_twse_snapshot(
                database_url=database_url,
                codes={"0050", "00631L"},
                client=FakeTwseClient(),  # type: ignore[arg-type]
            )
        )
