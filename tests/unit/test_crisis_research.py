from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.domain.models import StrategyInputs
from dynamic_leverage.domain.reserve_policy import (
    ReserveAccount,
    ReserveConfig,
    ReservePolicy,
    ReserveState,
)
from dynamic_leverage.services.crisis_research import crisis_inputs, synthetic_prices


def test_daily_reset_is_not_period_return_squared():
    closes = {date(2000, 1, 3): D(100), date(2000, 1, 4): D(110), date(2000, 1, 5): D(100)}
    one, two = synthetic_prices(closes, annual_drag=D(0))[date(2000, 1, 5)]
    assert one == 100
    assert abs(two - D(120) * (1 - D(2) / 11)) < D("1e-24")
    assert two < 100  # round-trip index still loses money at daily 2x


def test_drag_accrues_across_weekends_and_cannot_resurrect_exhausted_fund():
    rows = {date(2000, 1, 7): D(100), date(2000, 1, 10): D(100)}
    assert synthetic_prices(rows, annual_drag=D(".3652425"))[date(2000, 1, 10)][1] == D("99.7")
    rows[date(2000, 1, 10)] = D(49)
    with pytest.raises(ValueError, match="exhausted"):
        synthetic_prices(rows, annual_drag=D(0))


def test_absolute_mode_does_not_require_unused_decade_percentile():
    inputs = StrategyInputs(
        as_of=date(2008, 1, 2), pe_ttm=D(19), eps_yoy=D(0), rv20=D(".2"), taiex_daily_return=D(0)
    )
    account = ReserveAccount(D(500000), D(500000), D(0), D(0), False)
    assert ReservePolicy().evaluate(inputs, account, ReserveState()).desired == D("1.8")
    with pytest.raises(ValueError, match="complete"):
        ReservePolicy(ReserveConfig(valuation="percentile")).evaluate(
            inputs, account, ReserveState()
        )


def test_lagged_inputs_do_not_use_unpublished_month_or_future_prices():
    start = date(2005, 1, 1)
    closes = {start + timedelta(days=i): D(100 + i) for i in range(900)}
    vals = {y * 12 + m: D(20) for y in range(2005, 2008) for m in range(1, 13)}
    day = date(2007, 1, 10)
    before = crisis_inputs({d: p for d, p in closes.items() if d <= day}, vals, lag_days=45)[day]
    vals[2006 * 12 + 12] = D(2000)  # December not published on January 10 under 45-day lag
    after = crisis_inputs(closes, vals, lag_days=45)[day]
    assert before == after
    assert before.pe_ttm == 20 and before.pe_percentile is None
    assert before.eps_yoy is not None
