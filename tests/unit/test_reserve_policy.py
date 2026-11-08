from dataclasses import replace
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
from dynamic_leverage.services.account_replay import AccountReplay, Quote, ReplayConfig, ReplayDay


def inputs(day=date(2020, 1, 6), **changes):
    return replace(
        StrategyInputs(
            as_of=day,
            pe_ttm=D(19),
            pe_percentile=D(".1"),
            eps_yoy=D(".1"),
            rv20=D(".2"),
            taiex_daily_return=D(0),
        ),
        **changes,
    )


def account(**changes):
    return replace(ReserveAccount(D(1000), D(1000), D(0), D(0), False), **changes)


def replay_rows(prices):
    return [
        ReplayDay(
            date(2020, 1, 6) + timedelta(days=i),
            {"0050": Quote(D(p), 10000), "00631L": Quote(D(p), 10000)},
            D(1),
            policy_inputs=inputs(date(2020, 1, 6) + timedelta(days=i)),
        )
        for i, p in enumerate(prices)
    ]


def replay_config():
    return ReplayConfig(
        recipe="2x-cash-v1",
        initial_nav=D(1000),
        commission=D(0),
        minimum_commission=D(0),
        slippage=D(0),
        sell_tax=D(0),
        volume_fraction=D(1),
    )


@pytest.mark.parametrize(
    "pe,target", [(28, ".6"), (26, "1"), (24, "1.2"), (22, "1.4"), (20, "1.6"), (19, "1.8")]
)
def test_absolute_valuation_and_actual_account_tranche(pe, target):
    d = ReservePolicy().evaluate(inputs(pe_ttm=D(pe)), account(), ReserveState())
    assert d.desired == D(target)
    assert d.target == D(".2")
    assert d.buy_budget == 100 and d.cash_floor == 100


def test_once_per_week_and_no_virtual_position_advance():
    p = ReservePolicy()
    first = p.evaluate(inputs(), account(), ReserveState())
    second = p.evaluate(inputs(date(2020, 1, 7)), account(), first.state)
    assert not second.can_trade
    following = p.evaluate(inputs(date(2020, 1, 13)), account(), second.state)
    assert following.target == D(".2")  # no fill means no fabricated position


def test_soft_pause_requires_five_clear_days_and_does_not_unlock_hard_limit():
    p = ReservePolicy()
    first = p.evaluate(inputs(rv20=D(".5")), account(exposure=D("1.6")), ReserveState())
    assert first.state.paused and first.target == 1 and first.can_trade
    state = first.state
    for i in range(1, 6):
        d = p.evaluate(
            inputs(date(2020, 1, 6) + timedelta(days=i)),
            account(exposure=D(".8"), hard_limit_breached=True),
            state,
        )
        state = d.state
        assert state.paused == (i < 5)
        assert not d.can_trade and d.target <= D(".8")


def test_earnings_decline_and_account_drawdown_pause_buys():
    p = ReservePolicy()
    earnings = p.evaluate(inputs(eps_yoy=D("-.2")), account(), ReserveState())
    drawdown = p.evaluate(inputs(), account(drawdown=D(".25")), ReserveState())
    assert earnings.state.paused and not earnings.can_trade
    assert drawdown.state.paused and not drawdown.can_trade


def test_expensive_valuation_refills_cash_without_weekly_wait():
    state = ReserveState(last_buy_week=(2020, 2))
    d = ReservePolicy().evaluate(inputs(pe_ttm=D(30)), account(exposure=D("1.8")), state)
    assert d.target == D(".6") and d.can_trade


@pytest.mark.parametrize(
    "changes", [{"eps_yoy": None}, {"pe_ttm": D("NaN")}, {"critical_data_safe": False}]
)
def test_invalid_inputs_do_not_reuse_old_target(changes):
    with pytest.raises(ValueError, match="complete finite"):
        ReservePolicy().evaluate(inputs(**changes), account(), ReserveState())


def test_delayed_fill_keeps_original_tranche_and_cash_budget():
    rows = replay_rows([10, 10, 50, 50])
    cfg = replace(replay_config(), execution_delay=2)
    r = AccountReplay(cfg).run(rows, reserve_policy=ReservePolicy())
    assert r.daily[1]["quantity_00631L"] == 0
    assert r.daily[2]["quantity_00631L"] == 2
    assert r.daily[2]["cash"] == 900
    fills = [e for e in r.events if e["kind"] == "reserve_buy_fill"]
    assert fills[0]["budget_remaining"] == 0
    assert fills[0]["cash_after"] >= fills[0]["cash_floor"]


def test_unstaged_price_jump_cannot_consume_cash_floor():
    r = AccountReplay(replay_config()).run(
        replay_rows([10, 20]), reserve_policy=ReservePolicy(ReserveConfig(staged=False))
    )
    assert r.daily[-1]["cash"] == 100
    assert r.daily[-1]["quantity_00631L"] == 45


def test_zero_cost_cash_account_wealth_identity_and_hard_limit_survive_recovery():
    r = AccountReplay(replay_config()).run(
        replay_rows([10, 10, 5, 10, 10, 10, 10, 10, 10]),
        reserve_policy=ReservePolicy(ReserveConfig(staged=False)),
    )
    for row, p in zip(r.daily, [10, 10, 5, 10, 10, 10, 10, 10, 10], strict=True):
        assert row["nav"] == row["cash"] + row["receivables"] + row["quantity_00631L"] * p
    assert r.daily[2]["capital_limit_latched"]
    assert r.daily[-1]["capital_limit_latched"]
    assert len([f for f in r.fills if f["side"] == "BUY"]) == 1


def test_new_controller_cannot_bypass_gate_or_use_mixed_recipe():
    with pytest.raises(ValueError, match="gated 2x-cash"):
        AccountReplay(replace(replay_config(), mode="ungated")).run(
            replay_rows([10, 10]), reserve_policy=ReservePolicy()
        )
    rows = replay_rows([10, 10])
    rows[1] = replace(rows[1], broker_safe=False)
    r = AccountReplay(replay_config()).run(rows, reserve_policy=ReservePolicy())
    assert not r.fills


def test_point_in_time_date_and_state_are_validated():
    with pytest.raises(ValueError, match="dates must advance"):
        ReservePolicy().evaluate(inputs(), account(), ReserveState(last_day=date(2020, 1, 6)))
    rows = replay_rows([10, 10])
    rows[0] = replace(rows[0], policy_inputs=inputs(date(2020, 1, 7)))
    with pytest.raises(ValueError, match="decision date"):
        AccountReplay(replay_config()).run(rows, reserve_policy=ReservePolicy())


def test_percentile_candidate_changes_only_valuation_mapping():
    p = ReservePolicy(ReserveConfig(valuation="percentile"))
    decision = p.evaluate(inputs(pe_percentile=D(".9")), account(), ReserveState())
    assert decision.desired == D(".6")
    assert decision.target == D(".2") and decision.buy_budget == 100


def test_no_soft_control_ablation_keeps_budget_and_capital_limit():
    p = ReservePolicy(ReserveConfig(soft_controls=False))
    d = p.evaluate(inputs(eps_yoy=D("-.5")), account(), ReserveState())
    assert not d.state.paused and d.can_trade and d.buy_budget == 100
    blocked = p.evaluate(
        inputs(eps_yoy=D("-.5")), account(hard_limit_breached=True), ReserveState()
    )
    assert not blocked.can_trade
