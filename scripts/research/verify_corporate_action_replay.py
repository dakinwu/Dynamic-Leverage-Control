"""Replay the frozen real ETF candidate and benchmark after corporate action acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from dynamic_leverage.domain.drawdown_budget import BudgetConfig, DrawdownBudgetPolicy
from dynamic_leverage.services.account_replay import AccountReplay, ReplayConfig
from dynamic_leverage.services.research_artifacts import (
    json_report,
    read_dataset,
    write_csv,
)

ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(output: Path, audit_path: Path):
    audit = json.loads(audit_path.read_bytes())
    for name, provenance in audit["evidence"].items():
        if sha(ROOT / name.replace("\\", "/")) != provenance["sha256"]:
            raise ValueError(f"official distribution evidence changed: {name}")
    split_path = ROOT / "data/reference/corporate-actions-20260928/normalized-splits.json"
    splits = json.loads(split_path.read_bytes())["splits"]
    if len(splits) != 2 or {s["symbol"] for s in splits} != {"0050", "00631L"}:
        raise ValueError("complete split evidence required")
    split_sources = {}
    for event in splits:
        for name in event["source_evidence_filenames"]:
            path = split_path.parent / name
            provenance = json.loads(path.with_name(path.name + ".provenance.json").read_bytes())
            if sha(path) != provenance["sha256"]:
                raise ValueError("split source fingerprint mismatch")
            split_sources[str(path.relative_to(ROOT))] = provenance
    dataset = ROOT / "data/private/rearm-dataset-20260928"
    manifest_path = dataset / "manifest.json"
    if sha(manifest_path) != audit["frozen_manifest_sha256"]:
        raise ValueError("audited dataset manifest changed")
    manifest = json.loads(manifest_path.read_bytes())
    prior = ROOT / "docs/research/recovery-rearm-results-20260922"
    prior_results = json.loads((prior / "results.json").read_bytes())
    code_paths = [
        "src/dynamic_leverage/services/account_replay.py",
        "src/dynamic_leverage/domain/drawdown_budget.py",
    ]
    for name in code_paths:
        if sha(ROOT / name) != prior_results["evidence_sha256"][name]:
            raise ValueError(f"research engine changed: {name}")
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "audit_sha256": sha(audit_path),
        "groups": {},
        "frozen_inputs_changed": False,
        "new_out_of_sample": False,
        "profitability_demonstrated": False,
        "code_sha256": {name: sha(ROOT / name) for name in code_paths},
        "split_evidence_sha256": sha(split_path),
        "split_sources": split_sources,
    }
    for group in ("real-etf-base", "real-etf-stress"):
        entry = manifest["datasets"][group]
        if entry["sha256"] != audit["groups"][group]["dataset_sha256"]:
            raise ValueError("audited dataset changed")
        rows, spec = read_dataset(dataset / entry["file"], entry["sha256"])
        by_date = {r.day: r for r in rows}
        expected_splits = {
            (date.fromisoformat(s["resume_date"]), s["symbol"]): D(s["ratio"]) for s in splits
        }
        actual_splits = {(r.day, s): ratio for r in rows for s, ratio in r.splits.items()}
        if actual_splits != expected_splits:
            raise ValueError("split ratio or resume date differs from official evidence")
        expected_halts = {
            (date.fromisoformat(d), s["symbol"]) for s in splits for d in s["halt_dates"]
        }
        for event in splits:
            stock = event["symbol"]
            if by_date[date.fromisoformat(event["last_trade_date"])].quotes[stock].close != D(
                event["pre_split_close"]
            ):
                raise ValueError("pre-split close differs from official notice")
        actual_halts = set()
        for row in rows:
            for stock, quote in row.quotes.items():
                if quote.volume == 0 or not quote.buy_allowed or not quote.sell_allowed:
                    if quote.volume != 0 or quote.buy_allowed or quote.sell_allowed:
                        raise ValueError("split suspension must prohibit both buying and selling")
                    actual_halts.add((row.day, stock))
        if actual_halts != expected_halts:
            raise ValueError("suspension dates differ from official notices")
        cfg = ReplayConfig(**spec["execution_config"])
        candidate = AccountReplay(cfg).run(
            rows, reserve_policy=DrawdownBudgetPolicy(BudgetConfig(**spec["policy_config"]))
        )
        benchmark = AccountReplay(
            replace(cfg, mode="buyhold", recipe="1x-2x-v1", ceiling=D(1))
        ).run(rows)
        results = {}
        for label, replay in (("rearm", candidate), ("one-x-hold", benchmark)):
            if any((f["date"], f["symbol"]) in expected_halts for f in replay.fills):
                raise ValueError("execution during an official split suspension")
            traces = {}
            for suffix, records in (("daily", replay.daily), ("fills", replay.fills)):
                file = output / f"{group}-{label}-{suffix}.csv"
                write_csv(file, records)
                if sha(file) != sha(prior / file.name):
                    raise ValueError(f"replay changed: {file.name}")
                traces[file.name] = sha(file)
            dividends = [e for e in replay.events if e["kind"].startswith("dividend_")]
            # Every actual entitlement retains the pre-ex-date units and cash stays
            # unavailable until the official payment date (or next replay session).
            for index, row in enumerate(rows):
                for symbol, (amount, payment) in row.dividends.items():
                    quantity = replay.daily[index - 1][f"quantity_{symbol}"] if index else 0
                    expected = D(quantity) * amount
                    found = [
                        e
                        for e in dividends
                        if e["kind"] == "dividend_entitlement" and e["date"] == row.day
                    ]
                    if expected and (
                        len(found) != 1
                        or found[0]["amount"] != expected
                        or found[0]["payment_date"] != payment
                    ):
                        raise ValueError("dividend entitlement differs from audited event")
                    if not expected and found:
                        raise ValueError("dividend entitlement without eligible holdings")
            paid_total = D(0)
            for daily in replay.daily:
                paid_total += sum(
                    (
                        e["amount"]
                        for e in dividends
                        if e["kind"] == "dividend_paid" and e["date"] == daily["date"]
                    ),
                    D(0),
                )
                due_total = sum(
                    (
                        e["amount"]
                        for e in dividends
                        if e["kind"] == "dividend_entitlement"
                        and e["payment_date"] <= daily["date"]
                    ),
                    D(0),
                )
                if paid_total != due_total:
                    raise ValueError("dividend cash paid before/after audited payment session")
            nav = replay.daily[-1]["nav"]
            results[label] = {
                "final_nav": nav,
                "calendar_cagr": float(nav / cfg.initial_nav)
                ** (365.2425 / (rows[-1].day - rows[0].day).days)
                - 1,
                "identical_traces": traces,
                "dividend_entitlements": sum(
                    e["kind"] == "dividend_entitlement" for e in dividends
                ),
                "dividend_payment_schedule_passed": True,
            }
        results["cagr_gap_pp"] = 100 * (
            results["rearm"]["calendar_cagr"] - results["one-x-hold"]["calendar_cagr"]
        )
        results["split_events_matched"] = len(expected_splits)
        results["suspension_days_matched"] = len(expected_halts)
        report["groups"][group] = results
    with (output / "acceptance.json").open("x", encoding="utf-8") as f:
        json.dump(report, f, default=json_report, indent=2)
    print(json.dumps(report, default=json_report))


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--audit", type=Path, required=True)
    args = cli.parse_args()
    run(args.output, args.audit)
