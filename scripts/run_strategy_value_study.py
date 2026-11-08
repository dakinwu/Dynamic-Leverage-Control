"""Predeclared valuation attribution and descriptive risk-matched controls."""

from __future__ import annotations

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
from dynamic_leverage.services.account_replay import AccountReplay, Quote, ReplayDay
from dynamic_leverage.services.crisis_research import crisis_inputs, synthetic_prices
from dynamic_leverage.services.historical_study import research_inputs
from dynamic_leverage.services.strategy_value import dominates, match_risk, window_metrics

OUT = ROOT / "docs/research/strategy-value-results-20260921"
CONFIG = BudgetConfig(D(".15"), D(".30"), D(".05"))
MATCH_FIELDS = {"mean_actual_exposure": D(".05"), "annualized_volatility": D(".01")}


def prepared_groups():
    _, _, real_closes, vals, vix, audit = load_data()
    closes, tw50, crisis_vals, _, _ = read_sources()
    periods = [
        ("real-etf", START, date.fromisoformat(audit["price_end"]), real_closes, [None]),
        (
            "dotcom-valuation-price",
            date(2000, 6, 1),
            date(2003, 12, 31),
            closes,
            [D(0), D(".03"), D(".06")],
        ),
        (
            "gfc-tw50-total-return",
            date(2007, 1, 2),
            date(2010, 12, 31),
            closes,
            [D(0), D(".03"), D(".06")],
        ),
    ]
    for period, start, end, index, fundings in periods:
        recovery = market_recovery_flags(index)
        for funding in fundings:
            for stress in [False, True]:
                label = (
                    period
                    + (f"-fund{funding:.2f}" if funding is not None else "")
                    + ("-stress" if stress else "-base")
                )
                if funding is None:
                    inputs = research_inputs(
                        index, vals, vix, publication_lag_days=60 if stress else 45
                    )
                    rows = source_rows(index, inputs, "v11", start, end)
                else:
                    dates = sorted(d for d in index if start <= d <= end)
                    underlying = tw50 if period.startswith("gfc") else index
                    prices = synthetic_prices(
                        {d: underlying[d] for d in dates}, annual_drag=D(".0104") + funding
                    )
                    inputs = crisis_inputs(
                        {d: p for d, p in index.items() if d <= end},
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
                            D(1),
                            rv20=inputs[d].rv20,
                        )
                        for d in dates
                    ]
                yield (
                    label,
                    stress,
                    [
                        replace(
                            r,
                            policy_inputs=inputs[r.day],
                            market_recovery_ready=recovery[r.day],
                            strategy_state=SafetyState.NORMAL,
                        )
                        for r in rows
                    ],
                )


def paired_delta(candidate, control):
    return {
        "cagr_difference": candidate["calendar_cagr"] - control["calendar_cagr"],
        "volatility_difference": candidate["annualized_volatility"]
        - control["annualized_volatility"],
        "drawdown_magnitude_difference": -candidate["maximum_drawdown"]
        + control["maximum_drawdown"],
        "cost_difference": candidate["total_cost"] - control["total_cost"],
        "dominates": dominates(candidate, control),
    }


