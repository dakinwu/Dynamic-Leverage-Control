"""Compare frozen stages with their paired 0050 account; never grants eligibility."""

from __future__ import annotations


def compare_stages(results: dict) -> dict:
    stages = []
    for key, label in [
        ("legacy", "原回撤預算規則"),
        ("rearm", "修正反覆清倉"),
        ("rearm", "本輪資料與工程優化（規則未改）"),
        ("rearm-fast", "加快買回：未採用"),
        ("rearm-both-diagnostic", "減弱成交保護：僅診斷"),
        ("one-x-hold", "0050 持有比較組"),
    ]:
        row = {"id": f"stage-{len(stages)}", "strategy": key, "label": label}
        for condition, group in [("normal", "real-etf-base"), ("stress", "real-etf-stress")]:
            values = results["groups"][group]["results"]
            candidate, benchmark = values[key], values["one-x-hold"]
            row[condition] = {
                "cagr": candidate["calendar_cagr"],
                "cagr_gap_pp": 100 * (candidate["calendar_cagr"] - benchmark["calendar_cagr"]),
                "drawdown": abs(candidate["maximum_drawdown"]),
                "drawdown_gap_pp": 100
                * (abs(candidate["maximum_drawdown"]) - abs(benchmark["maximum_drawdown"])),
                "volatility": candidate["annualized_volatility"],
                "volatility_gap_pp": 100
                * (candidate["annualized_volatility"] - benchmark["annualized_volatility"]),
                "final_nav": candidate["final_nav"],
                "wealth_gap": candidate["final_nav"] - benchmark["final_nav"],
            }
        stages.append(row)
    normal = results["groups"]["real-etf-base"]
    a, b = normal["results"]["rearm"], normal["results"]["one-x-hold"]
    years = [
        {
            "year": y,
            "strategy_return": v,
            "benchmark_return": b["year_returns"][y],
            "gap_pp": 100 * (v - b["year_returns"][y]),
            "partial_year": y in {"2016", "2026"},
        }
        for y, v in a["year_returns"].items()
    ]
    return {
        "period": f"{normal['start']} / {normal['end']}",
        "new_out_of_sample": False,
        "current_stage_changes_policy": False,
        "runtime_strategy_not_backtested_here": "dvl-tw-v2-shadow",
        "stages": stages,
        "annual": years,
        "gap_definition": "strategy minus paired 0050; drawdown/volatility gaps >0 mean more risk",
    }
