"""Frozen research candidates; never update live strategy selection or broker state."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

from run_cash_reserve_study import extra_metrics
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

from dynamic_leverage.domain.reserve_policy import ReserveConfig, ReservePolicy

OUT = ROOT / "docs/research/valuation-reserve-results-20260918"
START = date(2016, 4, 1)
CANDIDATES = {
    "A-absolute-staged": ReserveConfig(),
    "B-percentile-staged": ReserveConfig(valuation="percentile"),
    "A-no-staging": ReserveConfig(staged=False),
    "A-no-soft-defense": ReserveConfig(soft_controls=False),
}


def base_config(stress=False, **changes):
    multiplier = D(2) if stress else D(1)
    return replace(
        ReplayConfig(
            recipe="2x-cash-v1",
            execution_delay=2 if stress else 1,
            commission=D(".001425") * multiplier,
            sell_tax=D(".001") * multiplier,
            slippage=D(".0005") * multiplier,
            minimum_commission=D(20) * multiplier,
        ),
        **changes,
    )


def control_regression(old, closes, valuations, vix, end):
    matched = 0
    for group_name, group in old["scenarios"].items():
        version, condition = group_name.split("-")
        stress = condition == "stress"
        inputs = research_inputs(closes, valuations, vix, publication_lag_days=60 if stress else 45)
        start = date.fromisoformat(group["start"])
        rows = source_rows(closes, inputs, version, start, end)
        for name, prior in group["results"].items():
            cfg_data = prior["config"].copy()
            for key in [
                "ceiling",
                "initial_nav",
                "commission",
                "sell_tax",
                "slippage",
                "minimum_commission",
                "volume_fraction",
                "no_trade_band",
            ]:
                cfg_data[key] = D(str(cfg_data[key]))
            cfg = ReplayConfig(**cfg_data)
            fixed = name.startswith("fixed-") or name in {"0050", "00631L"}
            selected = (
                [replace(r, target=cfg.ceiling, strategy_state=SafetyState.NORMAL) for r in rows]
                if fixed
                else rows
            )
            result = AccountReplay(cfg).run(selected)
            metrics = summarize(result, start, cfg.initial_nav)
            for key in [
                "final_nav",
                "calendar_cagr",
                "maximum_drawdown",
                "total_cost",
                "fill_count",
            ]:
                if abs(D(str(prior[key])) - metrics[key]) > D(".000001"):
                    raise AssertionError(f"Legacy behavior changed: {group_name}/{name}/{key}")
            matched += 1
    return matched


def path_metrics(rows, events):
    segments = {}
    for begin, finish in [(2016, 2019), (2020, 2022), (2023, 2026)]:
        selected = [r for r in rows if begin <= r["date"].year <= finish]
        if not selected:
            continue
        prior = [r for r in rows if r["date"] < selected[0]["date"]]
        initial = prior[-1]["nav"] if prior else rows[0]["nav"]
        segments[f"{begin}-{finish}"] = selected[-1]["nav"] / initial - 1
    peak_date = rows[0]["date"]
    longest = 0
    underwater = False
    for r in rows:
        if r["drawdown"] == 0:
            if underwater:
                longest = max(longest, (r["date"] - peak_date).days)
            peak_date = r["date"]
            underwater = False
        else:
            underwater = True
            longest = max(longest, (r["date"] - peak_date).days)
    decisions = [e for e in events if e["kind"] == "reserve_decision"]
    return {
        "segment_returns": segments,
        "longest_underwater_calendar_days": longest,
        "ending_below_peak": rows[-1]["drawdown"] > 0,
        "soft_pause_days": sum(e["paused"] for e in decisions),
        "soft_recovery_count": sum("SOFT_RECOVERY_CONFIRMED" in e["reasons"] for e in decisions),
        "reserve_reason_counts": dict(
            Counter(reason for e in decisions for reason in e["reasons"])
        ),
    }


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    prior_path = ROOT / "docs/research/cash-reserve-results-20260918/results.json"
    old = json.loads(prior_path.read_bytes())
    sources = {p: h for p, h in old["evidence_sha256"].items() if p.startswith("data/private/")}
    if not sources:
        raise AssertionError("missing frozen raw sources")
    for path, digest in sources.items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest:
            raise AssertionError(f"raw source changed: {path}")
    _, _, closes, valuations, vix, audit = load_data()
    end = date.fromisoformat(audit["price_end"])
    matched = control_regression(old, closes, valuations, vix, end)
    groups, plots = {}, {}
    for stress in (False, True):
        group = "stress" if stress else "base"
        inputs = research_inputs(closes, valuations, vix, publication_lag_days=60 if stress else 45)
        rows = [
            replace(r, policy_inputs=inputs[r.day])
            for r in source_rows(closes, inputs, "v11", START, end)
        ]
        cases = [
            (name, base_config(stress), rows, ReservePolicy(config))
            for name, config in CANDIDATES.items()
        ]
        cases += [
            (f"old-v11-cash-{cap}", base_config(stress, ceiling=D(cap)), rows, None)
            for cap in ("1.2", "2.0")
        ]
        fixed = [replace(r, target=D("1.2"), strategy_state=SafetyState.NORMAL) for r in rows]
        cases.append(("fixed-60-40", base_config(stress, ceiling=D("1.2")), fixed, None))
        for name, cap in [("0050", D(1)), ("00631L", D(2))]:
            fixed = [replace(r, target=cap, strategy_state=SafetyState.NORMAL) for r in rows]
            cfg = base_config(stress, mode="buyhold", recipe="1x-2x-v1", ceiling=cap)
            cases.append((name, cfg, fixed, None))
        results, paths = {}, {}
        for name, cfg, selected, policy in cases:
            result = AccountReplay(cfg).run(selected, reserve_policy=policy)
            m = summarize(result, START, cfg.initial_nav)
            m.update(extra_metrics(result.daily))
            m.update(path_metrics(result.daily, result.events))
            m["config"] = asdict(cfg)
            m["reserve_config"] = asdict(policy.config) if policy else None
            write_trace(f"{group}-{name}", result, output_dir=OUT)
            results[name] = m
            paths[name] = result.daily
        benchmark = results["0050"]
        for name, m in results.items():
            m["rolling_two_years"] = rolling_two_years(paths[name], paths["0050"])
            m["q7_numeric"] = {
                "higher_return": m["total_return"] > benchmark["total_return"],
                "volatility_not_higher": m["annualized_volatility"]
                <= benchmark["annualized_volatility"],
                "drawdown_not_worse": m["maximum_drawdown"] >= benchmark["maximum_drawdown"],
                "within_40_percent": m["maximum_drawdown"] >= D("-.4"),
            }
            m["q7_numeric_pass"] = all(m["q7_numeric"].values())
        groups[group] = {"start": str(START), "end": str(end), "results": results}
        if not stress:
            plots = paths
        print(
            group,
            {
                n: (
                    round(float(m["calendar_cagr"]) * 100, 2),
                    round(float(m["maximum_drawdown"]) * 100, 2),
                )
                for n, m in results.items()
            },
            flush=True,
        )
    files = [
        Path(__file__),
        prior_path,
        ROOT / "docs/research/valuation-reserve-protocol-20260918.md",
    ]
    files += [
        ROOT / p
        for p in [
            "src/dynamic_leverage/domain/reserve_policy.py",
            "src/dynamic_leverage/services/account_replay.py",
            "src/dynamic_leverage/domain/risk.py",
            "src/dynamic_leverage/domain/ledger.py",
            "src/dynamic_leverage/domain/strategies.py",
            "src/dynamic_leverage/domain/execution.py",
            "src/dynamic_leverage/domain/backtest.py",
            "src/dynamic_leverage/services/historical_study.py",
            "scripts/run_risk_replay.py",
            "scripts/run_cash_reserve_study.py",
            "scripts/run_historical_study.py",
            "tests/unit/test_reserve_policy.py",
            "tests/unit/test_account_replay.py",
        ]
    ]
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "valuation-reserve-exploration",
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "legacy_controls_reproduced": matched,
        "audit": audit,
        "scenarios": groups,
        "evidence_sha256": sources
        | {
            p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files
        },
    }
    (OUT / "results.json").write_text(
        json.dumps(report, default=json_ready, indent=2), encoding="utf-8"
    )
    render(report, plots)


def render(report, plots):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(13, 11), sharex=True)
    for name in ["0050", "fixed-60-40", *CANDIDATES]:
        rows = plots[name]
        dates = [r["date"] for r in rows]
        axes[0].plot(dates, [float(r["nav"]) / 500000 for r in rows], label=name)
        axes[1].plot(dates, [-100 * float(r["drawdown"]) for r in rows])
        axes[2].plot(dates, [100 * float(r["cash"] / r["nav"]) for r in rows])
    for ax, label in zip(
        axes, ["Wealth multiple", "Drawdown (%)", "Available cash (%)"], strict=True
    ):
        ax.set_ylabel(label)
        ax.grid(alpha=0.2)
    axes[0].set_title("Valuation reserve candidates | 2016-04-01 to 2026-09-17 | retrospective")
    axes[0].legend(fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(OUT / "comparison.png", dpi=150)
    plt.close(fig)
    lines = [
        "# 估值分批投入與恢復加碼：歷史研究結果",
        "",
        "50 萬、不借款、沿用 40% 硬界線；新候選的完整規則先記錄，再計算本輪績效。",
        "既有歷史已看過，屬探索研究，並非樣本外驗證。未切換正式策略。",
        "",
        "## 結論",
        "",
        "正常與壓力情境共 18 組，沒有候選同時通過 Q7 數值條件。"
        "所有原始候選均保留，不在看過績效後改參數。",
        "",
        "- A 絕對本益比分批：年化 23.57%、最大回撤 40.10%；"
        "2020-03-19 首次碰到 40% 界線。5 日恢復只能解除市場暫停，無法解除歷史資本鎖定。",
        "- B 分位版本：年化 18.91%、回撤 36.58%，沒有碰到 40%，但仍輸給同期 0050，"
        "回撤也高於 0050。",
        "- A 移除分批：年化 23.24%、回撤 40.0018%。"
        "分批對這段歷史的改善有限；四捨五入成 40.00% 不能算符合界線。",
        "- A 移除軟性防守：年化 31.72%、回撤 47.37%，"
        "平均曝險也較高。這是報酬與風險的取捨，並非符合使用者要求的改進。",
        "- 同期固定 60% 正二／40% 現金年化 25.75%、回撤 33.97%；"
        "0050 為 24.01%／33.88%。固定配方波動仍高於 0050，亦不通過 Q7。",
        "- 壓力情境下 A 回撤擴至 42.46%，顯示延後成交與資料延遲下更難守住界線；"
        "此情境同時改變多個因素，不作單一原因歸因。",
        "",
        "![候選資產、回撤與現金](comparison.png)",
        "",
        "A＝絕對本益比分批；B＝本益比分位分批；A-no-staging＝移除分批；",
        "A-no-soft-defense＝移除軟性防守，仍保留操作與 40% 帳戶限制。",
        "",
    ]
    for key, g in report["scenarios"].items():
        lines += [
            f"## {key}：{g['start']}～{g['end']}",
            "",
            "| 方案 | 期末資產 | 年化 | 波動 | 最大回撤 | 平均曝險 | 平均現金 | Q7數值 |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for name, m in g["results"].items():
            lines.append(
                f"| {name} | {m['final_nav']:,.0f} | {m['calendar_cagr']:.2%} | "
                f"{m['annualized_volatility']:.2%} | {m['maximum_drawdown']:.2%} | "
                f"{m['mean_actual_exposure']:.3f} | {m['mean_cash_fraction']:.2%} | "
                f"{m['q7_numeric_pass']} |"
            )
        lines += [
            "",
            "### 現金、暫停與成本",
            "",
            "| 方案 | 暫停加碼日 | 恢復次數 | 成交日 | 費稅滑價 | 首次40% | 最長未回高點曆日 |",
            "|---|---:|---:|---:|---:|---|---:|",
        ]
        for name, m in g["results"].items():
            lines.append(
                f"| {name} | {m['soft_pause_days']} | {m['soft_recovery_count']} | "
                f"{m['trade_days']} | {m['total_cost']:,.0f} | {m['first_40_percent_breach']} | "
                f"{m['longest_underwater_calendar_days']} |"
            )
        lines += [
            "",
            "### 路徑分段（非獨立開戶或樣本外）",
            "",
            "| 方案 | 2016–2019 | 2020–2022 | 2023–2026 | 最差兩年報酬 |",
            "|---|---:|---:|---:|---:|",
        ]
        for name, m in g["results"].items():
            parts = m["segment_returns"]
            worst = m["rolling_two_years"]["worst"]
            lines.append(
                f"| {name} | {parts['2016-2019']:.2%} | {parts['2020-2022']:.2%} | "
                f"{parts['2023-2026']:.2%} | {worst['return']:.2%} |"
            )
        lines.append("")
    lines += [
        "## 驗收界線與重現",
        "",
        f"未接新控制器時，{report['legacy_controls_reproduced']} 個前輪情境"
        "已重現相同淨值、收益、回撤、費用與成交數。",
        "新候選需要完整 EPS 代理，因此採共同起點 2016-04-01；不能直接與前輪 2016-01 起的年化混比。",
        "軟性暫停只在 5 個清除日後解除；歷史 40% 鎖定不解除。"
        "0.1 倍正常差額、每週一次及現金底額均留於事件明細。",
        "預留底額以訊號日淨值計算，不保證未來每一天現金占比；買單延後成交仍可能遇到額外下跌。",
        "每筆新買入預算含手續費，未成交不假裝持有；未用週額度不累積。",
        "日頻成交、交割銀行日曆、個人稅務與資料當時公布時間仍有前輪列示的限制。",
        "尚未驗證 2000／2008 的真實商品或合成危機路徑；不宣稱本輪涵蓋。",
        "兩年視窗重疊且沿用之前持倉與狀態；比例不是未來勝率。",
        "執行 `python scripts/run_valuation_reserve_study.py`。"
        "全部情境保留逐日帳戶、成交、控制器與風控事件。",
        "[事前規格](../valuation-reserve-protocol-20260918.md)。"
        "`results.json` 包含原始來源與程式雜湊。",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
