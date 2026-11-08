"""Observe one frozen candidate. No parameter search, ingestion, database or broker."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dynamic_leverage.domain.backtest import BacktestEngine, EquityPoint  # noqa: E402
from dynamic_leverage.domain.drawdown_budget import BudgetConfig, DrawdownBudgetPolicy  # noqa: E402
from dynamic_leverage.services.account_replay import AccountReplay, ReplayConfig  # noqa: E402
from dynamic_leverage.services.budget_diagnostics import (  # noqa: E402
    BudgetDiagnostics,
    rolling_two_years,
)
from dynamic_leverage.services.research_artifacts import (  # noqa: E402
    json_report,
    read_dataset,
    write_csv,
)


def metrics(result, initial):
    points = [
        EquityPoint(r["date"], r["nav"], r["daily_return"], r["actual_exposure"], D(0), r["cost"])
        for r in result.daily
    ]
    values = asdict(BacktestEngine(initial_nav=initial)._metrics(points))
    values["calendar_cagr"] = (
        float(points[-1].nav / initial)
        ** (365.2425 / (points[-1].trading_date - points[0].trading_date).days)
        - 1
    )
    return values


def run(dataset: Path, output: Path):
    manifest = json.loads((dataset / "manifest.json").read_bytes())
    output.mkdir(parents=True, exist_ok=False)
    prior_dir = ROOT / "docs/research/recovery-rearm-results-20260922"
    prior = json.loads((prior_dir / "results.json").read_bytes())
    report = {
        "schema": "budget-diagnostics-v1",
        "new_out_of_sample": False,
        "policy_changed": False,
        "data_end": "2026-09-17",
        "groups": {},
        "trace_files_byte_identical": 0,
        "dataset_manifest_sha256": hashlib.sha256(
            (dataset / "manifest.json").read_bytes()
        ).hexdigest(),
    }
    for label, item in manifest["datasets"].items():
        rows, specification = read_dataset(dataset / item["file"], item["sha256"])
        cfg = ReplayConfig(**specification["execution_config"])
        inspector = BudgetDiagnostics(
            DrawdownBudgetPolicy(BudgetConfig(**specification["policy_config"]))
        )
        candidate = AccountReplay(cfg).run(rows, reserve_policy=inspector)
        benchmark = AccountReplay(
            replace(cfg, mode="buyhold", recipe="1x-2x-v1", ceiling=D(1))
        ).run(rows)
        for name, replay in [("rearm", candidate), ("one-x-hold", benchmark)]:
            for suffix, records in [("daily", replay.daily), ("fills", replay.fills)]:
                path = output / f"{label}-{name}-{suffix}.csv"
                write_csv(path, records)
                if path.read_bytes() != (prior_dir / path.name).read_bytes():
                    raise ValueError(f"frozen trace changed: {path.name}")
                report["trace_files_byte_identical"] += 1
            path = output / f"{label}-{name}-events.json"
            path.write_text(
                json.dumps(replay.events, default=json_report, ensure_ascii=False), encoding="utf-8"
            )
            if path.read_bytes() != (prior_dir / path.name).read_bytes():
                raise ValueError(f"frozen trace changed: {path.name}")
            report["trace_files_byte_identical"] += 1
        # Descriptive exact relative log-growth partition, not rule-removal attribution.
        partition = defaultdict(lambda: {"days": 0, "relative_log_growth": 0.0})
        for i, (a, b) in enumerate(zip(candidate.daily, benchmark.daily, strict=True)):
            previous_exposure = candidate.daily[i - 1]["actual_exposure"] if i else D(0)
            key = (
                ("below_1x_up" if b["daily_return"] > 0 else "below_1x_down_or_flat")
                if previous_exposure < 1
                else "at_least_1x"
            )
            partition[key]["days"] += 1
            partition[key]["relative_log_growth"] += math.log1p(
                float(a["daily_return"])
            ) - math.log1p(float(b["daily_return"]))
        total = math.log(float(candidate.daily[-1]["nav"] / benchmark.daily[-1]["nav"]))
        if not math.isclose(
            sum(v["relative_log_growth"] for v in partition.values()), total, abs_tol=1e-9
        ):
            raise ValueError("relative growth partition does not reconcile")
        data = {
            "metrics": metrics(candidate, cfg.initial_nav),
            "benchmark_metrics": metrics(benchmark, cfg.initial_nav),
            "decision_days": len(inspector.records),
            "binding_caps": dict(Counter(r["binding_caps"] for r in inspector.records)),
            "binding_caps_in_recovery": dict(
                Counter(r["binding_caps"] for r in inspector.records if r["mode"] == "RECOVERY")
            ),
            "tranche_binding_days": sum(r["buy_tranche_binding"] for r in inspector.records),
            "weekly_wait_days": sum(r["weekly_wait"] for r in inspector.records),
            "paused_buy_days": sum(r["buy_paused"] for r in inspector.records),
            "relative_growth_partition_descriptive_only": dict(partition),
            "rolling_24_months": rolling_two_years(candidate.daily, benchmark.daily),
        }
        for field in ["calendar_cagr", "annualized_volatility", "maximum_drawdown"]:
            if not math.isclose(
                float(data["metrics"][field]),
                prior["groups"][label]["results"]["rearm"][field],
                abs_tol=1e-12,
            ):
                raise ValueError(f"frozen metric changed: {label}/{field}")
        (output / f"{label}-limits.json").write_text(
            json.dumps(inspector.records, default=json_report, ensure_ascii=False), encoding="utf-8"
        )
        simplified = [
            {
                k: v
                for k, v in row.items()
                if k not in {"state_before", "inputs", "account", "active_caps"}
            }
            for row in inspector.records
        ]
        write_csv(output / f"{label}-limits.csv", simplified)
        report["groups"][label] = data
        print(label, "verified", len(inspector.records), flush=True)
    report["source_sha256"] = {
        str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [
            Path(__file__),
            ROOT / "src/dynamic_leverage/services/budget_diagnostics.py",
            ROOT / "src/dynamic_leverage/services/research_artifacts.py",
            ROOT / "src/dynamic_leverage/domain/drawdown_budget.py",
            ROOT / "src/dynamic_leverage/services/account_replay.py",
        ]
    }
    report["output_sha256"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()
    }
    (output / "results.json").write_text(
        json.dumps(report, default=json_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("completed", report["trace_files_byte_identical"], "identical trace files")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.dataset, args.output)
