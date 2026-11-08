import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot
from dynamic_leverage.infrastructure.providers.official_indices import (
    TAIFEX_VIX_DAILY,
    TAIFEX_VIX_DOWNLOAD,
    OfficialIndicesClient,
    expected_close_date,
)


class FakeHttpClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, url: str, *, headers: dict[str, str] | None = None) -> HttpSnapshot:
        self.calls.append(url)
        now = datetime.now(UTC)
        if url == TAIFEX_VIX_DAILY:
            body = b"<a onClick=\"window.open('getVixData?filesname=20260903')\">x</a>"
            body += b"<a onClick=\"window.open('getVixData?filesname=20260904')\">x</a>"
            return HttpSnapshot(url, now, body, "text/html", "a" * 64)
        return HttpSnapshot(
            url,
            now,
            b"20260904\tLast 1 min AVG\t\t\t24.00\r\n",
            "application/octet-stream",
            "b" * 64,
        )


@pytest.mark.asyncio
async def test_vix_snapshot_follows_latest_download_link() -> None:
    client = FakeHttpClient()
    snapshot = await OfficialIndicesClient(client).vix_snapshot()  # type: ignore[arg-type]

    assert snapshot.source == f"{TAIFEX_VIX_DOWNLOAD}?filesname=20260904"
    assert client.calls == [
        TAIFEX_VIX_DAILY,
        f"{TAIFEX_VIX_DOWNLOAD}?filesname=20260904",
    ]


@pytest.mark.asyncio
async def test_index_fetches_completed_date_instead_of_lagging_latest_feed() -> None:
    class DailyHttp:
        async def fetch(self, url, **kwargs):
            day = "20260915" if "date=20260915" in url else "20260914"
            body = json.dumps(
                {"date": day, "data": [{"指數": "發行量加權股價指數", "收盤指數": "45000"}]}
            ).encode()
            return HttpSnapshot(url, datetime.now(UTC), body, "application/json", day)

    snapshot = await OfficialIndicesClient(DailyHttp()).taiex_snapshot(as_of=date(2026, 9, 15))
    assert "date=20260915" in snapshot.source


def test_recent_download_does_not_make_old_market_date_fresh() -> None:
    from dynamic_leverage.api.app import _freshness_rows

    old = expected_close_date() - timedelta(days=1)
    snapshot = SimpleNamespace(
        observed_at=datetime.now(UTC),
        snapshot_id="old-market",
        payload={
            "body": [
                {"日期": old.strftime("%Y%m%d"), "指數": "發行量加權股價指數", "收盤指數": "45000"}
            ]
        },
    )
    contract = SimpleNamespace(
        contract_key="taiex_index",
        kind="market.taiex_index.official",
        source="TWSE",
        owner="TWSE",
        max_age_minutes=1440,
    )
    repository = SimpleNamespace(
        list_freshness_contracts=lambda: [contract], latest_raw_snapshot=lambda **kwargs: snapshot
    )
    assert _freshness_rows(repository)[0].status == "STALE"


def test_before_close_and_weekend_use_previous_completed_weekday() -> None:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Taipei")
    assert expected_close_date(datetime(2026, 9, 16, 3, tzinfo=tz)) == date(2026, 9, 15)
    assert expected_close_date(datetime(2026, 9, 14, 9, tzinfo=tz)) == date(2026, 9, 11)
