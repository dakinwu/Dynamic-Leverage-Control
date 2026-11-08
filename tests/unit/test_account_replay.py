from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.services.account_replay import AccountReplay, Quote, ReplayConfig, ReplayDay


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
        ),
        **kwargs,
    )


def days(prices, targets=None):
    return [
        ReplayDay(
            date(2020, 1, 1) + timedelta(days=i),
            {"0050": Quote(D(p), 10000), "00631L": Quote(D(p), 10000)},
            D(targets[i] if targets else 1),
        )
        for i, p in enumerate(prices)
    ]


def test_orders_are_fixed_at_signal_and_cannot_profit_before_execution():
    result = AccountReplay(config()).run(days([10, 20, 30]))
    assert result.daily[0]["quantity_0050"] == 0
    assert result.daily[1]["quantity_0050"] == 50  # cash limits yesterday's 100-share order
    assert result.daily[1]["nav"] == 1000
    assert result.daily[2]["nav"] == 1500
    assert all(f["signal_date"] < f["date"] for f in result.fills)


def test_dividend_is_receivable_before_payment_and_split_does_not_create_wealth():
    rows = days([10, 10, 9, 9, 3, 3])
    rows[2] = replace(rows[2], dividends={"0050": (D(1), rows[4].day)})
    rows[4] = replace(rows[4], splits={"0050": D(3)})
    result = AccountReplay(config(mode="buyhold")).run(rows)
    assert result.daily[2]["receivables"] == 100
    assert result.daily[2]["cash"] == 0
    assert result.daily[2]["nav"] == 1000
    assert result.daily[4]["quantity_0050"] == 300
    assert result.daily[4]["cash"] == 100
    assert result.daily[4]["receivables"] == 0
    assert result.daily[5]["quantity_0050"] == 333
    assert result.daily[5]["nav"] == 1000


def test_unsettled_sale_proceeds_cannot_fund_purchase():
    rows = days([10] * 7, [2, 1, 1, 1, 1, 1, 1])
    result = AccountReplay(config()).run(rows)
    assert result.daily[2]["quantity_0050"] == 0
    assert result.daily[2]["quantity_00631L"] == 0
    assert result.daily[2]["receivables"] == 1000
    assert result.daily[3]["quantity_00631L"] == 0
    assert result.daily[4]["quantity_0050"] == 100
    assert all(r["cash"] >= 0 for r in result.daily)


def test_existing_gate_latches_after_recovery_and_does_not_force_liquidation():
    rows = days([10, 10, 5, 10, 10], [1, 1, 1, 2, 2])
    result = AccountReplay(config()).run(rows)
    assert result.daily[2]["maximum_drawdown"] == D("0.5")
    assert result.daily[2]["quantity_0050"] == 100
    assert result.daily[-1]["quantity_00631L"] == 0
    assert result.daily[-1]["capital_limit_latched"]
    assert "CAPITAL_LOSS_LIMIT" in result.daily[-1]["risk_reasons"]


@pytest.mark.parametrize("fault", ["broker", "data", "manual", "halt", "limit"])
def test_execution_faults_block_previously_queued_buys(fault):
    rows = days([10, 10, 10])
    changes = {
        "broker": {"broker_safe": False},
        "data": {"data_safe": False},
        "manual": {"manual_halt": True},
    }
    if fault in changes:
        rows[1] = replace(rows[1], **changes[fault])
    else:
        q = Quote(D(10), 0 if fault == "halt" else 10000, buy_allowed=fault != "limit")
        rows[1] = replace(rows[1], quotes={"0050": q, "00631L": q})
    result = AccountReplay(config()).run(rows)
    assert result.daily[1]["quantity_0050"] == 0
    assert any(e["kind"] == "unfilled" for e in result.events)


def test_volume_cap_partial_fills_are_recorded_without_borrowing():
    result = AccountReplay(config(volume_fraction=D("0.0005"))).run(days([10, 10]))
    assert result.fills[0]["quantity"] == 5
    assert result.fills[0]["requested_quantity"] == 100
    assert result.daily[-1]["cash"] == 950


def test_risk_reduction_can_overshoot_40_percent_during_execution_delay():
    rows = days([10, 10, 7, 5], [1, 1, 0, 0])
    result = AccountReplay(config()).run(rows)
    assert result.daily[2]["drawdown"] == D("0.3")
    assert result.daily[3]["drawdown"] == D("0.5")
    assert result.daily[3]["quantity_0050"] == 0


