from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.domain.backtest import BacktestEngine, BacktestPoint
from dynamic_leverage.domain.execution import OneXTwoXRecipe
from dynamic_leverage.domain.models import StrategyInputs
from dynamic_leverage.services.historical_study import (
    HistoricalPrice,
    lagged_points,
    month_end,
    official_total_returns,
    research_inputs,
    research_targets,
)


def test_official_return_reinvests_cash_dividend_and_preserves_split_value():
    days = [date(2020, 1, i) for i in [1, 2, 3, 4]]
    quotes = {days[0]: (D(100), 100), days[1]: (D(95), 100), days[3]: (D(25), 400)}
    prices = official_total_returns(quotes, {days[1]: D(5)}, {days[3]: D(4)}, days, {days[2]})
    assert prices[days[1]].adjusted_close == 100
    assert prices[days[2]].adjusted_close == 100
    assert prices[days[2]].volume == 0
    assert prices[days[3]].adjusted_close == D(100) * D(100) / D(95)


def test_missing_official_close_cannot_be_forward_filled_without_documented_halt():
    days = [date(2020, 1, i) for i in [1, 2]]
    with pytest.raises(ValueError, match="missing official ETF close"):
        official_total_returns({days[0]: (D(100), 100)}, {}, {}, days, set())


def test_future_signal_cannot_earn_same_or_next_close_return():
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(4)]
    one = {d: HistoricalPrice(D(100), 100) for d in days}
    two = {d: HistoricalPrice(D(100 + i * 10), 100) for i, d in enumerate(days)}
    signals = {days[0]: D(1), days[1]: D(2), days[2]: D(1)}
    points = lagged_points(one, two, signals, start=days[1], end=days[-1])
    assert [p.target_exposure for p in points] == [D(1), D(2)]
    assert points[0].trading_date == days[2]
    assert points[0].two_x_return == D(120) / D(110) - 1


def test_suspended_market_cannot_fill_but_existing_position_keeps_returns():
    points = [
        BacktestPoint(date(2020, 1, 1), D(0), D(0), D(1)),
        BacktestPoint(date(2020, 1, 2), D("0.1"), D("0.3"), D(2), rebalance_allowed=False),
        BacktestPoint(date(2020, 1, 3), D(0), D(0), D(2)),
    ]
    result = BacktestEngine(one_way_cost_rate=D(0), sell_tax_rate=D(0)).run(
        points, OneXTwoXRecipe()
    )
    assert result.equity[1].nav == D(550000)
    assert result.equity[1].cost == 0
    assert result.equity[1].turnover == 0
    assert result.equity[2].actual_exposure == 2


def test_monthly_pe_requires_120_prior_months_and_assumed_delay():
    ordinal = 2020 * 12 + 1
    vals = {key: D(10) for key in range(ordinal - 121, ordinal + 1)}
    vals[ordinal] = D(30)
    start = date(2020, 1, 1)
    closes = {start + timedelta(days=i): D(100 + i) for i in range(90)}
    inputs = research_inputs(closes, vals, {}, publication_lag_days=45)
    assert inputs[date(2020, 3, 15)].pe_ttm == 10
    assert inputs[date(2020, 3, 16)].pe_ttm == 30
    assert inputs[date(2020, 3, 16)].pe_percentile == 1
    del vals[ordinal - 100]
    assert not research_inputs(closes, vals, {}, publication_lag_days=45)
    assert month_end(ordinal) == date(2020, 1, 31)


def test_v2_missing_vix_is_rejected_instead_of_imputed():
    day = date(2020, 1, 1)
    inputs = {day: StrategyInputs(day, pe_percentile=D("0.5"), eps_yoy=D(0), rv20=D("0.2"))}
    with pytest.raises(ValueError, match="invalid v2_cash_cap2"):
        research_targets(inputs, version="v2_cash_cap2", start=day, end=day, market_dates=[day])


def test_cash_variant_never_keeps_unexecutable_policy_state():
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(100)]
    inputs = {
        d: StrategyInputs(
            d,
            pe_percentile=D("0.1"),
            eps_yoy=D(0),
            rv20=D("0.1"),
            taiwan_vix=D(10),
            taiex_drawdown=D("-0.4"),
        )
        for d in days
    }
    targets, capped = research_targets(
        inputs, version="v2_cash_cap2", start=days[0], end=days[-1], market_dates=days
    )
    assert max(targets.values()) == 2
    assert capped > 0
