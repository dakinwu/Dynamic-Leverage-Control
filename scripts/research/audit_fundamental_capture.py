"""Offline raw-source and normalized-value replay. Never updates the capture."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from dynamic_leverage.services.fundamental_signal import (
    SourceObservation,
    aware,
    parse_revenue,
)

ROOT = Path(__file__).resolve().parents[2]



def audit(capture: Path, cutoff: datetime, output: Path):
    aware(cutoff)
    if output.exists():
        raise FileExistsError("choose a new acceptance file")
    capture = capture.resolve()
    manifest = json.loads((capture / "observations.json").read_bytes())
    checks, values = [], []
    for entry in manifest["observations"]:
        path = (capture / entry["raw_file"]).resolve()
        if not path.is_relative_to(capture):
            raise ValueError("raw file must stay within capture directory")
        observation = SourceObservation(
            entry["source_id"],
            entry["source_url"],
            path.read_bytes(),
            entry["body_sha256"],
            datetime.fromisoformat(entry["received_at"]),
            datetime.fromisoformat(entry["recorded_at"]),
        )
        if observation.available_at.isoformat() != entry["available_at"]:
            raise ValueError("availability metadata mismatch")
        parsed = json.loads(observation.body)
        if not isinstance(parsed, list) or len(parsed) != entry["row_count"]:
            raise ValueError("raw row count mismatch")
        checks.append(
            {
                "source_url": observation.source_url,
                "body_sha256_verified": True,
                "row_count": len(parsed),
                "available_at": observation.available_at.isoformat(),
                "available_at_cutoff": observation.available_at <= cutoff,
            }
        )
        if entry["source_url"].endswith("/t187ap05_L"):
            values.extend(parse_revenue(observation))
    replayed = [
        {
            **asdict(v),
            "available_at": v.available_at.isoformat(),
            "value": str(v.value),
            "period_end": v.period_end.isoformat(),
        }
        for v in values
    ]
    persisted = json.loads((capture / "revenue-values.json").read_bytes())
    if replayed != persisted:
        raise ValueError("normalized values differ from raw replay")
    if manifest["failures"]:
        raise ValueError("capture has download/validation failures")
    result = {
        "schema": "fundamental-offline-acceptance-v1",
        "capture": str(capture),
        "cutoff": cutoff.isoformat(),
        "sources": checks,
        "normalized_revenue_rows": len(values),
        "available_revenue_rows": sum(v.available_at <= cutoff for v in values),
        "normalized_replay_matches": True,
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "remaining_blockers": [
            "CONTEMPORANEOUS_TW50_MEMBERS_AND_WEIGHTS",
            "PREVIOUS_REVENUE_MONTH",
            "VERIFIED_COMPARABLE_QUARTERS_AND_FINANCIAL_METRICS",
            "HISTORICAL_PUBLICATION_AND_REVISION_EVIDENCE",
        ],
    }
    with output.open("x", encoding="utf-8") as target:
        json.dump(result, target, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "sources"}, ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--cutoff", type=datetime.fromisoformat, default=datetime.now(UTC))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.capture, args.cutoff, args.output)
