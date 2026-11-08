"""Research-only capital schedules, return accounting and recovery diagnostics."""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal as D

from dynamic_leverage.services.account_replay import ReplayConfig, ReplayDay, ReplayResult


def add_months(day: date, months: int) -> date:
    month = day.year * 12 + day.month - 1 + months
    year, zero_month = divmod(month, 12)
    return date(year, zero_month + 1, min(day.day, calendar.monthrange(year, zero_month + 1)[1]))


def investment_schedule(
    rows: list[ReplayDay],
    initial_nav: D,
    *,
    installments: int = 1,
    monthly_contribution: D = D(0),
) -> list[ReplayDay]:
    """Release existing capital in equal calendar-month tranches, never fake deposits.

    Future contributions arrive on the first observed trading day in each calendar
    month after entry. All strategies must receive the same schedule.
    """
    if not rows or installments < 1 or initial_nav <= 0:
        raise ValueError("positive capital, installments and rows required")
    if not monthly_contribution.is_finite() or monthly_contribution < 0:
        raise ValueError("invalid monthly contribution")
    start = rows[0].day
    previous_month = (start.year, start.month)
    scheduled = []
    for index, row in enumerate(rows):
        offset = (row.day.year - start.year) * 12 + row.day.month - start.month
        released = min(offset + 1, installments)
        month = (row.day.year, row.day.month)
        new_month = month != previous_month
        scheduled.append(
            replace(
                row,
                contribution=monthly_contribution if new_month else D(0),
                reserved_cash=initial_nav * D(installments - released) / D(installments),
                scheduled_investment=index == 0 or (new_month and offset < installments),
            )
        )
        previous_month = month
    return scheduled


@dataclass
class CapitalRecoveryPolicy:
    """One 30% principal-loss exit, five-session recovery, permanent 40% stop.

    This matches the previously described experiment, not an automatic relaxation
    of the account gate. It runs in one chronological pass with actual account NAV.
    """

    soft_exit_used: bool = False
    in_cash: bool = False
    hard_stop: bool = False
    recovery_streak: int = 0
    events: list[dict] = field(default_factory=list)

    def target(self, row: ReplayDay, principal_loss: D) -> D:
        if principal_loss >= D("0.4") and not self.hard_stop:
            self.hard_stop = True
            self.events.append({"date": row.day, "kind": "permanent_principal_stop"})
        if self.hard_stop:
            return D(0)
        if principal_loss >= D("0.3") and not self.soft_exit_used:
            self.soft_exit_used = self.in_cash = True
            self.events.append({"date": row.day, "kind": "soft_exit_signal"})
            return D(0)
        if self.in_cash:
            self.recovery_streak = (
                self.recovery_streak + 1 if row.market_recovery_ready is True else 0
            )
            if self.recovery_streak < 5:
                return D(0)
            self.in_cash = False
            self.events.append({"date": row.day, "kind": "reentry_signal"})
        return row.target


def money_weighted_return(flows: list[tuple[date, float]]) -> float | None:
    """Annualized XIRR for deposits only plus terminal NAV: one sign change.

    No interim withdrawals are supported; monotonicity guarantees a unique root.
    """
    if len(flows) < 2 or flows[0][0] >= flows[-1][0]:
        return None
    if any(amount >= 0 for _, amount in flows[:-1]) or flows[-1][1] <= 0:
        raise ValueError("XIRR requires deposits followed by a positive terminal NAV")
    end = flows[-1][0]
    terminal = flows[-1][1]

    # Scale by terminal date to avoid singular discounted terminal values near -100%.
    def balance(log_rate: float) -> float:
        return (
            sum(
                amount * math.exp(min(700, log_rate * (end - day).days / 365.2425))
                for day, amount in flows[:-1]
            )
            + terminal
        )

    low, high = -32.0, 32.0
    if balance(low) < 0 or balance(high) > 0:
        return None
    for _ in range(100):
        middle = (low + high) / 2
        if balance(middle) > 0:
            low = middle
        else:
            high = middle
    return math.expm1((low + high) / 2)


def cashflow_metrics(result: ReplayResult, cfg: ReplayConfig) -> dict:
    daily = result.daily
    final = daily[-1]
    years = (final["date"] - daily[0]["date"]).days / 365.2425
    factor = math.prod(1 + float(r["daily_return"]) for r in daily)
    flows = [(daily[0]["date"], -float(cfg.initial_nav))]
    flows.extend((r["date"], -float(r["contribution"])) for r in daily if r["contribution"])
    flows.append((final["date"], float(final["nav"])))
    return {
        "start": daily[0]["date"],
        "end": final["date"],
        "final_nav": final["nav"],
        "net_contributions": final["net_contributions"],
        "net_profit": final["nav"] - final["net_contributions"],
        "calendar_cagr": float(final["nav"] / cfg.initial_nav) ** (1 / years) - 1
        if years > 0 and len(flows) == 2
        else None,
        "annualized_time_weighted_return": factor ** (1 / years) - 1 if years > 0 else None,
        "money_weighted_return": money_weighted_return(flows),
        "maximum_drawdown": max(r["maximum_drawdown"] for r in daily),
        "maximum_principal_loss": max(r["principal_loss"] for r in daily),
        "total_cost": sum(r["cost"] for r in daily),
        "cash_interest": sum(r["cash_interest"] for r in daily),
        "fill_count": len(result.fills),
        "cash_nonnegative": all(r["cash"] >= 0 for r in daily),
        "signal_before_fill": all(f["signal_date"] < f["date"] for f in result.fills),
        "numerical_40pct_limits_pass": all(
            r["maximum_drawdown"] < D("0.4") and r["principal_loss"] < D("0.4") for r in daily
        ),
    }


def reentry_acceptance(result: ReplayResult, policy: CapitalRecoveryPolicy) -> dict:
    signal = next((e["date"] for e in policy.events if e["kind"] == "reentry_signal"), None)
    stop = next((e["date"] for e in policy.events if e["kind"] == "permanent_principal_stop"), None)
    buys = [
        f
        for f in result.fills
        if signal
        and f["side"] == "BUY"
        and f["signal_date"] >= signal
        and (stop is None or f["signal_date"] < stop)
    ]
    blocked = [
        r
        for r in result.daily
        if signal and r["date"] >= signal and r["target"] > 0 and r["allowed_target"] == 0
    ]
    return {
        "reentry_signal_date": signal,
        "first_reentry_buy_date": buys[0]["date"] if buys else None,
        "reentry_buy_count": len(buys),
        "reentry_blocked_days": len(blocked),
        "reentry_executed": bool(buys),
        "permanent_principal_stop_date": stop,
        "policy_events": policy.events,
    }
