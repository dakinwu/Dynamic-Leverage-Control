import asyncio
import base64
import hashlib
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot
from dynamic_leverage.infrastructure.providers.vix import parse_vix_close
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services.freshness import freshness_rows
from dynamic_leverage.services.ingestion import ingest_official_http_snapshot
from dynamic_leverage.services.research_evidence import research_evidence

SOURCE = "https://www.taifex.com.tw/cht/7/getVixData?filesname=20260924"
BODY = "20260924\t13450000\t23.12\n20260924\tLast 1 min AVG\t23.12\n"
TZ = ZoneInfo("Asia/Taipei")


def test_close_is_bound_to_official_url_and_body_date():
    assert parse_vix_close(BODY, SOURCE) == (date(2026, 9, 24), Decimal("23.12"))


def test_vix_ingestion_keeps_raw_bytes_and_does_not_invent_publication_time(tmp_path):
    url = f"sqlite:///{(tmp_path / 'vix.db').as_posix()}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    raw = BODY.encode()
    received = datetime(2026, 9, 28, 16, tzinfo=TZ)
    snapshot = HttpSnapshot(SOURCE, received, raw, "text/plain", hashlib.sha256(raw).hexdigest())
    result = asyncio.run(
        ingest_official_http_snapshot(
            database_url=url, kind="market.taifex_vix.official", snapshot=snapshot
        )
    )
    assert result.created
    with make_session_factory(engine)() as session:
        row = AuditRepository(session).latest_raw_snapshot(kind="market.taifex_vix.official")
        assert row.payload["source_trade_date"] == "2026-09-24"
        assert row.payload["close_vix"] == "23.12"
        assert base64.b64decode(row.payload["body_base64"]) == raw
        assert row.observed_at.date() == date(2026, 9, 24)
        assert row.received_at.date() == date(2026, 9, 28)
        assert row.published_at is None
    engine.dispose()


@pytest.mark.parametrize(
    "body,source",
    [
        (BODY.replace("20260924", "20260923"), SOURCE),
        (BODY + "20260923\t13450000\t22\n", SOURCE),
        (BODY + BODY, SOURCE),
        ("20260924\t12000000\t23.12", SOURCE),
        (BODY.replace("23.12", "NaN"), SOURCE),
        (BODY, SOURCE.replace("www.taifex.com.tw", "example.com")),
        (BODY, SOURCE.replace("getVixData", "vixMinNew")),
    ],
)
def test_mismatch_intraday_ambiguous_or_unofficial_vix_is_rejected(body, source):
    with pytest.raises(ValueError):
        parse_vix_close(body, source)


@pytest.mark.parametrize(
    "now,body,status",
    [
        (datetime(2026, 9, 28, 16, tzinfo=TZ), BODY, "FRESH"),
        (datetime(2026, 9, 29, 14, 59, tzinfo=TZ), BODY, "FRESH"),
        (datetime(2026, 9, 29, 15, tzinfo=TZ), BODY, "STALE"),
        (datetime(2026, 9, 28, 16, tzinfo=TZ), "no dated close", "UNKNOWN"),
        (datetime(2027, 1, 5, 16, tzinfo=TZ), BODY, "UNKNOWN"),
    ],
)
def test_vix_freshness_uses_session_even_when_legacy_record_was_redownloaded(now, body, status):
    snapshot = SimpleNamespace(
        observed_at=now,
        received_at=now,
        source=SOURCE,
        snapshot_id="vix",
        payload={"body_text": body},
    )
    contract = SimpleNamespace(
        contract_key="taifex_vix",
        kind="market.taifex_vix.official",
        source="TAIFEX",
        owner="TAIFEX",
        max_age_minutes=1440,
    )
    repo = SimpleNamespace(
        list_freshness_contracts=lambda: [contract], latest_raw_snapshot=lambda **kwargs: snapshot
    )
    row = freshness_rows(repo, now=now)[0]
    assert row.status == status
    if status == "FRESH":
        assert row.source_trade_date == row.expected_trade_date == date(2026, 9, 24)


def test_comparison_keeps_paired_benchmark_and_unchanged_engineering_stage():
    comparison = research_evidence("dvl-tw-v2-shadow")["comparison"]
    stages = comparison["stages"]
    assert stages[1]["normal"] == stages[2]["normal"]
    assert stages[1]["stress"] == stages[2]["stress"]
    for row in stages:
        for condition in ("normal", "stress"):
            value, baseline = row[condition], stages[-1][condition]
            assert value["cagr_gap_pp"] == pytest.approx(100 * (value["cagr"] - baseline["cagr"]))
    assert stages[1]["normal"]["cagr_gap_pp"] < 0
    assert stages[1]["normal"]["volatility_gap_pp"] > 0
    assert stages[1]["stress"]["drawdown_gap_pp"] > 0
    assert comparison["new_out_of_sample"] is False
