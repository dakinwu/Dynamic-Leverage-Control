"""Pre-registered 0050 core + small 00631L satellite replay on frozen inputs only."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dynamic_leverage.domain.backtest import BacktestEngine, EquityPoint  # noqa: E402
from dynamic_leverage.domain.drawdown_budget import BudgetConfig, DrawdownBudgetPolicy  # noqa: E402
from dynamic_leverage.services.account_replay import AccountReplay, ReplayConfig  # noqa: E402
from dynamic_leverage.services.research_artifacts import (  # noqa: E402
    json_report,
    read_dataset,
    write_csv,
)

CORE = D("0.8")
LOW_VOL_TARGET = D("1.2")
HIGH_VOL_TARGET = D("0.8")
VOL_THRESHOLD = D("0.20")
CAPITAL_EXIT_TRIGGER = D("0.30")
RECOVERY_CONFIRMATION_DAYS = 5


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metrics(result, cfg):
    points = [
        EquityPoint(r["date"], r["nav"], r["daily_return"], r["actual_exposure"], D(0), r["cost"])
        for r in result.daily
    ]
    values = BacktestEngine(initial_nav=cfg.initial_nav)._metrics(points)
    years = (points[-1].trading_date - points[0].trading_date).days / 365.2425
    return {
        "final_nav": points[-1].nav,
        "calendar_cagr": float(points[-1].nav / cfg.initial_nav) ** (1 / years) - 1,
        "annualized_volatility": values.annualized_volatility,
        "maximum_drawdown": values.maximum_drawdown,
        "maximum_principal_loss": max(r["principal_loss"] for r in result.daily),
        "mean_actual_exposure": sum(r["actual_exposure"] for r in result.daily) / len(result.daily),
        "total_cost": sum(r["cost"] for r in result.daily),
        "fill_count": len(result.fills),
        "dates": {"start": str(points[0].trading_date), "end": str(points[-1].trading_date)},
    }


def cash_exit_reentry_rows(rows, trigger_date):
    if trigger_date is None:
        return rows, None
    in_cash = False
    recovery_streak = 0
    reentry_date = None
    scheduled = []
    for row in rows:
        if row.day < trigger_date:
            scheduled.append(row)
        elif row.day == trigger_date:
            in_cash = True
            scheduled.append(replace(row, target=D(0)))
        elif in_cash:
            recovery_streak = recovery_streak + 1 if row.market_recovery_ready is True else 0
            if recovery_streak >= RECOVERY_CONFIRMATION_DAYS:
                in_cash = False
                reentry_date = row.day
                scheduled.append(row)
            else:
                scheduled.append(replace(row, target=D(0)))
        else:
            scheduled.append(row)
    return scheduled, reentry_date


def apply_permanent_cash_exit(rows, trigger_date):
    if trigger_date is None:
        return rows
    return [replace(row, target=D(0)) if row.day >= trigger_date else row for row in rows]


def run(dataset: Path, output: Path):
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("new_out_of_sample") is not False:
        raise ValueError("this experiment accepts only explicitly frozen in-sample data")
    if output.exists():
        raise FileExistsError("choose a new output directory to preserve the research record")
    output.mkdir(parents=True)
    prior = json.loads(
        (ROOT / "docs/research/recovery-rearm-results-20260922/results.json").read_bytes()
    )
    replay_path = "src/dynamic_leverage/services/account_replay.py"
    reserve_path = "src/dynamic_leverage/domain/drawdown_budget.py"
    for name in (replay_path, reserve_path):
        if sha(ROOT / name) != prior["evidence_sha256"][name]:
            raise ValueError(f"frozen replay engine changed: {name}")
    report = {
        "schema": "core-satellite-vol20-v1",
        "new_out_of_sample": False,
        "production_configuration_changed": False,
        "profitability_demonstrated": False,
        "capital_limit_semantics": (
            "The 40% RiskGate is a risk-increase latch, not a guaranteed stop or liquidation. "
            "After a breach it blocks new exposure but can leave existing positions invested."
        ),
        "frozen_dataset_manifest_sha256": sha(manifest_path),
        "source_sha256": {
            str(Path(__file__).relative_to(ROOT)).replace("\\", "/"): sha(Path(__file__)),
            replay_path: sha(ROOT / replay_path),
            reserve_path: sha(ROOT / reserve_path),
        },
        "pre_registered_rule": {
            "signal": "provided point-in-time 20-day annualized TAIEX realized volatility",
            "decision_timing": (
                "signal at day D close; existing account replay executes no earlier than D+1"
            ),
            "below_threshold_target_exposure": LOW_VOL_TARGET,
            "at_or_above_threshold_target_exposure": HIGH_VOL_TARGET,
            "threshold": VOL_THRESHOLD,
            "recipe": {
                "at_0_8x": {
                    "0050_weight": D("0.8"),
                    "00631L_weight": D(0),
                    "cash_weight": D("0.2"),
                },
                "at_1_2x": {"0050_weight": CORE, "00631L_weight": D("0.2"), "cash_weight": D(0)},
            },
            "parameter_search": False,
        },
        "capital_guard_test": {
            "trigger": "maximum loss from initial principal reaches 30% at close",
            "action": (
                "target 100% cash from trigger close; "
                "signal executes no earlier than next trading day"
            ),
            "reentry": "none; this is a conservative containment test, not a complete strategy",
            "is_guaranteed_loss_cap": False,
        },
        "reentry_guard_test": {
            "cash_reentry_condition": (
                "index close above its trailing 60-session average for five consecutive sessions"
            ),
            "after_reentry": "resume the registered volatility target rule",
            "permanent_exit": "target cash from the first close with 40% principal loss",
            "signal_timing": "all triggers are decided at close and execute no earlier than D+1",
        },
        "groups": {},
    }
    for group, entry in manifest["datasets"].items():
        rows, spec = read_dataset(dataset / entry["file"], entry["sha256"])
        source_cfg = ReplayConfig(**spec["execution_config"])
        core_cfg = replace(
            source_cfg,
            mode="existing",
            recipe="1x-2x-v1",
            ceiling=D("1.2"),
        )
        rule_rows = []
        for row in rows:
            if row.policy_inputs is None or row.policy_inputs.as_of != row.day:
                raise ValueError(f"missing point-in-time volatility input on {row.day}")
            target = LOW_VOL_TARGET if row.policy_inputs.rv20 < VOL_THRESHOLD else HIGH_VOL_TARGET
            rule_rows.append(replace(row, target=target))
        preliminary = AccountReplay(core_cfg).run(rule_rows)
        trigger_row = next(
            (
                daily
                for daily in preliminary.daily
                if daily["principal_loss"] >= CAPITAL_EXIT_TRIGGER
            ),
            None,
        )
        cash_exit_date = trigger_row["date"] if trigger_row else None
        cash_exit_rows = [
            replace(row, target=D(0))
            if cash_exit_date is not None and row.day >= cash_exit_date
            else row
            for row in rule_rows
        ]
        reentry_rows, reentry_date = cash_exit_reentry_rows(rule_rows, cash_exit_date)
        reentry_preliminary = AccountReplay(core_cfg).run(reentry_rows)
        hard_exit_row = next(
            (daily for daily in reentry_preliminary.daily if daily["principal_loss"] >= D("0.4")),
            None,
        )
        hard_exit_date = hard_exit_row["date"] if hard_exit_row else None
        reentry_guarded_rows = apply_permanent_cash_exit(reentry_rows, hard_exit_date)
        controls = {
            "core-satellite-vol20": (core_cfg, rule_rows, None),
            "core-satellite-vol20-capital-exit-30": (core_cfg, cash_exit_rows, None),
            "core-satellite-vol20-exit30-reentry60d5": (
                core_cfg,
                reentry_guarded_rows,
                None,
            ),
            "static-0.8x-core-cash": (
                core_cfg,
                [replace(r, target=CORE) for r in rows],
                None,
            ),
            "static-1.0x-0050": (
                core_cfg,
                [replace(r, target=D(1)) for r in rows],
                None,
            ),
            "static-1.2x-core-satellite": (
                core_cfg,
                [replace(r, target=LOW_VOL_TARGET) for r in rows],
                None,
            ),
            "one-x-0050-buyhold": (replace(core_cfg, mode="buyhold", ceiling=D(1)), rows, None),
        }
        original_cfg = source_cfg
        policy = DrawdownBudgetPolicy(BudgetConfig(**spec["policy_config"]))
        controls["existing-rearm-control"] = (original_cfg, rows, policy)
        results = {}
        for name, (cfg, experiment_rows, active_policy) in controls.items():
            replay = AccountReplay(cfg).run(experiment_rows, reserve_policy=active_policy)
            stats = metrics(replay, cfg)
            stats["capital_loss_limit_breached"] = any(
                r["capital_limit_latched"] for r in replay.daily
            )
            stats["signal_target_counts"] = {
                str(target): sum(r.target == target for r in experiment_rows)
                for target in sorted({r.target for r in experiment_rows})
            }
            if name == "core-satellite-vol20-capital-exit-30":
                stats["capital_exit_trigger_date"] = cash_exit_date
                stats["capital_exit_triggered"] = cash_exit_date is not None
            if name == "core-satellite-vol20-exit30-reentry60d5":
                stats["capital_exit_trigger_date"] = cash_exit_date
                stats["market_reentry_date"] = reentry_date
                stats["permanent_40pct_exit_date"] = hard_exit_date
            write_csv(output / f"{group}-{name}-daily.csv", replay.daily)
            write_csv(output / f"{group}-{name}-fills.csv", replay.fills)
            results[name] = stats
        baseline = results["one-x-0050-buyhold"]
        for stats in results.values():
            stats["cagr_gap_vs_0050_pp"] = 100 * (
                stats["calendar_cagr"] - baseline["calendar_cagr"]
            )
            stats["volatility_gap_vs_0050_pp"] = 100 * (
                stats["annualized_volatility"] - baseline["annualized_volatility"]
            )
            stats["drawdown_magnitude_gap_vs_0050_pp"] = 100 * (
                abs(stats["maximum_drawdown"]) - abs(baseline["maximum_drawdown"])
            )
            stats["numerical_objective_pass"] = (
                stats["calendar_cagr"] > baseline["calendar_cagr"]
                and stats["annualized_volatility"] <= baseline["annualized_volatility"]
                and abs(stats["maximum_drawdown"]) <= abs(baseline["maximum_drawdown"])
                and stats["maximum_principal_loss"] < D("0.4")
                and abs(stats["maximum_drawdown"]) < D("0.4")
            )
        report["groups"][group] = {
            "dataset_sha256": entry["sha256"],
            "synthetic_crisis": group.startswith(("dotcom-", "gfc-")),
            "results": results,
        }
        print(
            group,
            {k: round(v["calendar_cagr"] * 100, 2) for k, v in results.items()},
            flush=True,
        )
    report["decision"] = {
        "all_scenarios_meet_original_numeric_objective": all(
            group["results"]["core-satellite-vol20"]["numerical_objective_pass"]
            for group in report["groups"].values()
        ),
        "all_actual_taiwan_scenarios_meet_original_numeric_objective": all(
            report["groups"][name]["results"]["core-satellite-vol20"]["numerical_objective_pass"]
            for name in ("real-etf-base", "real-etf-stress")
        ),
        "capital_exit_30_all_scenarios_principal_loss_below_40pct": all(
            group["results"]["core-satellite-vol20-capital-exit-30"]["maximum_principal_loss"]
            < D("0.4")
            for group in report["groups"].values()
        ),
        "capital_exit_30_all_scenarios_drawdown_below_40pct": all(
            abs(group["results"]["core-satellite-vol20-capital-exit-30"]["maximum_drawdown"])
            < D("0.4")
            for group in report["groups"].values()
        ),
        "reentry_guard_all_scenarios_principal_loss_below_40pct": all(
            group["results"]["core-satellite-vol20-exit30-reentry60d5"]["maximum_principal_loss"]
            < D("0.4")
            for group in report["groups"].values()
        ),
        "criteria": (
            "CAGR > paired 0050, volatility and max drawdown <= paired 0050; both loss and "
            "drawdown strictly <40% in every replayed scenario"
        ),
        "allocation_in_production": False,
    }
    (output / "results.json").write_text(
        json.dumps(report, default=json_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 0050 核心＋正二衛星部位：固定波動規則實驗",
        "",
        (
            "此規則只跑研究回放；沒有修改正式配置、連接券商或宣稱獲利。20% 門檻固定後再計算，"
            "未搜尋其他數值。2000／2008 情境使用合成正二路徑，標為壓力測試，"
            "不是當時真實上市 ETF 的成交紀錄。"
        ),
        "",
        (
            "規則：只使用當日收盤時已知的 20 日年化台股波動。低於 20% 時，"
            "下一交易日目標 1.2 倍（80% 0050＋20% 00631L）；等於或高於 20% 時，"
            "下一交易日目標 0.8 倍（80% 0050＋20% 現金）。所有組別沿用相同成本、"
            "成交延遲、流動性與 40% RiskGate。此 Gate 只阻止碰線後增加曝險，"
            "不保證清倉或將最大回撤限制在 40%。"
        ),
        "",
        (
            "結果以 `results.json` 為準。主要比較是正常、壓力兩組真實 ETF 資料；"
            "合成危機組用於額外淘汰檢查。門檻結果使用數學運算比較原值，表格才四捨五入。"
        ),
        "",
        (
            "| 情境 | 規則 | 年化 | 0050 年化 | 差距 | 波動差距 | "
            "最大回撤 | 本金虧損 | 觸發日 | 數值條件 |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for group, data in report["groups"].items():
        baseline_cagr = data["results"]["one-x-0050-buyhold"]["calendar_cagr"]
        for name, label in (
            ("core-satellite-vol20", "波動規則"),
            ("core-satellite-vol20-capital-exit-30", "30% 本金退出"),
            ("core-satellite-vol20-exit30-reentry60d5", "30% 退出＋60日回場"),
        ):
            candidate = data["results"][name]
            trigger_date = candidate.get("capital_exit_trigger_date") or "—"
            if "market_reentry_date" in candidate:
                trigger_date = (
                    f"30%:{trigger_date}; 回場:{candidate['market_reentry_date'] or '—'}; "
                    f"40%鎖定:{candidate['permanent_40pct_exit_date'] or '—'}"
                )
            lines.append(
                f"| {group} | {label} | {candidate['calendar_cagr']:.2%} | "
                f"{baseline_cagr:.2%} | {candidate['cagr_gap_vs_0050_pp']:+.2f} pp | "
                f"{candidate['volatility_gap_vs_0050_pp']:+.2f} pp | "
                f"{candidate['maximum_drawdown']:.2%} | "
                f"{float(candidate['maximum_principal_loss']):.2%} | "
                f"{trigger_date} | "
                f"{'通過' if candidate['numerical_objective_pass'] else '未通過'} |"
            )
    lines.extend(
        [
            "",
            (
                "40% Gate 代表停止增加曝險，不代表自動賣出；日線訊號及隔日成交，"
                "也無法保證最大損失不超過 40%。"
            ),
            (
                "30% 本金退出版在首次觸發當日收盤改發現金目標，隔日才開始賣出，"
                "而且不重新進場；它用來檢查風險緩衝，不是報酬候選或保證。"
            ),
            (
                "回場版本沿用專案既有的 60 日均線與連續 5 日確認條件；"
                "本金損失達 40% 時，下一交易日目標現金並永久鎖定。"
            ),
            "此為已使用過的歷史資料，不能稱為新樣本外驗證。策略只有全部條件通過才保留為研究候選；任何一項未通過即淘汰此規則，不再掃描門檻。實盤資格仍需真正向前累積的獨立資料。",
            "",
        ]
    )
    (output / "findings.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report["decision"], ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "data/private/rearm-dataset-20260928"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.dataset, args.output)
