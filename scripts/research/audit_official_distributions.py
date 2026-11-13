"""Archive TWSE distribution evidence and reconcile immutable real-ETF replay inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from dynamic_leverage.infrastructure.providers.official_distributions import (
    parse_distributions,
)
from dynamic_leverage.services.historical_study import month_end
from dynamic_leverage.services.research_artifacts import read_dataset

ROOT = Path(__file__).resolve().parents[2]



def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path, value):
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--evidence", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--offline", action="store_true")
    args = cli.parse_args()
    if args.output.exists():
        raise FileExistsError("select a new audit output; prior results are immutable")
    args.evidence.mkdir(parents=True, exist_ok=True)
    evidence, official = {}, {}
    with httpx.Client(timeout=45, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for stock in ("0050", "00631L"):
            url = (
                "https://www.twse.com.tw/en/ETFortune-institute/dividendList"
                f"?startDate=2016&endDate=2026&stkNo={stock}"
            )
            path = args.evidence / f"twse-distributions-{stock}.html"
            meta = path.with_suffix(".provenance.json")
            if not path.exists():
                if args.offline:
                    raise FileNotFoundError(path)
                time.sleep(1.5)
                response = client.get(url)
                response.raise_for_status()
                parse_distributions(response.content, str(response.url), stock, 2016, 2026)
                with path.open("xb") as f:
                    f.write(response.content)
                write_new(
                    meta,
                    {
                        "source": str(response.url),
                        "received_at": datetime.now(UTC).isoformat(),
                        "sha256": digest(path),
                        "published_at": None,
                        "historical_first_publication_proven": False,
                    },
                )
            provenance = json.loads(meta.read_bytes())
            if provenance["source"] != url or provenance["sha256"] != digest(path):
                raise ValueError("official evidence fingerprint/source mismatch")
            official[stock] = parse_distributions(path.read_bytes(), url, stock, 2016, 2026)
            evidence[path.relative_to(ROOT).as_posix()] = provenance

    dataset = ROOT / "data/private/rearm-dataset-20260928"
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    valuations_path = ROOT / "data/private/historical-research-20260918/valuations.json"
    expected_hash = manifest["source_evidence"][
        str(valuations_path.relative_to(ROOT)).replace("\\", "/")
    ]
    if digest(valuations_path) != expected_hash:
        raise ValueError("valuation source changed")
    valuations = json.loads(valuations_path.read_bytes())
    report = {
        "schema": "official-corporate-actions-audit-v1",
        "evidence": evidence,
        "frozen_manifest_sha256": digest(manifest_path),
        "valuation_source_sha256": expected_hash,
        "groups": {},
        "official_distributions": {
            s: [asdict(r) for _, r in sorted(rows.items())] for s, rows in official.items()
        },
        "frozen_inputs_changed": False,
        "historical_research_ready": False,
        "new_out_of_sample": False,
    }
    for group, lag in (("real-etf-base", 45), ("real-etf-stress", 60)):
        entry = manifest["datasets"][group]
        rows, _ = read_dataset(dataset / entry["file"], entry["sha256"])
        actual = {
            (row.day, stock): (amount, payment)
            for row in rows
            for stock, (amount, payment) in row.dividends.items()
        }
        expected = {
            (day, stock): (event.amount, event.payment_date)
            for stock, events in official.items()
            for day, event in events.items()
            if rows[0].day <= day <= rows[-1].day
        }
        if actual != expected:
            raise ValueError(f"{group}: dividend amount/ex-date/payment-date differs")
        inventory = {}
        for row in rows:
            ordinal = row.day.year * 12 + row.day.month - 1
            while month_end(ordinal) + timedelta(days=lag) > row.day:
                ordinal -= 1
            key = str(month_end(ordinal))[:7]
            if Decimal(valuations[key]["pe"]) != row.policy_inputs.pe_ttm:
                raise ValueError(f"{group}: valuation lag reconstruction differs on {row.day}")
            for used in range(ordinal - 120, ordinal + 1):
                used_key = str(month_end(used))[:7]
                if used_key not in inventory:
                    value = valuations[used_key]
                    inventory[used_key] = {
                        "first_decision_using_month": str(row.day),
                        "role_at_first_use": "current" if used == ordinal else "percentile_history",
                        "market_observation_date": value["as_of"],
                        "source": value["source"],
                        "first_published_at": None,
                        "revision_history_proven": False,
                        "availability_status": "UNKNOWN",
                        "reason": (
                            "source received_at/observed_at is not historical first publication"
                        ),
                    }
        report["groups"][group] = {
            "start": str(rows[0].day),
            "end": str(rows[-1].day),
            "dataset_sha256": entry["sha256"],
            "matched_distributions": len(actual),
            "dividend_differences": [],
            "assumed_publication_lag_days": lag,
            "valuation_decisions_reconciled": len(rows),
            "valuation_month_count_including_120_month_lookback": len(inventory),
            "valuation_publication_inventory": dict(sorted(inventory.items())),
        }
    write_new(args.output, report)
    print(
        json.dumps(
            {
                k: {
                    "matched_distributions": v["matched_distributions"],
                    "valuation_months": len(v["valuation_publication_inventory"]),
                }
                for k, v in report["groups"].items()
            }
        )
    )


if __name__ == "__main__":
    main()
