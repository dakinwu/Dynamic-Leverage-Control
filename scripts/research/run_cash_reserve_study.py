"""Compare allocation recipes while keeping historical signals and risk gates fixed."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

from run_risk_replay import (
    ROOT,
    AccountReplay,
    ReplayConfig,
    SafetyState,
    json_ready,
    load_data,
    research_inputs,
    rolling_two_years,
    source_rows,
    summarize,
    write_trace,
)

OUT = ROOT / "docs/research/cash-reserve-results-20260918"
RECIPES = {"mixed": "1x-2x-v1", "cash": "2x-cash-v1"}
CAPS = ("1.0", "1.2", "1.6", "2.0")


def extra_metrics(rows):
    snapshots = {}
    for year in (2020, 2022, 2025):
        annual = [r for r in rows if r["date"].year == year]
        if not annual:
            continue
        previous = [r for r in rows if r["date"].year < year]
        start = previous[-1] if previous else annual[0]
        worst = min(annual, key=lambda r: r["nav"])
        snapshots[str(year)] = {
            label: {k: r[k] for k in ["date", "nav", "cash", "actual_exposure", "drawdown"]}
            for label, r in [("before_year", start), ("lowest_nav_in_year", worst)]
        }
    return {
        "mean_cash_fraction": sum((r["cash"] / r["nav"] for r in rows), D(0)) / len(rows),
        "minimum_cash_fraction": min(r["cash"] / r["nav"] for r in rows),
        "crisis_snapshots": snapshots,
    }


def check_old_result(case, name, metrics, old):
    previous_name = None
    if name.startswith("dynamic-mixed-"):
        previous_name = "existing-cap" + name.rsplit("-", 1)[1]
    elif name.startswith("fixed-mixed-") and not name.endswith("1.0"):
        previous_name = "fixed" + name.rsplit("-", 1)[1]
    elif name in {"0050", "00631L"}:
        previous_name = name
    if previous_name is None:
        return 0
    previous = old["scenarios"][case]["results"][previous_name]
    for metric in ["final_nav", "calendar_cagr", "maximum_drawdown", "total_cost", "fill_count"]:
        if abs(D(str(previous[metric])) - D(str(metrics[metric]))) > D("0.000001"):
            raise AssertionError(f"Existing recipe changed: {case}/{name}/{metric}")
    return 1


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    previous_path = ROOT / "docs/research/risk-replay-results-20260918/results.json"
    old = json.loads(previous_path.read_bytes())
    _, _, closes, valuations, vix, audit = load_data()
    end = date.fromisoformat(audit["price_end"])
    groups, traces = {}, {}
    matched = 0
    for version in ("v11", "v2"):
        start = date(2016, 1, 4) if version == "v11" else date(2026, 6, 2)
        for stress in (False, True):
            lag, delay, multiplier = (60, 2, D(2)) if stress else (45, 1, D(1))
            inputs = research_inputs(closes, valuations, vix, publication_lag_days=lag)
            rows = source_rows(closes, inputs, version, start, end)
            group_name = f"{version}-{'stress' if stress else 'base'}"
            results, paths = {}, {}
            cases = [
                (f"{policy}-{recipe}-{cap}", "existing", recipe, D(cap), policy == "fixed")
                for policy in ("dynamic", "fixed")
                for recipe in RECIPES
                for cap in CAPS
            ]
            cases += [
                ("0050", "buyhold", "mixed", D(1), True),
                ("00631L", "buyhold", "mixed", D(2), True),
            ]
            for name, mode, recipe, cap, fixed in cases:
                config = ReplayConfig(
                    mode=mode,
                    recipe=RECIPES[recipe],
                    ceiling=cap,
                    execution_delay=delay,
                    commission=D("0.001425") * multiplier,
                    sell_tax=D("0.001") * multiplier,
                    slippage=D("0.0005") * multiplier,
                    minimum_commission=D(20) * multiplier,
                )
                selected = (
                    [replace(r, target=cap, strategy_state=SafetyState.NORMAL) for r in rows]
                    if fixed
                    else rows
                )
                replay = AccountReplay(config).run(selected)
                metrics = summarize(replay, start, config.initial_nav)
                metrics.update(extra_metrics(replay.daily))
                metrics["config"] = asdict(config)
                matched += check_old_result(group_name, name, metrics, old)
                results[name] = metrics
                paths[name] = replay.daily
                write_trace(f"{group_name}-{name}", replay, output_dir=OUT)
            baseline = results["0050"]
            for name, metrics in results.items():
                metrics["rolling_two_years"] = rolling_two_years(paths[name], paths["0050"])
                metrics["q7_numeric"] = {
                    "higher_return": metrics["total_return"] > baseline["total_return"],
                    "volatility_not_higher": metrics["annualized_volatility"]
                    <= baseline["annualized_volatility"],
                    "drawdown_not_worse": metrics["maximum_drawdown"]
                    >= baseline["maximum_drawdown"],
                    "within_40_percent": metrics["maximum_drawdown"] >= D("-0.4"),
                }
                metrics["q7_numeric_pass"] = all(metrics["q7_numeric"].values())
            groups[group_name] = {"start": str(start), "end": str(end), "results": results}
            if not stress:
                traces[version] = paths
            print(
                group_name,
                {
                    n: (
                        round(float(m["calendar_cagr"]) * 100, 2),
                        round(float(m["maximum_drawdown"]) * 100, 2),
                    )
                    for n, m in results.items()
                },
                flush=True,
            )
    # Preserve source hashes from the accepted source set and verify raw inputs still match.
    source_hashes = {
        p.replace("\\", "/"): digest
        for p, digest in old["evidence_sha256"].items()
        if p.replace("\\", "/").startswith("data/private/")
    }
    if not source_hashes:
        raise AssertionError("Previous study has no verifiable raw source hashes")
    for path, digest in source_hashes.items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest:
            raise AssertionError(f"Historical source changed: {path}")
    evidence = [
        Path(__file__),
        previous_path,
        ROOT / "docs/research/cash-reserve-protocol-20260918.md",
        ROOT / "scripts/run_risk_replay.py",
        ROOT / "scripts/run_historical_study.py",
        ROOT / "tests/unit/test_account_replay.py",
    ]
    evidence += [
        ROOT / f"src/dynamic_leverage/{p}"
        for p in [
            "services/account_replay.py",
            "services/historical_study.py",
            "domain/risk.py",
            "domain/strategies.py",
            "domain/execution.py",
            "domain/ledger.py",
            "domain/backtest.py",
        ]
    ]
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "allocation_recipe_comparison",
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "prior_scenarios_reproduced": matched,
        "audit": audit,
        "scenarios": groups,
        "evidence_sha256": source_hashes
        | {
            str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in evidence
        },
    }
    (OUT / "results.json").write_text(
        json.dumps(report, default=json_ready, indent=2), encoding="utf-8"
    )
    render(report, traces)


def render(report, traces):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    selected = [
        "0050",
        "dynamic-mixed-2.0",
        "dynamic-cash-2.0",
        "dynamic-cash-1.2",
        "fixed-cash-1.2",
    ]
    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    for col, version in enumerate(traces):
        for name in selected:
            rows = traces[version][name]
            days = [r["date"] for r in rows]
            axes[0, col].plot(days, [float(r["nav"]) / 500000 for r in rows], label=name)
            axes[1, col].plot(days, [100 * float(r["cash"] / r["nav"]) for r in rows])
        axes[0, col].set_title(f"{version} | recipe comparison, retrospective")
        axes[0, col].set_ylabel("Wealth multiple")
        axes[0, col].legend(fontsize=8)
        axes[1, col].set_ylabel("Available cash (% of NAV)")
        for row in range(2):
            axes[row, col].grid(alpha=0.2)
            axes[row, col].tick_params(axis="x", labelrotation=20)
    fig.tight_layout()
    fig.savefig(OUT / "comparison.png", dpi=150)
    plt.close(fig)
    lines = [
        "# 正二＋現金配方比較",
        "",
        "相同訊號、帳戶風控與 50 萬資金，隔離配置方式。尚未新增估值加碼規則。",
        "資料截至 2026-09-17；V1.1 長期結果不能替短期 V2 背書。",
        "",
        "mixed＝0050＋正二；cash＝正二＋現金；dynamic＝既有動態訊號；fixed＝固定目標曝險。",
        "固定目標依週調整、不交易區間及緊急減碼規則執行，不代表每天精準維持權重。",
        "",
        "## 本輪結論",
        "",
        "- 只換成正二＋現金，沒有普遍提高年化報酬。V1.1 動態 1.2 倍上限由 "
        "25.91% 降至 23.74%，最大回撤由 35.54% 降至 34.12%。",
        "- 相同現金配方，固定 60% 正二／40% 現金年化 25.93%、最大回撤 33.97%；"
        "動態版本仍未展現明確增益。兩者實際平均曝險及波動不完全相同，不能把差值全當擇時損失。",
        "- 固定現金配方同期 0050 年化 24.07%、最大回撤 33.88%、波動 19.92%；"
        "固定現金配方波動為 22.34%，故仍未通過波動及回撤不高於 0050 的條件。",
        "- 動態 2 倍上限現金配方年化 25.41%、最大回撤 48.80%，"
        "原配方為 24.94%／48.57%。改配方並未修復 40% 風險超標。",
        "- 全部 72 個情境沒有候選同時通過 Q7 數值條件。"
        "本輪沒有修改訊號、恢復條件、本金限制或借款規則。",
        "- 下一個獨立問題才是估值分批投入與恢復加碼規則；"
        "本輪結果不能冒充該新規則已被驗證，也不構成更換正式策略的依據。",
        "",
        "## 相同目標上限，配置差異（V1.1 正常情境）",
        "",
        "| 上限 | 原配方年化 | 現金配方年化 | 原配方回撤 | 現金配方回撤 | 現金配方平均現金 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    base = report["scenarios"]["v11-base"]["results"]
    for cap in CAPS:
        a, b = base[f"dynamic-mixed-{cap}"], base[f"dynamic-cash-{cap}"]
        lines.append(
            f"| {cap} | {a['calendar_cagr']:.2%} | {b['calendar_cagr']:.2%} | "
            f"{a['maximum_drawdown']:.2%} | {b['maximum_drawdown']:.2%} | "
            f"{b['mean_cash_fraction']:.2%} |"
        )
    lines += ["", "![配方與現金比較](comparison.png)", ""]
    for name, group in report["scenarios"].items():
        lines += [
            f"## {name}：{group['start']}～{group['end']}",
            "",
            "| 方案 | 期末資產 | 累計報酬 | 年化報酬 | 波動 | 最大回撤 | "
            "平均曝險 | 平均現金 | Q7數值 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for key, m in group["results"].items():
            lines.append(
                f"| {key} | {m['final_nav']:,.0f} | {m['total_return']:.2%} | "
                f"{m['calendar_cagr']:.2%} | {m['annualized_volatility']:.2%} | "
                f"{m['maximum_drawdown']:.2%} | {m['mean_actual_exposure']:.3f} | "
                f"{m['mean_cash_fraction']:.2%} | {m['q7_numeric_pass']} |"
            )
        lines += [
            "",
            "### 成交與兩年持有路徑",
            "",
            "| 方案 | 成交日 | 全期費稅滑價 | 首次40%觸發 | 最差兩年報酬 |",
            "|---|---:|---:|---|---:|",
        ]
        for key, m in group["results"].items():
            window = m["rolling_two_years"]["worst"]
            worst = f"{window['return']:.2%}" if window else "期間不足"
            lines.append(
                f"| {key} | {m['trade_days']} | {m['total_cost']:,.0f} | "
                f"{m['first_40_percent_breach']} | {worst} |"
            )
        lines.append("")
    lines += [
        "## 解讀與限制",
        "",
        "- 平均現金按每日可動用現金／淨值計算，不包含尚未交割款項；不假設現金利息收益。",
        "- 同上限不代表平均曝險或風險相同。固定配方是必要比較組，不能把配方效果當成訊號優勢。",
        "- 所有動態與固定目標保留 40% 歷史回撤鎖定；0050／00631L 買入持有為獨立市場比較。",
        "- 兩年視窗相互重疊，沿用同一帳戶持倉與風控歷史，不是獨立開戶或未來勝率。",
        "- 費用、交割與部分成交假設沿用前輪，沒有盤中委託簿、個人配息稅或完整銀行交割日曆。",
        "- 月報可知時間仍採假設延遲，EPS 仍是代理值；未通過真正樣本外驗收。",
        "- 2000／2008 不在本次真實商品區間，沒有宣稱完成那些危機的商品回測。",
        "",
        "## 重現與驗收",
        "",
        f"原配方共有 {report['prior_scenarios_reproduced']} 個對應情境的"
        "淨值、年化、回撤、成本及成交筆數"
        "與前輪一致。原始資料雜湊也逐一驗證。",
        "",
        "執行 `python scripts/run_cash_reserve_study.py`。"
        "全部 72 組情境皆保存逐日帳戶、成交及事件；"
        "`results.json` 另有各年度報酬、危機前現金快照、本金損失與來源雜湊。",
        "",
        "[事前規格](../cash-reserve-protocol-20260918.md) · "
        "[文章查核](../article-reference-review-20260918.md)",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
