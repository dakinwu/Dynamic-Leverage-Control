"""Index existing evidence without rerunning studies or changing historical outputs."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from dynamic_leverage.services.research_registry import build_registry

ROOT = Path(__file__).resolve().parents[2]



def run(root: Path, check: bool):
    registry = build_registry(root)
    path = root / "docs/research/experiment-registry.json"
    text = json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    if check:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            raise ValueError("research index is stale; regenerate before new experiments")
    else:
        path.write_text(text, encoding="utf-8")
        with (path.parent / "experiment-metrics.csv").open(
            "w", newline="", encoding="utf-8"
        ) as out:
            rows = [
                {
                    "case_id": case["case_id"],
                    "family": case["family"],
                    "revision_status": case["revision_status"],
                    "data_kind": case["data_kind"],
                    "start": case["period"].get("start"),
                    "end": case["period"].get("end"),
                    "calendar_cagr": case["metrics"].get("calendar_cagr"),
                    "maximum_drawdown": case["metrics"].get("maximum_drawdown"),
                    "maximum_principal_loss": case["metrics"].get("maximum_principal_loss"),
                    "final_nav": case["metrics"].get("final_nav"),
                    "total_cost": case["metrics"].get("total_cost"),
                    "artifact": case["artifact"],
                    "json_pointer": case["json_pointer"],
                }
                for case in registry["cases"]
            ]
            writer = csv.DictWriter(out, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print(
        json.dumps(
            {
                "studies": registry["study_count"],
                "cases": registry["case_count"],
                "identical_numeric_groups": len(registry["identical_numeric_groups"]),
                "check_only": check,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    run(ROOT, args.check)
