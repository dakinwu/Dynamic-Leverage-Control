"""Publish a paired stage/annual comparison from verified, frozen account traces."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import date
from pathlib import Path

from dynamic_leverage.services.research_evidence import research_evidence

ROOT = Path(__file__).resolve().parents[2]



def main():
    out = ROOT / "docs/research/stage-comparison-results-20260928"
    out.mkdir(exist_ok=False)
    prior = ROOT / "docs/research/recovery-rearm-results-20260922"
    data = research_evidence("dvl-tw-v2-shadow")
    if data["evidence_status"] != "verified-archive":
        raise ValueError("unverified research archive")
    comparison = data["comparison"]
    hashes = {}
    for name in {row["strategy"] for row in comparison["stages"]}:
        for condition, prefix in [("normal", "real-etf-base"), ("stress", "real-etf-stress")]:
            path = prior / f"{prefix}-{name}-daily.csv"
            rows = list(csv.DictReader(path.open(encoding="utf-8")))
            nav = float(rows[-1]["nav"])
            days = (date.fromisoformat(rows[-1]["date"]) - date.fromisoformat(rows[0]["date"])).days
            cagr = (nav / 500000) ** (365.2425 / days) - 1
            expected = next(r for r in comparison["stages"] if r["strategy"] == name)[condition]
            if not math.isclose(nav, expected["final_nav"], abs_tol=0.00001):
                raise ValueError("final wealth does not reconcile")
            if not math.isclose(cagr, expected["cagr"], abs_tol=1e-12):
                raise ValueError("calendar CAGR does not reconcile")
            hashes[path.relative_to(ROOT).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    diagnostic = json.loads(
        (
            ROOT / "docs/research/budget-diagnostics-results-20260928-verified/results.json"
        ).read_bytes()
    )
    for name, digest in diagnostic["source_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"frozen policy/replay changed: {name}")
    comparison["evidence_sha256"] = hashes
    comparison["validated_daily_traces"] = len(hashes)
    comparison["policy_sources_unchanged"] = True
    (out / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 各優化階段與 0050 比較（2026-09-28）",
        "",
        "結論：本輪補資料日期與官方價量核對，沒有改動研究規則，因此沒有新增績效改善。"
        "修正反覆清倉後仍落後 0050。",
        "",
        f"共同期間：{comparison['period']}。起始 50 萬、含股息及既定成本；"
        "各組均對照相同成交／成本條件的 0050。",
        "日曆年化以實際經過天數換算；百分點是兩個百分比相減。"
        "下列不是實盤紀錄，也不是新樣本外。V2 日常版本沒有繼承此處績效。",
        "",
        "| 階段 | 正常年化 | 與0050差距 | 壓力年化 | 與0050差距 | 正常回撤 | 正常波動 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in comparison["stages"]:
        n, s = row["normal"], row["stress"]
        lines.append(
            f"| {row['label']} | {n['cagr']:.2%} | {n['cagr_gap_pp']:+.2f} pp | "
            f"{s['cagr']:.2%} | {s['cagr_gap_pp']:+.2f} pp | "
            f"{n['drawdown']:.2%} | {n['volatility']:.2%} |"
        )
    n, b = comparison["stages"][2]["normal"], comparison["stages"][-1]["normal"]
    lines += [
        "",
        f"50 萬在這段模擬期末：目前 rearm 約 {n['final_nav']:,.0f} 元，"
        f"0050 約 {b['final_nav']:,.0f} 元，差 {n['wealth_gap']:,.0f} 元。"
        "這是十年以上複利結果，不是預測兩年後資產。",
        "",
        "較弱保護的診斷組不列為採用成果。正常回撤略低也不代表所有條件都較安全；"
        "壓力情境的 rearm 回撤高於 0050，完整數值在 JSON。",
        "",
        "## 每年市場階段：目前 rearm 對照 0050",
        "",
        "這是同一持續帳戶的逐年切片，不在每年重設資金或高點。"
        "2016 從 4/1 開始，2026 到 9/17，均為不完整年度。",
        "",
        "| 年份 | rearm報酬 | 0050報酬 | 差距（百分點） |",
        "|---|---:|---:|---:|",
    ]
    for y in comparison["annual"]:
        suffix = "（部分年度）" if y["partial_year"] else ""
        lines.append(
            f"| {y['year']}{suffix} | {y['strategy_return']:.2%} | "
            f"{y['benchmark_return']:.2%} | {y['gap_pp']:+.2f} |"
        )
    lines += [
        "",
        "## 驗證與限制",
        "",
        "10 份逐日帳戶的期末資產及日曆年化重新計算一致；原策略、執行引擎與診斷程式指紋不變。"
        "資料品質補強不是新市場觀察；2000／2008 合成危機仍保留於原報告，不混入真實 ETF 表格。",
        "估值首次公布／修訂證據、配息支付日全面官方核對及完整成交可行性仍未完成。不因本次核對就改成獲利通過。",
        "",
        "重現：`C:/anaconda3/python.exe scripts/compare_research_stages.py`（輸出目錄須不存在）。",
    ]
    (out / "findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(comparison["stages"][2], ensure_ascii=False))


if __name__ == "__main__":
    main()
