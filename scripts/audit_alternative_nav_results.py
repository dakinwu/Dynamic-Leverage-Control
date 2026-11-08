"""Reconcile each saved proxy ledger from fills, events and frozen input prices."""

from __future__ import annotations

import argparse
import csv
import json
from decimal import Decimal as D
from pathlib import Path

from run_alternative_nav_study import prepare

from dynamic_leverage.services.research_registry import file_sha


def audit(folder: Path, output: Path, *, prepared=None):
    if output.exists():
        raise FileExistsError("preserve prior audit")
    report = json.loads((folder / "results.json").read_bytes())
    for name, expected in report["output_sha256"].items():
        if file_sha(folder / name) != expected:
            raise ValueError("result CSV fingerprint mismatch")
    series, manifests, _ = prepare() if prepared is None else prepared
    if manifests != report["data_manifest_sha256"]:
        raise ValueError("frozen source manifest mismatch")
    cases, annual = [], []
    for scenario, group in report["groups"].items():
        delay = int(scenario.split("-")[0].removeprefix("delay"))
        for key, metrics in group["results"].items():
            symbol = key.split("-")[0]
            with (folder / f"{scenario}-{key}-daily.csv").open() as src:
                daily = list(csv.DictReader(src))
            fill_file = folder / f"{scenario}-{key}-fills.csv"
            with fill_file.open() as src:
                fills = list(csv.DictReader(src))
            by_day = {r["date"]: r for r in fills}
            if len(by_day) != len(fills):
                raise ValueError("unexpected multiple daily fills")
            dates = {str(p.day): i for i, p in enumerate(series[symbol])}
            if len(daily) != len(dates):
                raise ValueError("ledger calendar mismatch")
            previous_nav, units, cost_sum = D(500000), 0, D(0)
            previous_price, yearly = None, {}
            max_error = D(0)
            for point, row in zip(series[symbol], daily, strict=True):
                if row["date"] != str(point.day) or D(row["price"]) != point.price:
                    raise ValueError("ledger price/date mismatch")
                owned = D(units) * point.split
                expected = previous_nav
                if previous_price is not None:
                    expected += owned * point.price - D(units) * previous_price
                if point.dividend:
                    expected += owned * point.dividend[0]
                fill = by_day.get(str(point.day))
                if fill:
                    if dates[fill["date"]] - dates[fill["signal_date"]] < delay:
                        raise ValueError("fill violated signal delay")
                    if not point.fill_available:
                        raise ValueError("fill on unavailable proxy quote")
                    expected -= D(fill["cost"])
                    cost_sum += D(fill["cost"])
                    owned += int(fill["units"]) * (1 if fill["side"] == "BUY" else -1)
                if owned != int(row["units"]):
                    raise ValueError("unit conservation failed")
                actual = D(row["nav"])
                max_error = max(max_error, abs(expected - actual))
                if abs(expected - actual) > D("0.000001"):
                    raise ValueError("wealth reconciliation failed")
                if actual != D(row["cash"]) + D(row["receivables"]) + owned * point.price:
                    raise ValueError("balance sheet did not add up")
                if D(row["cash"]) < 0:
                    raise ValueError("borrowed cash detected")
                year = point.day.year
                yearly.setdefault(year, [previous_nav, actual])
                yearly[year][1] = actual
                previous_nav, previous_price, units = actual, point.price, int(owned)
            if cost_sum != D(metrics["total_cost"]) or previous_nav != D(metrics["final_nav"]):
                raise ValueError("summary does not match ledger")
            for year, (start, end) in yearly.items():
                annual.append(
                    {
                        "scenario": scenario,
                        "case": key,
                        "year": year,
                        "period_return": end / start - 1,
                        "partial_year": year in {2017, 2026},
                    }
                )
            cases.append(
                {
                    "scenario": scenario,
                    "case": key,
                    "sessions": len(daily),
                    "fills": len(fills),
                    "maximum_wealth_error_twd": str(max_error),
                }
            )
    with (folder / "annual-returns.csv").open("x", newline="", encoding="utf-8") as dst:
        writer = csv.DictWriter(dst, fieldnames=list(annual[0]))
        writer.writeheader()
        writer.writerows(annual)
    result = {
        "schema": "alternative-nav-ledger-audit-v1",
        "acceptance_passed": True,
        "results_sha256": file_sha(folder / "results.json"),
        "source_sha256": file_sha(Path(__file__)),
        "cases": cases,
        "reconciled_daily_balances": sum(c["sessions"] for c in cases),
        "annual_csv_sha256": file_sha(folder / "annual-returns.csv"),
        "execution_backtest_ready": False,
        "profitability_demonstrated": False,
    }
    with output.open("x", encoding="utf-8") as dst:
        json.dump(result, dst, indent=2)
        dst.write("\n")
    print(
        json.dumps(
            {
                "cases": len(cases),
                "daily_balances": result["reconciled_daily_balances"],
                "accounting_acceptance": True,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.folder, args.output)
