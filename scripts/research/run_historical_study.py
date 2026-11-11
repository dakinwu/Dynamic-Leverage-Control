"""Reproduce the dated retrospective study from its local, immutable source cache."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from bisect import bisect_left
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

from dynamic_leverage.domain.backtest import BacktestEngine
from dynamic_leverage.domain.execution import OneXTwoXRecipe
from dynamic_leverage.services.historical_study import (
    HistoricalPrice,
    lagged_points,
    official_total_returns,
    research_inputs,
    research_targets,
)

ROOT = Path(__file__).resolve().parents[2]


CACHE = ROOT / "data/private/historical-research-20260918"
OUT = ROOT / "docs/research/historical-results-20260918"
INITIAL = D(500000)


def yahoo(name):
    content = json.loads((CACHE / f"{name}.raw").read_bytes())["chart"]["result"][0]
    quote = content["indicators"]["quote"][0]
    adjusted = content["indicators"].get("adjclose", [{"adjclose": quote["close"]}])[0]["adjclose"]
    prices, raw = {}, {}
    for i, timestamp in enumerate(content["timestamp"]):
        day = datetime.fromtimestamp(timestamp, UTC).date()
        if adjusted[i] is None or quote["close"][i] is None:
            continue
        prices[day] = HistoricalPrice(D(str(adjusted[i])), int(quote["volume"][i] or 0))
        raw[day] = D(str(quote["close"][i]))
    return prices, raw, content.get("events", {})


def roc_day(value):
    year, month, day = map(int, value.split("/"))
    return date(year + 1911, month, day)


def load_data():
    official = {}
    for path in sorted(CACHE.glob("taiex-*.json")):
        for row in json.loads(path.read_bytes())["data"]:
            official[roc_day(row[0])] = D(str(row[4]).replace(",", ""))
    if len(list(CACHE.glob("taiex-*.json"))) != 141:
        raise ValueError("expected all 141 official index months, 2015-01 through 2026-09")
    end = min(max(official), date(2026, 9, 17))
    dates = sorted(d for d in official if date(2016, 1, 1) <= d <= end)
    closes = official.copy()
    dividend_rows = json.loads((CACHE / "TaiwanStockDividendResult.json").read_bytes())["data"]
    dividends = {
        date.fromisoformat(r["date"]): D(str(r["stock_and_cache_dividend"]))
        for r in dividend_rows
        if str(dates[0]) <= r["date"] <= str(end)
    }
    policy_rows = json.loads((CACHE / "TaiwanStockDividend.json").read_bytes())["data"]
    policy_dividends = {
        date.fromisoformat(r["CashExDividendTradingDate"]): D(str(r["CashEarningsDistribution"]))
        + D(str(r["CashStatutorySurplus"]))
        for r in policy_rows
        if str(dates[0]) <= r["CashExDividendTradingDate"] <= str(end)
    }
    if dividends != policy_dividends or len(dividends) != 21:
        raise ValueError("dividend result and policy records disagree")
    for dataset in ["TaiwanStockDividendResult", "TaiwanStockDividend"]:
        leveraged_dividends = json.loads((CACHE / f"{dataset}-00631L.json").read_bytes())
        if leveraged_dividends.get("status") != 200 or leveraged_dividends.get("data"):
            raise ValueError("00631L no-distribution assumption requires revalidation")
    official_dividend_checks = []
    for path in [
        *CACHE.glob("official-dividend-20*.json"),
        CACHE / "official-dividend-probe-startDate.json",
    ]:
        for row in json.loads(path.read_bytes()).get("data", []):
            if row[1] != "0050":
                continue
            year, month, day = map(int, re.findall(r"\d+", row[0]))
            observed = date(year + 1911, month, day)
            value = D(row[5])
            if dividends.get(observed) != value:
                raise ValueError(f"official dividend mismatch on {observed}")
            official_dividend_checks.append({"date": str(observed), "cash": str(value)})
    halts = {
        "0050": {date(2025, 6, d) for d in [11, 12, 13, 16, 17]},
        "00631L": {date(2026, 3, d) for d in [25, 26, 27, 30]},
    }
    splits = {"0050": {date(2025, 6, 18): D(4)}, "00631L": {date(2026, 3, 31): D(22)}}
    series, audit_quotes, raw_quotes = {}, {}, {}
    for stock in ["0050", "00631L"]:
        payload = json.loads((CACHE / f"finmind-price-{stock}.json").read_bytes())
        if payload.get("status") != 200:
            raise ValueError("raw ETF dataset unavailable")
        quotes = {
            date.fromisoformat(r["date"]): (D(str(r["close"])), int(r["Trading_Volume"]))
            for r in payload["data"]
            if str(dates[0]) <= r["date"] <= str(end)
        }
        comparisons, differences = 0, []
        for path in sorted(CACHE.glob(f"official-{stock}-*.json")):
            for row in json.loads(path.read_bytes())["data"]:
                day = roc_day(row[0])
                if day < dates[0] or day > end:
                    continue
                price, volume = D(row[6].replace(",", "")), int(row[1].replace(",", ""))
                if day in quotes:
                    comparisons += 1
                    if quotes[day][0] != price:
                        differences.append(
                            {
                                "date": str(day),
                                "finmind": str(quotes[day][0]),
                                "official": str(price),
                            }
                        )
                quotes[day] = (price, volume)
        raw_quotes[stock] = quotes
        audit_quotes[stock] = {
            "actual_quote_days": len(quotes),
            "official_crosschecked_days": comparisons,
            "official_overrides": differences,
            "supplemental_only_days": len(quotes) - comparisons,
        }
        series[stock] = official_total_returns(
            quotes, dividends if stock == "0050" else {}, splits[stock], dates, halts[stock]
        )
    if raw_quotes["0050"][date(2025, 6, 10)][0] != D("188.65"):
        raise ValueError("0050 pre-split close disagrees with official split notice")
    if raw_quotes["00631L"][date(2026, 3, 24)][0] != D("443.15"):
        raise ValueError("00631L pre-split close disagrees with official split notice")
    vix = {}
    for path in sorted(CACHE.glob("vix-*.txt")):
        for line in path.read_text(encoding="big5", errors="replace").splitlines():
            values = line.split()
            if len(values) >= 4 and values[0].isdigit() and len(values[0]) == 8:
                day = datetime.strptime(values[0], "%Y%m%d").date()
                vix[day] = D(values[-1])
    valuations = {
        int(k[:4]) * 12 + int(k[5:]): D(r["pe"])
        for k, r in json.loads((CACHE / "valuations.json").read_text(encoding="utf-8")).items()
    }
    if len(valuations) != 260:
        raise ValueError("expected 260 official monthly valuation observations")
    for prices in series.values():
        if any(
            abs(prices[b].adjusted_close / prices[a].adjusted_close - 1) > D("0.3")
            for a, b in zip(dates, dates[1:], strict=False)
        ):
            raise ValueError("unreviewed actual total return exceeds 30 percent")
    audit = {
        "return_method": "actual raw closes + ex-date cash dividends + actual split ratios",
        "primary_etf_sources": "TWSE direct observations, supplemented by FinMind TaiwanStockPrice",
        "quote_validation": audit_quotes,
        "dividend_count_0050": len(dividends),
        "dividend_policy_crosscheck_all_pass": True,
        "official_dividend_checks": official_dividend_checks,
        "excluded_vendor_dividend": {
            "source": "Yahoo",
            "date": "2022-08-02",
            "amount": "0.0575",
            "reason": "not present in TWSE 2022 July-August results or FinMind",
        },
        "valuation_months": len(valuations),
        "valuation_range": ["2005-01", "2026-08"],
        "official_index_days": len(official),
        "official_index_range": [str(min(official)), str(max(official))],
        "vix_days": len(vix),
        "vix_range": [str(min(vix)), str(max(vix))],
        "price_end": str(end),
        "complete_return_calendar": True,
        "nontradable_days": {stock: sorted(map(str, days)) for stock, days in halts.items()},
        "historical_research_ready": False,
        "profitability_demonstrated": False,
    }
    return series["0050"], series["00631L"], closes, valuations, vix, audit


def metrics(result, start):
    output = {k: float(v) if isinstance(v, D) else v for k, v in asdict(result.metrics).items()}
    years = (result.equity[-1].trading_date - start).days / 365.2425
    output["calendar_cagr"] = float(result.equity[-1].nav / INITIAL) ** (1 / years) - 1
    output["final_nav"] = float(result.equity[-1].nav)
    output["trading_intervals"] = len(result.equity)
    output["trade_days_including_initial"] = sum(point.cost > 0 for point in result.equity)
    output["mean_exposure"] = float(
        sum(p.actual_exposure for p in result.equity) / len(result.equity)
    )
    peak, peak_date, worst = INITIAL, start, D(0)
    for point in result.equity:
        if point.nav >= peak:
            peak, peak_date = point.nav, point.trading_date
        drawdown = point.nav / peak - 1
        if drawdown < worst:
            worst = drawdown
            output["maximum_drawdown_period"] = {
                "peak_date": str(peak_date),
                "trough_date": str(point.trading_date),
                "peak_nav": float(peak),
                "trough_nav": float(point.nav),
            }
    return output


def calendar_years(result):
    previous = INITIAL
    output = {}
    for year in sorted({p.trading_date.year for p in result.equity}):
        rows = [p for p in result.equity if p.trading_date.year == year]
        output[str(year)] = float(rows[-1].nav / previous - 1)
        previous = rows[-1].nav
    return output


def rolling_two_years(result, benchmark, start):
    dates = [start, *[p.trading_date for p in result.equity]]
    navs = [INITIAL, *[p.nav for p in result.equity]]
    base = [INITIAL, *[p.nav for p in benchmark.equity]]
    rows = []
    for i, day in enumerate(dates):
        anniversary = day.replace(
            year=day.year + 2, day=28 if day.month == 2 and day.day == 29 else day.day
        )
        j = bisect_left(dates, anniversary)
        if j >= len(dates):
            break
        rows.append(
            {
                "start": str(day),
                "end": str(dates[j]),
                "return": float(navs[j] / navs[i] - 1),
                "0050_return": float(base[j] / base[i] - 1),
            }
        )
    if not rows:
        return {"windows": 0}
    return {
        "windows": len(rows),
        "worst": min(rows, key=lambda r: r["return"]),
        "best": max(rows, key=lambda r: r["return"]),
        "loss_fraction": sum(r["return"] < 0 for r in rows) / len(rows),
        "beat_0050_fraction": sum(r["return"] > r["0050_return"] for r in rows) / len(rows),
        "overlapping_not_independent": True,
        "fresh_entry_cost_not_reapplied": True,
    }


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    one, two, closes, vals, vix, audit = load_data()
    end = date.fromisoformat(audit["price_end"])
    studies, main_results = {}, {}
    for version in ["v11", "v2_cash_cap2"]:
        for lag in [45, 60]:
            inputs = research_inputs(closes, vals, vix, publication_lag_days=lag)
            signal_start = min(inputs) if version == "v11" else min(vix)
            targets, capped = research_targets(
                inputs, version=version, start=signal_start, end=end, market_dates=sorted(closes)
            )
            start = (
                min(d for d in one if d >= date(2016, 1, 1))
                if version == "v11"
                else min(d for d in one if d > signal_start)
            )
            points = lagged_points(one, two, targets, start=start, end=end)
            if lag == 45:
                with (OUT / f"{version}-signals.csv").open("w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(
                        [
                            "decision_date",
                            "target",
                            "pe",
                            "pe_percentile",
                            "eps_yoy",
                            "rv20",
                            "taiwan_vix",
                        ]
                    )
                    for day, target in targets.items():
                        i = inputs[day]
                        writer.writerow(
                            [
                                day,
                                target,
                                i.pe_ttm,
                                i.pe_percentile,
                                i.eps_yoy,
                                i.rv20,
                                i.taiwan_vix,
                            ]
                        )
            for cost_multiple in [1, 2]:
                key = f"{version}-lag{lag}-cost{cost_multiple}"
                results, summary = {}, {}
                for name, fixed in [
                    (version, None),
                    ("0050", D(1)),
                    ("fixed1.2", D("1.2")),
                    ("fixed1.6", D("1.6")),
                    ("00631L", D(2)),
                ]:
                    engine = BacktestEngine(
                        initial_nav=INITIAL,
                        one_way_cost_rate=D("0.001925") * cost_multiple,
                        sell_tax_rate=D("0.001") * cost_multiple,
                        no_trade_band=D("Infinity") if name in {"0050", "00631L"} else D("0.15"),
                    )
                    rows = (
                        points
                        if fixed is None
                        else [replace(p, target_exposure=fixed) for p in points]
                    )
                    results[name] = engine.run(rows, OneXTwoXRecipe())
                    summary[name] = metrics(results[name], start)
                    if name in {"0050", "00631L"}:
                        prices = one if name == "0050" else two
                        expected = (
                            INITIAL
                            * (1 - D("0.001925") * cost_multiple)
                            * prices[end].adjusted_close
                            / prices[start].adjusted_close
                        )
                        error = abs(results[name].equity[-1].nav - expected)
                        if error > D("0.01"):
                            raise ValueError("buy-and-hold identity failed")
                        summary[name]["independent_buy_hold_check_twd_error"] = float(error)
                    summary[name]["year_returns"] = calendar_years(results[name])
                base = summary["0050"]
                for name, value in summary.items():
                    value["rolling_two_years"] = rolling_two_years(
                        results[name], results["0050"], start
                    )
                    value["numerical_q7"] = {
                        "return_beats_0050": value["total_return"] > base["total_return"],
                        "volatility_not_above_0050": value["annualized_volatility"]
                        <= base["annualized_volatility"],
                        "drawdown_not_worse_0050": value["maximum_drawdown"]
                        >= base["maximum_drawdown"],
                        "drawdown_within_40_percent": value["maximum_drawdown"] >= -0.4,
                    }
                    value["numerical_q7_all_pass"] = all(value["numerical_q7"].values())
                studies[key] = {
                    "start_close": str(start),
                    "end_close": str(end),
                    "capped_decision_days": capped,
                    "results": summary,
                }
                if lag == 45 and cost_multiple == 1:
                    main_results[version] = (start, results)
                    with (OUT / f"{version}-equity.csv").open(
                        "w", newline="", encoding="utf-8"
                    ) as f:
                        writer = csv.writer(f)
                        writer.writerow(["date", *results])
                        writer.writerow([start, *[INITIAL for _ in results]])
                        for idx, point in enumerate(results[version].equity):
                            writer.writerow(
                                [point.trading_date, *[r.equity[idx].nav for r in results.values()]]
                            )
                print(
                    key,
                    json.dumps(
                        {
                            n: {k: v[k] for k in ["final_nav", "maximum_drawdown", "calendar_cagr"]}
                            for n, v in summary.items()
                        }
                    ),
                    flush=True,
                )
    manifest = []
    for path in sorted(CACHE.iterdir()):
        used = path.name in {
            "valuations.json",
            "db-history.json",
            "official-dividend-probe-startDate.json",
        } or path.name.startswith(
            (
                "pe-",
                "taiex-",
                "vix-",
                "finmind-price-",
                "TaiwanStockDividend",
                "official-0050-",
                "official-00631L-",
                "official-dividend-20",
            )
        )
        if used and path.suffix in {".json", ".zip", ".txt"}:
            manifest.append(
                {
                    "file": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                    "local_file_timestamp_utc": datetime.fromtimestamp(
                        path.stat().st_mtime, UTC
                    ).isoformat(),
                }
            )
    protocol = ROOT / "docs/research/historical-study-protocol-20260918.md"
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "retrospective_not_oos",
        "protocol_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest(),
        "initial_nav": 500000,
        "audit": audit,
        "studies": studies,
        "source_manifest": manifest,
        "code_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                Path(__file__),
                ROOT / "src/dynamic_leverage/services/historical_study.py",
                ROOT / "src/dynamic_leverage/domain/backtest.py",
                ROOT / "src/dynamic_leverage/domain/strategies.py",
                ROOT / "src/dynamic_leverage/domain/indicators.py",
                ROOT / "src/dynamic_leverage/domain/execution.py",
            ]
        },
    }
    (OUT / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    render(report, main_results)


def render(report, main_results):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    for column, (version, (start, results)) in enumerate(main_results.items()):
        for name, result in results.items():
            dates = [start, *[p.trading_date for p in result.equity]]
            nav = [500000, *[float(p.nav) for p in result.equity]]
            peak = nav[0]
            dd = []
            for value in nav:
                peak = max(peak, value)
                dd.append(100 * (value / peak - 1))
            axes[0, column].plot(dates, [v / 500000 for v in nav], label=name, linewidth=1.2)
            axes[1, column].plot(dates, dd, label=name, linewidth=1.2)
        axes[0, column].set_title(f"{version} | retrospective, after assumed costs")
        axes[0, column].set_ylabel("Wealth multiple (initial = 1)")
        axes[1, column].set_ylabel("Drawdown from peak (%)")
        axes[0, column].legend(fontsize=8)
        for row in range(2):
            axes[row, column].grid(alpha=0.2)
            axes[row, column].tick_params(axis="x", labelrotation=20)
    fig.suptitle("Actual ETF closes + dividends + splits | retrospective research")
    fig.tight_layout()
    fig.savefig(OUT / "performance.png", dpi=160)
    plt.close(fig)
    lines = [
        "# 真實歷史回測結果（2026-09-18）",
        "",
        "使用真實歷史資料的事後研究；不是樣本外驗證，也不代表未來可獲利。初始資金 50 萬。",
        "結論：V1.1 與 V2 現金上限版均未通過既定 Q7 條件。",
        "這是策略配置規則的研究，尚未重播完整帳戶 40% RiskGate、人工核准與券商成交流程。",
        "主測：月估值延遲 45 天、買賣手續費各 0.1425%、滑價各 0.05%、賣出稅 0.1%。",
        "",
        "![實際歷史報酬與回撤曲線](performance.png)",
        "",
    ]
    for version in main_results:
        study = report["studies"][f"{version}-lag45-cost1"]
        lines += [
            f"## {version}：{study['start_close']} 收盤至 {study['end_close']} 收盤",
            "",
            "| 策略 | 期末資產 | 累積報酬 | 曆日年化報酬 | 年化波動 | 最大回撤 | 成交日數 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for name, r in study["results"].items():
            lines.append(
                f"| {name} | {r['final_nav']:,.0f} | {r['total_return']:.2%} | "
                f"{r['calendar_cagr']:.2%} | {r['annualized_volatility']:.2%} | "
                f"{r['maximum_drawdown']:.2%} | {r['trade_days_including_initial']} |"
            )
        candidate = study["results"][version]
        lines += [
            "",
            f"既定 Q7 四條件：`{candidate['numerical_q7']}`；全部通過："
            f"`{candidate['numerical_q7_all_pass']}`。",
            "",
            f"兩年滾動持有結果：`{candidate['rolling_two_years']}`。",
            f"最大回撤起迄與金額：`{candidate.get('maximum_drawdown_period')}`。",
            "",
            "### 分年報酬（第一年、最後一年可能不完整）",
            "",
            "| 年份 | 策略 | 0050 | 固定 1.2 倍 | 固定 1.6 倍 | 00631L |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for year in candidate["year_returns"]:
            lines.append(
                "| "
                + year
                + " | "
                + " | ".join(f"{r['year_returns'][year]:.2%}" for r in study["results"].values())
                + " |"
            )
        lines += [
            "",
            "### 延遲／成本敏感度",
            "",
            "| 月報假設延遲 | 成本倍數 | 期末資產 | 最大回撤 | Q7 全通過 |",
            "|---|---:|---:|---:|---|",
        ]
        for lag in [45, 60]:
            for cost in [1, 2]:
                r = report["studies"][f"{version}-lag{lag}-cost{cost}"]["results"][version]
                lines.append(
                    f"| {lag} 天 | {cost} | {r['final_nav']:,.0f} | "
                    f"{r['maximum_drawdown']:.2%} | {r['numerical_q7_all_pass']} |"
                )
        lines.append("")
    lines += [
        "## 解讀與限制",
        "",
        "- V1.1 是既有不使用 VIX 的策略，不能用它的長期成績宣稱 V2 有效。",
        "- V2 此次是明確限制 2 倍的研究變體；原 V2 可產生 3.2 倍，現金帳戶無法按該目標執行。",
        "- 收盤訊號隔一交易日收盤才成交；例如週一訊號週二收盤成交，賺取週二至週三報酬。",
        "- 沿用三日增加確認、每週最多增加 0.2 倍；每日檢查 0.15 倍再平衡門檻，"
        "並非每週人工交易的精確模擬。",
        "- ETF 使用證交所原始成交價及 FinMind 原始成交價補充；已取得的官方紀錄逐日交叉核對。"
        "總報酬由實際配息、分割與收盤價重建，未以 Yahoo 還原價格作為最終報酬來源。",
        "- 配息金額用 FinMind 兩種資料表逐筆核對，並抽查官方除息結果；"
        "假設除息日收盤立即再投入，未模擬到帳等待、再投入費用及個人稅務。",
        "- 以官方交易日排除資料商多出的休市日期；零成交量禁止調倉，"
        "停牌時依最後價格估值，無法觀察盤中損失。",
        "- 成本採固定假設、比例部位與可成交假設；未模擬整張／零股、成交量上限、"
        "漲跌停無量與逐筆滑價。期末沒有全數賣出。",
        "- 月報延遲 45／60 天不是已驗證公布時間；EPS 是指數除以本益比的推算值，"
        "且資料可能經事後修訂。",
        "- 本次未調參，但規則在歷史發生後制定，不能稱為樣本外；"
        "短期間年化僅為算術換算，不是未來預測。",
        "- 兩年滾動視窗彼此重疊，勝率不是未來機率；沿既有持倉路徑計算，"
        "未於每個視窗另計初始建倉成本。",
        "- 最大回撤包含初始成本，從資產歷史高點計算；持有期間報酬與高點損失是不同指標。",
        "- 本報告不含完整帳戶 RiskGate 回放；40% 門檻在系統中是只允許減碼的條件，"
        "不是觸及後保證以該價格清倉。本次績效不得當作完整系統執行結果。",
        "- 尚缺必要證據，維持 historical_research_ready=false、"
        "profitability_demonstrated=false。",
        "",
        "## 資料驗收",
        "",
        "```json",
        json.dumps(report["audit"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 可重現資料",
        "",
        "`results.json` 包含所有情境、來源檔案 SHA256 與驗收結果；"
        "兩份 signals.csv / equity.csv 可核對每日訊號與淨值。",
        "原始來源留在專案私有研究目錄；重跑：`python scripts/run_historical_study.py`。",
        "",
        "來源：[TWSE 月報](https://www.twse.com.tw/zh/trading/statistics/index04.html)、"
        "[TAIEX 歷史](https://www.twse.com.tw/zh/indices/taiex/mi-5mins-hist.html)、"
        "[TAIFEX VIX](https://www.taifex.com.tw/cht/7/vixDaily3MNew)、"
        "[FinMind 原始行情文件](https://finmind.github.io/tutor/TaiwanMarket/Technical/)、"
        "[TWSE 除息結果](https://www.twse.com.tw/exchangeReport/TWT49U?response=html)。",
        "",
        "0050 分割與停牌日期：[TWSE 公告](https://wwwc.twse.com.tw/zh/ETFortune/announcement?company=A00005&date=20250617&fund=0050&seq=1&type=all)。",
        "00631L 分割與停牌日期：[TWSE 公告](https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d69c37647f019c994679ba00cf.pdf)。",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
