from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import sqrt
from statistics import mean, pstdev

from dynamic_leverage.domain.execution import ExecutionRecipe


@dataclass(frozen=True, slots=True)
class BacktestPoint:
    trading_date: date
    one_x_return: Decimal
    two_x_return: Decimal
    target_exposure: Decimal
    two_x_exposure: Decimal = Decimal("2.0")
    cash_return: Decimal = Decimal("0")
    # A carried closing price on a suspended day is a valuation, not a fill.
    rebalance_allowed: bool = True


@dataclass(frozen=True, slots=True)
class EquityPoint:
    trading_date: date
    nav: Decimal
    daily_return: Decimal
    actual_exposure: Decimal
    turnover: Decimal
    cost: Decimal


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    total_return: Decimal
    cagr: Decimal
    annualized_volatility: Decimal
    maximum_drawdown: Decimal
    longest_recovery_days: int
    sortino: Decimal | None
    turnover: Decimal
    total_cost: Decimal
    worst_1_day: Decimal
    worst_5_day: Decimal
    worst_20_day: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    equity: tuple[EquityPoint, ...]
    metrics: BacktestMetrics


class BacktestEngine:
    def __init__(
        self,
        *,
        initial_nav: Decimal = Decimal("500000"),
        one_way_cost_rate: Decimal = Decimal("0.001425"),
        sell_tax_rate: Decimal = Decimal("0.001"),
        no_trade_band: Decimal = Decimal("0.15"),
    ) -> None:
        if initial_nav <= 0:
            raise ValueError("initial NAV must be positive")
        self.initial_nav = initial_nav
        self.one_way_cost_rate = one_way_cost_rate
        self.sell_tax_rate = sell_tax_rate
        self.no_trade_band = no_trade_band

    def run(self, points: Iterable[BacktestPoint], recipe: ExecutionRecipe) -> BacktestResult:
        rows = list(points)
        if not rows:
            raise ValueError("backtest requires at least one point")
        if any(
            current.trading_date <= previous.trading_date
            for previous, current in zip(rows, rows[1:], strict=False)
        ):
            raise ValueError("backtest dates must be strictly increasing")

        nav = self.initial_nav
        weights: dict[str, Decimal] = {}
        equity: list[EquityPoint] = []

        for point in rows:
            opening_nav = nav
            target_allocations = recipe.allocate(point.target_exposure)
            target_weights = {
                allocation.instrument: allocation.weight for allocation in target_allocations
            }
            actual_exposure = self._actual_exposure(weights, point.two_x_exposure)
            turnover = Decimal("0")
            cost = Decimal("0")

            if point.rebalance_allowed and (
                not weights or abs(actual_exposure - point.target_exposure) >= self.no_trade_band
            ):
                turnover = self._turnover(weights, target_weights)
                buy_weight, sell_weight = self._trade_notional_weights(weights, target_weights)
                cost = nav * (
                    (buy_weight + sell_weight) * self.one_way_cost_rate
                    + sell_weight * self.sell_tax_rate
                )
                nav -= cost
                weights = target_weights

            gross_return = sum(
                (
                    weight * self._instrument_return(instrument, point)
                    for instrument, weight in weights.items()
                ),
                Decimal("0"),
            )
            nav *= Decimal("1") + gross_return
            daily_return = nav / opening_nav - Decimal("1")
            weights = self._drift_weights(weights, point, gross_return)
            actual_exposure = self._actual_exposure(weights, point.two_x_exposure)
            equity.append(
                EquityPoint(
                    trading_date=point.trading_date,
                    nav=nav,
                    daily_return=daily_return,
                    actual_exposure=actual_exposure,
                    turnover=turnover,
                    cost=cost,
                )
            )

        return BacktestResult(tuple(equity), self._metrics(equity))

    @staticmethod
    def _instrument_return(instrument: str, point: BacktestPoint) -> Decimal:
        if instrument == "TWD":
            return point.cash_return
        if instrument in {"0050", "006208", "1X"}:
            return point.one_x_return
        if instrument in {"00631L", "00685L", "2X"}:
            return point.two_x_return
        raise ValueError(f"unsupported backtest instrument: {instrument}")

    @staticmethod
    def _turnover(current: dict[str, Decimal], target: dict[str, Decimal]) -> Decimal:
        instruments = set(current) | set(target)
        return sum(
            (
                abs(target.get(item, Decimal("0")) - current.get(item, Decimal("0")))
                for item in instruments
            ),
            Decimal("0"),
        ) / Decimal("2")

    @staticmethod
    def _trade_notional_weights(
        current: dict[str, Decimal], target: dict[str, Decimal]
    ) -> tuple[Decimal, Decimal]:
        instruments = (set(current) | set(target)) - {"TWD"}
        buy_weight = sum(
            (
                max(target.get(item, Decimal("0")) - current.get(item, Decimal("0")), Decimal("0"))
                for item in instruments
            ),
            Decimal("0"),
        )
        sell_weight = sum(
            (
                max(current.get(item, Decimal("0")) - target.get(item, Decimal("0")), Decimal("0"))
                for item in instruments
            ),
            Decimal("0"),
        )
        return buy_weight, sell_weight

    @staticmethod
    def _actual_exposure(weights: dict[str, Decimal], two_x_exposure: Decimal) -> Decimal:
        return (
            weights.get("0050", Decimal("0"))
            + weights.get("006208", Decimal("0"))
            + weights.get("1X", Decimal("0"))
            + two_x_exposure
            * (
                weights.get("00631L", Decimal("0"))
                + weights.get("00685L", Decimal("0"))
                + weights.get("2X", Decimal("0"))
            )
        )

    def _drift_weights(
        self,
        weights: dict[str, Decimal],
        point: BacktestPoint,
        portfolio_return: Decimal,
    ) -> dict[str, Decimal]:
        denominator = Decimal("1") + portfolio_return
        return {
            instrument: weight
            * (Decimal("1") + self._instrument_return(instrument, point))
            / denominator
            for instrument, weight in weights.items()
        }

    def _metrics(self, equity: list[EquityPoint]) -> BacktestMetrics:
        returns = [float(item.daily_return) for item in equity]
        navs = [item.nav for item in equity]
        total_return = navs[-1] / self.initial_nav - Decimal("1")
        years = Decimal(len(equity)) / Decimal("252")
        cagr = Decimal(str(float(navs[-1] / self.initial_nav) ** (1 / float(years)) - 1))
        volatility = Decimal(str(pstdev(returns) * sqrt(252))) if len(returns) > 1 else Decimal("0")
        maximum_drawdown, longest_recovery = self._drawdown([self.initial_nav, *navs])
        downside_deviation = sqrt(mean(min(value, 0) ** 2 for value in returns) * 252)
        sortino = (
            Decimal(str(mean(returns) * 252 / downside_deviation))
            if downside_deviation > 0
            else None
        )
        return BacktestMetrics(
            total_return=total_return,
            cagr=cagr,
            annualized_volatility=volatility,
            maximum_drawdown=maximum_drawdown,
            longest_recovery_days=longest_recovery,
            sortino=sortino,
            turnover=sum((item.turnover for item in equity), Decimal("0")),
            total_cost=sum((item.cost for item in equity), Decimal("0")),
            worst_1_day=self._worst_window(equity, 1),
            worst_5_day=self._worst_window(equity, 5),
            worst_20_day=self._worst_window(equity, 20),
        )

    @staticmethod
    def _drawdown(navs: list[Decimal]) -> tuple[Decimal, int]:
        peak = navs[0]
        maximum = Decimal("0")
        recovery_start: int | None = None
        longest = 0
        for index, nav in enumerate(navs):
            if nav >= peak:
                peak = nav
                if recovery_start is not None:
                    longest = max(longest, index - recovery_start)
                    recovery_start = None
            else:
                recovery_start = recovery_start if recovery_start is not None else index
                maximum = min(maximum, nav / peak - Decimal("1"))
        if recovery_start is not None:
            longest = max(longest, len(navs) - recovery_start)
        return maximum, longest

    @staticmethod
    def _worst_window(equity: list[EquityPoint], window: int) -> Decimal:
        if len(equity) < window:
            return Decimal("0")
        compounded = []
        for start in range(len(equity) - window + 1):
            value = Decimal("1")
            for point in equity[start : start + window]:
                value *= Decimal("1") + point.daily_return
            compounded.append(value - Decimal("1"))
        return min(compounded)
