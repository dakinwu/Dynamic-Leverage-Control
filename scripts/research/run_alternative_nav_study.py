"""Fixed R17 NAV scenarios. Offline only; no executable-price or OOS claim."""

from __future__ import annotations

import argparse
import bisect
import calendar
import csv
import json
import math
import statistics
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from audit_alternative_nav import parse_nav

from dynamic_leverage.services.nav_proxy_research import (
    NavPoint,
    ProxyConfig,
    simulate,
)
from dynamic_leverage.services.research_artifacts import read_dataset
from dynamic_leverage.services.research_registry import (
    digest,
    duplicate_preflight,
    file_sha,
)

ROOT = Path(__file__).resolve().parents[2]



START, END = date(2017, 3, 30), date(2026, 9, 17)
SPEC = {
    "route_id": "R17",
    "start": str(START),
    "end": str(END),
    "capital_twd": 500000,
    "inflows": 0,
    "borrowing": False,
    "cash_rate": 0,
    "universe": ["0050", "00675L", "00670L", "00662"],
    "rule": "observed RV20 < 20%: 60% 2x, otherwise 40% 2x",
    "static": "60% 2x / 40% cash",
    "risk": "static and dynamic permanent exit at 40% peak or principal loss",
    "buyhold": "ungated; paid cash distributions reinvested after signal delay",
    "delays": [1, 2],
    "cost_multipliers": [1, 2],
    "trade_exposure_band": 0.15,
    "costs": {"commission": 0.001425, "min": 20, "sell_tax": 0.001, "slippage": 0.0005},
    "units": "integer",
    "settlement_sessions": 2,
    "rolling": "first observed session of month; fresh capital; at least 24 calendar months",
    "missing_nav": "carry valuation only; not a new return observation or proxy fill",
    "data_kind": "nav_proxy",
    "new_out_of_sample": False,
}


