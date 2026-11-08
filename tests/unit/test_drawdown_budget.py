from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.domain.drawdown_budget import (
    BudgetConfig,
    BudgetState,
    DrawdownBudgetPolicy,
    market_recovery_flags,
)
from dynamic_leverage.domain.models import StrategyInputs
from dynamic_leverage.domain.reserve_policy import ReserveAccount
from dynamic_leverage.services.account_replay import AccountReplay, Quote, ReplayConfig, ReplayDay


def policy(**changes):
    return DrawdownBudgetPolicy(replace(BudgetConfig(D(".15"), D(".30"), D(".05")), **changes))


def signal(day=date(2020, 1, 6)):
    return StrategyInputs(
        as_of=day, pe_ttm=D(19), eps_yoy=D(".1"), rv20=D(".2"), taiex_daily_return=D(0)
    )


def account(**changes):
    return replace(
        ReserveAccount(D(500000), D(500000), D(0), D(0), False, D(500000), True), **changes
    )


def test_market_recovery_requires_sixty_sessions_and_cannot_see_future_prices():
    start = date(2020, 1, 1)
    closes = {start + timedelta(days=i): D(100 + i) for i in range(80)}
    early = {d: p for d, p in closes.items() if d <= start + timedelta(days=65)}
    flags = market_recovery_flags(early)
    assert min(flags) == start + timedelta(days=59)
    altered = {d: p if d in early else D(1) for d, p in closes.items()}
    assert flags == {d: f for d, f in market_recovery_flags(altered).items() if d in early}
    assert all(flags.values())


@pytest.mark.parametrize("begin,end", [(".2", ".2"), (".1", ".4"), ("NaN", ".3")])
def test_parameters_cannot_relax_user_hard_limit(begin, end):
    with pytest.raises(ValueError):
        policy(de_risk_start=D(begin), cash_trigger=D(end))


def test_risk_budget_reserves_buffer_under_assumed_two_x_loss():
    snap = account(nav=D(425000), drawdown=D(".15"), exposure=D("1.7"), cash=D(63750))
    d = policy().evaluate(signal(), snap, BudgetState())
    assert d.can_trade and d.target < snap.exposure
    stressed_nav = snap.nav * (1 - d.target / 2 * D(".4"))
    assert abs(stressed_nav - d.risk_floor) < D(".00001")
    assert d.risk_floor == 325000  # 300k hard floor + 25k execution buffer


def test_cash_trigger_overrides_market_recovery_and_waits_for_actual_exit():
    p = policy()
    prior = BudgetState(mode="RECOVERY", clear_days=20, last_drawdown=D(".29"))
    snap = account(nav=D(350000), drawdown=D(".3"), exposure=D(".6"))
    d = p.evaluate(signal(), snap, prior)
    assert d.target == 0 and d.can_trade and d.state.cash_exit_pending
    for i in range(1, 8):
        d = p.evaluate(signal(date(2020, 1, 6) + timedelta(days=i)), snap, d.state)
        assert d.target == 0 and d.state.clear_days == 0  # no pretend sale while unfilled


def test_market_reentry_works_from_cash_without_erasing_original_drawdown():
    p = policy()
    state = BudgetState(mode="CASH", last_drawdown=D(".3"))
    snap = account(nav=D(350000), cash=D(350000), drawdown=D(".3"))
    for i in range(5):
        d = p.evaluate(signal(date(2020, 1, 6) + timedelta(days=i)), snap, state)
        state = d.state
        assert d.can_trade == (i == 4)
    assert state.mode == "RECOVERY"
    assert d.target == D(".1") and d.buy_budget == 17500
    assert d.risk_floor == 325000 and state.last_drawdown == D(".3")
    repeated = p.evaluate(signal(date(2020, 1, 11)), snap, state)
    assert not repeated.can_trade


def test_cut_only_control_retains_old_account_recovery_block():
    p = policy(market_reentry=False)
    state = BudgetState(mode="CASH", last_drawdown=D(".3"))
    for i in range(10):
        d = p.evaluate(
            signal(date(2020, 1, 6) + timedelta(days=i)),
            account(nav=D(350000), drawdown=D(".3")),
            state,
        )
        state = d.state
        assert not d.can_trade and d.target == 0


