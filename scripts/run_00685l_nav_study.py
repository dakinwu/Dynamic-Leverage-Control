"""Add only 00685L to R17; reuse frozen 0050/00675L controls without reruns."""

from __future__ import annotations

import argparse
import csv
import json
from decimal import Decimal as D
from pathlib import Path

from audit_alternative_nav_results import audit
from run_alternative_nav_study import (
    ROOT,
    SPEC,
    numeric_pass,
    prepare,
    rolling_ranges,
    serial,
    verified_file,
    write_csv,
)

from dynamic_leverage.services.capital_nav_research import capital_points, parse_capital_nav
from dynamic_leverage.services.nav_proxy_research import ProxyConfig, simulate
from dynamic_leverage.services.research_registry import digest, duplicate_preflight, file_sha

OLD = "docs/research/alternative-nav-results-20261018-v1"
PROTOCOL = "docs/research/00685l-experiment-protocol-20261018.md"
MODES = ("buyhold", "static-1.2", "vol20")


def numeric_metrics(record):
    return {
        **record,
        **{
            key: D(str(record[key]))
            for key in ("maximum_drawdown", "maximum_principal_loss", "final_nav", "total_cost")
        },
    }


def load_inputs(root=ROOT):
    series, sources, _ = prepare(root)
    old_folder = root / OLD
    old = json.loads((old_folder / "results.json").read_bytes())
    for name, expected in old["source_sha256"].items():
        if file_sha(root / name) != expected:
            raise ValueError("reference scientific engine changed; do not silently mix studies")
    if old["data_manifest_sha256"] != sources:
        raise ValueError("reference inputs changed")
    for name, expected in old["output_sha256"].items():
        if file_sha(old_folder / name) != expected:
            raise ValueError("frozen control output changed")
    sources[OLD + "/results.json"] = file_sha(old_folder / "results.json")
    nav_folder = root / "data/private/00685l-nav-20261018"
    event_folder = root / "data/private/00685l-events-20261018"
    nav = json.loads((nav_folder / "manifest.json").read_bytes())
    events = json.loads((event_folder / "manifest.json").read_bytes())
    if len(nav["observations"]) != 1:
        raise ValueError("unique reviewed fund capture required")
    capture = nav["observations"][0]
    values = parse_capital_nav(
        verified_file(nav_folder, capture), capture, [p.day for p in series["0050"]]
    )
    for entry in events["source_observations"]:
        verified_file(event_folder, entry)
    for entry in events["derived_files"]:
        verified_file(event_folder, {**entry, "raw_file": entry.get("raw_file", entry.get("file"))})
    for entry in events["existing_source_references"]:
        if file_sha(root / entry["file"]) != entry["body_sha256"]:
            raise ValueError("cross-referenced source changed")
    distribution, split = events["distribution_acceptance"], events["split_acceptance"]
    if (
        distribution["status"] != "passed_dual_primary_source"
        or distribution["official_twse_valid_table_event_count"] != 0
        or D(split["ratio"]) != 24
        or split["trading_halt_start"] != "2026-07-01"
        or split["trading_halt_end"] != "2026-07-06"
        or split["trading_resumption"] != "2026-07-07"
        or events["nav_download_basis"] != "raw_nav_unit_scale_changes_on_2026-07-01"
    ):
        raise ValueError("reviewed distribution and split acceptance required")
    for folder in (nav_folder, event_folder):
        sources[str((folder / "manifest.json").relative_to(root)).replace("\\", "/")] = file_sha(
            folder / "manifest.json"
        )
    points = capital_points(values, series["00675L"])
    with (old_folder / "rolling-24m.csv").open() as src:
        windows = {(r["scenario"], r["case"], r["start"], r["end"]): r for r in csv.DictReader(src)}
    return points, sources, old, windows


