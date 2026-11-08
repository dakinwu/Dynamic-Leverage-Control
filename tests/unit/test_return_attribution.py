from decimal import Decimal as D
from math import log

import pytest

from dynamic_leverage.services.return_attribution import attribution_bridge, total_return


def test_split_and_dividend_are_economic_returns_not_price_crashes():
    assert total_return(D(100), D(24), D(4), D(1)) == 0
    assert total_return(D(100), D(95), D(1), D(5)) == 0


def test_cost_and_timing_components_telescope_to_actual_gap():
    days = [
        dict(
            two_x_weight=0.8,
            two_x_return=-0.2,
            one_x_return=-0.1,
            strategy_return=-0.161,
            cost_fraction=0.001,
            benchmark_return=-0.101,
        ),
        dict(
            two_x_weight=0.1,
            two_x_return=0.2,
            one_x_return=0.1,
            strategy_return=0.019,
            cost_fraction=0.001,
            benchmark_return=0.099,
        ),
    ]
    r = attribution_bridge(days, 365)
    expected = log((1 - 0.161) * (1 + 0.019) / ((1 - 0.101) * (1 + 0.099)))
    assert sum(r["log_contributions"].values()) == pytest.approx(expected)
    assert r["log_contributions"]["timing"] < 0
    assert r["log_contributions"]["direct_cost"] < 0


def test_constant_weights_have_zero_timing_contribution():
    days = [
        dict(
            two_x_weight=0.5,
            two_x_return=x,
            one_x_return=x / 2,
            strategy_return=x / 2,
            cost_fraction=0,
            benchmark_return=x / 2,
        )
        for x in [0.1, -0.2, 0.3]
    ]
    r = attribution_bridge(days, 365)
    assert r["log_contributions"]["timing"] == 0
    assert r["cagr_gap"] == 0


def test_attribution_rejects_current_close_weight_or_broken_account_identity():
    with pytest.raises(ValueError, match="prior-close"):
        attribution_bridge(
            [
                dict(
                    two_x_weight=0.9,
                    two_x_return=0.1,
                    one_x_return=0.05,
                    strategy_return=0.05,
                    cost_fraction=0,
                    benchmark_return=0.05,
                )
            ],
            1,
        )
