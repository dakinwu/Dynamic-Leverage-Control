from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.services.nav_proxy_research import NavPoint, ProxyConfig, simulate


def points(prices, **kwargs):
    start = date(2020, 1, 1)
    return [
        NavPoint(start + timedelta(days=i), D(p), start + timedelta(days=i), **kwargs)
        for i, p in enumerate(prices)
    ]


FREE = dict(commission=D(0), minimum_fee=D(0), sell_tax=D(0), slippage=D(0))


def test_entry_lag_does_not_capture_already_observed_jump():
    metrics, daily, fills = simulate(points([10, 20, 20]), "buyhold", ProxyConfig(**FREE))
    assert metrics["final_nav"] == 500000
    assert daily[0]["units"] == 0
    assert fills[0]["date"] > fills[0]["signal_date"]
    assert fills[0]["units"] == 25000


def test_missing_quote_marks_last_value_but_delays_order():
    data = points([10, 10, 20])
    data[1] = NavPoint(data[1].day, D(10), data[0].day, fill_available=False)
    _, daily, fills = simulate(data, "buyhold", ProxyConfig(**FREE))
    assert daily[1]["price_as_of"] == data[0].day
    assert fills[0]["date"] == data[2].day
    assert fills[0]["units"] == 25000


def test_split_preserves_wealth_and_changes_units():
    data = points([100, 100, 25])
    data[2] = NavPoint(data[2].day, D(25), data[2].day, split=D(4))
    m, daily, _ = simulate(data, "buyhold", ProxyConfig(**FREE), exposure_multiple=D(1))
    assert m["final_nav"] == 500000
    assert daily[-1]["units"] == 20000
    assert m["maximum_drawdown"] == 0


def test_dividend_is_wealth_on_ex_date_but_reinvested_only_after_payment():
    data = points([100, 100, 90, 90, 90, 90])
    data[2] = NavPoint(data[2].day, D(90), data[2].day, dividend=(D(10), data[4].day))
    m, daily, fills = simulate(data, "buyhold", ProxyConfig(**FREE), exposure_multiple=D(1))
    assert daily[2]["nav"] == 500000
    assert daily[2]["cash"] == 0
    assert daily[2]["receivables"] == 50000
    assert daily[4]["cash"] == 50000
    assert len(fills) == 2
    assert fills[1]["date"] == data[5].day
    assert m["final_nav"] == 500000


def test_sales_remain_receivable_for_two_sessions_and_stop_is_permanent():
    data = points([100, 100, 20, 20, 100, 100, 100], market_volatility=D("0.1"))
    m, daily, fills = simulate(data, "static-1.2", ProxyConfig(**FREE))
    assert m["stop_signal_date"] == data[2].day
    assert fills[-1]["side"] == "SELL"
    assert fills[-1]["date"] == data[3].day
    assert daily[3]["cash"] == 200000
    assert daily[3]["receivables"] == 60000
    assert daily[4]["cash"] == 200000
    assert daily[5]["cash"] == 260000
    assert daily[-1]["units"] == 0
    assert m["maximum_drawdown"] == D("0.48")


def test_costs_are_deducted_and_cash_never_negative():
    m, daily, fills = simulate(points([100, 100, 100]), "buyhold", ProxyConfig())
    assert 0 < m["total_cost"] == 500000 - m["final_nav"]
    assert all(row["cash"] >= 0 for row in daily)
    assert fills[0]["units"] < 5000


def test_unknown_volatility_prevents_initial_dynamic_entry():
    _, daily, fills = simulate(points([10, 11, 12]), "vol20", ProxyConfig(**FREE))
    assert not fills
    assert all(row["units"] == 0 for row in daily)


def test_future_price_and_invalid_volatility_fail_closed():
    data = points([10, 10])
    data[0] = NavPoint(data[0].day, D(10), data[1].day)
    with pytest.raises(ValueError, match="nonfuture"):
        simulate(data, "buyhold", ProxyConfig())
    with pytest.raises(ValueError, match="observed volatility"):
        simulate(points([10, 10], market_volatility=D("NaN")), "vol20", ProxyConfig())
