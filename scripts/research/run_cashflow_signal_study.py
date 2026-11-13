"""Frozen-input fair cashflow comparisons and chronological reentry diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from dynamic_leverage.services.account_replay import AccountReplay, ReplayConfig
from dynamic_leverage.services.cashflow_research import (
    CapitalRecoveryPolicy,
    add_months,
    cashflow_metrics,
    investment_schedule,
    reentry_acceptance,
)
from dynamic_leverage.services.research_artifacts import (
    json_report,
    read_dataset,
    write_csv,
)
from dynamic_leverage.services.research_registry import (
    cashflow_signature,
    duplicate_preflight,
)

ROOT = Path(__file__).resolve().parents[2]



def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def volatility_rows(rows):
    if any(r.policy_inputs is None or r.policy_inputs.as_of != r.day for r in rows):
        raise ValueError("contemporaneous volatility input required")
    return [
        replace(r, target=D("1.2") if r.policy_inputs.rv20 < D("0.2") else D("0.8")) for r in rows
    ]


def run(dataset, output, monthly_contribution, rerun_reason=None):
    if output.exists():
        raise FileExistsError("research output must be a new directory")
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("new_out_of_sample") is not False:
        raise ValueError("frozen exploratory dataset required")
    report = {
        "schema": "cashflow-signal-research-v1",
        "new_out_of_sample": False,
        "production_configuration_changed": False,
        "profitability_demonstrated": False,
        "source_sha256": {
            p: sha(ROOT / p)
            for p in (
                "src/dynamic_leverage/services/account_replay.py",
                "src/dynamic_leverage/services/cashflow_research.py",
                "scripts/run_cashflow_signal_study.py",
            )
        },
        "manifest_sha256": sha(manifest_path),
        "registered_specification": {
            "initial_nav": 500000,
            "staging_months": [1, 6, 12],
            "rolling_horizon_months": 24,
            "start_frequency": "first trading day each month",
            "cash_rates": [0, 0.02],
            "cash_rate_is_assumed_sensitivity_not_verified_deposit_rate": True,
            "monthly_contribution": monthly_contribution,
            "entry_timing": "release/deposit at first observed monthly session; execution D+1",
            "dynamic_rule": "rv20 <20%: 1.2x; otherwise 0.8x; no parameter search",
            "benchmark_gate": "buyhold control bypasses market loss gate, not a 40% guarantee",
            "diagnostic_risk_relaxation": (
                "principal-only-research disables historical drawdown latch ONLY in labelled "
                "diagnostics. Permanent 40% principal latch remains. It does NOT satisfy "
                "the original 40% drawdown limit merely because a reentry buys."
            ),
        },
        "groups": {},
    }
    signature = cashflow_signature(
        report["manifest_sha256"], report["registered_specification"], report["source_sha256"]
    )
    duplicates = duplicate_preflight(ROOT, signature, rerun_reason)
    report["deduplication"] = {
        "experiment_signature": signature,
        "prior_identical_results": duplicates,
        "rerun_reason": rerun_reason,
    }
    output.mkdir(parents=True)
    windows = []
    for group, entry in manifest["datasets"].items():
        rows, spec = read_dataset(dataset / entry["file"], entry["sha256"])
        cfg = replace(ReplayConfig(**spec["execution_config"]), recipe="1x-2x-v1", ceiling=D("1.2"))
        rule_rows = volatility_rows(rows)
        group_report = {
            "dataset_sha256": entry["sha256"],
            "synthetic_crisis": group.startswith(("dotcom-", "gfc-")),
            "reentry": {},
        }
        # State is fresh for every independent replay; no triggers borrowed from another path.
        for basis in ("principal-and-drawdown", "principal-only-research"):
            policy = CapitalRecoveryPolicy()
            active_cfg = replace(cfg, loss_gate_basis=basis)
            replay = AccountReplay(active_cfg).run(rule_rows, signal_policy=policy)
            stats = cashflow_metrics(replay, active_cfg)
            stats.update(reentry_acceptance(replay, policy))
            stats["risk_relaxed_diagnostic"] = basis == "principal-only-research"
            group_report["reentry"][basis] = stats
            write_csv(output / f"{group}-{basis}-daily.csv", replay.daily)
            write_csv(output / f"{group}-{basis}-fills.csv", replay.fills)
        if group.startswith("real-etf-"):
            group_report["cashflow_comparison"] = {}
            for rate in (D(0), D("0.02")):
                for name, installments, mode in (
                    ("0050-lump", 1, "buyhold"),
                    ("0050-stage6", 6, "buyhold"),
                    ("0050-stage12", 12, "buyhold"),
                    ("dynamic-vol20", 1, "existing"),
                ):
                    active_cfg = replace(cfg, mode=mode, cash_interest_rate=rate)
                    selected = (
                        rule_rows if mode == "existing" else [replace(r, target=D(1)) for r in rows]
                    )
                    scheduled = investment_schedule(
                        selected,
                        cfg.initial_nav,
                        installments=installments,
                        monthly_contribution=monthly_contribution,
                    )
                    replay = AccountReplay(active_cfg).run(scheduled)
                    key = f"{name}-cash{rate}"
                    group_report["cashflow_comparison"][key] = cashflow_metrics(replay, active_cfg)
                    write_csv(output / f"{group}-{key}-daily.csv", replay.daily)
                    write_csv(output / f"{group}-{key}-fills.csv", replay.fills)
            starts = [
                i for i, r in enumerate(rows) if i == 0 or r.day.month != rows[i - 1].day.month
            ]
            for index in starts:
                end_day = add_months(rows[index].day, 24)
                end_index = next(
                    (j for j in range(index + 1, len(rows)) if rows[j].day >= end_day), None
                )
                if end_index is None:
                    continue
                chunk = rows[index : end_index + 1]
                for rate in (D(0), D("0.02")):
                    for name, installments, mode in (
                        ("0050-lump", 1, "buyhold"),
                        ("0050-stage6", 6, "buyhold"),
                        ("0050-stage12", 12, "buyhold"),
                        ("dynamic-vol20", 1, "existing"),
                    ):
                        active_cfg = replace(cfg, mode=mode, cash_interest_rate=rate)
                        selected = (
                            volatility_rows(chunk)
                            if mode == "existing"
                            else [replace(r, target=D(1)) for r in chunk]
                        )
                        scheduled = investment_schedule(
                            selected,
                            cfg.initial_nav,
                            installments=installments,
                            monthly_contribution=monthly_contribution,
                        )
                        replay = AccountReplay(active_cfg).run(scheduled)
                        windows.append(
                            {
                                "group": group,
                                "cash_rate": rate,
                                "strategy": name,
                                **cashflow_metrics(replay, active_cfg),
                            }
                        )
        report["groups"][group] = group_report
        print(f"completed {group}", flush=True)
    summaries = []
    for group in ("real-etf-base", "real-etf-stress"):
        for rate in (D(0), D("0.02")):
            baseline = {
                r["start"]: r
                for r in windows
                if r["group"] == group and r["cash_rate"] == rate and r["strategy"] == "0050-lump"
            }
            for name in ("0050-lump", "0050-stage6", "0050-stage12", "dynamic-vol20"):
                selected = [
                    r
                    for r in windows
                    if r["group"] == group and r["cash_rate"] == rate and r["strategy"] == name
                ]
                summaries.append(
                    {
                        "group": group,
                        "cash_rate": rate,
                        "strategy": name,
                        "window_count": len(selected),
                        "median_final_nav": statistics.median(r["final_nav"] for r in selected),
                        "worst_final_nav": min(r["final_nav"] for r in selected),
                        "median_xirr": statistics.median(
                            r["money_weighted_return"] for r in selected
                        ),
                        "worst_drawdown": max(r["maximum_drawdown"] for r in selected),
                        "principal_loss_windows": sum(r["net_profit"] < 0 for r in selected),
                        "wins_vs_lump": sum(
                            r["final_nav"] > baseline[r["start"]]["final_nav"] for r in selected
                        ),
                        "ties_vs_lump": sum(
                            r["final_nav"] == baseline[r["start"]]["final_nav"] for r in selected
                        ),
                        "all_40pct_limits_pass": all(
                            r["numerical_40pct_limits_pass"] for r in selected
                        ),
                    }
                )
    report["rolling_summary"] = summaries
    report["rolling_windows_overlap_and_are_not_independent"] = True
    report["decision"] = (
        "Exploration only. No new fundamental signal return evidence or live promotion."
    )
    write_csv(output / "rolling-windows.csv", windows)
    write_csv(output / "rolling-summary.csv", summaries)
    (output / "results.json").write_text(
        json.dumps(report, default=json_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 相同資金條件的投入方式比較",
        "",
        "既有 50 萬從起點就全部計入總資產；分批是保留現金，沒有虛構入金。",
        (
            "現金利率 0%／2% 是敏感度假設，不是銀行即時報價；"
            "費用、股息、拆分、流動性與隔日成交沿用封存資料。"
        ),
        "每月起點的 24 個月區間互相重疊，並非獨立樣本或新的樣本外驗證。",
        "",
        "|情境|現金利率|方式|區間數|勝過一次投入|期末資產中位數|最差期末資產|最差回撤|本金虧損區間|",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        lines.append(
            f"|{s['group']}|{s['cash_rate']:.0%}|{s['strategy']}|{s['window_count']}|"
            f"{s['wins_vs_lump']}|{s['median_final_nav']:,.0f}|{s['worst_final_nav']:,.0f}|"
            f"{s['worst_drawdown']:.2%}|{s['principal_loss_windows']}|"
        )
    lines.extend(
        [
            "",
            "## 回場驗收",
            "",
            "預設保留 40% 歷史回撤鎖定。principal-only-research 是放寬回撤鎖定的研究診斷，",
            "不是修好之後自動符合原風險限制；原最大回撤仍完整列出，不重設高點。",
            "",
            "|情境|風控口徑|回場訊號|第一筆買回|回場買入數|最大回撤|40% 數值限制|",
            "|---|---|---|---|---:|---:|---|",
        ]
    )
    for group, data in report["groups"].items():
        for basis, s in data["reentry"].items():
            lines.append(
                f"|{group}|{basis}|{s['reentry_signal_date'] or '—'}|"
                f"{s['first_reentry_buy_date'] or '—'}|{s['reentry_buy_count']}|"
                f"{s['maximum_drawdown']:.2%}|{s['numerical_40pct_limits_pass']}|"
            )
    (output / "findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "data/private/rearm-dataset-20260928"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--monthly-contribution", type=D, default=D(0))
    parser.add_argument(
        "--rerun-reason", help="record why an identical experiment must be repeated"
    )
    args = parser.parse_args()
    run(args.dataset, args.output, args.monthly_contribution, args.rerun_reason)
