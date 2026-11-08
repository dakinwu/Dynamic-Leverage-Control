from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.domain.drawdown_budget import BudgetConfig, BudgetState, DrawdownBudgetPolicy
from dynamic_leverage.domain.models import StrategyInputs
from dynamic_leverage.domain.reserve_policy import ReserveAccount
from dynamic_leverage.services.strategy_value import match_risk, window_metrics


def policy_inputs():
    return StrategyInputs(
        as_of=date(2020, 1, 6), pe_ttm=D(19), eps_yoy=D(".1"), rv20=D(".2"), taiex_daily_return=D(0)
    )


def account():
    return ReserveAccount(D(500000), D(500000), D(0), D(0), False, D(500000), True)


def test_fixed_allocation_does_not_use_pe_but_keeps_identical_earnings_gate():
    p = DrawdownBudgetPolicy(BudgetConfig(D(".15"), D(".3"), D(".05"), fixed_target=D("1.2")))
    a = p.evaluate(policy_inputs(), account(), BudgetState())
    b = p.evaluate(replace(policy_inputs(), pe_ttm=None), account(), BudgetState())
    assert a == b and a.desired == D("1.2")
    blocked = p.evaluate(replace(policy_inputs(), eps_yoy=D("-.3")), account(), BudgetState())
    assert blocked.state.paused and not blocked.can_trade


def test_price_only_control_needs_no_fabricated_valuation_or_earnings():
    p = DrawdownBudgetPolicy(
        BudgetConfig(D(".15"), D(".3"), D(".05"), fixed_target=D("1.2"), earnings_filter=False)
    )
    d = p.evaluate(replace(policy_inputs(), pe_ttm=None, eps_yoy=None), account(), BudgetState())
    assert d.can_trade and not d.state.paused
    with pytest.raises(ValueError):
        p.evaluate(replace(policy_inputs(), rv20=None), account(), BudgetState())


def test_active_inputs_still_fail_closed():
    p = DrawdownBudgetPolicy(BudgetConfig(D(".15"), D(".3"), D(".05")))
    for field in ["pe_ttm", "eps_yoy", "rv20", "taiex_daily_return"]:
        with pytest.raises(ValueError):
            p.evaluate(replace(policy_inputs(), **{field: None}), account(), BudgetState())


def test_match_chooses_risk_distance_not_return_and_ties_choose_lower_target():
    controls = {
        "low": {"fixed_target": D(1), "mean_actual_exposure": D(1), "calendar_cagr": D("-.9")},
        "high": {"fixed_target": D("1.2"), "mean_actual_exposure": D("1.2"), "calendar_cagr": D(9)},
    }
    m = match_risk({"mean_actual_exposure": D("1.1")}, controls, "mean_actual_exposure", D(".05"))
    assert m["control"] == "low" and not m["within_tolerance"]
    with pytest.raises(ValueError):
        match_risk({"calendar_cagr": D(0)}, controls, "calendar_cagr", D(0))


def test_segment_uses_preceding_nav_and_does_not_skip_first_day_loss():
    start = date(2019, 12, 31)
    rows = [
        {"date": start + timedelta(days=i), "nav": D(n), "actual_exposure": D(1), "cost": D(0)}
        for i, n in enumerate([100, 80, 100])
    ]
    m = window_metrics(rows, date(2020, 1, 1), date(2020, 1, 2), D(100))
    assert m["total_return"] == 0 and m["maximum_drawdown"] == D("-.2")
    assert m["baseline_day"] == start and m["baseline_nav"] == 100
    assert rows[0]["nav"] == 100 and not m["account_state_restarted"]