def test_defensive_reentry_waits_for_confirmation_and_steps_weekly():
    rows = days([10] * 20)
    result = AccountReplay(config(mode="defensive")).run(rows)
    assert all(r["target"] == 0 for r in result.daily[:4])
    assert result.daily[4]["target"] == D("0.2")
    assert result.daily[5]["target"] == D("0.4")  # next ISO week
    assert result.daily[6]["target"] == D("0.4")
    assert max(r["target"] for r in result.daily) <= D("0.8")


def test_cash_account_rejects_above_two_ceiling():
    with pytest.raises(ValueError, match="between zero and two"):
        config(ceiling=D("3.2"))


def test_cash_recipe_preserves_reserve_at_same_initial_exposure():
    rows = days([10, 10], ["1.2", "1.2"])
    traditional = AccountReplay(config()).run(rows).daily[-1]
    reserve = AccountReplay(config(recipe="2x-cash-v1")).run(rows).daily[-1]
    assert traditional["quantity_0050"] == 80
    assert traditional["quantity_00631L"] == 20
    assert traditional["cash"] == 0
    assert reserve["quantity_0050"] == 0
    assert reserve["quantity_00631L"] == 60
    assert reserve["cash"] == 400
    assert reserve["actual_exposure"] == traditional["actual_exposure"] == D("1.2")


def test_cash_recipe_can_buy_from_settled_reserve_after_decline():
    # Monday decline after entry in the prior ISO week; normal weekly increase permitted.
    rows = days([10, 10, 10, 10, 10, 5, 5], ["1.2"] * 7)
    result = AccountReplay(config(recipe="2x-cash-v1")).run(rows)
    assert result.daily[5]["nav"] == 700
    assert result.daily[6]["quantity_00631L"] == 84
    assert result.daily[6]["cash"] == 280
    assert result.daily[6]["receivables"] == 0


def test_cash_recipe_keeps_capital_loss_gate_after_deeper_decline():
    result = AccountReplay(config(recipe="2x-cash-v1")).run(days([10, 10, 3, 3, 3], ["1.2"] * 5))
    assert result.daily[-1]["capital_limit_latched"]
    assert result.daily[-1]["quantity_00631L"] == 60
    assert result.daily[-1]["cash"] == 400


def test_unknown_recipe_fails_closed():
    with pytest.raises(ValueError, match="unknown allocation recipe"):
        config(recipe="typo")


def test_falling_target_does_not_postpone_an_already_queued_reduction():
    rows = days([10] * 7, [2, 2, "1.5", "1.4", "1.4", "1.4", "1.4"])
    result = AccountReplay(config(execution_delay=2)).run(rows)
    sales = [f for f in result.fills if f["side"] == "SELL"]
    assert sales[0]["signal_date"] == rows[2].day
    assert sales[0]["date"] == rows[4].day
    assert result.daily[4]["quantity_00631L"] == 50


def test_constant_prices_conserve_capital_less_all_execution_costs():
    rows = days([10] * 7, [1, 1, 0, 0, 0, 0, 0])
    result = AccountReplay(
        config(
            commission=D("0.001425"),
            minimum_commission=D(20),
            slippage=D("0.0005"),
            sell_tax=D("0.001"),
        )
    ).run(rows)
    assert result.daily[-1]["nav"] == D(1000) - sum(r["cost"] for r in result.daily)
    assert result.daily[-1]["cash"] == result.daily[-1]["nav"]
    assert result.daily[-1]["receivables"] == 0
    assert all(f["commission"] == 20 for f in result.fills)


@pytest.mark.parametrize("fault", ["data", "etf", "broker", "manual"])
def test_queued_reduction_respects_operational_gate_but_can_reduce_unsafe_data(fault):
    rows = days([10] * 4, [1, 0, 0, 0])
    changes = {
        "data": {"data_safe": False},
        "etf": {"etf_abnormal": True},
        "broker": {"broker_safe": False},
        "manual": {"manual_halt": True},
    }
    rows[2] = replace(rows[2], **changes[fault])
    result = AccountReplay(config()).run(rows)
    expected_shares = 0 if fault in {"data", "etf"} else 100
    assert result.daily[2]["quantity_0050"] == expected_shares
