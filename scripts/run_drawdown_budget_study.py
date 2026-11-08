"""Compare fixed drawdown-budget candidates with unchanged research controls."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from decimal import Decimal as D

from collect_oos_crisis import ROOT
from run_historical_study import load_data
from run_oos_crisis_study import read_sources, regression
from run_risk_replay import json_ready, source_rows, summarize, write_trace
from run_valuation_reserve_study import START, base_config

from dynamic_leverage.domain.drawdown_budget import (
    BudgetConfig,
    DrawdownBudgetPolicy,
    market_recovery_flags,
)
from dynamic_leverage.domain.models import SafetyState
from dynamic_leverage.domain.reserve_policy import ReservePolicy
from dynamic_leverage.services.account_replay import AccountReplay, Quote, ReplayDay
from dynamic_leverage.services.crisis_research import crisis_inputs, synthetic_prices
from dynamic_leverage.services.historical_study import research_inputs

OUT = ROOT / "docs/research/drawdown-budget-results-20260921"
PROFILES = {
    "balanced": (D(".15"), D(".30")),
    "active": (D(".20"), D(".35")),
    "conservative": (D(".10"), D(".25")),
}


def run(profile, recovery_fraction, selection_status):
    OUT.mkdir(parents=True, exist_ok=True)
    config = BudgetConfig(*PROFILES[profile], D(recovery_fraction))
    # Keep frozen research artifacts intact; validate their raw data and reproduce controls.
    matched = regression()
    _, _, real_closes, vals, vix, audit = load_data()
    crisis_closes, tw50, crisis_vals, hashes, _ = read_sources()
    old_crisis = json.loads(
        (ROOT / "docs/research/oos-crisis-results-20260921/results.json").read_bytes()
    )
    real_end = date.fromisoformat(audit["price_end"])
    groups, plots, checked_rows, checked_fills, crisis_controls = {}, {}, 0, 0, 0
    periods = [
        ("real-etf", START, real_end, real_closes, [None]),
        (
            "dotcom-valuation-price",
            date(2000, 6, 1),
            date(2003, 12, 31),
            crisis_closes,
            [D(0), D(".03"), D(".06")],
        ),
        (
            "gfc-tw50-total-return",
            date(2007, 1, 2),
            date(2010, 12, 31),
            crisis_closes,
            [D(0), D(".03"), D(".06")],
        ),
    ]
    for period, start, end, closes, fundings in periods:
        recovery = market_recovery_flags(closes)
        for funding in fundings:
            for stress in [False, True]:
                condition = "stress" if stress else "base"
                label = (
                    period
                    + (f"-fund{funding:.2f}" if funding is not None else "")
                    + "-"
                    + condition
                )
                if period == "real-etf":
                    inputs = research_inputs(
                        closes, vals, vix, publication_lag_days=60 if stress else 45
                    )
                    rows = source_rows(closes, inputs, "v11", start, end)
                else:
                    dates = sorted(d for d in closes if start <= d <= end)
                    underlying = tw50 if period == "gfc-tw50-total-return" else closes
                    prices = synthetic_prices(
                        {d: underlying[d] for d in dates}, annual_drag=D(".0104") + funding
                    )
                    inputs = crisis_inputs(
                        {d: p for d, p in closes.items() if d <= end},
                        crisis_vals,
                        lag_days=60 if stress else 45,
                    )
                    rows = [
                        ReplayDay(
                            d,
                            {
                                "0050": Quote(prices[d][0], 10**12),
                                "00631L": Quote(prices[d][1], 10**12),
                            },
                            D("1.2"),
                            rv20=inputs[d].rv20,
                        )
                        for d in dates
                    ]
                rows = [
                    replace(r, policy_inputs=inputs[r.day], market_recovery_ready=recovery[r.day])
                    for r in rows
                ]
                cases = [
                    ("old-A", base_config(stress), D("1.2"), ReservePolicy()),
                    (
                        "budget-cuts-only",
                        base_config(stress),
                        D("1.2"),
                        DrawdownBudgetPolicy(replace(config, market_reentry=False)),
                    ),
                    (
                        "budget-market-reentry",
                        base_config(stress),
                        D("1.2"),
                        DrawdownBudgetPolicy(config),
                    ),
                    ("fixed-60-40", base_config(stress, ceiling=D("1.2")), D("1.2"), None),
                ]
                cases += [
                    (
                        name,
                        base_config(stress, mode="buyhold", recipe="1x-2x-v1", ceiling=cap),
                        cap,
                        None,
                    )
                    for name, cap in [("one-x-hold", D(1)), ("two-x-hold", D(2))]
                ]
                metrics, paths = {}, {}
                for name, cfg, target, policy in cases:
                    selected = [
                        replace(r, target=target, strategy_state=SafetyState.NORMAL) for r in rows
                    ]
                    result = AccountReplay(cfg).run(selected, reserve_policy=policy)
                    m = summarize(result, selected[0].day, cfg.initial_nav)
                    m["mean_cash_fraction"] = sum(r["cash"] / r["nav"] for r in result.daily) / len(
                        rows
                    )
                    m["policy_config"] = asdict(policy.config) if policy else None
                    m["execution_config"] = asdict(cfg)
                    decisions = [e for e in result.events if e["kind"] == "reserve_decision"]
                    m["mode_days"] = {
                        mode: sum(e.get("mode") == mode for e in decisions)
                        for mode in ["NORMAL", "DEFENSE", "CASH", "RECOVERY", "HARD_LOCK"]
                    }
                    m["recovery_episodes"] = sum(
                        e.get("mode") == "RECOVERY"
                        and (i == 0 or decisions[i - 1].get("mode") != "RECOVERY")
                        for i, e in enumerate(decisions)
                    )
                    for i, r in enumerate(result.daily):
                        nav = (
                            r["cash"]
                            + r["receivables"]
                            + sum(
                                r[f"quantity_{s}"] * selected[i].quotes[s].close
                                for s in ["0050", "00631L"]
                            )
                        )
                        if abs(nav - r["nav"]) > D(".000001") or r["cash"] < 0:
                            raise AssertionError("account conservation failure")
                    indices = {r.day: i for i, r in enumerate(selected)}
                    for f in result.fills:
                        if indices[f["date"]] - indices[f["signal_date"]] < cfg.execution_delay:
                            raise AssertionError("fill uses future information")
                        if (
                            policy
                            and f["side"] == "BUY"
                            and result.daily[indices[f["date"]] - 1]["capital_limit_latched"]
                        ):
                            raise AssertionError("buy after hard limit")
                    for e in result.events:
                        if e["kind"] == "reserve_buy_fill" and (
                            e["budget_remaining"] < 0 or e["cash_after"] < e["cash_floor"]
                        ):
                            raise AssertionError("buy budget/cash reserve exceeded")
                    checked_rows += len(result.daily)
                    checked_fills += len(result.fills)
                    if funding is not None and name in {
                        "old-A",
                        "fixed-60-40",
                        "one-x-hold",
                        "two-x-hold",
                    }:
                        old_name = {
                            "old-A": "A-absolute-staged",
                            "one-x-hold": "synthetic-1x",
                            "two-x-hold": "synthetic-2x",
                        }.get(name, name)
                        old = old_crisis["groups"][label]["results"][old_name]
                        for field in ["final_nav", "maximum_drawdown", "total_cost", "fill_count"]:
                            if abs(D(str(old[field])) - m[field]) > D(".000001"):
                                raise AssertionError(
                                    f"crisis control changed: {label}/{name}/{field}"
                                )
                        crisis_controls += 1
                    write_trace(label + "-" + name, result, output_dir=OUT)
                    metrics[name], paths[name] = m, result.daily
                benchmark = metrics["one-x-hold"]
                for m in metrics.values():
                    m["q7_numeric"] = {
                        "higher_return": m["total_return"] > benchmark["total_return"],
                        "volatility_not_higher": m["annualized_volatility"]
                        <= benchmark["annualized_volatility"],
                        "drawdown_not_worse": m["maximum_drawdown"]
                        >= benchmark["maximum_drawdown"],
                        "within_40_percent": m["maximum_drawdown"] >= D("-.4"),
                    }
                    m["q7_numeric_pass"] = all(m["q7_numeric"].values())
                groups[label] = {
                    "start": str(start),
                    "end": str(end),
                    "results": metrics,
                    "funding_assumption": funding,
                    "stress": stress,
                }
                if not stress and funding in {None, D(".03")}:
                    plots[period] = paths
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
    evidence = [
        "scripts/run_drawdown_budget_study.py",
        "src/dynamic_leverage/domain/drawdown_budget.py",
        "src/dynamic_leverage/domain/reserve_policy.py",
        "src/dynamic_leverage/services/account_replay.py",
        "tests/unit/test_drawdown_budget.py",
        "docs/research/drawdown-budget-protocol-20260921.md",
        "docs/research/oos-crisis-results-20260921/results.json",
        "docs/research/valuation-reserve-results-20260918/results.json",
    ]
    # Include transitive sources/code, but hash today's versions of shared modules.
    prior = json.loads((ROOT / evidence[-1]).read_bytes())
    for path in set(prior["evidence_sha256"]) | set(old_crisis["evidence_sha256"]) | set(evidence):
        hashes[path] = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "profile": profile,
        "selection_status": selection_status,
        "budget_config": asdict(config),
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "evidence_class": "retesting-known-history-not-new-OOS",
        "groups": groups,
        "validation": {
            "old_real_scenarios_reproduced": matched,
            "old_crisis_controls_reproduced": crisis_controls,
            "account_rows_checked": checked_rows,
            "fill_delay_checked": checked_fills,
        },
        "evidence_sha256": hashes,
    }
    (OUT / "results.json").write_text(
        json.dumps(report, default=json_ready, indent=2), encoding="utf-8"
    )
    render(report, plots)


def render(report, plots):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(13, 12))
    for ax, (period, paths) in zip(axes, plots.items(), strict=True):
        for name in ["one-x-hold", "old-A", "budget-cuts-only", "budget-market-reentry"]:
            ax.plot(
                [r["date"] for r in paths[name]],
                [float(r["nav"]) / 500000 for r in paths[name]],
                label=name,
            )
        ax.set_title(period)
        ax.set_ylabel("Wealth / initial capital")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.2)
    fig.suptitle("Risk-budget research | known history, not new out-of-sample evidence")
    fig.tight_layout()
    fig.savefig(OUT / "comparison.png", dpi=150)
    plt.close(fig)
    current = report["groups"]["real-etf-base"]["results"]["budget-market-reentry"]
    benchmark = report["groups"]["real-etf-base"]["results"]["one-x-hold"]
    lines = [
        "# 提前減碼與恢復機制研究結果",
        "",
        "結論：新版在已測歷史中改善回撤，也能從防守恢復持倉；"
        "但尚未達成『報酬高於 0050、風險不高於 0050』的完整目標。",
        f"2016 年後實際商品正常成本組：新版年化 {current['calendar_cagr']:.2%}、"
        f"最大回撤 {abs(current['maximum_drawdown']):.2%}；"
        f"同期 0050 年化 {benchmark['calendar_cagr']:.2%}。",
        "",
        f"設定狀態：{report['selection_status']}；參數組：{report['profile']}。",
        "暫定設定不等於使用者已選定；所有候選只用於研究，正式策略與下單設定未變更。",
        "",
        "```json",
        json.dumps(report["budget_config"], default=json_ready, indent=2),
        "```",
        "",
        "全部資料都已用於先前研究，本輪是已知歷史重測，不宣稱新的樣本外績效。",
        "2000 為價格指數合成且從 6 月起；2008 為臺灣 50 含息合成；"
        "2016 年後為實際 ETF 價量與公司行動。",
        "下表為正常成交成本，危機合成模型融資成本假設 3%；完整敏感度見 results.json。",
        "",
    ]
    for label in [
        "real-etf-base",
        "dotcom-valuation-price-fund0.03-base",
        "gfc-tw50-total-return-fund0.03-base",
    ]:
        g = report["groups"][label]
        lines += [
            f"## {label}：{g['start']}～{g['end']}",
            "",
            "| 候選 | 累計報酬 | 年化 | 最大回撤 | 首次40% | Q7數值條件 |",
            "|---|---:|---:|---:|---|---|",
        ]
        for name, m in g["results"].items():
            lines.append(
                f"| {name} | {m['total_return']:.2%} | {m['calendar_cagr']:.2%} "
                f"| {m['maximum_drawdown']:.2%} | {m['first_40_percent_breach'] or '未觸發'} "
                f"| {'通過' if m['q7_numeric_pass'] else '未通過'} |"
            )
        lines.append("")
    lines += [
        "## 敏感度與限制",
        "",
        "| 期間 | 市場恢復候選年化範圍 | 最大回撤範圍 | 超過40%組數 |",
        "|---|---:|---:|---:|",
    ]
    for period in plots:
        ms = [
            g["results"]["budget-market-reentry"]
            for k, g in report["groups"].items()
            if k.startswith(period + "-")
        ]
        lines.append(
            f"| {period} | {min(m['calendar_cagr'] for m in ms):.2%}～"
            f"{max(m['calendar_cagr'] for m in ms):.2%} | "
            f"{min(m['maximum_drawdown'] for m in ms):.2%}～"
            f"{max(m['maximum_drawdown'] for m in ms):.2%} | "
            f"{sum(m['first_40_percent_breach'] is not None for m in ms)}/{len(ms)} |"
        )
    lines += [
        "",
        "Q7 = 扣成本收益高於同期間 1 倍、波動與回撤不高於 1 倍、最大回撤不超過 40%。",
        "在合成期間，1 倍仍是合成指數而非實際 0050，不能以這個數值判斷宣布實盤就緒。",
        "風險額度是假設正二再跌 40% 的情境計算，不是最壞跌幅保證；不能涵蓋所有跳空與無法成交。",
        "恢復持倉仍使用原始高點；40% 硬界線不會因價格回升自動解除。",
        f"2016 年後正常成本組共 {current['recovery_episodes']} 段市場恢復狀態；"
        f"平均現金 {current['mean_cash_fraction']:.2%}，"
        f"累計交易成本 {current['total_cost']:,.0f} 元。",
        "只加減碼但沿用帳戶先回本的恢復條件，會長時間留在低持倉；"
        "加入市場恢復可改善此問題，但仍會錯過部分上漲並增加交易。",
        "新增極端跳空測試：容許帳戶如實記錄超過 40% 的損失，再依成交延遲清倉；"
        "不把報表損失截成 40%，也不在市場回升時抹除硬界線事件。",
        "",
        "## 驗收",
        "",
        "```json",
        json.dumps(report["validation"], indent=2),
        "```",
        "",
        "各案例保留完整逐日帳戶、成交、決策狀態與風險額度。重跑：",
        "",
        "```powershell",
        "$env:PYTHONPATH='src;data/private/research-deps'",
        "C:\\anaconda3\\python.exe scripts/run_drawdown_budget_study.py "
        f"--profile {report['profile']} --recovery-fraction "
        f"{str(report['budget_config']['recovery_buy_fraction'])[1:]}",
        "```",
        "",
        "![比較圖](comparison.png)",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILES, default="balanced")
    parser.add_argument("--recovery-fraction", choices=[".025", ".05", ".1"], default=".05")
    parser.add_argument(
        "--selection-status",
        choices=["provisional-recommended", "user-confirmed"],
        default="provisional-recommended",
    )
    args = parser.parse_args()
    run(args.profile, args.recovery_fraction, args.selection_status)
