from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.domain.drawdown_budget import BudgetConfig, BudgetState, DrawdownBudgetPolicy
from dynamic_leverage.domain.models import StrategyInputs
from dynamic_leverage.domain.reserve_policy import ReserveAccount
from dynamic_leverage.services.account_replay import AccountReplay, Quote, ReplayConfig, ReplayDay


def policy(**changes):
    config = BudgetConfig(D(".15"), D(".30"), D(".05"), cash_rearm_drawdown=D(".15"))
    return DrawdownBudgetPolicy(replace(config, **changes))


def signal(offset=0):
    return StrategyInputs(
        as_of=date(2020, 1, 6) + timedelta(days=offset),
        pe_ttm=D(19),
        eps_yoy=D(".1"),
        rv20=D(".2"),
        taiex_daily_return=D(0),
    )


def account(dd=".301", exposure=".3", **changes):
    nav = D(500000) * (1 - D(dd))
    return replace(
        ReserveAccount(
            nav, nav * (1 - D(exposure) / 2), D(exposure), D(dd), False, D(500000), True
        ),
        **changes,
    )


def recovery(**changes):
    return replace(
        BudgetState(mode="RECOVERY", clear_days=20, last_drawdown=D(".299"), cash_exit_armed=False),
        **changes,
    )


def test_cash_threshold_recross_does_not_cancel_confirmed_recovery():
    d = policy().evaluate(signal(), account(), recovery())
    assert d.state.mode == "RECOVERY" and d.desired > 0 and not d.state.cash_exit_armed
    assert "SOFT_CASH_EXIT_NOT_REARMED" in d.reasons
    assert "CASH_EXIT_BEFORE_ANY_REENTRY" not in d.reasons


def test_first_cross_still_liquidates_then_waits_for_actual_fill():
    p = policy()
    d = p.evaluate(signal(), account(), recovery(cash_exit_armed=True))
    assert d.target == 0 and d.can_trade and d.state.cash_exit_pending
    assert not d.state.cash_exit_armed
    for i in range(1, 8):
        # Even a price rebound cannot rearm or pretend an unfilled tiny position was sold.
        d = p.evaluate(signal(i), account(dd=".14", exposure=".0001"), d.state)
        assert d.target == 0 and d.state.clear_days == 0 and not d.state.cash_exit_armed
    done = p.evaluate(signal(8), account(dd=".14", exposure="0"), d.state)
    assert not done.state.cash_exit_pending and done.state.clear_days == 0


def test_rearm_requires_drawdown_inside_original_start_and_keeps_high_water_mark():
    p = policy()
    at_boundary = p.evaluate(signal(), account(dd=".15"), recovery())
    assert not at_boundary.state.cash_exit_armed
    d = p.evaluate(signal(1), account(dd=".14"), at_boundary.state)
    assert d.state.cash_exit_armed and d.risk_floor == D(325000)
    assert "SOFT_CASH_EXIT_REARMED" in d.reasons
    again = p.evaluate(signal(2), account(), d.state)
    assert again.target == 0 and again.state.cash_exit_pending


def test_market_failure_still_exits_without_soft_threshold_armed():
    d = policy().evaluate(signal(), account(market_recovery_ready=False), recovery())
    assert d.target == 0 and d.can_trade and d.state.mode == "CASH"
    assert d.state.cash_exit_pending


@pytest.mark.parametrize("no_buffer", [False, True])
def test_hard_limit_overrides_rearm_and_diagnostic_cap(no_buffer):
    p = policy(recovery_without_execution_buffer=no_buffer)
    d = p.evaluate(signal(), account(dd=".41"), recovery())
    assert d.target == 0 and d.state.mode == "HARD_LOCK"
    for i in range(1, 8):
        d = p.evaluate(signal(i), account(dd="0", exposure="0"), d.state)
        assert d.target == 0 and not d.can_trade and d.state.mode == "HARD_LOCK"


def test_speed_and_cap_are_independent_and_floor_not_reset():
    snap = account(dd=".30", exposure="0")
    slow = policy().evaluate(signal(), snap, recovery())
    fast = policy(recovery_buy_fraction=D(".1")).evaluate(signal(), snap, recovery())
    cap = policy(recovery_without_execution_buffer=True).evaluate(signal(), snap, recovery())
    assert slow.target == D(".1") and fast.target == D(".2")
    assert slow.risk_exposure_cap == fast.risk_exposure_cap
    assert cap.buy_budget == slow.buy_budget and cap.risk_exposure_cap > slow.risk_exposure_cap
    assert slow.risk_floor == D(325000) and cap.risk_floor == D(300000)
    assert abs(snap.nav * (1 - cap.risk_exposure_cap / 2 * D(".4")) - cap.risk_floor) < D("1e-20")
    normal = policy(recovery_without_execution_buffer=True).evaluate(
        signal(), account(dd="0", exposure="0"), BudgetState()
    )
    assert normal.risk_floor == D(325000)


@pytest.mark.parametrize("value", ["0", ".16", "NaN"])
def test_invalid_rearm_threshold_rejected(value):
    with pytest.raises(ValueError):
        policy(cash_rearm_drawdown=D(value))


@pytest.mark.parametrize("no_buffer", [False, True])
def test_gap_is_not_clipped_and_no_buy_after_breach(no_buffer):
    rows = [
        ReplayDay(
            signal(i).as_of,
            {"0050": Quote(D(p), 10**6), "00631L": Quote(D(p), 10**6)},
            D("1.8"),
            policy_inputs=signal(i),
            market_recovery_ready=True,
        )
        for i, p in enumerate([100] * 60 + [10] * 10 + [100] * 10)
    ]
    cfg = ReplayConfig(
        recipe="2x-cash-v1",
        commission=D(0),
        minimum_commission=D(0),
        sell_tax=D(0),
        slippage=D(0),
        execution_delay=2,
    )
    result = AccountReplay(cfg).run(
        rows, reserve_policy=policy(recovery_without_execution_buffer=no_buffer)
    )
    assert result.daily[60]["drawdown"] > D(".4")
    assert result.daily[60]["quantity_00631L"] > 0 and result.daily[62]["quantity_00631L"] == 0
    assert not any(f["side"] == "BUY" and f["date"] > rows[60].day for f in result.fills)
