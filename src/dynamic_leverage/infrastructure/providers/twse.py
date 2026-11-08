from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot, HttpSnapshotClient
from dynamic_leverage.services.calendar import expected_close_date

TWSE_STOCK_DAY_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"


@dataclass(frozen=True, slots=True)
class TwseDailyQuote:
    code: str
    name: str
    close: Decimal
    volume: int
    trade_value: Decimal


@dataclass(frozen=True, slots=True)
class TwseDailyQuoteSnapshot:
    raw: HttpSnapshot
    quotes: tuple[TwseDailyQuote, ...]
    source_trade_date: date | None = None


class TwseClient:
    def __init__(self, snapshot_client: HttpSnapshotClient | None = None) -> None:
        self.http = snapshot_client or HttpSnapshotClient()

    async def daily_quote_snapshot(self, *, as_of: date | None = None) -> TwseDailyQuoteSnapshot:
        requested = as_of or expected_close_date()
        snapshot = await self.http.fetch(
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
            f"?date={requested:%Y%m%d}&type=ALL&response=json"
        )
        payload = json.loads(snapshot.body)
        if not isinstance(payload, dict) or str(payload.get("stat", "")).upper() != "OK":
            raise ValueError("TWSE dated daily quotes not published yet")
        observed = datetime.strptime(str(payload.get("date", "")), "%Y%m%d").date()
        if observed != requested:
            raise ValueError("TWSE quote response date differs from requested date")
        tables = [t for t in payload.get("tables", []) if "證券代號" in t.get("fields", [])]
        if len(tables) != 1:
            raise ValueError("TWSE quote table missing or ambiguous")
        table = tables[0]
        quotes = []
        for values in table["data"]:
            item = dict(zip(table["fields"], values, strict=True))
            code = str(item["證券代號"])
            close = item["收盤價"]
            if not code or close in {None, "", "--"}:
                continue
            quotes.append(
                TwseDailyQuote(
                    code=code,
                    name=str(item["證券名稱"]),
                    close=Decimal(str(close).replace(",", "")),
                    volume=int(str(item["成交股數"]).replace(",", "")),
                    trade_value=Decimal(str(item["成交金額"]).replace(",", "")),
                )
            )
        return TwseDailyQuoteSnapshot(
            raw=snapshot, quotes=tuple(quotes), source_trade_date=observed
        )

    async def daily_quotes(self) -> tuple[TwseDailyQuote, ...]:
        return (await self.daily_quote_snapshot()).quotes

    async def selected_quotes(self, codes: set[str]) -> dict[str, TwseDailyQuote]:
        quotes = await self.daily_quotes()
        return {quote.code: quote for quote in quotes if quote.code in codes}
