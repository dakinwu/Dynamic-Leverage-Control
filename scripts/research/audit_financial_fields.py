"""Inventory already saved statement fields; no HTTP requests or inferred periods."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from dynamic_leverage.services.financial_periods import parse_statements
from dynamic_leverage.services.fundamental_signal import SourceObservation

ROOT = Path(__file__).resolve().parents[2]


def run(capture: Path, output: Path):
    if output.exists():
        raise FileExistsError("use a new inventory path")
    capture = capture.resolve()
    manifest = json.loads((capture / "observations.json").read_bytes())
    reports = []
    for entry in manifest["observations"]:
        endpoint = entry["source_url"].split("/")[-1]
        if not endpoint.startswith("t187ap06_L_"):
            continue
        path = (capture / entry["raw_file"]).resolve()
        if not path.is_relative_to(capture):
            raise ValueError("raw path outside capture")
        observation = SourceObservation(
            entry["source_id"],
            entry["source_url"],
            path.read_bytes(),
            entry["body_sha256"],
            datetime.fromisoformat(entry["received_at"]),
            datetime.fromisoformat(entry["recorded_at"]),
        )
        rows = parse_statements(observation, endpoint.removeprefix("t187ap06_L_"))
        fields = sorted({key for row in rows for key in row.values})
        reports.append(
            {
                "source_url": observation.source_url,
                "body_sha256": observation.body_sha256,
                "statement_kind": rows[0].kind,
                "row_count": len(rows),
                "period_labels": sorted({f"{row.fiscal_year_label}Q{row.quarter}" for row in rows}),
                "fields": fields,
                "nonmissing_field_counts": {
                    key: sum(row.values.get(key) is not None for row in rows) for key in fields
                },
                "reported_company_count": len({row.company for row in rows}),
                "amount_unit": "unknown",
                "scope": "unknown",
                "period_basis": "unknown",
                "gross_margin_mapping_eligible_after_context_verification": rows[0].kind == "ci",
                "normalized_quarter_signals": 0,
            }
        )
    result = {
        "schema": "financial-field-inventory-v1",
        "capture": str(capture),
        "downloaded_new_sources": False,
        "reports": reports,
        "reported_rows": sum(r["row_count"] for r in reports),
        "period_context_verification_ready": False,
        "profitability_demonstrated": False,
    }
    with output.open("x", encoding="utf-8") as target:
        json.dump(result, target, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {
                "statement_tables": len(reports),
                "reported_rows": result["reported_rows"],
                "normalized_quarter_signals": 0,
                "downloaded_new_sources": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.capture, args.output)
