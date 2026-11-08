"""Frozen primary rearm candidate and separately labelled recovery diagnostics."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal as D

from run_risk_replay import json_ready, summarize, write_trace
from run_strategy_value_study import CONFIG, ROOT, prepared_groups
from run_valuation_reserve_study import base_config

from dynamic_leverage.domain.drawdown_budget import DrawdownBudgetPolicy
from dynamic_leverage.services.account_replay import AccountReplay
from dynamic_leverage.services.strategy_value import dominates

OUT = ROOT / "docs/research/recovery-rearm-results-20260922"
PRIOR = ROOT / "docs/research/strategy-value-results-20260921/results.json"
REARM = replace(CONFIG, cash_rearm_drawdown=CONFIG.de_risk_start)
CANDIDATES = {
    "legacy": CONFIG,
    "rearm": REARM,
    "rearm-fast": replace(REARM, recovery_buy_fraction=D(".1")),
    "rearm-cap-diagnostic": replace(REARM, recovery_without_execution_buffer=True),
    "rearm-both-diagnostic": replace(
        REARM, recovery_buy_fraction=D(".1"), recovery_without_execution_buffer=True
    ),
}


def validate(result, rows, cfg, policy):
    for i, r in enumerate(result.daily):
        nav = (
            r["cash"]
            + r["receivables"]
            + sum(r[f"quantity_{s}"] * rows[i].quotes[s].close for s in ["0050", "00631L"])
        )
        assert abs(nav - r["nav"]) < D(".000001") and r["cash"] >= 0
    dates = {r.day: i for i, r in enumerate(rows)}
    for f in result.fills:
        assert dates[f["date"]] - dates[f["signal_date"]] >= cfg.execution_delay
        if policy and f["side"] == "BUY":
            assert not result.daily[dates[f["date"]] - 1]["capital_limit_latched"]
    for e in result.events:
        if e["kind"] == "reserve_decision":
            assert e["target"] <= e["risk_exposure_cap"] + D("1e-8")
        if e["kind"] == "reserve_buy_fill":
            assert e["budget_remaining"] >= 0 and e["cash_after"] >= e["cash_floor"]


def comparisons(ms):
    pairs = [
        ("rearm-vs-legacy", "rearm", "legacy"),
        ("speed-only", "rearm-fast", "rearm"),
        ("cap-only-diagnostic", "rearm-cap-diagnostic", "rearm"),
        ("both-diagnostic", "rearm-both-diagnostic", "rearm"),
    ]
    result = {}
    for label, a, b in pairs:
        result[label] = {
            k: ms[a][k] - ms[b][k]
            for k in [
                "calendar_cagr",
                "maximum_drawdown",
                "annualized_volatility",
                "total_cost",
                "mean_actual_exposure",
            ]
        }
    result["cagr_interaction"] = (
        ms["rearm-both-diagnostic"]["calendar_cagr"]
        - ms["rearm-fast"]["calendar_cagr"]
        - ms["rearm-cap-diagnostic"]["calendar_cagr"]
        + ms["rearm"]["calendar_cagr"]
    )
    return result


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    prior = json.loads(PRIOR.read_bytes())
    allowed = {"src/dynamic_leverage/domain/drawdown_budget.py"}
    changed = []
    for p, h in prior["evidence_sha256"].items():
        if hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h:
            assert p in allowed, f"unplanned frozen source change: {p}"
            changed.append(p)
    groups = {}
    checks = {"scenarios": 0, "account_rows": 0, "fills": 0, "prior_cases_reproduced": 0}
    plots = {}
    for label, stress, rows in prepared_groups():
        metrics = {}
        paths = {}
        for name in [*CANDIDATES, "one-x-hold"]:
            cfg = (
                base_config(stress)
                if name in CANDIDATES
                else base_config(stress, mode="buyhold", recipe="1x-2x-v1", ceiling=D(1))
            )
            policy = DrawdownBudgetPolicy(CANDIDATES[name]) if name in CANDIDATES else None
            selected = [replace(r, target=D(1)) for r in rows]
            result = AccountReplay(cfg).run(selected, reserve_policy=policy)
            validate(result, selected, cfg, policy)
            m = summarize(result, rows[0].day, cfg.initial_nav)
            m["policy_config"] = asdict(policy.config) if policy else None
            m["execution_config"] = asdict(cfg)
            ds = [e for e in result.events if e["kind"] == "reserve_decision"]
            m["mode_days"] = dict(Counter(e["mode"] for e in ds))
            m["soft_recross_ignored_days"] = sum(
                "SOFT_CASH_EXIT_NOT_REARMED" in e["reasons"] for e in ds
            )
            m["rearm_events"] = sum("SOFT_CASH_EXIT_REARMED" in e["reasons"] for e in ds)
            m["recovery_to_cash"] = sum(
                a["mode"] == "RECOVERY" and b["mode"] == "CASH"
                for a, b in zip(ds, ds[1:], strict=False)
            )
            m["cash_while_all_market_conditions_clear"] = sum(
                e["mode"] == "CASH"
                and e["eps_yoy_proxy"] > D("-.2")
                and e["rv20"] < D(".35")
                and e["index_daily_return"] > D("-.03")
                and e["market_recovery_ready"]
                for e in ds
            )
            m["known_2025_recross_dates"] = [
                {
                    "date": e["date"],
                    "mode": e["mode"],
                    "desired": e["desired"],
                    "target": e["target"],
                    "reasons": e["reasons"],
                }
                for e in ds
                if str(e["date"]) in {"2025-07-04", "2025-08-01", "2025-08-28"}
            ]
            if name in {"legacy", "one-x-hold"}:
                old = prior["groups"][label]["results"]["pe-eps" if name == "legacy" else name]
                for k in [
                    "final_nav",
                    "calendar_cagr",
                    "maximum_drawdown",
                    "annualized_volatility",
                    "total_cost",
                    "fill_count",
                    "mean_actual_exposure",
                ]:
                    assert abs(m[k] - D(str(old[k]))) < D(".000001"), (label, name, k)
                checks["prior_cases_reproduced"] += 1
            m["within_40_percent"] = m["maximum_drawdown"] >= D("-.4") and m[
                "maximum_principal_loss"
            ] <= D(".4")
            checks["scenarios"] += 1
            checks["account_rows"] += len(result.daily)
            checks["fills"] += len(result.fills)
            write_trace(label + "-" + name, result, output_dir=OUT)
            metrics[name] = m
            paths[name] = result.daily
        for m in metrics.values():
            m["q7_numeric_pass"] = dominates(m, metrics["one-x-hold"]) and m["within_40_percent"]
        groups[label] = {
            "start": rows[0].day,
            "end": rows[-1].day,
            "results": metrics,
            "paired_differences": comparisons(metrics),
        }
        if label in {
            "real-etf-base",
            "dotcom-valuation-price-fund0.03-base",
            "gfc-tw50-total-return-fund0.03-base",
        }:
            plots[label] = paths
        print(
            label,
            {
                n: (
                    round(float(m["calendar_cagr"]) * 100, 2),
                    round(float(m["maximum_drawdown"]) * 100, 2),
                )
                for n, m in metrics.items()
            },
            flush=True,
        )
    decisions = {}
    for n in CANDIDATES:
        safe = all(g["results"][n]["within_40_percent"] for g in groups.values())
        real = all(
            groups[label]["results"][n]["q7_numeric_pass"]
            for label in ["real-etf-base", "real-etf-stress"]
        )
        decisions[n] = {
            "all_periods_within_40_percent": safe,
            "real_q7_pass": real,
            "original_numeric_objective_pass": safe and real,
            "role": "primary" if n == "rearm" else "control-or-diagnostic",
            "status": ("retain-for-forward-research" if safe and real else "reject-live-candidacy")
            if n == "rearm"
            else "diagnostic-only-no-promotion",
        }
    evidence = set(prior["evidence_sha256"]) | {
        PRIOR.relative_to(ROOT).as_posix(),
        "scripts/run_recovery_rearm_study.py",
        "tests/unit/test_recovery_rearm.py",
        "docs/research/recovery-rearm-protocol-20260922.md",
    }
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "groups": groups,
        "decisions": decisions,
        "validation": checks,
        "permitted_shared_source_changes": changed,
        "evidence_sha256": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sorted(evidence)
        },
        "prior_frozen_bundle": {
            "path": "data/private/return-attribution-complete-20260921.zip",
            "sha256": hashlib.sha256(
                (ROOT / "data/private/return-attribution-complete-20260921.zip").read_bytes()
            ).hexdigest(),
        },
        "new_out_of_sample": False,
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "production_configuration_changed": False,
    }
    (OUT / "results.json").write_text(
        json.dumps(report, default=json_ready, indent=2), encoding="utf-8"
    )
    render(report, plots)


def render(report, plots):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    for ax, (label, paths) in zip(axes, plots.items(), strict=True):
        for n, rows in paths.items():
            ax.plot([r["date"] for r in rows], [float(r["nav"]) / 500000 for r in rows], label=n)
        ax.set_title(label, fontsize=9)
        ax.set_ylabel("Account / initial capital")
        ax.grid(alpha=0.2)
        ax.legend(fontsize=7)
        ax.tick_params(axis="x", labelrotation=35)
    fig.suptitle("Frozen recovery experiments | historical diagnosis, not new OOS")
    fig.tight_layout()
    fig.savefig(OUT / "comparison.png", dpi=160)
    plt.close(fig)
    lines = [
        "# 恢復規則改版：固定候選比較",
        "",
        "主候選rearm只改30%軟清倉的重新武裝：回到原15%減碼起點以內才再次啟動。",
        "40%硬限制、原始高點、清倉等待與市場惡化退出保留。其他版本是事前固定的診斷對照。",
        "cap／both移除恢復期成交緩衝，保障較弱，不能冒稱相同風險或直接上線。",
        "實際商品2016-04-01～2026-09-17；2000與2008是上市前合成，且2000不含股息。",
        "所有資料均已看過，不是新的樣本外驗證。",
        "",
        "## 原數值目標判定",
        "",
        "| 版本 | 全14條件不超40% | 實際商品兩条件Q7 | 資格 |",
        "|---|---|---|---|",
    ]
    for n, d in report["decisions"].items():
        lines.append(
            f"| {n} | {d['all_periods_within_40_percent']} | {d['real_q7_pass']} | {d['status']} |"
        )
    for label in [
        "real-etf-base",
        "real-etf-stress",
        "dotcom-valuation-price-fund0.03-base",
        "gfc-tw50-total-return-fund0.03-base",
    ]:
        ms = report["groups"][label]["results"]
        lines += [
            "",
            f"## {label}",
            "",
            "| 版本 | 年化 | 波動 | 最大回撤 | 平均曝險 | 成本 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for n, m in ms.items():
            lines.append(
                f"| {n} | {m['calendar_cagr']:.2%} | {m['annualized_volatility']:.2%} "
                f"| {m['maximum_drawdown']:.2%} | {m['mean_actual_exposure']:.3f} "
                f"| {m['total_cost']:,.0f} |"
            )
    lines += [
        "",
        "## 已知2025重複清倉日：正常條件",
        "",
        "| 日期 | 版本 | 狀態 | 最終目標曝險 | 原因 |",
        "|---|---|---|---:|---|",
    ]
    for n in ["legacy", "rearm"]:
        for e in report["groups"]["real-etf-base"]["results"][n]["known_2025_recross_dates"]:
            lines.append(
                f"| {e['date']} | {n} | {e['mode']} | {e['target']:.3f} "
                f"| {', '.join(e['reasons'])} |"
            )
    lines += [
        "",
        "## 超限情境（不隱藏、不截斷損失）",
        "",
        "| 情境 | 版本 | 最大回撤 | 本金最大損失 | 首次40%事件 |",
        "|---|---|---:|---:|---|",
    ]
    breaches = []
    for label, g in report["groups"].items():
        for n, m in g["results"].items():
            if not m["within_40_percent"]:
                breaches.append((label, n))
                lines.append(
                    f"| {label} | {n} | {m['maximum_drawdown']:.2%} "
                    f"| {m['maximum_principal_loss']:.2%} | {m['first_40_percent_breach']} |"
                )
    if not breaches:
        lines.append("| 無 | — | — | — | — |")
    lines += [
        "",
        "## 驗收與重現",
        "",
        "Q7：年化高於0050、波動與回撤不高於0050，並保留40%本金／高點損失要求。",
        "回測不超限不保證未來不超限；保留研究資格也不等於獲利就緒。",
        "較早研究的來源雜湊對應其凍結封存包；本輪允許修改的研究策略檔另列，legacy直接重現舊績效。",
        "",
        "```json",
        json.dumps(report["validation"], indent=2),
        "```",
        "",
        "```powershell",
        "$env:PYTHONPATH='src;data/private/research-deps'",
        "C:\\anaconda3\\python.exe scripts/run_recovery_rearm_study.py",
        "```",
        "",
        "![候選比較](comparison.png)",
        "",
    ]
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    run()
