from datetime import date
from decimal import Decimal as D
from io import BytesIO

import pytest
from openpyxl import Workbook

from dynamic_leverage.services.capital_nav_research import capital_points, parse_capital_nav
from dynamic_leverage.services.nav_proxy_research import NavPoint, ProxyConfig, simulate

DAYS = [date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 7)]
CAPTURE = {
    "symbol": "00685L",
    "source_url": "https://www.capitalfund.com.tw/etf/product/detail/094/trend",
    "observed_page_heading": "00685L 群益臺灣加權正2ETF",
    "observed_selected_option": "00685L 群益臺灣加權正2",
    "observed_input_ids": ["nav-start-time-2", "nav-end-time-2"],
    "query_start": str(DAYS[0]),
    "query_end": str(DAYS[-1]),
    "observed_workbook_headers": [["群益臺灣加權正2", None], ["日期", "淨值"]],
}


def workbook(rows=None):
    book = Workbook()
    sheet = book.active
    sheet.title = "data"
    for row in CAPTURE["observed_workbook_headers"]:
        sheet.append(row)
    for row in rows or list(
        zip(
            [str(d).replace("-", "/") for d in DAYS],
            ["306.70", "13.02", "12.68", "12.16"],
            strict=True,
        )
    ):
        sheet.append(row)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def test_reviewed_ui_workbook_has_exact_calendar_and_independent_anchors():
    values = parse_capital_nav(workbook(), CAPTURE, DAYS)
    assert values[DAYS[0]] == D("306.70")
    assert values[DAYS[1]] == D("13.02")


def test_wrong_fund_is_rejected_even_if_workbook_header_matches():
    with pytest.raises(ValueError, match="UI identity"):
        parse_capital_nav(workbook(), {**CAPTURE, "symbol": "00675L"}, DAYS)


@pytest.mark.parametrize(
    "rows,match",
    [
        ([("2026/06/30", "306.70"), ("2026/06/30", "306.70")], "unique dates"),
        ([("2026/06/30", "NaN")], "finite positive"),
        ([("2026/06/30", "306.70")], "exact valuation calendar"),
        (
            [
                (str(d).replace("-", "/"), v)
                for d, v in zip(DAYS, ["306.70", "312.49", "12.68", "12.16"], strict=True)
            ],
            "anchor mismatch",
        ),
    ],
)
def test_bad_values_missing_dates_and_wrong_split_scale_are_rejected(rows, match):
    with pytest.raises(ValueError, match=match):
        parse_capital_nav(workbook(rows), CAPTURE, DAYS)


def test_entitlements_change_with_nav_scale_and_halt_blocks_fills():
    values = parse_capital_nav(workbook(), CAPTURE, DAYS)
    ref = [NavPoint(d, D(1), d, market_volatility=D(".1")) for d in DAYS]
    data = capital_points(values, ref)
    assert [p.fill_available for p in data] == [True, False, False, True]
    assert [p.split for p in data] == [D(1), D(24), D(1), D(1)]
    _, daily, fills = simulate(data, "buyhold", ProxyConfig())
    assert fills[0]["date"] == DAYS[-1]
    assert all(row["units"] == 0 for row in daily[:-1])


def test_owned_units_preserve_split_wealth_before_trading_resumes():
    day0, day1 = date(2026, 6, 26), date(2026, 6, 29)
    days = [day0, day1, *DAYS]
    values = dict(
        zip(
            days, [D("300"), D("300"), D("306.70"), D("13.02"), D("12.68"), D("12.16")], strict=True
        )
    )
    ref = [NavPoint(d, D(1), d, market_volatility=D(".1")) for d in days]
    _, daily, fills = simulate(
        capital_points(values, ref),
        "buyhold",
        ProxyConfig(commission=D(0), minimum_fee=D(0), sell_tax=D(0), slippage=D(0)),
    )
    assert daily[3]["units"] == daily[2]["units"] * 24
    assert daily[3]["nav"] / daily[2]["nav"] > D(".99")
    assert not any(DAYS[1] <= f["date"] < DAYS[-1] for f in fills)
