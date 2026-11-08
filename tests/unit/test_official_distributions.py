from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.infrastructure.providers.official_distributions import (
    HEADERS,
    parse_distributions,
)
from dynamic_leverage.services.account_replay import AccountReplay, Quote, ReplayConfig, ReplayDay

URL = (
    "https://www.twse.com.tw/en/ETFortune-institute/dividendList"
    "?startDate=2016&endDate=2026&stkNo=0050"
)
ROW = ["0050", "ETF", "2025/07/21", "2025/08/08", "0.36", "2025"]


def page(rows):
    header = "<tr>" + "".join(f"<th>{h}</th>" for h in HEADERS) + "</tr>"
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table>{header}<tbody>{body}</tbody></table>".encode()


def test_payment_date_and_unadjusted_cash_are_distinct_from_ex_date():
    event = parse_distributions(page([ROW]), URL, "0050", 2016, 2026)[date(2025, 7, 21)]
    assert event.payment_date == date(2025, 8, 8)
    assert event.amount == D("0.36")


@pytest.mark.parametrize(
    "column,value",
    [
        (0, "00631L"),
        (2, "2015/07/21"),
        (2, "2025/02/30"),
        (3, "2025/07/20"),
        (3, ""),
        (4, "NaN"),
        (4, "-1"),
        (4, "N/A"),
    ],
)
def test_reject_invalid_distribution_row(column, value):
    row = ROW.copy()
    row[column] = value
    with pytest.raises(ValueError):
        parse_distributions(page([row]), URL, "0050", 2016, 2026)


@pytest.mark.parametrize(
    "body", [b"<html>Access denied</html>", page([ROW, ROW]), page([]) + page([]), page([ROW])[:-8]]
)
def test_reject_missing_duplicate_or_truncated_table(body):
    with pytest.raises(ValueError):
        parse_distributions(body, URL, "0050", 2016, 2026)


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("www.twse.com.tw", "example.com"),
        URL.replace("2016", "2020"),
        URL + "&stkNo=00631L",
    ],
)
def test_reject_wrong_source_or_query(url):
    with pytest.raises(ValueError):
        parse_distributions(page([ROW]), url, "0050", 2016, 2026)


def test_valid_empty_official_table_is_not_download_failure():
    url = URL.replace("0050", "00631L")
    assert parse_distributions(page([]), url, "00631L", 2016, 2026) == {}


def test_buy_on_ex_date_gets_no_dividend_and_selling_after_ex_date_preserves_receivable():
    cfg = ReplayConfig(
        mode="ungated",
        initial_nav=D(1000),
        commission=D(0),
        sell_tax=D(0),
        slippage=D(0),
        minimum_commission=D(0),
        no_trade_band=D(0),
    )
    rows = [
        ReplayDay(
            date(2025, 7, 1) + timedelta(days=i),
            {s: Quote(D(10), 10000) for s in ("0050", "00631L")},
            D(1),
        )
        for i in range(7)
    ]
    rows[1] = replace(rows[1], dividends={"0050": (D(1), rows[5].day)})
    bought_ex_day = AccountReplay(cfg).run(rows)
    assert not any(e["kind"] == "dividend_entitlement" for e in bought_ex_day.events)
    rows[1] = replace(rows[1], dividends={})
    rows[2] = replace(rows[2], dividends={"0050": (D(1), rows[5].day)}, target=D(0))
    rows[3:] = [replace(r, target=D(0)) for r in rows[3:]]
    sold = AccountReplay(cfg).run(rows)
    assert sold.daily[3]["quantity_0050"] == 0
    assert any(
        e["kind"] == "dividend_paid" and e["amount"] == 100 and e["date"] == rows[5].day
        for e in sold.events
    )
    assert not any(e["kind"] == "dividend_paid" and e["date"] < rows[5].day for e in sold.events)
