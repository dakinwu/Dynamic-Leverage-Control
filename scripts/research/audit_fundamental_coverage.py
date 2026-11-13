"""Show exact remaining company/period coverage; never renormalize a partial pool."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path

from dynamic_leverage.services.comparable_fundamentals import (
    parse_mops_monthly_revenue,
)
from dynamic_leverage.services.fundamental_signal import (
    SourceObservation,
    parse_revenue,
)

ROOT = Path(__file__).resolve().parents[2]


def run(output: Path):
    if output.exists():
        raise FileExistsError("use a new coverage output")
    proxy = ROOT / "data/private/0050-holdings-proxy-20261005"
    manifest = json.loads((proxy / "manifest.json").read_bytes())
    body = (proxy / "holdings.json").read_bytes()
    if hashlib.sha256(body).hexdigest() != manifest["files"]["holdings.json"]["sha256"]:
        raise ValueError("proxy holdings changed")
    members = json.loads(body)
    capture = ROOT / "data/private/fundamental-evidence-20261005-v2"
    original = json.loads((capture / "observations.json").read_bytes())
    revenues, financials = {}, {}
    for entry in original["observations"]:
        path = (capture / entry["raw_file"]).resolve()
        if not path.is_relative_to(capture.resolve()):
            raise ValueError("raw path outside capture")
        source = SourceObservation(
            entry["source_id"],
            entry["source_url"],
            path.read_bytes(),
            entry["body_sha256"],
            datetime.fromisoformat(entry["received_at"]),
            datetime.fromisoformat(entry["recorded_at"]),
        )
        if entry["source_url"].endswith("t187ap05_L"):
            revenues = {
                value.company: value
                for value in parse_revenue(source)
                if value.period_end.isoformat() == "2026-08-31"
            }
        else:
            for row in json.loads(source.body):
                if row["年度"] == "115" and row["季別"] == "2":
                    if row["公司代號"] in financials:
                        raise ValueError("ambiguous company statement kind")
                    financials[row["公司代號"]] = entry["source_url"].split("_")[-1]
    prior_revenues = {}
    prior_folder = ROOT / "data/private/fundamental-comparable-20261005"
    from audit_comparable_fundamentals import load_source

    prior_manifest = json.loads((prior_folder / "observations.json").read_bytes())
    for entry in prior_manifest["observations"]:
        if (
            entry["source_id"].startswith("mopsov")
            and entry["status"] == "captured-awaiting-validation"
        ):
            for value in parse_mops_monthly_revenue(
                load_source(prior_folder, entry), date(2026, 7, 31)
            ):
                if value.company in prior_revenues:
                    raise ValueError("duplicate domestic/foreign company revenue")
                prior_revenues[value.company] = value
    comparable = ROOT / "docs/research/fundamental-comparable-acceptance-20261005-verified.json"
    accepted = json.loads(comparable.read_bytes())
    if (
        not accepted["acceptance_passed"]
        or accepted["diagnostic"]["status"] != "company_diagnostic_only"
    ):
        raise ValueError("verified partial company evidence required")
    complete = {accepted["diagnostic"]["company"]}
    records = [
        {
            **member,
            "august_yoy_normalized": member["company_code"] in revenues,
            "july_yoy_normalized": member["company_code"] in prior_revenues,
            "revenue_acceleration": str(
                revenues[member["company_code"]].value
                - prior_revenues[member["company_code"]].value
            )
            if member["company_code"] in revenues and member["company_code"] in prior_revenues
            else None,
            "revenue_pair_economic_semantics_verified": member["company_code"] in complete,
            "reported_115q2_statement_kind": financials.get(member["company_code"]),
            "comparable_company_inputs_verified": member["company_code"] in complete,
            "remaining_blocker": None
            if member["company_code"] in complete
            else "comparable-quarter-and-accounting-revision-validation-required",
        }
        for member in members
    ]
    result = {
        "schema": "fundamental-proxy-coverage-acceptance-v1",
        "acceptance_passed": len(records) == 50
        and len({row["company_code"] for row in records}) == 50,
        "universe_kind": manifest["universe_kind"],
        "universe_as_of": manifest["as_of"],
        "proxy_weight_sum_percent": manifest["published_weight_sum_percent"],
        "current_month_yoy_company_count": sum(row["august_yoy_normalized"] for row in records),
        "paired_month_yoy_company_count": sum(
            row["august_yoy_normalized"] and row["july_yoy_normalized"] for row in records
        ),
        "prior_month_normalized_row_count": len(prior_revenues),
        "current_reported_financial_company_count": sum(
            row["reported_115q2_statement_kind"] is not None for row in records
        ),
        "comparable_input_company_count": sum(
            row["comparable_company_inputs_verified"] for row in records
        ),
        "comparable_input_proxy_nav_weight_percent": str(
            sum(
                (
                    D(row["weight_percent"])
                    for row in records
                    if row["comparable_company_inputs_verified"]
                ),
                D(0),
            )
        ),
        "rows": records,
        "comparable_acceptance_sha256": hashlib.sha256(comparable.read_bytes()).hexdigest(),
        "aggregate_signal_ready": False,
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "weights_normalized": False,
        "paired_month_source_manifest_sha256": hashlib.sha256(
            (prior_folder / "observations.json").read_bytes()
        ).hexdigest(),
        "source_sha256": {
            file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest()
            for file in (
                "src/dynamic_leverage/services/comparable_fundamentals.py",
                "scripts/audit_fundamental_coverage.py",
            )
        },
        "target_exposure": None,
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    with output.open("x", encoding="utf-8") as target:
        target.write(serialized)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "acceptance_passed",
                    "current_month_yoy_company_count",
                    "paired_month_yoy_company_count",
                    "current_reported_financial_company_count",
                    "comparable_input_company_count",
                    "comparable_input_proxy_nav_weight_percent",
                )
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
