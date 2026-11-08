"""Observe frozen policy decisions without altering the account or policy."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import asdict
from decimal import Decimal as D

from dynamic_leverage.domain.reserve_policy import absolute_valuation_target


class BudgetDiagnostics:
    def __init__(self, policy):
        self.policy = policy
        self.records: list[dict] = []

    def initial_state(self):
        return self.policy.initial_state()

    def evaluate(self, inputs, account, state):
        decision = self.policy.evaluate(inputs, account, state)
        cfg = self.policy.config
        allocation = (
            absolute_valuation_target(inputs.pe_ttm)
            if cfg.fixed_target is None
            else cfg.fixed_target
        )
        caps = {"valuation": allocation, "risk_budget": decision.risk_exposure_cap}
        if "HARD_LIMIT_LIQUIDATE_NO_AUTO_REENTRY" in decision.reasons:
            caps["hard_lock"] = D(0)
        elif "CASH_EXIT_BEFORE_ANY_REENTRY" in decision.reasons:
            caps["cash_exit"] = D(0)
        elif not {"MARKET_REENTRY_CONFIRMED", "LEGACY_ACCOUNT_RECOVERY"}.intersection(
            decision.reasons
        ):
            caps["drawdown"] = decision.drawdown_exposure_cap
            if "DEFENSE_NO_BUYS" in decision.reasons:
                shock = (
                    (cfg.earnings_filter and inputs.eps_yoy <= D("-.2"))
                    or inputs.rv20 >= D(".45")
                    or inputs.taiex_daily_return <= D("-.07")
                )
                caps["shock"] = D(1) if shock else D("1.8")
        desired = min(caps.values())
        if desired != decision.desired:
            raise ValueError("diagnostic desired exposure differs from policy")
        target = desired
        if desired > account.exposure + D(".001"):
            if decision.state.paused:
                target = min(desired, account.exposure)
            elif (
                state.last_buy_week != inputs.as_of.isocalendar()[:2]
                and decision.buy_budget > 0
                and (
                    decision.state.mode == "RECOVERY"
                    or state.mode != "NORMAL"
                    or desired >= account.exposure + D(".1")
                )
            ):
                target = min(desired, account.exposure + 2 * decision.buy_budget / account.nav)
        if target != decision.target:
            raise ValueError("diagnostic final target differs from policy")
        self.records.append(
            {
                "date": inputs.as_of,
                "state_before": asdict(state),
                "inputs": asdict(inputs),
                "account": asdict(account),
                "mode": decision.state.mode,
                "valuation_request": allocation,
                "risk_budget_cap": decision.risk_exposure_cap,
                "drawdown_cap": decision.drawdown_exposure_cap,
                "active_caps": caps,
                "binding_caps": "|".join(k for k, v in caps.items() if v == desired),
                "desired": desired,
                "target": target,
                "buy_budget": decision.buy_budget,
                "actual_exposure_at_decision": account.exposure,
                "can_trade": decision.can_trade,
                "clear_days": decision.state.clear_days,
                "reasons": "|".join(decision.reasons),
                "buy_tranche_binding": target < desired
                and "BUDGETED_WEEKLY_BUY" in decision.reasons,
                "weekly_wait": "WEEKLY_BUY_ALREADY_PROPOSED" in decision.reasons,
                "buy_paused": "BUY_PAUSED" in decision.reasons,
            }
        )
        return decision


def rolling_two_years(rows, benchmark):
    dates = [row["date"] for row in rows]
    windows = []
    for i, day in enumerate(dates):
        anniversary = day.replace(
            year=day.year + 2, day=28 if day.month == 2 and day.day == 29 else day.day
        )
        j = bisect_left(dates, anniversary)
        if j == len(dates):
            break
        windows.append(
            {
                "start": day,
                "end": dates[j],
                "return": rows[j]["nav"] / rows[i]["nav"] - 1,
                "benchmark_return": benchmark[j]["nav"] / benchmark[i]["nav"] - 1,
            }
        )
    return {
        "count": len(windows),
        "overlapping_not_independent": True,
        "new_entry_cost_not_reapplied": True,
        "worst": min(windows, key=lambda w: w["return"]) if windows else None,
        "beat_benchmark_fraction": (
            sum(w["return"] > w["benchmark_return"] for w in windows) / len(windows)
            if windows
            else None
        ),
    }
