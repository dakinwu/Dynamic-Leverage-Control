"""Descriptive attribution helpers; risk matching never selects on performance."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from math import sqrt
from statistics import pstdev


def window_metrics(rows: list[dict], start: date, end: date, initial_nav: D = D(500000)):
    if not rows or any(a["date"] >= b["date"] for a, b in zip(rows, rows[1:], strict=False)):
        raise ValueError("strictly increasing account dates required")
    indices = [i for i, r in enumerate(rows) if start <= r["date"] <= end]
    if not indices:
        raise ValueError("empty evaluation window")
    first = indices[0]
    baseline = rows[first - 1]["nav"] if first else initial_nav
    baseline_day = rows[first - 1]["date"] if first else rows[0]["date"]
    selected = [rows[i] for i in indices]
    days = (selected[-1]["date"] - baseline_day).days
    if days <= 0 or baseline <= 0:
        raise ValueError("positive baseline and elapsed period required")
    peak, dd = baseline, D(0)
    previous, returns = baseline, []
    for row in selected:
        nav = row["nav"]
        if nav <= 0 or not nav.is_finite():
            raise ValueError("finite positive NAV required")
        peak = max(peak, nav)
        dd = min(dd, nav / peak - 1)
        returns.append(float(nav / previous - 1))
        previous = nav
    return {
        "start": selected[0]["date"],
        "end": selected[-1]["date"],
        "baseline_day": baseline_day,
        "baseline_nav": baseline,
        "total_return": selected[-1]["nav"] / baseline - 1,
        "calendar_cagr": D(str(float(selected[-1]["nav"] / baseline) ** (365.2425 / days) - 1)),
        "annualized_volatility": D(str(pstdev(returns) * sqrt(252))),
        "maximum_drawdown": dd,
        "mean_actual_exposure": sum(r["actual_exposure"] for r in selected) / len(selected),
        "total_cost": sum(r["cost"] for r in selected),
        "account_state_restarted": False,
    }


def match_risk(candidate: dict, controls: dict[str, dict], field: str, tolerance: D):
    if field not in {"mean_actual_exposure", "annualized_volatility"} or tolerance < 0:
        raise ValueError("only exposure or volatility can select a risk match")
    if not controls:
        raise ValueError("nonempty fixed control grid required")
    name = min(
        controls,
        key=lambda n: (abs(controls[n][field] - candidate[field]), controls[n]["fixed_target"]),
    )
    distance = abs(controls[name][field] - candidate[field])
    return {
        "control": name,
        "selection_metric": field,
        "distance": distance,
        "within_tolerance": distance <= tolerance,
        "tolerance": tolerance,
        "selected_on_return": False,
    }


def dominates(candidate, benchmark):
    return (
        candidate["calendar_cagr"] > benchmark["calendar_cagr"]
        and candidate["annualized_volatility"] <= benchmark["annualized_volatility"]
        and candidate["maximum_drawdown"] >= benchmark["maximum_drawdown"]
    )