def compare_group(metrics, paths, is_real):
    pe = metrics["pe-eps"]
    static = {n: m for n, m in metrics.items() if n.startswith("static-")}
    matches = {}
    for field, tolerance in MATCH_FIELDS.items():
        match = match_risk(pe, static, field, tolerance)
        match["candidate_minus_control"] = paired_delta(pe, static[match["control"]])
        match["hindsight_selection"] = True
        matches[field] = match
    comparison = {
        "pe_incremental": {
            n: paired_delta(pe, metrics[n]) for n in ["fixed1.2-eps", "fixed1.8-eps"]
        },
        "eps_incremental": {
            name: paired_delta(metrics[name + "-eps"], metrics[name + "-noeps"])
            for name in ["pe", "fixed1.2", "fixed1.8"]
        },
        "full_period_descriptive_matches": matches,
    }
    if is_real:
        train_end, test_start = date(2019, 12, 31), date(2020, 1, 1)
        train = {n: window_metrics(p, START, train_end) for n, p in paths.items()}
        train_static = {
            n: {**m, "fixed_target": metrics[n]["fixed_target"]}
            for n, m in train.items()
            if n in static
        }
        test = {n: window_metrics(p, test_start, p[-1]["date"]) for n, p in paths.items()}
        calibrated = {}
        for field, tolerance in MATCH_FIELDS.items():
            match = match_risk(train["pe-eps"], train_static, field, tolerance)
            control = match["control"]
            calibrated[field] = {
                "selection": match,
                "calibration_end": train_end,
                "evaluation_start": test_start,
                "candidate": test["pe-eps"],
                "control_metrics": test[control],
                "one_x_hold": test["one-x-hold"],
                "candidate_minus_control": paired_delta(test["pe-eps"], test[control]),
                "evaluation_risk_distance": abs(test["pe-eps"][field] - test[control][field]),
                "evaluation_risk_still_close": abs(test["pe-eps"][field] - test[control][field])
                <= tolerance,
                "new_out_of_sample": False,
            }
        comparison["prior_segment_calibration"] = calibrated
    return comparison


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    previous = json.loads(
        (ROOT / "docs/research/drawdown-budget-results-20260921/results.json").read_bytes()
    )
    for p, digest in previous["evidence_sha256"].items():
        if (
            p.startswith("data/private/")
            and hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != digest
        ):
            raise AssertionError("frozen raw data changed")
    matched_old = regression()
    groups, comparisons, matched_budget = {}, {}, 0
    counts = {"account_rows": 0, "fills": 0, "budget_decisions": 0}
    for label, stress, rows in prepared_groups():
        cases = []
        for eps in [True, False]:
            for name, target in [("pe", None), ("fixed1.2", D("1.2")), ("fixed1.8", D("1.8"))]:
                cfg = replace(CONFIG, fixed_target=target, earnings_filter=eps)
                cases.append(
                    (
                        name + ("-eps" if eps else "-noeps"),
                        base_config(stress),
                        D(1),
                        DrawdownBudgetPolicy(cfg),
                    )
                )
        for i in range(19):
            target = D(i) / 10
            cases.append(
                (
                    f"static-{target:.1f}",
                    base_config(
                        stress, mode="ungated", ceiling=D(2), no_trade_band=min(D(".15"), target)
                    ),
                    target,
                    None,
                )
            )
        cases.append(
            (
                "one-x-hold",
                base_config(stress, mode="buyhold", recipe="1x-2x-v1", ceiling=D(1)),
                D(1),
                None,
            )
        )
        metrics, paths = {}, {}
        for name, cfg, target, policy in cases:
            selected = [replace(r, target=target) for r in rows]
            result = AccountReplay(cfg).run(selected, reserve_policy=policy)
            m = summarize(result, rows[0].day, cfg.initial_nav)
            m["mean_cash_fraction"] = sum(r["cash"] / r["nav"] for r in result.daily) / len(rows)
            m["policy_config"] = asdict(policy.config) if policy else None
            m["execution_config"] = asdict(cfg)
            if name.startswith("static-"):
                m["fixed_target"] = target
                m["no_dynamic_risk_gate"] = True
            if name == "pe-eps":
                old = previous["groups"][label]["results"]["budget-market-reentry"]
                for key in [
                    "final_nav",
                    "calendar_cagr",
                    "maximum_drawdown",
                    "total_cost",
                    "fill_count",
                ]:
                    if abs(m[key] - D(str(old[key]))) > D(".000001"):
                        raise AssertionError(f"default behavior changed: {label}/{key}")
                matched_budget += 1
            dates = {r.day: i for i, r in enumerate(rows)}
            for i, r in enumerate(result.daily):
                nav = (
                    r["cash"]
                    + r["receivables"]
                    + sum(r[f"quantity_{s}"] * rows[i].quotes[s].close for s in ["0050", "00631L"])
                )
                if abs(nav - r["nav"]) > D(".000001") or r["cash"] < 0:
                    raise AssertionError("asset conservation failure")
            for f in result.fills:
                if dates[f["date"]] - dates[f["signal_date"]] < cfg.execution_delay:
                    raise AssertionError("execution look-ahead")
                if (
                    policy
                    and f["side"] == "BUY"
                    and result.daily[dates[f["date"]] - 1]["capital_limit_latched"]
                ):
                    raise AssertionError("buy after hard lock")
            for e in result.events:
                if e["kind"] == "reserve_decision":
                    counts["budget_decisions"] += 1
                    if e["target"] > e["risk_exposure_cap"] + D("1e-8"):
                        raise AssertionError("risk budget exceeded by decision")
                if e["kind"] == "reserve_buy_fill" and (
                    e["budget_remaining"] < 0 or e["cash_after"] < e["cash_floor"]
                ):
                    raise AssertionError("purchase exceeds reserved budget")
            counts["account_rows"] += len(result.daily)
            counts["fills"] += len(result.fills)
            write_trace(label + "-" + name, result, output_dir=OUT)
            metrics[name], paths[name] = m, result.daily
        groups[label] = {"start": rows[0].day, "end": rows[-1].day, "results": metrics}
        comparisons[label] = compare_group(metrics, paths, label.startswith("real-"))
        print(
            label,
            {
                n: round(float(metrics[n]["calendar_cagr"]) * 100, 2)
                for n in [
                    "pe-eps",
                    "fixed1.2-eps",
                    "fixed1.8-eps",
                    "pe-noeps",
                    "fixed1.2-noeps",
                    "fixed1.8-noeps",
                ]
            },
            flush=True,
        )
    real_labels = ["real-etf-base", "real-etf-stress"]
    crisis_safe = all(
        g["results"]["pe-eps"]["maximum_drawdown"] >= D("-.4")
        for n, g in groups.items()
        if not n.startswith("real-")
    )
    pe_robust = (
        all(c["dominates"] for n in real_labels for c in comparisons[n]["pe_incremental"].values())
        and crisis_safe
    )
    objective_pass = (
        all(
            dominates(groups[n]["results"]["pe-eps"], groups[n]["results"]["one-x-hold"])
            and groups[n]["results"]["pe-eps"]["maximum_drawdown"] >= D("-.4")
            for n in real_labels
        )
        and crisis_safe
    )
    decision = {
        "pe_allocation": "retain-for-research" if pe_robust else "not-proven-stop-live-candidacy",
        "whole_strategy": "retain-for-research"
        if objective_pass
        else "reject-current-live-candidate",
        "original_objective_pass": objective_pass,
        "crisis_within_40_percent": crisis_safe,
        "new_out_of_sample": False,
        "production_configuration_changed": False,
        "profitability_demonstrated": False,
        "scope": "current frozen candidate only",
    }
    files = set(previous["evidence_sha256"]) | {
        "scripts/run_strategy_value_study.py",
        "src/dynamic_leverage/services/strategy_value.py",
        "tests/unit/test_strategy_value.py",
        "docs/research/strategy-value-protocol-20260921.md",
        "docs/research/drawdown-budget-results-20260921/results.json",
    }
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "groups": groups,
        "comparisons": comparisons,
        "decision": decision,
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "validation": {
            **counts,
            "scenario_count": sum(len(g["results"]) for g in groups.values()),
            "old_valuation_cases_reproduced": matched_old,
            "old_budget_cases_reproduced": matched_budget,
        },
        "evidence_sha256": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sorted(files)
        },
    }
    (OUT / "results.json").write_text(
        json.dumps(report, default=json_ready, indent=2), encoding="utf-8"
    )
    (OUT / "decision.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")
    render(report)


