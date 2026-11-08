import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot
from dynamic_leverage.infrastructure.providers.official_valuation import (
    OfficialValuationClient,
    parse_market_eps,
    parse_market_valuation,
    parse_monthly_index,
)


def test_parse_market_valuation_requires_explicit_market_row() -> None:
    body = json.dumps(
        {
            "data": [
                {"類別": "大盤", "日期": "115年08月", "本益比": "22.5"},
            ]
        }
    ).encode()
    as_of, pe = parse_market_valuation(body, "application/json")
    assert as_of == date(2026, 8, 1)
    assert pe == Decimal("22.5")


def test_parse_market_eps_reads_mops_aggregate_row() -> None:
    body = json.dumps(
        [
            {
                "出表日期": "115/08/10",
                "公司名稱": "上市公司合計",
                "基本每股盈餘(元)": "910.5",
            }
        ]
    ).encode()
    as_of, eps = parse_market_eps(body, "application/json")
    assert as_of == date(2026, 8, 10)
    assert eps == Decimal("910.5")


def test_parse_market_valuation_rejects_stock_only_report() -> None:
    body = json.dumps([{"公司名稱": "台積電", "日期": "115/08/10", "本益比": "30"}]).encode()
    with pytest.raises(ValueError, match="explicit"):
        parse_market_valuation(body, "application/json")


def test_parse_market_valuation_reads_html_table() -> None:
    body = """
    <table><tr><th>類別</th><th>日期</th><th>本益比</th></tr>
    <tr><td>大盤</td><td>115年08月</td><td>21.8</td></tr></table>
    """.encode()
    as_of, pe = parse_market_valuation(body, "text/html")
    assert as_of == date(2026, 8, 1)
    assert pe == Decimal("21.8")


@pytest.mark.asyncio
async def test_current_report_skips_http_200_error_page(monkeypatch: pytest.MonkeyPatch) -> None:
    invalid = HttpSnapshot(
        source="https://example.test/unpublished",
        received_at=datetime.now(UTC),
        body=b"<html>not published</html>",
        content_type="text/html",
        checksum="a" * 64,
    )
    valid = HttpSnapshot(
        source="https://example.test/published",
        received_at=datetime.now(UTC),
        body=json.dumps(
            {"data": [{"類別": "大盤", "日期": "115年08月", "本益比": "22.5"}]}
        ).encode(),
        content_type="application/json",
        checksum="b" * 64,
    )
    client = OfficialValuationClient()
    calls = 0

    async def fake_historical(_year: int, _month: int) -> HttpSnapshot:
        nonlocal calls
        calls += 1
        return invalid if calls == 1 else valid

    monkeypatch.setattr(client, "historical_monthly_report", fake_historical)
    result = await client._fetch_monthly_report()

    assert result is valid
    assert calls == 2


@pytest.mark.asyncio
async def test_backfill_skips_existing_months_and_throttles_failed_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OfficialValuationClient()
    calls: list[tuple[int, int]] = []
    sleeps: list[float] = []

    async def unavailable_report(year: int, month: int) -> HttpSnapshot:
        calls.append((year, month))
        raise RuntimeError("temporarily unavailable")

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client, "historical_monthly_report", unavailable_report)
    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    result = await client.backfill_months(
        months=4,
        end=date(2020, 4, 1),
        pause_seconds=0.5,
        exclude_months={(2020, 1)},
        max_month_attempts=2,
    )

    assert result == []
    assert calls == [(2020, 2), (2020, 3)]
    assert sleeps == [0.5, 0.5]


def test_monthly_index_rejects_wrong_month_and_uses_last_trading_date() -> None:
    body = json.dumps(
        {
            "stat": "OK",
            "fields": ["日期", "收盤指數"],
            "data": [["109/02/27", "11,000"], ["109/02/26", "10,900"]],
        }
    ).encode()
    assert parse_monthly_index(body, 2020, 2)[-1] == (date(2020, 2, 27), Decimal("11000"))
    with pytest.raises(ValueError, match="different month"):
        parse_monthly_index(body, 2020, 3)
