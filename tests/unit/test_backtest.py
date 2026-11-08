from datetime import date, timedelta
from decimal import Decimal

import pytest

from dynamic_leverage.domain.backtest import BacktestEngine, BacktestPoint
from dynamic_leverage.domain.execution import OneXTwoXRecipe, TwoXCashRecipe


def _points(days: int = 30) -> list[BacktestPoint]:
    start = date(2026, 1, 1)
    return [
        BacktestPoint(
            trading_date=start + timedelta(days=index),
            one_x_return=Decimal("0.001"),
            two_x_return=Decimal("0.002"),
            target_exposure=Decimal("1.2"),
        )
        for index in range(days)
    ]


def test_backtest_uses_actual_instrument_returns_not_target_times_index() -> None:
    engine = BacktestEngine(one_way_cost_rate=Decimal("0"), sell_tax_rate=Decimal("0"))
    result = engine.run(_points(), TwoXCashRecipe())
    assert result.equity[0].daily_return == Decimal("0.0012")
    assert result.equity[-1].nav > Decimal("200000")


def test_recipes_with_equal_initial_exposure_can_have_different_paths() -> None:
    points = _points()
    points[5] = BacktestPoint(
        trading_date=points[5].trading_date,
        one_x_return=Decimal("0"),
        two_x_return=Decimal("-0.04"),
        target_exposure=Decimal("1.2"),
    )
    engine = BacktestEngine(one_way_cost_rate=Decimal("0"), sell_tax_rate=Decimal("0"))
    cash_result = engine.run(points, TwoXCashRecipe())
    mixed_result = engine.run(points, OneXTwoXRecipe())
    assert cash_result.equity[-1].nav != mixed_result.equity[-1].nav


def test_backtest_includes_costs_in_daily_return_and_risk_metrics() -> None:
    point = BacktestPoint(
        trading_date=date(2026, 1, 1),
        one_x_return=Decimal("0"),
        two_x_return=Decimal("0"),
        target_exposure=Decimal("1.2"),
    )
    result = BacktestEngine(initial_nav=Decimal("200000")).run([point], TwoXCashRecipe())

    assert result.equity[0].cost == Decimal("171.0000000")
    assert result.equity[0].daily_return == Decimal("-0.0008550")
    assert result.metrics.total_return == Decimal("-0.0008550")
    assert result.metrics.worst_1_day == Decimal("-0.0008550")
    assert result.metrics.maximum_drawdown == Decimal("-0.0008550")


def test_backtest_drawdown_starts_at_initial_nav() -> None:
    point = BacktestPoint(
        trading_date=date(2026, 1, 1),
        one_x_return=Decimal("-0.1"),
        two_x_return=Decimal("-0.2"),
        target_exposure=Decimal("1"),
    )
    result = BacktestEngine(
        initial_nav=Decimal("100"),
        one_way_cost_rate=Decimal("0"),
        sell_tax_rate=Decimal("0"),
    ).run([point], OneXTwoXRecipe())

    assert result.metrics.maximum_drawdown == Decimal("-0.10")
    assert result.metrics.longest_recovery_days == 1


def test_backtest_charges_buys_and_sells_separately() -> None:
    points = [
        BacktestPoint(date(2026, 1, 1), Decimal("0"), Decimal("0"), Decimal("1")),
        BacktestPoint(date(2026, 1, 2), Decimal("0"), Decimal("0"), Decimal("2")),
    ]
    result = BacktestEngine(
        initial_nav=Decimal("100000"),
        one_way_cost_rate=Decimal("0.001"),
        sell_tax_rate=Decimal("0.002"),
    ).run(points, OneXTwoXRecipe())

    assert result.equity[0].cost == Decimal("100.000")
    assert result.equity[1].cost == Decimal("399.600000")
    assert result.metrics.sortino is not None


def test_execution_recipe_rejects_unsupported_exposure_instead_of_clamping() -> None:
    with pytest.raises(ValueError, match="between 0.0x and 2.0x"):
        OneXTwoXRecipe().allocate(Decimal("3.2"))


def test_backtest_accepts_all_cash_research_target() -> None:
    point = BacktestPoint(
        trading_date=date(2026, 1, 1),
        one_x_return=Decimal("-0.1"),
        two_x_return=Decimal("-0.2"),
        target_exposure=Decimal("0"),
    )
    result = BacktestEngine(initial_nav=Decimal("500000")).run([point], OneXTwoXRecipe())

    assert result.equity[0].nav == Decimal("500000")
    assert result.equity[0].actual_exposure == Decimal("0.0")
