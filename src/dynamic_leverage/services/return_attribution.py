"""Exact, descriptive attribution of frozen historical weights; no trade recommendations."""

from __future__ import annotations

from math import exp, isfinite, log1p
from statistics import mean


def total_return(previous_close, current_close, split=1, dividend=0):
    """A pre-ex-date share becomes split shares, each with its ex-date cash entitlement."""
    if previous_close <= 0 or current_close <= 0 or split <= 0 or dividend < 0:
        raise ValueError("positive prices/split and nonnegative dividend required")
    return split * (current_close + dividend) / previous_close - 1


def attribution_bridge(days: list[dict], elapsed_days: int):
    if not days or elapsed_days <= 0:
        raise ValueError("nonempty path and positive elapsed time required")
    weight = mean(d["two_x_weight"] for d in days)
    totals = dict.fromkeys(
        [
            "benchmark_net",
            "one_x_full",
            "constant_linear",
            "constant_two_x",
            "dynamic_gross",
            "dynamic_net",
        ],
        0.0,
    )
    components = dict.fromkeys(
        ["benchmark_account", "average_exposure", "vehicle", "timing", "direct_cost"], 0.0
    )
    output = []
    for day in days:
        returns = {
            "benchmark_net": day["benchmark_return"],
            "one_x_full": day["one_x_return"],
            "constant_linear": 2 * weight * day["one_x_return"],
            "constant_two_x": weight * day["two_x_return"],
            "dynamic_gross": day["strategy_return"] + day["cost_fraction"],
            "dynamic_net": day["strategy_return"],
        }
        if any(not isfinite(v) or v <= -1 for v in returns.values()):
            raise ValueError("finite returns greater than -100% required")
        if abs(returns["dynamic_gross"] - day["two_x_weight"] * day["two_x_return"]) > 1e-10:
            raise ValueError("prior-close weight does not reconcile account return")
        logs = {n: log1p(v) for n, v in returns.items()}
        parts = dict(
            zip(
                components,
                [b - a for a, b in zip(logs.values(), list(logs.values())[1:], strict=False)],
                strict=True,
            )
        )
        for key, value in logs.items():
            totals[key] += value
        for key, value in parts.items():
            components[key] += value
        output.append({**day, **{f"log_{k}": v for k, v in parts.items()}})
    gap = totals["dynamic_net"] - totals["benchmark_net"]
    if abs(sum(components.values()) - gap) > 1e-10:
        raise AssertionError("attribution does not sum to observed compound return gap")
    years = elapsed_days / 365.2425
    levels = {name: exp(value / years) - 1 for name, value in totals.items()}
    return {
        "mean_prior_close_two_x_weight": weight,
        "mean_prior_close_equivalent_exposure": 2 * weight,
        "cagr_levels": levels,
        "log_contributions": components,
        "annualized_log_contributions": {n: v / years for n, v in components.items()},
        "total_log_relative_return": gap,
        "cagr_gap": levels["dynamic_net"] - levels["benchmark_net"],
        "reconciliation_residual": sum(components.values()) - gap,
        "daily": output,
    }