def run(output):
    if output.exists():
        raise FileExistsError("preserve old outputs")
    points, sources, old, controls = load_inputs()
    specification = {
        **SPEC,
        "universe": ["00685L"],
        "controls_reused_from": OLD,
        "nav_scale_day": "2026-07-01",
        "split": 24,
        "halt": ["2026-07-01", "2026-07-06"],
        "controls_not_rerun": True,
    }
    code = {
        name: file_sha(ROOT / name)
        for name in (
            "scripts/run_00685l_nav_study.py",
            "scripts/audit_alternative_nav_results.py",
            "scripts/run_alternative_nav_study.py",
            "src/dynamic_leverage/services/capital_nav_research.py",
            "src/dynamic_leverage/services/nav_proxy_research.py",
        )
    }
    identity = {
        "schema": "alternative-nav-proxy-study-v1",
        "specification": specification,
        "manifests": sources,
        "engines": code,
        "protocol_sha256": file_sha(ROOT / PROTOCOL),
    }
    duplicate_preflight(ROOT, digest(identity))
    output.mkdir(parents=True)
    report = {
        "schema": "alternative-nav-proxy-study-v1",
        "route_id": "R17",
        "execution_date_taipei": "2026-11-08",
        "experiment_identity": identity,
        "registered_specification": specification,
        "source_sha256": code,
        "manifest_sha256": file_sha(ROOT / "data/private/00685l-nav-20261018/manifest.json"),
        "data_manifest_sha256": sources,
        "reference_artifact": OLD + "/results.json",
        "new_out_of_sample": False,
        "profitability_demonstrated": False,
        "target_exposure": None,
        "groups": {},
        "acceptance": {
            "sessions": len(points),
            "known_halt_sessions": sum(not p.fill_available for p in points),
            "nav_scale_day": "2026-07-01",
            "full_halt_history_verified": False,
            "first_publication_clock_verified": False,
            "execution_ready": False,
        },
    }
    rolling, ranges = [], rolling_ranges(points)
    for delay in SPEC["delays"]:
        for cost in SPEC["cost_multipliers"]:
            scenario = f"delay{delay}-cost{cost}"
            cfg = ProxyConfig(delay=delay, cost_multiplier=D(cost))
            old_results = old["groups"][scenario]["results"]
            base = numeric_metrics(old_results["0050-buyhold"])
            group = {"results": {}, "rolling_summary": {}}
            report["groups"][scenario] = group
            for mode in MODES:
                key = "00685L-" + mode
                metrics, daily, fills = simulate(points, mode, cfg)
                comparator = numeric_metrics(old_results["00675L-" + mode])
                metrics.update(
                    {
                        "data_kind": "nav_proxy",
                        "numerical_objective_pass": numeric_pass(metrics, base),
                        "cagr_difference_vs_0050": metrics["calendar_cagr"] - base["calendar_cagr"],
                        "cagr_difference_vs_00675l_same_mode": metrics["calendar_cagr"]
                        - comparator["calendar_cagr"],
                        "drawdown_difference_vs_00675l_same_mode": metrics["maximum_drawdown"]
                        - comparator["maximum_drawdown"],
                        "control_pointer": (
                            f"{OLD}/results.json#/groups/{scenario}/results/00675L-{mode}"
                        ),
                    }
                )
                group["results"][key] = metrics
                write_csv(output / f"{scenario}-{key}-daily.csv", daily)
                write_csv(output / f"{scenario}-{key}-fills.csv", fills)
                windows = []
                for left, right in ranges:
                    value, _, _ = simulate(points[left:right], mode, cfg)
                    lookup = (scenario, "0050-buyhold", str(value["start"]), str(value["end"]))
                    baseline = numeric_metrics(controls[lookup])
                    baseline["calendar_cagr"] = float(baseline["calendar_cagr"])
                    baseline["annualized_volatility"] = float(baseline["annualized_volatility"])
                    other = controls[
                        (scenario, "00675L-" + mode, str(value["start"]), str(value["end"]))
                    ]
                    row = {
                        "scenario": scenario,
                        "case": key,
                        **value,
                        "outperforms_0050": value["calendar_cagr"] > baseline["calendar_cagr"],
                        "numeric_objective_pass": numeric_pass(value, baseline),
                        "cagr_difference_vs_00675l_same_mode": value["calendar_cagr"]
                        - float(other["calendar_cagr"]),
                    }
                    rolling.append(row)
                    windows.append(row)
                worst = min(windows, key=lambda r: r["final_nav"])
                group["rolling_summary"][key] = {
                    "window_count": len(windows),
                    "overlapping_not_independent": True,
                    "return_wins_vs_0050": sum(r["outperforms_0050"] for r in windows),
                    "numeric_objective_pass_count": sum(
                        r["numeric_objective_pass"] for r in windows
                    ),
                    "return_wins_vs_00675l_same_mode": sum(
                        r["cagr_difference_vs_00675l_same_mode"] > 0 for r in windows
                    ),
                    "worst_two_year_return": worst["final_nav"] / cfg.initial - 1,
                    "worst_two_year_start": worst["start"],
                    "worst_two_year_end": worst["end"],
                    "largest_window_drawdown": max(r["maximum_drawdown"] for r in windows),
                }
            print(scenario + " complete", flush=True)
    report["decision"] = {
        "numerically_robust_candidates": [
            "00685L-" + mode
            for mode in MODES
            if all(
                g["results"]["00685L-" + mode]["numerical_objective_pass"]
                for g in report["groups"].values()
            )
        ],
        "upgrade_to_execution": False,
        "controls_reused_not_rerun": True,
    }
    write_csv(output / "rolling-24m.csv", rolling)
    report["output_sha256"] = {p.name: file_sha(p) for p in sorted(output.glob("*.csv"))}
    with (output / "results.json").open("x", encoding="utf-8") as dst:
        json.dump(report, dst, indent=2, ensure_ascii=False, default=serial)
        dst.write("\n")
    audit(output, output / "acceptance.json", prepared=({"00685L": points}, sources, {}))
    print(json.dumps({"cases": 12, "windows": len(rolling), "decision": report["decision"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.output)