def render(report):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = [
        "real-etf-base",
        "dotcom-valuation-price-fund0.03-base",
        "gfc-tw50-total-return-fund0.03-base",
    ]
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    for ax, label in zip(axes, labels, strict=True):
        ms = report["groups"][label]["results"]
        grid = [m for n, m in ms.items() if n.startswith("static-")]
        ax.plot(
            [float(m["annualized_volatility"]) * 100 for m in grid],
            [float(m["calendar_cagr"]) * 100 for m in grid],
            ".-",
            label="static 2x/cash grid",
            alpha=0.6,
        )
        for name in ["pe-eps", "fixed1.2-eps", "fixed1.8-eps", "one-x-hold"]:
            m = ms[name]
            ax.scatter(
                float(m["annualized_volatility"]) * 100, float(m["calendar_cagr"]) * 100, label=name
            )
        ax.set_title(label, fontsize=9)
        ax.set_xlabel("Annualized volatility (%)")
        ax.set_ylabel("Calendar CAGR (%)")
        ax.grid(alpha=0.2)
        ax.legend(fontsize=7)
    fig.suptitle(
        "Strategy value audit | known-history research; no return-based benchmark selection"
    )
    fig.tight_layout()
    fig.savefig(OUT / "risk-return.png", dpi=160)
    plt.close(fig)
    lines = [
        "# 策略價值拆解與淘汰判定",
        "",
        "比較規格在本輪計算前固定。既有歷史已看過，所有結果均非新的向前樣本外證據。",
        "15%／30%／5% 沿用上一輪暫定研究值，不代表使用者已確認正式投資參數。",
        "",
        "## 判定",
        "",
        "本輪結果：淘汰目前版本作為實盤候選；保留資料、風控與回測工具。不是已證明能賺超額報酬的策略。",
        "此判定僅針對本輪凍結版本，未改正式配置或停用其他既有服務。",
        "",
        "```json",
        json.dumps(report["decision"], ensure_ascii=False, indent=2),
        "```",
        "",
        "PE 組使用本益比動態配置；fixed 組使用固定的風控前目標，仍受相同帳戶風控調整。",
        "eps／noeps 分別保留／移除 EPS 代理過濾；主要同風控比較只在 eps 組之間進行。",
        "PE（本益比，價格相對盈餘的倍數）；EPS（每股盈餘，此處是指數／PE 推算的代理值）。",
        "年化是把整段複利換算成每年比例；波動是每日報酬起伏；回撤是從先前資產高點跌落的比例。",
        "例如 static-1.0 是目標 50% 正二／50% 現金，不是持有 100% 的 0050。",
        "",
    ]
    for label in [labels[0], "real-etf-stress", *labels[1:]]:
        g = report["groups"][label]
        ms = g["results"]
        lines += [
            f"## {label}：{g['start']}～{g['end']}",
            "",
            "| 規則 | 年化 | 波動 | 最大回撤 | 平均曝險 | 交易成本 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for name in [
            "pe-eps",
            "fixed1.2-eps",
            "fixed1.8-eps",
            "pe-noeps",
            "fixed1.2-noeps",
            "fixed1.8-noeps",
            "one-x-hold",
        ]:
            m = ms[name]
            lines.append(
                f"| {name} | {m['calendar_cagr']:.2%} | {m['annualized_volatility']:.2%} "
                f"| {m['maximum_drawdown']:.2%} | {m['mean_actual_exposure']:.3f} "
                f"| {m['total_cost']:,.0f} |"
            )
        lines += [
            "",
            "事後風險匹配（使用全期間資料，只作診斷，不是事前策略）：",
            "",
            "| 匹配依據 | 固定曝險組 | 距離 | 容差內 | 固定年化 | 固定波動 | 固定回撤 | "
            "PE年化減固定年化 |",
            "|---|---|---:|---|---:|---:|---:|---:|",
        ]
        for field, match in report["comparisons"][label]["full_period_descriptive_matches"].items():
            control = ms[match["control"]]
            lines.append(
                f"| {field} | {match['control']} | {match['distance']:.5f} "
                f"| {match['within_tolerance']} "
                f"| {control['calendar_cagr']:.2%} | {control['annualized_volatility']:.2%} "
                f"| {control['maximum_drawdown']:.2%} "
                f"| {match['candidate_minus_control']['cagr_difference']:.2%} |"
            )
        lines.append("")
    lines += [
        "## 只用前段選基準、後段評估（不是新樣本外）",
        "",
        "前段 2016–2019，只按持倉／波動距離選；2020 起不重選，延續各自帳戶。",
        "後段風險可能漂移，表中明列是否仍在容差內；不可硬稱後段風險相同。",
        "",
        "| 情境／依據 | 前段選定配置 | 後段PE年化 | 後段固定年化 | 後段0050年化 | 後段風險仍接近 |",
        "|---|---|---:|---:|---:|---|",
    ]
    for label in ["real-etf-base", "real-etf-stress"]:
        for field, c in report["comparisons"][label]["prior_segment_calibration"].items():
            lines.append(
                f"| {label}/{field} | {c['selection']['control']} "
                f"| {c['candidate']['calendar_cagr']:.2%} "
                f"| {c['control_metrics']['calendar_cagr']:.2%} "
                f"| {c['one_x_hold']['calendar_cagr']:.2%} "
                f"| {c['evaluation_risk_still_close']} |"
            )
    lines += [
        "",
        "## 結果解讀與下一步",
        "",
        "1. 同風控的正常組，PE 年化 13.15%，固定 1.2／1.8 目標為 12.92%／12.20%。",
        "   PE 相對固定 1.2 只多 0.23 個百分點，波動與回撤卻略高；"
        "壓力組 PE 9.78% 又低於固定 1.2 的 16.50%。",
        "   估值規則沒有通過事前的穩健增益標準；不能由正常組略勝就宣稱有效。",
        "2. 正常組依全期間波動配對為 static-1.0：年化 22.49%、波動 19.59%、回撤 31.32%。",
        "   PE 為 13.15%、19.96%、31.82%；波動差 0.37 個百分點，年化卻少 9.34 個百分點。",
        "   這個期間的低報酬不能只解釋成低風險，也不能只解釋成持有較少股票。匹配是事後診斷。",
        "3. 移除 EPS 過濾，PE 正常組年化由 13.15% 升至 16.43%，壓力組反而由 9.78% 降至 8.41%。",
        "   因此無法簡化成『刪掉 EPS 就好』；成交時點、資料延遲與狀態切換的交互影響仍明顯。",
        "4. 交易成本確實較多：正常組 PE 45,618 元，static-1.0 為 7,407 元。",
        "   但總費用差不能直接當成年化差的完整成因；還有持倉時機與複利，本輪未做零成本反事實分解。",
        "5. 危機保護有作用，但不是估值獨有：2007–2010 正常／3% 融資假設下，",
        "   PE 與固定 1.8 同風控結果完全一致。按波動匹配 static-0.6，PE 年化 3.63% 高於 2.82%，",
        "   回撤 30.37% 小於 38.21%；這是局部正面結果，不能蓋過完整期間與其他情境的失敗。",
        "6. 不直接換成高收益固定配置：static-1.0 在 2000／2008 正常／3% 合成組"
        "回撤約 61.00%／56.62%。",
        "   前段選出的 static-1.5 到 2020 之後風險也已不接近 PE，"
        "不能把其 39.05% 年化當成同風險勝出。",
        "",
        "目前應停止替這個候選繼續尋找好看的參數。保留平台的資料驗收、持倉風險、交易成本與研究功能；",
        "未來新策略須另訂可否證的收益來源、凍結規格，並累積未用於設計的資料。這批結果不建立任何替代策略的實盤資格。",
        "",
        "## 限制與重現",
        "",
        "相同風控算法不代表相同實現風險；固定網格未套動態減碼，可能突破 40%，不能因此推薦上線。",
        "2000 是未含息大盤合成且從 6 月起；2008 是含息臺灣 50 合成，皆不是真實上市前 ETF。",
        "EPS 為指數／PE 代理、公布時間為延遲假設。原始資料與完整逐日／成交／事件保留。",
        "淘汰指這個候選不再作為實盤候選，不刪除研究程式，也不推論所有動態槓桿都無效。",
        "",
        "```powershell",
        "$env:PYTHONPATH='src;data/private/research-deps'",
        "C:\\anaconda3\\python.exe scripts/run_strategy_value_study.py",
        "```",
        "",
        "```json",
        json.dumps(report["validation"], indent=2),
        "```",
        "",
        "![風險與報酬](risk-return.png)",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