def serial(value):
    if isinstance(value, D):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def write_csv(path, rows):
    if not rows:
        return
    with path.open("x", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def verified_file(folder, entry):
    path = (folder / entry["raw_file"]).resolve()
    if not path.is_relative_to(folder.resolve()) or file_sha(path) != entry["body_sha256"]:
        raise ValueError("source path or fingerprint mismatch")
    return path.read_bytes()


def prepare(root=ROOT):
    nav_folder = root / "data/private/alternative-leveraged-nav-20261018"
    event_folder = root / "data/private/alternative-return-events-20261018"
    frozen = root / "data/private/rearm-dataset-20260928"
    sources = {}
    for folder in (nav_folder, event_folder, frozen):
        sources[str((folder / "manifest.json").relative_to(root)).replace("\\", "/")] = file_sha(
            folder / "manifest.json"
        )
    events = json.loads((event_folder / "manifest.json").read_bytes())
    for entry in events["observations"] + events["derived_files"]:
        verified_file(event_folder, entry)
    for symbol in ("00675L", "00670L", "00662"):
        if events["acceptance"].get(symbol) != "qualified_for_NAV_path_research":
            raise ValueError("reviewed NAV path acceptance required")
    nav_manifest = json.loads((nav_folder / "manifest.json").read_bytes())
    histories = {}
    for entry in nav_manifest["observations"]:
        rows = parse_nav(verified_file(nav_folder, entry), entry["symbol"])
        histories[entry["symbol"]] = {
            date.fromisoformat(r["day"]): D(r["reported_unit_nav"]) for r in rows
        }
    if set(histories) != {"00675L", "00670L", "00662"}:
        raise ValueError("exact declared NAV universe required")
    manifest = json.loads((frozen / "manifest.json").read_bytes())
    entry = manifest["datasets"]["real-etf-base"]
    replay, _ = read_dataset(frozen / entry["file"], entry["sha256"])
    history = histories["00662"]
    days = sorted(history)
    returns = [float(history[b] / history[a] - 1) for a, b in zip(days, days[1:], strict=False)]
    vols = {
        days[i + 1]: D(str(statistics.stdev(returns[i - 19 : i + 1]) * math.sqrt(252)))
        for i in range(19, len(returns))
    }
    vdays = sorted(vols)
    series = {key: [] for key in SPEC["universe"]}
    missing = {key: 0 for key in histories}
    ordered = {key: sorted(values) for key, values in histories.items()}
    for row in replay:
        if not START <= row.day <= END:
            continue
        quote = row.quotes["0050"]
        series["0050"].append(
            NavPoint(
                row.day,
                quote.close,
                row.day,
                quote.buy_allowed and quote.sell_allowed,
                split=row.splits.get("0050", D(1)),
                dividend=row.dividends.get("0050"),
            )
        )
        for symbol, values in histories.items():
            pos = bisect.bisect_right(ordered[symbol], row.day) - 1
            if pos < 0:
                raise ValueError("no prior NAV observation for valuation")
            asof = ordered[symbol][pos]
            vi = bisect.bisect_right(vdays, row.day) - 1
            rv = (
                row.policy_inputs.rv20
                if symbol == "00675L"
                else vols[vdays[vi]]
                if vi >= 0
                else None
            )
            available = row.day in values
            missing[symbol] += not available
            series[symbol].append(NavPoint(row.day, values[asof], asof, available, rv))
    if series["0050"][0].day != START or series["0050"][-1].day != END:
        raise ValueError("declared common period not covered")
    return (
        series,
        sources,
        {
            "sessions": len(series["0050"]),
            "carried_nav_sessions": missing,
            "full_halt_history_verified": False,
            "publication_clock_verified": False,
        },
    )


def numeric_pass(candidate, baseline):
    return (
        candidate["calendar_cagr"] > baseline["calendar_cagr"]
        and candidate["annualized_volatility"] <= baseline["annualized_volatility"]
        and candidate["maximum_drawdown"] <= baseline["maximum_drawdown"]
        and candidate["maximum_drawdown"] <= D("0.4")
        and candidate["maximum_principal_loss"] <= D("0.4")
    )


def rolling_ranges(points):
    days = [p.day for p in points]
    starts = [i for i in range(len(days)) if i == 0 or days[i].month != days[i - 1].month]
    ranges = []
    for i in starts:
        start = days[i]
        anniversary = date(
            start.year + 2,
            start.month,
            min(start.day, calendar.monthrange(start.year + 2, start.month)[1]),
        )
        end = bisect.bisect_left(days, anniversary)
        if end < len(days):
            ranges.append((i, end + 1))
    return ranges


def run(output):
    if output.exists():
        raise FileExistsError("preserve old experiment; use a new output")
    series, manifests, acceptance = prepare()
    code = {
        name: file_sha(ROOT / name)
        for name in (
            "scripts/run_alternative_nav_study.py",
            "scripts/audit_alternative_nav.py",
            "src/dynamic_leverage/services/nav_proxy_research.py",
        )
    }
    protocol = "docs/research/alternative-return-protocol-20261018.md"
    identity = {
        "schema": "alternative-nav-proxy-study-v1",
        "specification": SPEC,
        "manifests": manifests,
        "engines": code,
        "protocol_sha256": file_sha(ROOT / protocol),
    }
    duplicate_preflight(ROOT, digest(identity))
    output.mkdir(parents=True)
    report = {
        "schema": "alternative-nav-proxy-study-v1",
        "route_id": "R17",
        "experiment_identity": identity,
        "registered_specification": SPEC,
        "source_sha256": code,
        "data_manifest_sha256": manifests,
        "acceptance": acceptance,
        "new_out_of_sample": False,
        "profitability_demonstrated": False,
        "target_exposure": None,
        "groups": {},
    }
    instruments = [("0050", "buyhold"), ("00662", "buyhold")] + [
        (s, mode) for s in ("00675L", "00670L") for mode in ("buyhold", "static-1.2", "vol20")
    ]
    ranges = rolling_ranges(series["0050"])
    window_rows = []
    for delay in SPEC["delays"]:
        for cost in SPEC["cost_multipliers"]:
            group_id = f"delay{delay}-cost{cost}"
            group = {"results": {}, "rolling_summary": {}}
            report["groups"][group_id] = group
            cfg = ProxyConfig(delay=delay, cost_multiplier=D(cost))
            for symbol, mode in instruments:
                key = f"{symbol}-{mode}"
                multiple = D(1) if symbol in {"0050", "00662"} else D(2)
                metrics, daily, fills = simulate(
                    series[symbol], mode, cfg, exposure_multiple=multiple
                )
                metrics["data_kind"] = "market_history" if symbol == "0050" else "nav_proxy"
                group["results"][key] = metrics
                write_csv(output / f"{group_id}-{key}-daily.csv", daily)
                write_csv(output / f"{group_id}-{key}-fills.csv", fills)
            baseline = group["results"]["0050-buyhold"]
            for metrics in group["results"].values():
                metrics["numerical_objective_pass"] = numeric_pass(metrics, baseline)
                metrics["cagr_difference_vs_0050"] = (
                    metrics["calendar_cagr"] - baseline["calendar_cagr"]
                )
            windows = {f"{s}-{m}": [] for s, m in instruments}
            for left, right in ranges:
                observations = {}
                for symbol, mode in instruments:
                    key = f"{symbol}-{mode}"
                    metrics, _, _ = simulate(
                        series[symbol][left:right],
                        mode,
                        cfg,
                        exposure_multiple=D(1) if symbol in {"0050", "00662"} else D(2),
                    )
                    observations[key] = metrics
                for key, metrics in observations.items():
                    base = observations["0050-buyhold"]
                    row = {
                        "scenario": group_id,
                        "case": key,
                        **metrics,
                        "outperforms_0050": metrics["calendar_cagr"] > base["calendar_cagr"],
                        "numeric_objective_pass": numeric_pass(metrics, base),
                    }
                    windows[key].append(row)
                    window_rows.append(row)
            for key, rows in windows.items():
                worst = min(rows, key=lambda row: row["final_nav"])
                group["rolling_summary"][key] = {
                    "window_count": len(rows),
                    "overlapping_not_independent": True,
                    "return_win_fraction_vs_0050": sum(r["outperforms_0050"] for r in rows)
                    / len(rows),
                    "numeric_objective_pass_fraction": sum(
                        r["numeric_objective_pass"] for r in rows
                    )
                    / len(rows),
                    "worst_two_year_start": worst["start"],
                    "worst_two_year_end": worst["end"],
                    "worst_two_year_return": worst["final_nav"] / cfg.initial - 1,
                    "worst_window_drawdown": worst["maximum_drawdown"],
                }
            print(group_id + " complete", flush=True)
    keys = report["groups"]["delay1-cost1"]["results"]
    report["decision"] = {
        "numerically_robust_candidates": [
            k
            for k in keys
            if all(g["results"][k]["numerical_objective_pass"] for g in report["groups"].values())
        ],
        "upgrade_to_execution": False,
        "meaning": "NAV screening only; market price and publication clock remain unverified",
    }
    write_csv(output / "rolling-24m.csv", window_rows)
    report["output_sha256"] = {p.name: file_sha(p) for p in sorted(output.glob("*.csv"))}
    with (output / "results.json").open("x", encoding="utf-8") as out:
        json.dump(report, out, ensure_ascii=False, indent=2, default=serial)
        out.write("\n")
    print(
        json.dumps(
            {
                "cases": len(keys) * len(report["groups"]),
                "rolling_windows_per_case": len(ranges),
                "decision": report["decision"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.output)