def test_failed_recovery_returns_to_cash_and_does_not_unlock_hard_limit():
    p = policy()
    prior = BudgetState(mode="RECOVERY", last_drawdown=D(".3"))
    d = p.evaluate(
        signal(),
        account(nav=D(350000), drawdown=D(".3"), exposure=D(".2"), market_recovery_ready=False),
        prior,
    )
    assert d.can_trade and d.target == 0 and d.state.mode == "CASH"
    d = p.evaluate(
        signal(date(2020, 1, 7)), account(hard_limit_breached=True, exposure=D(".2")), d.state
    )
    assert d.target == 0 and d.state.mode == "HARD_LOCK"
    for i in range(10):
        d = p.evaluate(signal(date(2020, 1, 8) + timedelta(days=i)), account(), d.state)
        assert d.target == 0 and not d.can_trade  # recovered price cannot erase the breach


def test_principal_loss_has_protection_even_if_high_water_mark_input_is_lower():
    d = policy().evaluate(
        signal(), account(nav=D(290000), drawdown=D(".1"), exposure=D(1)), BudgetState()
    )
    assert d.target == 0 and d.state.mode == "HARD_LOCK"


@pytest.mark.parametrize("changes", [{"initial_nav": None}, {"market_recovery_ready": None}])
def test_missing_required_account_evidence_rejected(changes):
    with pytest.raises(ValueError):
        policy().evaluate(signal(), account(**changes), BudgetState())


def test_small_recovery_choice_is_not_swallowed_by_old_no_trade_band():
    p = policy(recovery_buy_fraction=D(".025"))
    prior = BudgetState(mode="CASH", clear_days=4, last_drawdown=D(".3"))
    d = p.evaluate(signal(), account(nav=D(350000), drawdown=D(".3")), prior)
    assert d.can_trade and d.target == D(".05")


def test_cash_exit_does_not_treat_tiny_remaining_position_as_fully_sold():
    p = policy()
    state = BudgetState(mode="CASH", cash_exit_pending=True, last_drawdown=D(".3"))
    d = p.evaluate(signal(), account(nav=D(350000), drawdown=D(".3"), exposure=D(".0001")), state)
    assert d.can_trade and d.target == 0 and d.state.cash_exit_pending
    assert d.state.clear_days == 0


def test_replay_liquidates_after_threshold_using_delayed_real_fills():
    prices = [100] * 60 + [60] * 12  # fully invested research path then sudden ETF loss
    rows = [
        ReplayDay(
            date(2020, 1, 6) + timedelta(days=i),
            {"0050": Quote(D(price), 1000000), "00631L": Quote(D(price), 1000000)},
            D("1.8"),
            policy_inputs=signal(date(2020, 1, 6) + timedelta(days=i)),
            market_recovery_ready=False,
        )
        for i, price in enumerate(prices)
    ]
    cfg = ReplayConfig(
        recipe="2x-cash-v1",
        commission=D(0),
        minimum_commission=D(0),
        sell_tax=D(0),
        slippage=D(0),
        execution_delay=2,
    )
    result = AccountReplay(cfg).run(rows, reserve_policy=policy())
    assert result.daily[60]["drawdown"] >= D(".3")
    assert result.daily[60]["quantity_00631L"] > 0  # no same-close liquidations
    assert result.daily[62]["quantity_00631L"] == 0
    assert all(f["signal_date"] < f["date"] for f in result.fills)
    assert any(e.get("mode") == "CASH" for e in result.events)


def test_large_gap_records_hard_breach_instead_of_clipping_reported_losses():
    start = date(2020, 1, 6)
    rows = [
        ReplayDay(
            start + timedelta(days=i),
            {"0050": Quote(D(p), 1000000), "00631L": Quote(D(p), 1000000)},
            D("1.8"),
            policy_inputs=signal(start + timedelta(days=i)),
            market_recovery_ready=True,
        )
        for i, p in enumerate([100] * 60 + [10] * 10 + [100] * 10)
    ]
    cfg = ReplayConfig(
        recipe="2x-cash-v1", commission=D(0), minimum_commission=D(0), sell_tax=D(0), slippage=D(0)
    )
    result = AccountReplay(cfg).run(rows, reserve_policy=policy())
    assert result.daily[60]["drawdown"] > D(".4")
    assert all(r["capital_limit_latched"] for r in result.daily[60:])
    assert result.daily[61]["quantity_00631L"] == 0
    assert not any(f["side"] == "BUY" and f["date"] > rows[60].day for f in result.fills)
