"""Replay saved SEC monthly reports and two previously reviewed TSMC PDFs offline."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal as D
from io import BytesIO
from pathlib import Path

from dynamic_leverage.services.comparable_fundamentals import (
    company_diagnostic,
    parse_tsmc_sec_revenue,
)
from dynamic_leverage.services.financial_periods import (
    FinancialContext,
    ReportedStatement,
    general_quarter_margin,
)
from dynamic_leverage.services.fundamental_signal import (
    SourceObservation,
    evaluate_signal,
)
from dynamic_leverage.services.research_artifacts import json_report

ROOT = Path(__file__).resolve().parents[2]



def load_source(folder: Path, entry: dict) -> SourceObservation:
    path = (folder / entry["raw_file"]).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError("source path outside capture")
    return SourceObservation(
        entry.get("source_id", entry.get("document_id")),
        entry.get("source_url", entry.get("url")),
        path.read_bytes(),
        entry["body_sha256"],
        datetime.fromisoformat(entry["received_at"]),
        datetime.fromisoformat(entry["recorded_at"]),
    )


def run(capture: Path, pdfs: Path, output: Path):
    if output.exists():
        raise FileExistsError("use a new acceptance path")
    from pypdf import PdfReader

    manifest = json.loads((capture / "observations.json").read_bytes())
    sources, revenues, checks = {}, [], []
    for entry in manifest["observations"]:
        if entry.get("status") != "captured-awaiting-validation":
            continue
        source = load_source(capture, entry)
        sources[source.source_id] = source
        if source.source_id == "sec-2330-2026-07":
            revenues.append(parse_tsmc_sec_revenue(source, date(2026, 7, 31)))
        elif source.source_id == "sec-2330-2026-08":
            revenues.append(parse_tsmc_sec_revenue(source, date(2026, 8, 31)))
    if len(revenues) != 2:
        raise ValueError("two actual saved revenue reports required")
    reviews, statements = [], []
    for entry in json.loads((pdfs / "observations.json").read_bytes())["observations"]:
        if entry["document_id"] not in {"2330-q1", "2330-q2"}:
            continue
        source = load_source(pdfs, entry)
        sources[source.source_id] = source
        quarter = int(source.source_id[-1])
        reader = PdfReader(BytesIO(source.body))
        text = " ".join(reader.pages[4].extract_text().split())
        notes = "".join(
            "".join(reader.pages[index].extract_text().split()) for index in (10, 11, 12)
        )
        if any(
            token not in notes
            for token in (
                "IAS34",
                "December31,2025",
                "didnothaveamaterialimpactontheaccountingpolicies",
                "consistentwiththoseapplied",
            )
        ):
            raise ValueError("reviewed accounting basis notes changed")
        heading = "Three Months Ended " + ("March 31" if quarter == 1 else "June 30")
        required = (heading, "2026 2025", "CONSOLIDATED", "Thousands of New Taiwan Dollars")
        if any(token not in text for token in required):
            raise ValueError("reviewed PDF period, scope or unit changed")
        # This page's manually reviewed first amount column is current single
        # quarter. Never take the Q2 third amount column (six-month cumulative).
        revenue_matches = re.findall(r"NET REVENUE \(Notes [^)]*\) \$ ([\d,]+)", text)
        gross_matches = re.findall(r"GROSS PROFIT ([\d,]+)", text)
        if len(revenue_matches) != 1 or len(gross_matches) != 1:
            raise ValueError("unique reviewed revenue and gross profit row required")
        amounts = {
            "營業收入": D(revenue_matches[0].replace(",", "")),
            "營業毛利（毛損）淨額": D(gross_matches[0].replace(",", "")),
        }
        statement = ReportedStatement(
            "2330",
            "115",
            quarter,
            "ci",
            amounts,
            source.source_id,
            source.body_sha256,
            source.available_at,
        )
        statements.append(statement)
        reviews.append(
            {
                "company": "2330",
                "document_id": source.source_id,
                "document_sha256": source.body_sha256,
                "pdf_page": 5,
                "accounting_notes_pages": [11, 12, 13],
                "accounting_notes_checked": True,
                "reviewed_column": "2026 single-quarter first amount column",
                "amount_unit": "TWD-thousand",
                "scope": "consolidated",
                "period_basis": "single-quarter",
                "quarter": quarter,
                "amounts": amounts,
                "method": "previous manual page review plus strict layout and amount extraction",
            }
        )
    if len(statements) != 2:
        raise ValueError("both reviewed PDFs required")
    verified_at = datetime.now(UTC)
    review_body = json.dumps(
        reviews, default=json_report, ensure_ascii=False, sort_keys=True
    ).encode()
    review = SourceObservation(
        "2330-comparable-period-review",
        "local-review:2330-q1-q2-20261005",
        review_body,
        hashlib.sha256(review_body).hexdigest(),
        verified_at,
        verified_at,
    )
    sources[review.source_id] = review
    margins = []
    for statement in sorted(statements, key=lambda x: x.quarter):
        month = statement.quarter * 3
        context = FinancialContext(
            "2330",
            statement.source_id,
            statement.body_sha256,
            review.source_id,
            date(2026, month - 2, 1),
            date(2026, month, calendar.monthrange(2026, month)[1]),
            "single-quarter",
            "TWD-thousand",
            "consolidated",
            "2330-TIFRS-2026",
            "2330-2026-q1-q2-reviewed-same-basis",
            verified_at,
        )
        margins.append(general_quarter_margin(statement, context, sources, verified_at))
    revenues.sort(key=lambda value: value.period_end)
    diagnostic = company_diagnostic(
        revenues[1], revenues[0], margins[1], margins[0], sources, verified_at
    )
    old_cutoff = datetime(2026, 9, 30, tzinfo=UTC)
    old = company_diagnostic(revenues[1], revenues[0], margins[1], margins[0], sources, old_cutoff)
    values = [*revenues, *(margin.value for margin in margins)]
    index = evaluate_signal(
        list(sources.values()),
        values,
        None,
        cutoff=verified_at,
        revenue_month=date(2026, 8, 31),
        margin_quarter=date(2026, 6, 30),
    )
    # Cross-source current month agrees exactly with the saved TWSE precision.
    twse_folder = ROOT / "data/private/fundamental-evidence-20261005-v2"
    twse_manifest = json.loads((twse_folder / "observations.json").read_bytes())
    entry = next(e for e in twse_manifest["observations"] if e["source_url"].endswith("t187ap05_L"))
    twse = load_source(twse_folder, entry)
    row = next(r for r in json.loads(twse.body) if r["公司代號"] == "2330")
    agreement = row["資料年月"] == "11508" and abs(
        D(row["營業收入-去年同月增減(%)"]) / 100 - revenues[1].value
    ) < D("0.00000001")
    checks.extend(
        [
            {"check": "exact-sec-twse-august-yoy-agreement", "passed": agreement},
            {
                "check": "two-comparable-quarter-and-month-inputs",
                "passed": diagnostic["status"] == "company_diagnostic_only",
            },
            {
                "check": "no-backdating-to-september",
                "passed": old["status"] == "unavailable" and old["improving"] is None,
            },
            {
                "check": "partial-inputs-never-create-index-feature",
                "passed": index["status"] == "unavailable" and index["target_exposure"] is None,
            },
        ]
    )
    result = {
        "schema": "fundamental-comparable-company-acceptance-v1",
        "verified_at": verified_at,
        "sources": [
            {
                "source_id": s.source_id,
                "source_url": s.source_url,
                "body_sha256": s.body_sha256,
                "available_at": s.available_at,
            }
            for s in [*sources.values(), twse]
        ],
        "local_review_evidence": reviews,
        "local_review_body_sha256": review.body_sha256,
        "values": [vars(value) for value in values],
        "quarter_source_dependencies": [asdict(margin) for margin in margins],
        "diagnostic": diagnostic,
        "historical_cutoff_check": old,
        "index_signal": index,
        "checks": checks,
        "acceptance_passed": all(check["passed"] for check in checks),
        "company_count": 1,
        "aggregate_tw50_signal_ready": False,
        "historical_publication_time_verified": False,
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "new_out_of_sample": False,
        "failed_source_attempts": [e for e in manifest["observations"] if e["status"] == "failed"],
        "source_sha256": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in (
                "src/dynamic_leverage/services/comparable_fundamentals.py",
                "src/dynamic_leverage/services/financial_periods.py",
                "scripts/audit_comparable_fundamentals.py",
            )
        },
    }
    serialized = json.dumps(result, default=json_report, ensure_ascii=False, indent=2)
    with output.open("x", encoding="utf-8") as target:
        target.write(serialized)
    print(
        json.dumps(
            {"acceptance_passed": result["acceptance_passed"], "diagnostic": diagnostic},
            default=json_report,
        )
    )
    if not result["acceptance_passed"]:
        raise ValueError("comparable company acceptance failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture", type=Path, default=ROOT / "data/private/fundamental-comparable-20261005"
    )
    parser.add_argument(
        "--pdfs", type=Path, default=ROOT / "data/private/issuer-financial-pdfs-20261005"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.capture, args.pdfs, args.output)
