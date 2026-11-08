"""Strict acceptance of the reviewed public 00685L UI workbook capture."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from io import BytesIO

from openpyxl import load_workbook

from dynamic_leverage.services.nav_proxy_research import NavPoint

NAV_SCALE_DAY = date(2026, 7, 1)
RESUME = date(2026, 7, 7)
FACTOR = D(24)
ANCHORS = {date(2026, 6, 30): D("306.70"), NAV_SCALE_DAY: D("13.02")}


def parse_capital_nav(body: bytes, capture: dict, calendar: list[date]) -> dict[date, D]:
    if (
        capture.get("symbol") != "00685L"
        or capture.get("source_url")
        != "https://www.capitalfund.com.tw/etf/product/detail/094/trend"
        or capture.get("observed_page_heading") != "00685L 群益臺灣加權正2ETF"
        or capture.get("observed_selected_option") != "00685L 群益臺灣加權正2"
        or capture.get("observed_input_ids") != ["nav-start-time-2", "nav-end-time-2"]
        or capture.get("query_start") != str(calendar[0])
        or capture.get("query_end") != str(calendar[-1])
    ):
        raise ValueError("reviewed UI identity and exact query period required")
    book = load_workbook(BytesIO(body), data_only=True, read_only=True)
    try:
        if book.sheetnames != ["data"]:
            raise ValueError("reviewed single worksheet required")
        sheet = book["data"]
        rows = list(sheet.values)
        expected_headers = [["群益臺灣加權正2", None], ["日期", "淨值"]]
        if (
            sheet.max_column != 2
            or [list(r) for r in rows[:2]] != expected_headers
            or capture.get("observed_workbook_headers") != expected_headers
        ):
            raise ValueError("reviewed workbook headers and exact two columns required")
    finally:
        book.close()
    values = {}
    for raw_day, raw_value in rows[2:]:
        if not isinstance(raw_day, str) or not isinstance(raw_value, str):
            raise ValueError("reviewed literal date/value cells required")
        day = date.fromisoformat(raw_day.replace("/", "-"))
        value = D(raw_value)
        if day in values or not value.is_finite() or value <= 0:
            raise ValueError("unique dates and finite positive NAV required")
        values[day] = value
    if list(values) != calendar:
        raise ValueError("complete ordered exact valuation calendar required")
    if any(values.get(day) != value for day, value in ANCHORS.items()):
        raise ValueError("issuer split and performance anchor mismatch")
    return values


def capital_points(values: dict[date, D], reference: list[NavPoint]) -> list[NavPoint]:
    """Change economic entitlement units with raw NAV scale, block halt fills."""
    if list(values) != [p.day for p in reference]:
        raise ValueError("exact reference calendar required")
    return [
        NavPoint(
            p.day,
            values[p.day],
            p.day,
            fill_available=not NAV_SCALE_DAY <= p.day < RESUME,
            market_volatility=p.market_volatility,
            split=FACTOR if p.day == NAV_SCALE_DAY else D(1),
        )
        for p in reference
    ]
