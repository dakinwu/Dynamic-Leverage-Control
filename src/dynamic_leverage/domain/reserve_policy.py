"""Research-only valuation allocation with explicit cash budgets and soft recovery."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal as D

from dynamic_leverage.domain.models import StrategyInputs


def absolute_valuation_target(pe: D) -> D:
    """Frozen absolute PE allocation bands shared by research controls."""
    if not pe.is_finite() or pe <= 0:
        raise ValueError("finite positive PE required")
    bands = [
        (D(28), D(".6")),
        (D(26), D(1)),
        (D(24), D("1.2")),
        (D(22), D("1.4")),
        (D(20), D("1.6")),
    ]
    return next((target for threshold, target in bands if pe >= threshold), D("1.8"))


@dataclass(frozen=True)
class ReserveConfig:
    valuation: str = "absolute"
    staged: bool = True
    soft_controls: bool = True

    def __post_init__(self):
        if self.valuation not in {"absolute", "percentile"}:
            raise ValueError("unknown reserve valuation model")


@dataclass(frozen=True)
class ReserveAccount:
    nav: D
    cash: D
    exposure: D
    drawdown: D
    hard_limit_breached: bool
    initial_nav: D | None = None
    market_recovery_ready: bool | None = None


@dataclass(frozen=True)
class ReserveState:
    paused: bool = False
    clear_days: int = 0
    last_buy_week: tuple[int, int] | None = None
    last_day: date | None = None


@dataclass(frozen=True)
class ReserveDecision:
    desired: D
    target: D
    can_trade: bool
    cash_floor: D
    buy_budget: D
    reasons: tuple[str, ...]
    state: ReserveState


class ReservePolicy:
    def __init__(self, config: ReserveConfig | None = None):
        self.config = config or ReserveConfig()

    def initial_state(self) -> ReserveState:
        return ReserveState()

    def evaluate(
        self, inputs: StrategyInputs, account: ReserveAccount, state: ReserveState
    ) -> ReserveDecision:
        required = (
            inputs.pe_ttm,
            inputs.eps_yoy,
            inputs.rv20,
            inputs.taiex_daily_return,
        )
        if self.config.valuation == "percentile":
            required += (inputs.pe_percentile,)
        if not inputs.critical_data_safe or any(v is None or not v.is_finite() for v in required):
            raise ValueError("reserve policy needs complete finite historical inputs")
        if (
            inputs.pe_ttm <= 0
            or inputs.rv20 < 0
            or (self.config.valuation == "percentile" and not 0 <= inputs.pe_percentile <= 1)
        ):
            raise ValueError("invalid reserve valuation or volatility")
        if state.last_day is not None and inputs.as_of <= state.last_day:
            raise ValueError("reserve policy dates must advance")
        if (
            any(
                not v.is_finite() or v < 0
                for v in (account.nav, account.cash, account.exposure, account.drawdown)
            )
            or account.nav <= 0
        ):
            raise ValueError("invalid reserve account snapshot")
        if self.config.valuation == "absolute":
            desired = absolute_valuation_target(inputs.pe_ttm)
        else:
            bands = [(D(".8"), D(".6")), (D(".6"), D(1)), (D(".4"), D("1.2")), (D(".2"), D("1.6"))]
            value = inputs.pe_percentile
            desired = next((target for threshold, target in bands if value >= threshold), D("1.8"))
        reasons = ["VALUATION_" + self.config.valuation.upper()]
        next_state = replace(state, last_day=inputs.as_of)
        if self.config.soft_controls:
            triggers = [
                name
                for hit, name in [
                    (inputs.eps_yoy <= D("-.2"), "EARNINGS_DECLINE"),
                    (inputs.rv20 >= D(".45"), "HIGH_VOLATILITY"),
                    (inputs.taiex_daily_return <= D("-.07"), "DAILY_CRASH"),
                    (account.drawdown >= D(".25"), "ACCOUNT_SOFT_DRAWDOWN"),
                ]
                if hit
            ]
            paused = state.paused or bool(triggers)
            clear = (
                inputs.eps_yoy > D("-.2")
                and inputs.rv20 < D(".35")
                and inputs.taiex_daily_return > D("-.03")
                and account.drawdown < D(".2")
            )
            streak = state.clear_days + 1 if paused and clear and not triggers else 0
            if paused and streak >= 5:
                paused, streak = False, 0
                reasons.append("SOFT_RECOVERY_CONFIRMED")
            next_state = replace(next_state, paused=paused, clear_days=streak)
            reasons.extend(triggers)
            if paused:
                desired = min(desired, D(1))
                reasons.append("SOFT_BUY_PAUSE")
        cash_floor = account.nav * D(".1")
        available = max(D(0), account.cash - cash_floor)
        budget = min(available, account.nav * D(".1")) if self.config.staged else available
        target, can_trade = desired, False
        week = inputs.as_of.isocalendar()[:2]
        forced_reduction = (next_state.paused or account.hard_limit_breached) and (
            desired < account.exposure - D(".001")
        )
        if desired <= account.exposure - D(".1") or forced_reduction:
            can_trade = True
            reasons.append("REFILL_CASH_RESERVE")
        elif desired >= account.exposure + D(".1"):
            if next_state.paused or account.hard_limit_breached:
                target = min(desired, account.exposure)
                reasons.append("BUY_PAUSED_OR_HARD_LIMIT")
            elif state.last_buy_week == week:
                reasons.append("WEEKLY_BUY_ALREADY_PROPOSED")
            elif budget <= 0:
                reasons.append("NO_SPENDABLE_RESERVE")
            else:
                target = min(desired, account.exposure + 2 * budget / account.nav)
                can_trade = target >= account.exposure + D(".1")
                if can_trade:
                    next_state = replace(next_state, last_buy_week=week)
                    reasons.append("STAGED_BUY" if self.config.staged else "UNSTAGED_BUY")
        else:
            reasons.append("WITHIN_REBALANCE_BAND")
        return ReserveDecision(
            desired, target, can_trade, cash_floor, budget, tuple(reasons), next_state
        )
