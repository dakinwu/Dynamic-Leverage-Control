from __future__ import annotations

import json
import re
from datetime import date, timedelta

from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot, HttpSnapshotClient
from dynamic_leverage.infrastructure.providers.official_valuation import parse_taiex_index
from dynamic_leverage.infrastructure.providers.vix import parse_vix_close
from dynamic_leverage.services.calendar import expected_close_date, is_trading_day

# These are official, end-of-day oriented endpoints.  They are used for
# immutable evidence collection; the broker stream remains the live quote
# source for any future execution path.
TWSE_TAIEX_INDEX = "https://openapi.twse.com.tw/v1/indicesReport/MI_INDEX"
TAIFEX_VIX_DAILY = "https://www.taifex.com.tw/cht/7/vixMinNew"
TAIFEX_VIX_DOWNLOAD = "https://www.taifex.com.tw/cht/7/getVixData"
YUANTA_00631L_PCF = "https://www.yuantaetfs.com/tradeInfo/pcf/00631L"
CAPITAL_00685L_PCF = "https://www.capitalfund.com.tw/etf/product/detail/094/portfolio"


class OfficialIndicesClient:
    def __init__(self, snapshot_client: HttpSnapshotClient | None = None) -> None:
        self.http = snapshot_client or HttpSnapshotClient()

    async def taiex_snapshot(self, *, as_of: date | None = None) -> HttpSnapshot:
        requested = as_of or expected_close_date()
        for offset in range(7):
            day = requested - timedelta(days=offset)
            if not is_trading_day(day):
                continue
            url = (
                "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
                f"?date={day:%Y%m%d}&type=IND&response=json"
            )
            snapshot = await self.http.fetch(url)
            payload = json.loads(snapshot.body)
            if isinstance(payload, dict) and payload.get("stat") in {
                "很抱歉，沒有符合條件的資料!",
                "很抱歉，沒有符合條件的資料！",
            }:
                continue
            observed, _ = parse_taiex_index(snapshot.body, snapshot.content_type)
            if observed != day:
                raise ValueError("official index response date differs from requested date")
            return snapshot
        raise ValueError("no verified index close in the last seven calendar days")

    async def vix_snapshot(self) -> HttpSnapshot:
        # The public page is only an index of downloadable daily files.  The
        # actual VIX observations (including the official last-minute average)
        # live in the Big5-encoded file behind getVixData.
        page = await self.http.fetch(
            TAIFEX_VIX_DAILY,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,*/*",
            },
        )
        file_names = re.findall(rb"getVixData\?filesname=(\d{8})", page.body)
        if not file_names:
            raise ValueError("TAIFEX VIX page has no downloadable daily file")
        latest = max(file_names).decode("ascii")
        snapshot = await self.http.fetch(
            f"{TAIFEX_VIX_DOWNLOAD}?filesname={latest}",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/plain,application/octet-stream,*/*",
                "Referer": TAIFEX_VIX_DAILY,
            },
        )
        parse_vix_close(snapshot.body, snapshot.source)
        return snapshot

    async def yuanta_00631l_pcf_snapshot(self) -> HttpSnapshot:
        return await self.http.fetch(YUANTA_00631L_PCF)

    async def capital_00685l_pcf_snapshot(self) -> HttpSnapshot:
        return await self.http.fetch(CAPITAL_00685L_PCF)

    async def twse_distributions_snapshot(self, symbol: str, year: int) -> HttpSnapshot:
        """TWSE ETF cash-distribution table (ex-date, payment date, amount)."""
        from dynamic_leverage.services.corporate_actions import distribution_url

        return await self.http.fetch(distribution_url(symbol, year))
