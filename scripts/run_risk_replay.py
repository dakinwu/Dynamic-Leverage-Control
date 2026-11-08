"""Reproduce cash-account risk and exposure-cap research, never submit broker orders."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from bisect import bisect_left
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from run_historical_study import CACHE, load_data, roc_day  # noqa: E402

from dynamic_leverage.domain.backtest import BacktestEngine, EquityPoint  # noqa: E402
from dynamic_leverage.domain.models import PolicyState, SafetyState  # noqa: E402
from dynamic_leverage.domain.strategies import DvlV2Policy, DvlV11Policy  # noqa: E402
from dynamic_leverage.services.account_replay import (  # noqa: E402
    AccountReplay,
    Quote,
    ReplayConfig,
    ReplayDay,
)
from dynamic_leverage.services.historical_study import research_inputs  # noqa: E402

OUT = ROOT / "docs/research/risk-replay-results-20260918"


def source_rows(closes, inputs, version, start, end):
    stocks = {}
    for stock in ["0050", "00631L"]:
        payload = json.loads((CACHE / f"finmind-price-{stock}.json").read_bytes())
        quotes = {
            date.fromisoformat(r["date"]): Quote(D(str(r["close"])), r["Trading_Volume"])
            for r in payload["data"]
        }
        for path in CACHE.glob(f"official-{stock}-*.json"):
            for r in json.loads(path.read_bytes())["data"]:
                quotes[roc_day(r[0])] = Quote(D(r[6].replace(",", "")), int(r[1].replace(",", "")))
        stocks[stock] = quotes
    dividends = {}
    for row in json.loads((CACHE / "TaiwanStockDividend.json").read_bytes())["data"]:
        day = date.fromisoformat(row["CashExDividendTradingDate"])
        amount = D(str(row["CashEarningsDistribution"])) + D(str(row["CashStatutorySurplus"]))
        dividends[day] = (amount, date.fromisoformat(row["CashDividendPaymentDate"]))
    splits = {date(2025, 6, 18): {"0050": D(4)}, date(2026, 3, 31): {"00631L": D(22)}}
    halts = {
        "0050": {date(2025, 6, d) for d in [11, 12, 13, 16, 17]},
        "00631L": {date(2026, 3, d) for d in [25, 26, 27, 30]},
    }
    market_dates = sorted(closes)
    trend = {
        day: closes[day] > sum((closes[d] for d in market_dates[i - 199 : i + 1]), D(0)) / 200
        for i, day in enumerate(market_dates)
        if i >= 199
    }
    policy = DvlV11Policy() if version == "v11" else DvlV2Policy()
    state = PolicyState()
    signal_start = min(inputs) if version == "v11" else date(2026, 6, 1)
    signals = {}
    for day in (d for d in market_dates if signal_start <= d <= end):
        decision = policy.evaluate(inputs[day], state)
        if not decision.valid:
            raise ValueError(f"invalid historical signal: {day}")
        target = min(decision.target_exposure, D(2))
        state = replace(decision.next_state, previous_target=target)
        signals[day] = (target, decision.safety_state)
    result = []
    previous = {}
    for day in (d for d in market_dates if start <= d <= end):
        quotes = {}
        for stock in stocks:
            if day in stocks[stock]:
                quotes[stock] = stocks[stock][day]
            elif day in halts[stock] and stock in previous:
                quotes[stock] = Quote(previous[stock].close, 0, False, False)
            else:
                raise ValueError(f"unexplained missing official trading date: {day} {stock}")
        result.append(
            ReplayDay(
                day,
                quotes,
                signals[day][0],
                signals[day][1],
                inputs[day].rv20,
                trend[day],
                {"0050": dividends[day]} if day in dividends else {},
                splits.get(day, {}),
            )
        )
        previous = quotes
    return result


def summarize(result, start, initial):
    points = [
        EquityPoint(r["date"], r["nav"], r["daily_return"], r["actual_exposure"], D(0), r["cost"])
        for r in result.daily
    ]
    metrics = asdict(BacktestEngine(initial_nav=initial)._metrics(points))
    metrics.pop("turnover")  # this account engine reports actual fills rather than weight turnover
    metrics["calendar_cagr"] = D(
        str(
            float(points[-1].nav / initial) ** (365.2425 / (points[-1].trading_date - start).days)
            - 1
        )
    )
    metrics["final_nav"] = points[-1].nav
    metrics["fill_count"] = len(result.fills)
    metrics["trade_days"] = len({f["date"] for f in result.fills})
    metrics["mean_actual_exposure"] = sum((r["actual_exposure"] for r in result.daily), D(0)) / len(
        points
    )
    metrics["max_actual_exposure"] = max(r["actual_exposure"] for r in result.daily)
    metrics["maximum_principal_loss"] = max(r["principal_loss"] for r in result.daily)
    metrics["minimum_cash"] = min(r["cash"] for r in result.daily)
    metrics["first_40_percent_breach"] = next(
        (str(e["date"]) for e in result.events if e["kind"] == "capital_limit_first_breach"), None
    )
    metrics["events"] = dict(Counter(e["kind"] for e in result.events))
    metrics["unfilled_reasons"] = dict(
        Counter(e["reason"] for e in result.events if e["kind"] == "unfilled")
    )
    metrics["terminal_receivables"] = result.daily[-1]["receivables"]
    metrics["terminal_cash"] = result.daily[-1]["cash"]
    metrics["year_returns"] = {}
    previous = initial
    for year in sorted({p.trading_date.year for p in points}):
        nav = [p.nav for p in points if p.trading_date.year == year][-1]
        metrics["year_returns"][str(year)] = nav / previous - 1
        previous = nav
    return metrics


def json_ready(value):
    if isinstance(value, D):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def rolling_two_years(rows, benchmark):
    dates = [row["date"] for row in rows]
    windows = []
    for i, day in enumerate(dates):
        anniversary = day.replace(
            year=day.year + 2, day=28 if day.month == 2 and day.day == 29 else day.day
        )
        j = bisect_left(dates, anniversary)
        if j == len(dates):
            break
        windows.append(
            {
                "start": str(day),
                "end": str(dates[j]),
                "return": rows[j]["nav"] / rows[i]["nav"] - 1,
                "benchmark_return": benchmark[j]["nav"] / benchmark[i]["nav"] - 1,
            }
        )
    return {
        "count": len(windows),
        "overlapping_not_independent": True,
        "new_entry_cost_not_reapplied": True,
        "worst": min(windows, key=lambda row: row["return"]) if windows else None,
        "loss_fraction": sum(row["return"] < 0 for row in windows) / len(windows)
        if windows
        else None,
        "beat_0050_fraction": sum(row["return"] > row["benchmark_return"] for row in windows)
        / len(windows)
        if windows
        else None,
    }


def write_trace(name, result, *, output_dir=OUT):
    for suffix, records in [("daily", result.daily), ("fills", result.fills)]:
        path = output_dir / f"{name}-{suffix}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            if records:
                writer = csv.DictWriter(handle, fieldnames=list(records[0]))
                writer.writeheader()
                writer.writerows(records)
    (output_dir / f"{name}-events.json").write_text(
        json.dumps(result.events, default=json_ready, ensure_ascii=False), encoding="utf-8"
    )


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    _, _, closes, vals, vix, audit = load_data()
    end = date.fromisoformat(audit["price_end"])
    summaries, plots = {}, {}
    for version in ["v11", "v2"]:
        start = date(2016, 1, 4) if version == "v11" else date(2026, 6, 2)
        for stress in [False, True]:
            lag, delay, costs = (60, 2, D(2)) if stress else (45, 1, D(1))
            inputs = research_inputs(closes, vals, vix, publication_lag_days=lag)
            rows = source_rows(closes, inputs, version, start, end)
            case = f"{version}-{'stress' if stress else 'base'}"
            cases = [
                (f"{mode}-cap{cap}", mode, D(cap), None)
                for mode in ["ungated", "existing", "defensive"]
                for cap in ["1.0", "1.2", "1.6", "2.0"]
            ]
            cases += [("0050", "buyhold", D(1), D(1)), ("00631L", "buyhold", D(2), D(2))]
            cases += [(f"fixed{cap}", "existing", D(cap), D(cap)) for cap in ["1.2", "1.6", "2.0"]]
            group = {"start": str(start), "end": str(end), "results": {}}
            replay_paths = {}
            for name, mode, cap, fixed in cases:
                config = ReplayConfig(
                    mode=mode,
                    ceiling=cap,
                    execution_delay=delay,
                    commission=D("0.001425") * costs,
                    sell_tax=D("0.001") * costs,
                    slippage=D("0.0005") * costs,
                    minimum_commission=D(20) * costs,
                )
                data = (
                    rows
                    if fixed is None
                    else [replace(r, target=fixed, strategy_state=SafetyState.NORMAL) for r in rows]
                )
                replay = AccountReplay(config).run(data)
                summary = summarize(replay, start, config.initial_nav)
                summary["config"] = asdict(config)
                summary["signal_days_above_ceiling"] = sum(r.target > cap for r in data)
                group["results"][name] = summary
                replay_paths[name] = replay.daily
                write_trace(f"{case}-{name}", replay)
                if not stress:
                    plots[(version, name)] = replay.daily
            benchmark = group["results"]["0050"]
            for name, summary in group["results"].items():
                summary["rolling_two_years"] = rolling_two_years(
                    replay_paths[name], replay_paths["0050"]
                )
                summary["q7"] = {
                    "higher_return": summary["total_return"] > benchmark["total_return"],
                    "volatility_not_higher": summary["annualized_volatility"]
                    <= benchmark["annualized_volatility"],
                    "drawdown_not_worse": summary["maximum_drawdown"]
                    >= benchmark["maximum_drawdown"],
                    "drawdown_within_40_percent": summary["maximum_drawdown"] >= D("-0.4"),
                }
                summary["q7_pass"] = all(summary["q7"].values())
            summaries[case] = group
            print(
                case,
                {
                    n: (
                        round(float(m["calendar_cagr"]) * 100, 2),
                        round(float(m["maximum_drawdown"]) * 100, 2),
                    )
                    for n, m in group["results"].items()
                },
                flush=True,
            )
    protocol = ROOT / "docs/research/risk-replay-protocol-20260918.md"
    evidence_paths = [
        protocol,
        Path(__file__),
        ROOT / "src/dynamic_leverage/services/account_replay.py",
        ROOT / "src/dynamic_leverage/domain/risk.py",
        ROOT / "src/dynamic_leverage/domain/ledger.py",
        ROOT / "src/dynamic_leverage/domain/strategies.py",
        ROOT / "scripts/run_historical_study.py",
        ROOT / "src/dynamic_leverage/services/historical_study.py",
        ROOT / "tests/unit/test_account_replay.py",
    ]
    evidence_paths += [
        p
        for p in CACHE.iterdir()
        if p.suffix in {".json", ".txt"}
        and (
            p.name.startswith(
                (
                    "finmind-price",
                    "TaiwanStockDividend",
                    "official-0050",
                    "official-00631L",
                    "taiex-",
                    "vix-",
                )
            )
            or p.name == "valuations.json"
        )
    ]
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "retrospective_account_replay",
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "audit": audit,
        "scenarios": summaries,
        "evidence_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in evidence_paths
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

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    chosen = ["0050", "ungated-cap2.0", "existing-cap2.0", "defensive-cap2.0", "fixed1.2"]
    for col, version in enumerate(["v11", "v2"]):
        for name in chosen:
            rows = plots[(version, name)]
            dates = [r["date"] for r in rows]
            axes[0, col].plot(dates, [float(r["nav"]) / 500000 for r in rows], label=name)
            axes[1, col].plot(dates, [-100 * float(r["drawdown"]) for r in rows])
        axes[0, col].set_title(version + " | actual account cash flows, retrospective")
        axes[0, col].set_ylabel("Wealth multiple")
        axes[0, col].legend(fontsize=8)
        axes[1, col].set_ylabel("Drawdown (%)")
        for row in range(2):
            axes[row, col].grid(alpha=0.2)
            axes[row, col].tick_params(axis="x", labelrotation=20)
    fig.tight_layout()
    fig.savefig(OUT / "performance.png", dpi=150)
    plt.close(fig)
    lines = [
        "# 帳戶風控回放與最大曝險研究",
        "",
        "50 萬元、真實原始價格／配息到帳／分割、整數股、費稅與滑價、交割與委託延遲。",
        "這是收盤日頻的帳戶模擬，並非完整實盤重建，也不是樣本外證明。",
        "",
        "## 判讀結論",
        "",
        "- 所有候選均未同時達到 Q7 數值條件：扣成本報酬勝過 0050、"
        "波動與回撤不高於 0050、回撤不超過 40%。",
        "- V1.1 接現有風控：上限 1.2 倍年化 25.91%、最大回撤 35.54%；"
        "上限 2 倍年化 24.94%、最大回撤 48.57%。提高上限沒有改善這組規則。",
        "- 同引擎省略帳戶風控的 2 倍上限年化 31.02%、最大回撤 49.30%；"
        "接現有風控後報酬降得多，回撤改善有限。2022-09-26 首次碰到 40% 界線，"
        "歷史最大回撤鎖定後一直禁止增加曝險，並不自動出清部位。",
        "- 本輪預先固定的防守方案（2 倍上限）年化 13.74%、最大回撤 25.36%，"
        "防守較強但犧牲大量報酬，也未通過 Q7。不能直接替換正式策略。",
        "- 同期 0050 年化 24.07%、回撤 33.88%；固定 1.2 倍加相同風控年化 27.17%、"
        "回撤 37.26%。動態策略尚未證明比簡單配置更有價值。",
        "- 曝險上限只在原始目標超過上限時生效，不會創造判斷優勢。"
        "例如 30 萬元 0050 加 20 萬元 00631L，目標曝險約為 1.4 倍；"
        "即使允許上限 2 倍，仍可以只持有 1.4 倍。",
        "- 下一個研究問題是提前減碼、恢復加碼條件與交易頻率能否改善取捨；"
        "保留真實歷史回撤紀錄，不能靠重設風險紀錄或放寬 40% 使結果通過。",
        "",
        "![帳戶淨值與回撤](performance.png)",
        "",
    ]
    for key, group in report["scenarios"].items():
        lines += [
            f"## {key}：{group['start']}～{group['end']}",
            "",
            "| 方案 | 期末資產 | 累計報酬 | 曆日年化 | 年化波動 | 最大回撤 | "
            "平均曝險 | 最大曝險 | 成交日 | Q7 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for name, m in group["results"].items():
            lines.append(
                f"| {name} | {m['final_nav']:,.0f} | {m['total_return']:.2%} | "
                f"{m['calendar_cagr']:.2%} | "
                f"{m['annualized_volatility']:.2%} | {m['maximum_drawdown']:.2%} | "
                f"{m['mean_actual_exposure']:.3f} | {m['max_actual_exposure']:.3f} | "
                f"{m['trade_days']} | {m['q7_pass']} |"
            )
        lines += [
            "",
            "### 風控事件與成本",
            "",
            "| 方案 | 首次 40% 觸發 | 費稅及滑價金額合計 | 風控拒絕日數 | 未成交紀錄數 |",
            "|---|---|---:|---:|---:|",
        ]
        for name, m in group["results"].items():
            lines.append(
                f"| {name} | {m['first_40_percent_breach']} | {m['total_cost']:,.0f} | "
                f"{m['events'].get('risk_rejected', 0)} | {m['events'].get('unfilled', 0)} |"
            )
        lines.append("")
        lines += [
            "### 兩年持有視窗（重疊，不是未來勝率）",
            "",
            "| 方案 | 最差兩年報酬 | 視窗虧損比例 | 勝過 0050 的視窗比例 |",
            "|---|---:|---:|---:|",
        ]
        for name, m in group["results"].items():
            window = m["rolling_two_years"]
            if window["count"]:
                lines.append(
                    f"| {name} | {window['worst']['return']:.2%} | "
                    f"{window['loss_fraction']:.2%} | {window['beat_0050_fraction']:.2%} |"
                )
        if key.startswith("v2"):
            lines.append("\nV2 期間不足兩年，沒有兩年持有視窗。")
        lines.append("")
        lines += [
            "### 主要方案分年報酬（首尾年可能不完整）",
            "",
            "| 年份 | 0050 | 無帳戶風控 2 倍上限 | 現有風控 2 倍上限 | "
            "防守 2 倍上限 | 固定 1.2 倍 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for year in group["results"]["0050"]["year_returns"]:
            values = " | ".join(
                f"{group['results'][name]['year_returns'][year]:.2%}" for name in chosen
            )
            lines.append(f"| {year} | {values} |")
        lines.append("")
    lines += [
        "## 比較方式與限制",
        "",
        "- ungated：同一帳戶模擬下省略 RiskGate；existing：原有 RiskGate；"
        "defensive：原有 RiskGate 加事前固定的均線／波動防守與分批恢復。",
        "- cap 是配置目標上限；價格變動與延後成交仍可使實際曝險暫時超過，表中如實列出。",
        "- 超過上限 0.001 倍即提出減碼，不受正常 0.15 倍不交易區間限制；"
        "因此小額交易與最低手續費也計入。",
        "- 兩年視窗是同一帳戶路徑切片，沿用先前持倉與風控鎖定，不重新開戶或重算入場費。",
        "- 壓力情境同時採月報延遲 60 天、成交延遲兩個交易日、全部成本兩倍；"
        "它不是單一因素因果拆解。正常情境月報延遲 45 天、成交延遲一個交易日。",
        "- 完整回放了本次定義的帳戶／風控／委託／成交循環；"
        "正常歷史帳戶資格、資料健康、人工同意及券商對帳為假設成立。",
        "- 40% 歷史最大回撤鎖定不自動解除；門檻不保證止損價格。",
        "- 配息除息日起列應收款、實際付款日才可用；賣款兩個交易日後可用，"
        "交割銀行日曆與券商淨額交割仍是近似。未計個人稅務。",
        "- 每筆最低手續費 20 元、最多占日量 1%、收盤加減滑價為研究假設；"
        "並非重建歷史零股撮合或逐筆委託簿。實際漲跌停無法成交需補足當時價量資料。",
        "- 停牌禁止成交；資料不安全禁止買入、對帳失敗及人工暫停阻擋全部成交。"
        "故障注入測試驗證的是邏輯，不能冒充歷史曾發生的故障。",
        "- 與上一輪比較時，交割、配息、股數及成交時點均有改變；"
        "只比較本輪同引擎的 ungated／existing 才可判斷風控影響。",
        "- 當期年化數字不代表未來；V2 樣本只有約三個半月。研究規則已看過歷史，"
        "任何較佳結果都須另做未參與設計的資料驗證。",
        "- 所有方案保持不借款、無放空，沒有擅自放寬 40% 界線或更新運作策略。",
        "",
        "## 重現與證據",
        "",
        "執行 `python scripts/run_risk_replay.py`。`results.json` 包含所有 68 個情境與來源雜湊；"
        "所有情境另輸出逐日帳戶、每筆成交及委託／阻擋事件。",
        "",
        "規格：[研究設計](../risk-replay-protocol-20260918.md)。"
        "原始來源及驗收：[前輪報告](../historical-results-20260918/report.md)。",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
