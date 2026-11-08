"""Research-only risk budgets and market-led reentry; never reset account high-water marks."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal as D

from dynamic_leverage.domain.models import StrategyInputs
from dynamic_leverage.domain.reserve_policy import (
    ReserveAccount,
    ReserveDecision,
    ReservePolicy,
    ReserveState,
    absolute_valuation_target,
)


def market_recovery_flags(closes: dict[date, D]) -> dict[date, bool]:
    """Current close versus the last 60 observed sessions, with no future-price access."""
    if any(not value.is_finite() or value <= 0 for value in closes.values()):
        raise ValueError("positive finite historical index closes required")
    dates = sorted(closes)
    return {
        day: closes[day] > sum((closes[p] for p in dates[i - 59 : i + 1]), D(0)) / 60
        for i, day in enumerate(dates)
        if i >= 59
    }


@dataclass(frozen=True)
class BudgetConfig:
    de_risk_start: D
    cash_trigger: D
    recovery_buy_fraction: D
    market_reentry: bool = True
    assumed_two_x_loss: D = D(".4")
    recovery_days: int = 5
    fixed_target: D | None = None
    earnings_filter: bool = True
    cash_rearm_drawdown: D | None = None
    recovery_without_execution_buffer: bool = False

    def __post_init__(self):
        values = (
            self.de_risk_start,
            self.cash_trigger,
            self.recovery_buy_fraction,
            self.assumed_two_x_loss,
        )
        if any(not x.is_finite() for x in values):
            raise ValueError("finite budget parameters required")
        if not 0 < self.de_risk_start < self.cash_trigger < D(".4"):
            raise ValueError("drawdown triggers must precede the fixed 40% hard limit")
        if not 0 < self.recovery_buy_fraction <= D(".1"):
            raise ValueError("weekly recovery tranche must be in (0, 10%]")
        if not 0 < self.assumed_two_x_loss <= 1 or self.recovery_days < 1:
            raise ValueError("invalid stress loss or recovery streak")
        if self.fixed_target is not None and (
            not self.fixed_target.is_finite() or not 0 <= self.fixed_target <= D("1.8")
        ):
            raise ValueError("fixed allocation must be finite and within 0 to 1.8")
        if self.cash_rearm_drawdown is not None and (
            not self.cash_rearm_drawdown.is_finite()
            or not 0 < self.cash_rearm_drawdown <= self.de_risk_start
        ):
            raise ValueError("cash exit rearm must be positive and at or below de-risk start")


@dataclass(frozen=True)
class BudgetState(ReserveState):
    mode: str = "NORMAL"
    cash_exit_pending: bool = False
    last_drawdown: D = D(0)
    cash_exit_armed: bool = True


@dataclass(frozen=True)
class BudgetDecision(ReserveDecision):
    risk_floor: D
    risk_exposure_cap: D
    drawdown_exposure_cap: D


class DrawdownBudgetPolicy(ReservePolicy):
    """Same account-policy interface, different research-only risk and recovery rules."""

    def __init__(self, config: BudgetConfig):
        self.config = config

    def initial_state(self) -> BudgetState:
        return BudgetState()

    def evaluate(
        self, inputs: StrategyInputs, account: ReserveAccount, state: BudgetState
    ) -> BudgetDecision:
        if not isinstance(state, BudgetState):
            raise ValueError("budget policy requires its own state")
        cfg = self.config
        required = (inputs.rv20, inputs.taiex_daily_return)
        if cfg.earnings_filter:
            required += (inputs.eps_yoy,)
        if cfg.fixed_target is None:
            required += (inputs.pe_ttm,)
        if not inputs.critical_data_safe or any(v is None or not v.is_finite() for v in required):
            raise ValueError("complete finite active policy inputs required")
        if inputs.rv20 < 0 or (state.last_day is not None and inputs.as_of <= state.last_day):
            raise ValueError("nonnegative volatility and advancing dates required")
        if (
            any(
                not v.is_finite() or v < 0
                for v in (account.nav, account.cash, account.exposure, account.drawdown)
            )
            or account.nav <= 0
        ):
            raise ValueError("invalid account snapshot")
        allocation = (
            absolute_valuation_target(inputs.pe_ttm)
            if cfg.fixed_target is None
            else cfg.fixed_target
        )
        if (
            account.initial_nav is None
            or not account.initial_nav.is_finite()
            or account.initial_nav <= 0
        ):
            raise ValueError("initial capital required for principal-loss protection")
        if not 0 <= account.drawdown < 1 or account.market_recovery_ready is None:
            raise ValueError("valid drawdown and observed recovery signal required")
        peak = account.nav / (1 - account.drawdown)
        reference = max(peak, account.initial_nav)
        # Half the gap between planned cash exit and 40% is retained as an execution buffer.
        buffer = (D(".4") - cfg.cash_trigger) / 2
        floor = reference * (D(".6") + buffer)
        risk_cap = min(
            D("1.8"), 2 * max(account.nav - floor, D(0)) / (account.nav * cfg.assumed_two_x_loss)
        )
        dd_cap = D("1.8") * min(
            D(1),
            max(
                D(0), (cfg.cash_trigger - account.drawdown) / (cfg.cash_trigger - cfg.de_risk_start)
            ),
        )
        fundamental_clear = (
            (not cfg.earnings_filter or inputs.eps_yoy > D("-.2"))
            and inputs.rv20 < D(".35")
            and inputs.taiex_daily_return > D("-.03")
        )
        clear = fundamental_clear and (
            account.market_recovery_ready if cfg.market_reentry else account.drawdown < D(".2")
        )
        shock = (
            (cfg.earnings_filter and inputs.eps_yoy <= D("-.2"))
            or inputs.rv20 >= D(".45")
            or inputs.taiex_daily_return <= D("-.07")
        )
        hard = (
            account.hard_limit_breached
            or state.mode == "HARD_LOCK"
            or account.drawdown >= D(".4")
            or account.nav <= account.initial_nav * D(".6")
        )
        defense = state.mode != "NORMAL" or account.drawdown >= cfg.de_risk_start or shock
        streak = state.clear_days + 1 if defense and clear and not shock else 0
        reasons = [
            "VALUATION_ABSOLUTE" if cfg.fixed_target is None else "FIXED_ALLOCATION",
            "STRESS_RISK_BUDGET",
        ]
        crossed_cash = (
            account.drawdown >= cfg.cash_trigger and state.last_drawdown < cfg.cash_trigger
        )
        armed = state.cash_exit_armed
        if cfg.cash_rearm_drawdown is not None:
            if account.drawdown < cfg.cash_rearm_drawdown and not state.cash_exit_pending:
                armed = True
                if not state.cash_exit_armed:
                    reasons.append("SOFT_CASH_EXIT_REARMED")
            crossed_cash = armed and account.drawdown >= cfg.cash_trigger
            if not armed and account.drawdown >= cfg.cash_trigger:
                reasons.append("SOFT_CASH_EXIT_NOT_REARMED")
        exit_pending = False
        if hard:
            mode, desired = "HARD_LOCK", D(0)
            reasons.append("HARD_LIMIT_LIQUIDATE_NO_AUTO_REENTRY")
        elif crossed_cash or state.cash_exit_pending:
            mode, desired, streak = "CASH", D(0), 0
            exit_pending = account.exposure > 0
            reasons.append("CASH_EXIT_BEFORE_ANY_REENTRY")
        elif defense:
            if (state.mode == "RECOVERY" and clear and not shock) or streak >= cfg.recovery_days:
                mode = "NORMAL" if account.drawdown < cfg.de_risk_start else "RECOVERY"
                if mode == "RECOVERY" and cfg.recovery_without_execution_buffer:
                    # Diagnostic only: same hard floor, less protection against execution gaps.
                    floor = reference * D(".6")
                    risk_cap = min(
                        D("1.8"),
                        2 * max(account.nav - floor, D(0)) / (account.nav * cfg.assumed_two_x_loss),
                    )
                    reasons.append("RECOVERY_BUFFER_REMOVED_DIAGNOSTIC")
                desired = min(allocation, risk_cap)
                reasons.append(
                    "MARKET_REENTRY_CONFIRMED" if cfg.market_reentry else "LEGACY_ACCOUNT_RECOVERY"
                )
            else:
                mode = "CASH" if account.drawdown >= cfg.cash_trigger else "DEFENSE"
                # A failed recovery returns to the original protective curve; high-water mark stays.
                desired = min(allocation, risk_cap, dd_cap, D(1) if shock else D("1.8"))
                exit_pending = mode == "CASH" and account.exposure > 0
                reasons.append("DEFENSE_NO_BUYS")
        else:
            mode, desired = "NORMAL", min(allocation, risk_cap, dd_cap)
        if cfg.cash_rearm_drawdown is not None and mode in {"CASH", "HARD_LOCK"}:
            armed = False
        next_state = replace(
            state,
            mode=mode,
            paused=mode in {"DEFENSE", "CASH", "HARD_LOCK"},
            clear_days=streak,
            last_day=inputs.as_of,
            cash_exit_pending=exit_pending,
            last_drawdown=account.drawdown,
            cash_exit_armed=armed,
        )
        cash_floor = account.nav * D(".1")
        tranche = (
            cfg.recovery_buy_fraction if mode == "RECOVERY" or state.mode != "NORMAL" else D(".1")
        )
        budget = min(max(account.cash - cash_floor, D(0)), account.nav * tranche)
        target, can_trade = desired, False
        week = inputs.as_of.isocalendar()[:2]
        active_cap = min(risk_cap, dd_cap) if mode not in {"RECOVERY", "HARD_LOCK"} else risk_cap
        urgent_cut = next_state.paused or account.exposure > active_cap + D(".001")
        liquidate = desired == 0 and account.exposure > 0
        if liquidate or (
            desired < account.exposure - D(".001")
            and (urgent_cut or desired <= account.exposure - D(".1"))
        ):
            can_trade = True
            reasons.append("MANDATORY_BUDGET_REDUCTION")
        elif desired > account.exposure + D(".001"):
            if next_state.paused:
                target = min(desired, account.exposure)
                reasons.append("BUY_PAUSED")
            elif state.last_buy_week == week:
                reasons.append("WEEKLY_BUY_ALREADY_PROPOSED")
            elif budget > 0 and (
                mode == "RECOVERY"
                or state.mode != "NORMAL"
                or desired >= account.exposure + D(".1")
            ):
                target = min(desired, account.exposure + 2 * budget / account.nav)
                # Small recovery tranches must not be blocked by the old 0.1 exposure band.
                can_trade = target > account.exposure + D(".001")
                if can_trade:
                    next_state = replace(next_state, last_buy_week=week)
                    reasons.append("BUDGETED_WEEKLY_BUY")
        else:
            reasons.append("WITHIN_SHARE_PRECISION_TOLERANCE")
        return BudgetDecision(
            desired,
            target,
            can_trade,
            cash_floor,
            budget,
            tuple(reasons),
            next_state,
            floor,
            risk_cap,
            dd_cap,
        )
