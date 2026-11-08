"""Frozen-rule crisis replay and honest prospective OOS eligibility audit."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from decimal import Decimal as D

from collect_oos_crisis import CACHE, ROOT, parse_archive
from run_historical_study import CACHE as OLD_CACHE
from run_historical_study import load_data, roc_day
from run_risk_replay import json_ready, source_rows, summarize, write_trace
from run_valuation_reserve_study import CANDIDATES, START, base_config

from dynamic_leverage.domain.models import SafetyState
from dynamic_leverage.domain.reserve_policy import ReservePolicy
from dynamic_leverage.services.account_replay import AccountReplay, Quote, ReplayDay
from dynamic_leverage.services.crisis_research import crisis_inputs, synthetic_prices
from dynamic_leverage.services.historical_study import research_inputs

OUT = ROOT / "docs/research/oos-crisis-results-20260921"
AS_OF = date(2026, 9, 18)  # last completed session at this study's collection time
PERIODS = {
    "dotcom-full-price": (date(2000, 1, 4), date(2003, 12, 31), "taiex-price", False),
    "dotcom-valuation-price": (date(2000, 6, 1), date(2003, 12, 31), "taiex-price", True),
    "gfc-taiex-price": (date(2007, 1, 2), date(2010, 12, 31), "taiex-price", True),
    "gfc-tw50-total-return": (date(2007, 1, 2), date(2010, 12, 31), "tw50-total-return", True),
}


def read_sources():
    manifest = json.loads((CACHE / "manifest.json").read_text())
    closes, tw50, vals, hashes = {}, {}, {}, {}
    old = json.loads((OLD_CACHE / "valuations.json").read_text())
    for key, row in old.items():
        year, month = map(int, key.split("-"))
        vals[year * 12 + month] = D(row["pe"])
    hashes[(OLD_CACHE / "valuations.json").relative_to(ROOT).as_posix()] = hashlib.sha256(
        (OLD_CACHE / "valuations.json").read_bytes()
    ).hexdigest()
    for name, entry in manifest.items():
        if not entry["valid"]:
            continue
        path = CACHE / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise AssertionError(f"source checksum mismatch: {name}")
        hashes[path.relative_to(ROOT).as_posix()] = digest
        if name.startswith("pe-"):
            observed, pe, _ = parse_archive(path.read_bytes())
            if str(observed) != entry["as_of"] or pe != D(entry["pe"]):
                raise AssertionError("valuation reparse mismatch")
            key = observed.year * 12 + observed.month
            if key in vals and vals[key] != pe:
                raise AssertionError("conflicting official PE")
            vals[key] = pe
        elif name.startswith(("taiex-", "tw50-")):
            target, column = (tw50, 2) if name.startswith("tw50-") else (closes, 4)
            for row in json.loads(path.read_bytes())["data"]:
                day, value = roc_day(row[0]), D(row[column].replace(",", ""))
                if day > AS_OF or not value.is_finite() or value <= 0:
                    raise AssertionError("future session or invalid index")
                if day in target:
                    raise AssertionError("duplicate daily index")
                target[day] = value
    for y in range(1999, 2011):
        for m in range(1, 13):
            if not manifest.get(f"taiex-{y}{m:02}.json", {}).get("valid"):
                raise AssertionError("missing monthly crisis index")
    for y in range(2007, 2011):
        for m in range(1, 13):
            if not manifest.get(f"tw50-{y}{m:02}.json", {}).get("valid"):
                raise AssertionError("missing monthly Taiwan 50 index")
    market_days = {d for d in closes if 2007 <= d.year <= 2010}
    if market_days - set(tw50):
        raise AssertionError("Taiwan 50 missing an actual TAIEX trading session")
    extra_days = set(tw50) - market_days
    expected_extra = {date(2007, 9, 18), date(2008, 7, 28), date(2008, 9, 29), date(2009, 8, 7)}
    if extra_days != expected_extra:
        raise AssertionError("unreviewed Taiwan 50 publication calendar difference")
    return closes, tw50, vals, hashes, manifest


def regression():
    """Prove optional A-percentile validation changes none of the prior 18 results."""
    prior = json.loads(
        (ROOT / "docs/research/valuation-reserve-results-20260918/results.json").read_text()
    )
    for path, digest in prior["evidence_sha256"].items():
        if (
            path.startswith("data/private/")
            and hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest
        ):
            raise AssertionError("old raw evidence changed")
    _, _, closes, vals, vix, audit = load_data()
    end = date.fromisoformat(audit["price_end"])
    matched = 0
    for stress in [False, True]:
        inputs = research_inputs(closes, vals, vix, publication_lag_days=60 if stress else 45)
        rows = [
            replace(r, policy_inputs=inputs[r.day])
            for r in source_rows(closes, inputs, "v11", START, end)
        ]
        for name, old in prior["scenarios"]["stress" if stress else "base"]["results"].items():
            policy, selected, cfg = None, rows, base_config(stress)
            if name in CANDIDATES:
                policy = ReservePolicy(CANDIDATES[name])
            elif name.startswith("old-v11"):
                cfg = base_config(stress, ceiling=D(name.rsplit("-", 1)[1]))
            else:
                cap = D("1.2") if name == "fixed-60-40" else D(1) if name == "0050" else D(2)
                selected = [replace(r, target=cap, strategy_state=SafetyState.NORMAL) for r in rows]
                cfg = (
                    base_config(stress, ceiling=cap)
                    if name == "fixed-60-40"
                    else base_config(stress, mode="buyhold", recipe="1x-2x-v1", ceiling=cap)
                )
            metrics = summarize(
                AccountReplay(cfg).run(selected, reserve_policy=policy), START, cfg.initial_nav
            )
            for key in [
                "final_nav",
                "calendar_cagr",
                "maximum_drawdown",
                "total_cost",
                "fill_count",
            ]:
                if abs(metrics[key] - D(str(old[key]))) > D(".000001"):
                    raise AssertionError(f"old regression differs: {name}/{key}")
            matched += 1
    return matched


def oos_audit(manifest):
    prior = json.loads(
        (ROOT / "docs/research/valuation-reserve-results-20260918/results.json").read_text()
    )
    freeze = datetime.fromisoformat(prior["created_at"])
    days = {roc_day(r[0]) for r in json.loads((CACHE / "taiex-202609.json").read_bytes())["data"]}
    for symbol in ["0050", "00631L"]:
        days &= {
            roc_day(r[0])
            for r in json.loads((CACHE / f"official-{symbol}-202609.json").read_bytes())["data"]
        }
    # Taipei close 13:30 == UTC 05:30; a close preceding rule freeze is not prospective.
    prospective = sorted(
        d for d in days if datetime(d.year, d.month, d.day, 5, 30, tzinfo=UTC) > freeze
    )
    return {
        "rule_freeze_evidence_at": freeze.isoformat(),
        "old_explored_end": "2026-09-17",
        "latest_joint_complete_close": str(max(days)),
        "prospective_complete_days": len(prospective),
        "prospective_dates": prospective,
        "annualized_performance": None,
        "profitability_demonstrated": False,
        "status": "insufficient-new-observations",
        "reason": "9/18 close preceded freeze; 9/21 session was not complete at collection.",
        "new_before_freeze_dates": sorted(
            d for d in days if d > date(2026, 9, 17) and d not in prospective
        ),
    }


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    closes, tw50, vals, hashes, manifest = read_sources()
    matched = regression()
    print("OLD_SCENARIOS_REPRODUCED", matched, flush=True)
    groups, plots, verified_rows, verified_fills = {}, {}, 0, 0
    for label, (start, end, basis, candidate_allowed) in PERIODS.items():
        dates = sorted(d for d in closes if start <= d <= end)
        underlying = tw50 if basis == "tw50-total-return" else closes
        for funding in [D(0), D(".03"), D(".06")]:
            prices = synthetic_prices(
                {d: underlying[d] for d in dates}, annual_drag=D(".0104") + funding
            )
            for stress in [False, True]:
                group = f"{label}-fund{funding:.2f}-{'stress' if stress else 'base'}"
                inputs = crisis_inputs(
                    {d: p for d, p in closes.items() if d <= end},
                    vals,
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
                        policy_inputs=inputs[d],
                    )
                    for d in dates
                ]
                cases = [
                    (
                        "synthetic-1x",
                        base_config(stress, mode="buyhold", recipe="1x-2x-v1", ceiling=D(1)),
                        D(1),
                        None,
                    ),
                    (
                        "synthetic-2x",
                        base_config(stress, mode="buyhold", recipe="1x-2x-v1", ceiling=D(2)),
                        D(2),
                        None,
                    ),
                    ("fixed-60-40", base_config(stress, ceiling=D("1.2")), D("1.2"), None),
                ]
                unavailable = {}
                for name, config in CANDIDATES.items():
                    fields = ["pe_ttm", "eps_yoy", "rv20", "taiex_daily_return"]
                    if config.valuation == "percentile":
                        fields.append("pe_percentile")
                    missing = [
                        str(d) for d in dates if any(getattr(inputs[d], f) is None for f in fields)
                    ]
                    if not candidate_allowed or missing:
                        unavailable[name] = {
                            "reason": "full-price-control-period"
                            if not candidate_allowed
                            else "missing-required-inputs",
                            "missing_days": len(missing),
                            "first_missing": missing[:5],
                            "required": fields,
                        }
                    else:
                        cases.append((name, base_config(stress), D("1.2"), ReservePolicy(config)))
                metrics, paths = {}, {}
                for name, cfg, target, policy in cases:
                    selected = [replace(r, target=target) for r in rows]
                    replay = AccountReplay(cfg).run(selected, reserve_policy=policy)
                    m = summarize(replay, dates[0], cfg.initial_nav)
                    m["config"], m["reserve_config"] = (
                        asdict(cfg),
                        asdict(policy.config) if policy else None,
                    )
                    m["mean_cash_fraction"] = sum(r["cash"] / r["nav"] for r in replay.daily) / len(
                        rows
                    )
                    m["terminal_hard_lock"] = replay.daily[-1]["capital_limit_latched"]
                    m["trough"] = min(replay.daily, key=lambda r: r["nav"])
                    m["worst_drawdown_day"] = min(replay.daily, key=lambda r: -r["drawdown"])
                    m["paused_days"] = sum(
                        e["kind"] == "reserve_decision" and e["paused"] for e in replay.events
                    )
                    m["soft_recoveries"] = sum(
                        e["kind"] == "reserve_decision"
                        and "SOFT_RECOVERY_CONFIRMED" in e["reasons"]
                        for e in replay.events
                    )
                    m["ending_below_initial"] = replay.daily[-1]["nav"] < cfg.initial_nav
                    for r in replay.daily:
                        if r["cash"] < 0 or r["nav"] <= 0:
                            raise AssertionError("negative account assets")
                    for i, r in enumerate(replay.daily):
                        nav = (
                            r["cash"]
                            + r["receivables"]
                            + r["quantity_0050"] * selected[i].quotes["0050"].close
                            + r["quantity_00631L"] * selected[i].quotes["00631L"].close
                        )
                        if abs(nav - r["nav"]) > D(".000001"):
                            raise AssertionError("account conservation failure")
                    for fill in replay.fills:
                        if fill["signal_date"] >= fill["date"]:
                            raise AssertionError("same-day or future signal fill")
                    if policy:
                        latched_before = {
                            replay.daily[i]["date"]: replay.daily[i - 1]["capital_limit_latched"]
                            for i in range(1, len(replay.daily))
                        }
                        if any(
                            f["side"] == "BUY" and latched_before.get(f["date"], False)
                            for f in replay.fills
                        ):
                            raise AssertionError("buy after prior hard lock")
                        for event in replay.events:
                            if event["kind"] == "reserve_buy_fill" and (
                                event["budget_remaining"] < 0
                                or event["cash_after"] < event["cash_floor"]
                            ):
                                raise AssertionError("reserve spending breach")
                    verified_rows += len(replay.daily)
                    verified_fills += len(replay.fills)
                    write_trace(group + "-" + name, replay, output_dir=OUT)
                    metrics[name], paths[name] = m, replay.daily
                groups[group] = {
                    "start": str(dates[0]),
                    "end": str(dates[-1]),
                    "basis": basis,
                    "funding_assumption": funding,
                    "stress": stress,
                    "results": metrics,
                    "unavailable": unavailable,
                }
                if funding == D(".03") and not stress:
                    plots[label] = paths
                    print(
                        group,
                        {
                            n: (
                                round(float(m["total_return"]) * 100, 2),
                                round(float(m["maximum_drawdown"]) * 100, 2),
                            )
                            for n, m in metrics.items()
                        },
                        flush=True,
                    )
    files = [
        ROOT / "docs/research/oos-crisis-protocol-20260921.md",
        ROOT / "docs/research/oos-crisis-sources-20260921.md",
        ROOT / "scripts/collect_oos_crisis.py",
        ROOT / "scripts/run_oos_crisis_study.py",
        ROOT / "src/dynamic_leverage/services/crisis_research.py",
        ROOT / "src/dynamic_leverage/domain/reserve_policy.py",
        ROOT / "src/dynamic_leverage/services/account_replay.py",
        CACHE / "manifest.json",
    ]
    files += [
        ROOT / p
        for p in [
            "docs/research/valuation-reserve-results-20260918/results.json",
            "scripts/run_valuation_reserve_study.py",
            "scripts/run_risk_replay.py",
            "scripts/run_historical_study.py",
            "scripts/run_cash_reserve_study.py",
            "src/dynamic_leverage/services/historical_study.py",
            "src/dynamic_leverage/domain/risk.py",
            "src/dynamic_leverage/domain/ledger.py",
            "src/dynamic_leverage/domain/execution.py",
            "src/dynamic_leverage/domain/backtest.py",
            "src/dynamic_leverage/domain/models.py",
            "src/dynamic_leverage/domain/indicators.py",
            "src/dynamic_leverage/infrastructure/providers/official_valuation.py",
            "tests/unit/test_crisis_research.py",
        ]
    ]
    hashes.update(
        {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    )
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "prospective_oos": oos_audit(manifest),
        "legacy_scenarios_reproduced": matched,
        "groups": groups,
        "evidence_sha256": hashes,
        "calendar_alignment": {
            "execution_calendar": "official TAIEX actual sessions",
            "tw50_extra_publication_dates": [
                "2007-09-18",
                "2008-07-28",
                "2008-09-29",
                "2009-08-07",
            ],
            "non_session_total_return_changes_included_in_next_session": True,
        },
        "validation": {
            "account_rows_checked": verified_rows,
            "fills_checked": verified_fills,
            "no_future_prices": True,
            "no_fabricated_valuation_inputs": True,
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
    for ax, (label, paths) in zip(axes.flat, plots.items(), strict=True):
        for name in ["synthetic-1x", "synthetic-2x", "fixed-60-40", "A-absolute-staged"]:
            if name in paths:
                rows = paths[name]
                ax.plot(
                    [r["date"] for r in rows], [float(r["nav"]) / 500000 for r in rows], label=name
                )
        ax.set_title(label)
        ax.axhline(0.6, color="red", linestyle="--", alpha=0.5, label="60% initial capital")
        ax.set_ylabel("Wealth / initial 500,000")
        ax.legend(fontsize=7)
        ax.grid(alpha=0.2)
    fig.suptitle("Historical synthetic stress | assumed funding 3% | not actual ETF performance")
    fig.tight_layout()
    fig.savefig(OUT / "comparison.png", dpi=160)
    plt.close(fig)
    lines = [
        "# 固定規則危機測試與樣本外驗收",
        "",
        "結論：主候選在兩次危機均突破 40% 最大回撤，未達使用者風險條件。"
        "不能以本輪結果支持實盤部署或宣稱已證明獲利。",
        "",
        "資料驗收日：2026-09-21 上午。",
        "真正向前樣本外完整交易日：**0**；9/18 已在規則凍結前收盤，9/21 尚未收盤。",
        "以下是已知危機的回溯壓力驗證，不是實際 ETF 上市前績效或獲利保證。",
        "",
        "## 同期間比較",
        "",
        "下表為融資成本假設 3%、正常交易成本；不是各年實際融資利率。",
        "TAIEX-price 不含股息；TW50-total-return 含指數股息再投入。所有組別以 50 萬現金起始。",
        "",
    ]
    for label in PERIODS:
        group = report["groups"][label + "-fund0.03-base"]
        lines += [
            f"### {label}：{group['start']}～{group['end']}",
            "",
            "| 規則 | 累計報酬 | 年化 | 最大回撤 | 最後資產 | 首次40%界線 |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for name, m in group["results"].items():
            lines.append(
                f"| {name} | {m['total_return']:.2%} | {m['calendar_cagr']:.2%} "
                f"| {m['maximum_drawdown']:.2%} | {m['final_nav']:,.0f} "
                f"| {m['first_40_percent_breach'] or '未觸發'} |"
            )
        lines += [
            "",
            "未執行："
            + "; ".join(
                f"{n}: {v['reason']} ({v['missing_days']}日)"
                for n, v in group["unavailable"].items()
            ),
            "",
        ]
    lines += [
        "## 假設敏感度",
        "",
        "同時報全部 0%／3%／6% 融資假設，以及正常／雙倍交易成本與兩日延遲；不挑績效最好一組。",
        "",
        "| 期間 | A 累計報酬範圍 | A 最大回撤範圍 |",
        "|---|---:|---:|",
    ]
    for label in PERIODS:
        metrics = [
            g["results"]["A-absolute-staged"]
            for k, g in report["groups"].items()
            if k.startswith(label + "-") and "A-absolute-staged" in g["results"]
        ]
        if metrics:
            lines.append(
                f"| {label} | {min(m['total_return'] for m in metrics):.2%}～"
                f"{max(m['total_return'] for m in metrics):.2%} | "
                f"{min(m['maximum_drawdown'] for m in metrics):.2%}～"
                f"{max(m['maximum_drawdown'] for m in metrics):.2%} |"
            )
    lines += [
        "",
        "## 如何解讀",
        "",
        "- 2000 可用期間主候選：50 萬最低剩約 24.97 萬，期末約 31.38 萬；"
        "2008 臺灣 50 含息版本最低约 32.34 萬，期末約 46.85 萬。"
        "2008 回撤比本金損失大，是因先前已上漲；回撤以帳戶歷史高點計算。",
        "- 40% 是阻止繼續加碼的界線，不是帳戶保本或全部清倉指令。"
        "鎖住之後仍有持股，繼續下跌仍會虧更多。",
        "- 2000 策略從 6 月才有完整輸入，不能把避開年初的部分跌幅當成估值預測能力；"
        "另列完整年度價格對照。",
        "- 2000 舊報表 PE 採當年會計口徑，部分公司使用稅前純益；"
        "EPS 是指數除 PE 的代理，並非原始公告盈利。",
        "- 主候選兩段期間都落後同期間合成 1 倍持有；保留現金雖減少下跌，"
        "也壓低反彈參與。2000 可用期間平均現金約 73.94%，2008 含息版本約 58.57%。"
        "移除軟性防守的最大回撤更大，並未解決使用者風險條件。",
        "- B 的前十年 PE 不足，整段不執行；不拿 2009 年之後才足夠的歷史補回 2007 年決策。",
        "- 合成 2 倍逐日重設、扣假設費用，但未重建實際期貨基差、利率、漲跌停與停牌；"
        "流動性假設不構成可成交證明。",
        "- TAIEX-price 與 TW50-total-return 的差異同時包含成分股與股息，"
        "不能將兩者差距全部歸因於股息。",
        "",
        "## 重跑與驗收",
        "",
        "```powershell",
        "$env:PYTHONPATH='src;data/private/research-deps'",
        "C:\\anaconda3\\python.exe scripts/collect_oos_crisis.py",
        "C:\\anaconda3\\python.exe scripts/collect_oos_crisis.py --old-valuations",
        "C:\\anaconda3\\python.exe scripts/run_oos_crisis_study.py",
        "```",
        "",
        f"既有 18 組重現：{report['legacy_scenarios_reproduced']}；"
        f"逐日資產守恆：{report['validation']['account_rows_checked']} 列；"
        f"隔日成交檢查：{report['validation']['fills_checked']} 筆。",
        "原始資料與完整逐日／成交／事件紀錄、設定及來源雜湊均保留。"
        "詳細來源見 ../oos-crisis-sources-20260921.md。",
        "",
        "![歷史壓力比較](comparison.png)",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
