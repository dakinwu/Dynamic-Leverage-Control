"""Frozen-weight return attribution and zero-explicit-cost counterfactuals."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D
from math import exp, log1p

from run_risk_replay import json_ready, summarize, write_trace
from run_strategy_value_study import CONFIG, ROOT, prepared_groups
from run_valuation_reserve_study import base_config

from dynamic_leverage.domain.drawdown_budget import DrawdownBudgetPolicy
from dynamic_leverage.services.account_replay import AccountReplay
from dynamic_leverage.services.return_attribution import attribution_bridge, total_return

OUT = ROOT / "docs/research/return-attribution-results-20260921"
PRIOR = ROOT / "docs/research/strategy-value-results-20260921/results.json"


def save_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def segment(days):
    a = sum(log1p(d["strategy_return"]) for d in days)
    b = sum(log1p(d["benchmark_return"]) for d in days)
    return {
        "start": days[0]["date"],
        "end": days[-1]["date"],
        "sessions": len(days),
        "strategy_return": exp(a) - 1,
        "benchmark_return": exp(b) - 1,
        "log_relative_return": a - b,
        "timing_log": sum(d["log_timing"] for d in days),
        "cost_log": sum(d["log_direct_cost"] for d in days),
        "mean_exposure": sum(2 * d["two_x_weight"] for d in days) / len(days),
        "state_sessions": dict(Counter(d["prior_state"] for d in days)),
    }


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    prior = json.loads(PRIOR.read_bytes())
    for path, digest in prior["evidence_sha256"].items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest:
            raise AssertionError(f"prior source changed: {path}")
    groups = {}
    checked_rows, checked_cases = 0, 0
    for label, stress, rows in prepared_groups():
        if not label.startswith("real-"):
            break
        cases, metrics = {}, {}
        for free in [False, True]:
            for name in ["pe-eps", "one-x-hold"]:
                cfg = (
                    base_config(stress)
                    if name == "pe-eps"
                    else base_config(stress, mode="buyhold", recipe="1x-2x-v1", ceiling=D(1))
                )
                if free:
                    cfg = replace(
                        cfg, commission=D(0), sell_tax=D(0), slippage=D(0), minimum_commission=D(0)
                    )
                policy = DrawdownBudgetPolicy(CONFIG) if name == "pe-eps" else None
                result = AccountReplay(cfg).run(rows, reserve_policy=policy)
                key = name + ("-zero-cost" if free else "")
                cases[key] = result
                metrics[key] = summarize(result, rows[0].day, cfg.initial_nav)
                if not free:
                    old = prior["groups"][label]["results"][name]
                    for field in [
                        "final_nav",
                        "calendar_cagr",
                        "maximum_drawdown",
                        "total_cost",
                        "fill_count",
                    ]:
                        assert abs(metrics[key][field] - D(str(old[field]))) < D(".000001")
                    checked_cases += 1
                write_trace(label + "-" + key, result, output_dir=OUT)
        strategy, benchmark = cases["pe-eps"], cases["one-x-hold"]
        events = {e["date"]: e for e in strategy.events if e["kind"] == "reserve_decision"}
        prev_nav, prev_bench = D(500000), D(500000)
        days = []
        for i, (row, s, b) in enumerate(zip(rows, strategy.daily, benchmark.daily, strict=True)):
            returns = {sym: D(0) for sym in ["0050", "00631L"]}
            weight, bw = D(0), D(0)
            if i:
                for sym in returns:
                    returns[sym] = total_return(
                        rows[i - 1].quotes[sym].close,
                        row.quotes[sym].close,
                        row.splits.get(sym, D(1)),
                        row.dividends.get(sym, (D(0), None))[0],
                    )
                assert strategy.daily[i - 1]["quantity_0050"] == 0
                weight = (
                    D(strategy.daily[i - 1]["quantity_00631L"])
                    * rows[i - 1].quotes["00631L"].close
                    / prev_nav
                )
                bw = (
                    D(benchmark.daily[i - 1]["quantity_0050"])
                    * rows[i - 1].quotes["0050"].close
                    / prev_bench
                )
            assert abs(s["daily_return"] - (weight * returns["00631L"] - s["cost"] / prev_nav)) < D(
                "1e-20"
            )
            assert abs(b["daily_return"] - (bw * returns["0050"] - b["cost"] / prev_bench)) < D(
                "1e-20"
            )
            previous_event = events[rows[i - 1].day] if i else {}
            event = events[row.day]
            days.append(
                {
                    "date": row.day,
                    "two_x_weight": float(weight),
                    "one_x_return": float(returns["0050"]),
                    "two_x_return": float(returns["00631L"]),
                    "strategy_return": float(s["daily_return"]),
                    "benchmark_return": float(b["daily_return"]),
                    "cost_fraction": float(s["cost"] / prev_nav),
                    "prior_state": previous_event.get("mode", "INITIAL"),
                    "close_state": event["mode"],
                    "pe": event["pe_ttm"],
                    "eps_yoy": event["eps_yoy_proxy"],
                    "rv20": event["rv20"],
                    "market_ready": event["market_recovery_ready"],
                    "desired": event["desired"],
                    "risk_cap": event["risk_exposure_cap"],
                    "drawdown_cap": event["drawdown_exposure_cap"],
                    "decision_reasons": "|".join(event["reasons"]),
                }
            )
            prev_nav, prev_bench = s["nav"], b["nav"]
            checked_rows += 2
        decomposition = attribution_bridge(days, (rows[-1].day - rows[0].day).days)
        path = decomposition.pop("daily")
        for key, name in [("dynamic_net", "pe-eps"), ("benchmark_net", "one-x-hold")]:
            assert (
                abs(decomposition["cagr_levels"][key] - float(metrics[name]["calendar_cagr"]))
                < 1e-12
            )
        yearly, quarters, states = defaultdict(list), defaultdict(list), defaultdict(list)
        for day in path:
            yearly[str(day["date"].year)].append(day)
            quarters[f"{day['date'].year}-Q{(day['date'].month-1)//3+1}"].append(day)
            states[day["prior_state"]].append(day)
        yearly = {n: segment(p) for n, p in yearly.items()}
        quarters = {n: segment(p) for n, p in quarters.items()}
        states = {n: segment(p) for n, p in states.items()}
        episodes, current = [], []
        for day in path:
            if day["prior_state"] not in {"NORMAL", "INITIAL"}:
                current.append(day)
            elif current:
                episodes.append(segment(current))
                current = []
        if current:
            episodes.append(segment(current))
        transitions = Counter(
            a["close_state"] + "->" + b["close_state"]
            for a, b in zip(path, path[1:], strict=False)
            if a["close_state"] != b["close_state"]
        )
        zero_events = {
            e["date"]: e
            for e in cases["pe-eps-zero-cost"].events
            if e["kind"] == "reserve_decision"
        }
        blocked_conditions = defaultdict(Counter)
        for event in events.values():
            if event["mode"] not in {"DEFENSE", "CASH"}:
                continue
            counter = blocked_conditions[str(event["date"].year)]
            counter["defense_cash_days"] += 1
            failed = {
                "earnings_at_or_below_minus20pct": event["eps_yoy_proxy"] <= D("-.2"),
                "volatility_at_or_above35pct": event["rv20"] >= D(".35"),
                "daily_loss_at_or_below_minus3pct": event["index_daily_return"] <= D("-.03"),
                "market_recovery_false": not event["market_recovery_ready"],
            }
            for condition, active in failed.items():
                counter[condition] += int(active)
            counter["all_market_conditions_clear_but_waiting"] += int(not any(failed.values()))
        recrosses = []
        decision_path = list(events.values())
        daily_by_date = {d["date"]: d for d in strategy.daily}
        for old, event in zip(decision_path, decision_path[1:], strict=False):
            if old["mode"] != "RECOVERY" or event["mode"] != "CASH":
                continue
            d = daily_by_date[event["date"]]
            fills = [f for f in strategy.fills if f["signal_date"] == event["date"]]
            recrosses.append(
                {
                    "date": event["date"],
                    "drawdown": d["drawdown"],
                    "prior_drawdown": daily_by_date[old["date"]]["drawdown"],
                    "exposure": d["actual_exposure"],
                    "target": event["target"],
                    "eps_yoy_proxy": event["eps_yoy_proxy"],
                    "rv20": event["rv20"],
                    "index_daily_return": event["index_daily_return"],
                    "market_recovery_ready": event["market_recovery_ready"],
                    "risk_cap": event["risk_exposure_cap"],
                    "reasons": event["reasons"],
                    "actual_fills": fills,
                }
            )
        groups[label] = {
            "metrics": metrics,
            "attribution": decomposition,
            "yearly": yearly,
            "quarters": quarters,
            "prior_state_buckets": states,
            "non_normal_episodes": episodes,
            "transitions": dict(transitions),
            "worst_five_quarters": sorted(
                quarters, key=lambda n: quarters[n]["log_relative_return"]
            )[:5],
            "zero_cost_state_changed_sessions": sum(
                events[d]["mode"] != zero_events[d]["mode"] for d in events
            ),
            "defense_cash_condition_counts_nonexclusive": dict(blocked_conditions),
            "recovery_to_cash_events": recrosses,
        }
        save_csv(OUT / (label + "-attribution-daily.csv"), path)
        save_csv(OUT / (label + "-episodes.csv"), episodes)
        print(label, decomposition["cagr_levels"], flush=True)
        print("zero-cost", float(metrics["pe-eps-zero-cost"]["calendar_cagr"]), flush=True)
    sources = set(prior["evidence_sha256"]) | {
        str(PRIOR.relative_to(ROOT).as_posix()),
        "scripts/run_return_attribution.py",
        "src/dynamic_leverage/services/return_attribution.py",
        "tests/unit/test_return_attribution.py",
        "docs/research/return-attribution-protocol-20260921.md",
    }
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "groups": groups,
        "validation": {
            "account_return_identities": checked_rows,
            "prior_cases_reproduced": checked_cases,
        },
        "new_out_of_sample": False,
        "profitability_demonstrated": False,
        "production_configuration_changed": False,
        "evidence_sha256": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sorted(sources)
        },
    }
    (OUT / "results.json").write_text(
        json.dumps(report, default=json_ready, indent=2), encoding="utf-8"
    )
    plot_paths()
    render(report)


def plot_paths():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with (OUT / "real-etf-base-pe-eps-daily.csv").open(encoding="utf-8-sig") as f:
        strategy = list(csv.DictReader(f))
    with (OUT / "real-etf-base-one-x-hold-daily.csv").open(encoding="utf-8-sig") as f:
        benchmark = list(csv.DictReader(f))
    fig, axes = plt.subplots(2, 2, figsize=(13, 7), sharex="col")
    for column, year in enumerate(["2020", "2025"]):
        selected = [i for i, d in enumerate(strategy) if d["date"].startswith(year)]
        first = selected[0]
        dates = [datetime.fromisoformat(strategy[i]["date"]) for i in selected]
        for path, label in [(strategy, "Dynamic strategy"), (benchmark, "0050 hold")]:
            baseline = float(path[first - 1]["nav"])
            axes[0, column].plot(
                dates, [100 * float(path[i]["nav"]) / baseline for i in selected], label=label
            )
        axes[0, column].set_title(year + " | prior year-end wealth = 100")
        axes[0, column].set_ylabel("Wealth, dividends included")
        axes[0, column].legend()
        axes[1, column].plot(
            dates, [float(strategy[i]["actual_exposure"]) for i in selected], color="tab:green"
        )
        axes[1, column].set_ylabel("Close equivalent exposure (x)")
        axes[1, column].axhline(1, color="gray", linestyle="--", alpha=0.5)
        if year == "2025":
            for signal in ["2025-07-04", "2025-08-01", "2025-08-28"]:
                axes[1, column].axvline(datetime.fromisoformat(signal), color="tab:red", alpha=0.5)
        for ax in axes[:, column]:
            ax.grid(alpha=0.2)
            ax.tick_params(axis="x", labelrotation=30)
    fig.suptitle(
        "Known-history diagnosis | account state preserved | red lines: repeated cash signals"
    )
    fig.tight_layout()
    fig.savefig(OUT / "recovery-timing.png", dpi=160)
    plt.close(fig)


def render(report):
    names = {
        "benchmark_net": "0050實際帳戶",
        "one_x_full": "理論完全投資0050（除息即認列）",
        "constant_linear": "固定平均等效曝險×0050日報酬",
        "constant_two_x": "固定平均正二權重（未扣交易成本）",
        "dynamic_gross": "實際每日權重、剝除交易費用",
        "dynamic_net": "動態策略實際帳戶",
    }
    lines = [
        "# 落後0050的報酬差額拆解",
        "",
        "本輪只診斷，未改策略門檻。2016-04-01～2026-09-17 已知歷史，並非新的樣本外。",
        "年化橋接表按固定順序拆解；每步差額與順序有關，不是唯一因果答案。",
        "固定平均權重是事後統計，理論每日再平衡不含其所需交易成本，不是可直接交易的建議。",
        "",
    ]
    for label, g in report["groups"].items():
        a = g["attribution"]
        lines += [
            f"## {label}",
            "",
            f"平均前收盤正二權重 {a['mean_prior_close_two_x_weight']:.2%}；"
            f"平均等效曝險 {a['mean_prior_close_equivalent_exposure']:.3f} 倍。",
            "",
            "| 橋接步驟 | 年化 | 比上一步差額（百分點） |",
            "|---|---:|---:|",
        ]
        last = None
        for n, v in a["cagr_levels"].items():
            delta = "—" if last is None else f"{(v-last)*100:+.2f}"
            lines.append(f"| {names[n]} | {v:.2%} | {delta} |")
            last = v
        lines += [
            "",
            "費用設零後完整重跑（容許股數／風控改變，與上表固定路徑不同）：",
            "",
            "| 帳戶 | 年化 | 最大回撤 | 實際交易成本 |",
            "|---|---:|---:|---:|",
        ]
        for n, m in g["metrics"].items():
            lines.append(
                f"| {n} | {m['calendar_cagr']:.2%} | {m['maximum_drawdown']:.2%} "
                f"| {m['total_cost']:,.0f} |"
            )
        lines += [
            "",
            f"零費用導致風控狀態不同的交易日：{g['zero_cost_state_changed_sessions']}。",
            "",
            "年度報酬（首尾年度不是完整年）：",
            "",
            "| 年份 | 動態 | 0050 | 平均等效曝險 |",
            "|---|---:|---:|---:|",
        ]
        for n, m in g["yearly"].items():
            lines.append(
                f"| {n} | {m['strategy_return']:.2%} | {m['benchmark_return']:.2%} "
                f"| {m['mean_exposure']:.3f} |"
            )
        lines += [
            "",
            "相對0050拖累最大的五個季度（按複利相對損失排序）：",
            "",
            "| 季度 | 動態 | 0050 | 平均等效曝險 | 持倉開始時風控狀態天數 |",
            "|---|---:|---:|---:|---|",
        ]
        for n in g["worst_five_quarters"]:
            m = g["quarters"][n]
            lines.append(
                f"| {n} | {m['strategy_return']:.2%} | {m['benchmark_return']:.2%} "
                f"| {m['mean_exposure']:.3f} | {m['state_sessions']} |"
            )
        lines += [
            "",
            "狀態切換：",
            "",
            "```json",
            json.dumps(g["transitions"], indent=2),
            "```",
            "",
        ]
        lines += [
            "恢復中再次轉為現金的決策與實際賣出：",
            "",
            "| 決策日 | 回撤 | 原等效曝險 | 目標 | 市場恢復條件 | 成交日／股數 |",
            "|---|---:|---:|---:|---|---|",
        ]
        for event in g["recovery_to_cash_events"]:
            fills = "; ".join(f"{f['date']}/{f['quantity']}" for f in event["actual_fills"])
            clear = (
                event["eps_yoy_proxy"] > D("-.2")
                and event["rv20"] < D(".35")
                and event["index_daily_return"] > D("-.03")
                and event["market_recovery_ready"]
            )
            lines.append(
                f"| {event['date']} | {event['drawdown']:.2%} | {event['exposure']:.3f} "
                f"| {event['target']:.3f} | {clear} | {fills} |"
            )
        lines.append("")
    lines += [
        "## 解讀限制",
        "",
        "正二商品項目混合內扣費用、追蹤差異、日複利與波動效應；不能全當成交易費用。",
        "狀態分組描述資金在何時承擔市場報酬；同一天可能有尚未成交的指令，不代表狀態直接造成全部損益。",
        "逐日淨報酬已用前收盤股數、分割、除息權利、交易費用核對；股息支付不重複計收益。",
        "0050理論持有與實際帳戶差額包含股息現金等待、建倉延遲、整股及交易成本。",
        "零成本完整重跑只移除顯性費用，ETF內扣成本仍在真實價格中。風控門檻未改。",
        "條件失敗天數可能重疊，不能相加當成因果貢獻；完整明細保存在results.json。",
        "",
        "![2020與2025恢復區段](recovery-timing.png)",
        "",
        "```powershell",
        "$env:PYTHONPATH='src;data/private/research-deps'",
        "C:\\anaconda3\\python.exe scripts/run_return_attribution.py",
        "```",
        "",
        "驗收：",
        "",
        "```json",
        json.dumps(report["validation"], indent=2),
        "```",
        "",
    ]
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    run()
