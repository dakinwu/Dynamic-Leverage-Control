from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.services.account_replay import AccountReplay, Quote, ReplayConfig, ReplayDay
from dynamic_leverage.services.cashflow_research import (
    CapitalRecoveryPolicy,
    cashflow_metrics,
    investment_schedule,
    money_weighted_return,
    reentry_acceptance,
)


def config(**kwargs):
    return replace(
        ReplayConfig(
            initial_nav=D(1000),
            commission=D(0),
            sell_tax=D(0),
            slippage=D(0),
            minimum_commission=D(0),
            volume_fraction=D(1),
            no_trade_band=D("0.01"),
            settlement_delay=0,
        ),
        **kwargs,
    )


def rows(prices, start=date(2020, 1, 1)):
    return [
        ReplayDay(
            start + timedelta(days=i),
            {s: Quote(D(str(p)), 100000) for s in ("0050", "00631L")},
            D(1),
            market_recovery_ready=True,
        )
        for i, p in enumerate(prices)
    ]


def test_staged_capital_stays_in_nav_and_is_not_reported_as_deposits():
    data = rows([10] * 100)
    scheduled = investment_schedule(data, D(1000), installments=2)
    replay = AccountReplay(config(mode="buyhold")).run(scheduled)
    assert replay.daily[1]["quantity_0050"] == 50
    assert replay.daily[1]["cash"] == 500
    february = next(r for r in replay.daily if r["date"] == date(2020, 2, 2))
    assert february["quantity_0050"] == 100
    assert all(r["nav"] == 1000 and r["net_contributions"] == 1000 for r in replay.daily)
    assert cashflow_metrics(replay, config())["net_profit"] == 0


def test_new_deposit_is_not_profit_or_drawdown_recovery():
    data = rows([10, 10, 8, 8, 8])
    data[3] = replace(data[3], contribution=D(200))
    replay = AccountReplay(config(mode="buyhold")).run(data)
    assert replay.daily[3]["nav"] == 1000
    assert replay.daily[3]["daily_return"] == 0
    assert replay.daily[3]["drawdown"] == D("0.2")
    stats = cashflow_metrics(replay, config())
    assert stats["net_contributions"] == 1200
    assert stats["net_profit"] == -200
    assert stats["calendar_cagr"] is None
    assert stats["annualized_time_weighted_return"] < 0


def test_interest_only_accrues_on_settled_cash_and_calendar_days():
    data = rows([10, 10])
    data[1] = replace(data[1], day=date(2021, 1, 1))
    data = [replace(r, target=D(0)) for r in data]
    replay = AccountReplay(config(cash_interest_rate=D("0.02"))).run(data)
    expected = D(1000) * D("1.02") ** (D(366) / D("365.2425"))
    assert replay.daily[-1]["nav"] == expected
    assert replay.daily[0]["cash_interest"] == 0
    assert not replay.fills


def test_reentry_diagnostic_distinguishes_blocked_signal_from_real_buy():
    data = rows([10, 10, 20] + [6.8] * 15)
    original_policy = CapitalRecoveryPolicy()
    original = AccountReplay(config()).run(data, signal_policy=original_policy)
    original_acceptance = reentry_acceptance(original, original_policy)
    assert original_acceptance["reentry_signal_date"] is not None
    assert original_acceptance["reentry_buy_count"] == 0
    assert original_acceptance["reentry_blocked_days"] > 0
    diagnostic_policy = CapitalRecoveryPolicy()
    diagnostic = AccountReplay(config(loss_gate_basis="principal-only-research")).run(
        data,
        signal_policy=diagnostic_policy,
    )
    acceptance = reentry_acceptance(diagnostic, diagnostic_policy)
    assert acceptance["reentry_executed"]
    assert acceptance["first_reentry_buy_date"] > acceptance["reentry_signal_date"]
    assert not cashflow_metrics(diagnostic, config())["numerical_40pct_limits_pass"]


def test_principal_only_diagnostic_never_rearms_after_40pct_principal_loss():
    data = rows([10, 10, 5, 10, 10, 10, 10])
    replay = AccountReplay(config(loss_gate_basis="principal-only-research")).run(data)
    assert replay.daily[-1]["principal_loss"] == 0
    assert replay.daily[-1]["principal_limit_latched"]
    assert replay.daily[-1]["capital_limit_latched"]


def test_recovery_policy_uses_actual_loss_in_one_pass_and_permanent_stop_wins():
    policy = CapitalRecoveryPolicy()
    data = rows([10] * 8)
    assert policy.target(data[0], D("0.3")) == 0
    assert all(policy.target(r, D("0.31")) == 0 for r in data[1:5])
    assert policy.target(data[5], D("0.31")) == 1
    assert policy.target(data[6], D("0.4")) == 0
    assert policy.target(data[7], D(0)) == 0


def test_money_weighted_return_has_correct_deposit_timing():
    assert money_weighted_return(
        [
            (date(2020, 1, 1), -100),
            (date(2021, 1, 1), 110),
        ]
    ) == pytest.approx(1.1 ** (365.2425 / 366) - 1)
    assert money_weighted_return(
        [
            (date(2020, 1, 1), -100),
            (date(2020, 7, 1), -100),
            (date(2021, 1, 1), 200),
        ]
    ) == pytest.approx(0, abs=1e-12)


@pytest.mark.parametrize(
    "change",
    [
        {"contribution": D("NaN")},
        {"contribution": D(-1)},
        {"reserved_cash": D(-1)},
    ],
)
def test_invalid_cashflows_fail_closed(change):
    with pytest.raises(ValueError, match="cash flow"):
        AccountReplay(config()).run([replace(rows([10])[0], **change)])


def test_unknown_loss_semantics_rejected():
    with pytest.raises(ValueError, match="loss gate"):
        config(loss_gate_basis="automatic-reset")
