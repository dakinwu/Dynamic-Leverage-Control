"""Offline, layout-specific acceptance of two FTSE vintages and a fund proxy."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal as D
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def checked_file(folder, filename, expected):
    path = (folder / filename).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError("file outside capture")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected:
        raise ValueError("universe evidence fingerprint mismatch")
    return body


def run(ftse: Path, proxy: Path, output: Path):
    if output.exists():
        raise FileExistsError("use a new acceptance path")
    from pypdf import PdfReader

    entries = json.loads((ftse / "observations.json").read_bytes())["observations"]
    source_metadata, bodies = {}, {}
    for entry in entries:
        bodies[entry["source_id"]] = checked_file(ftse, entry["raw_file"], entry["body_sha256"])
        source_metadata[entry["source_id"]] = entry
    reader = PdfReader(BytesIO(bodies["ftse-tw50-full-weights"]))
    page = reader.pages[0].extract_text()
    text = " ".join(page.split())
    if "19 August 2026" not in text or "30 June 2026" not in text:
        raise ValueError("FTSE indicative vintage changed; register a new protocol")
    table = re.split(r"Country/Market\s+", page, maxsplit=1)[1]
    table = re.sub(r"Constituent\s+Index\s+weight\s+\(%\)\s+Country/Market\s*", "", table)
    weights = [
        {
            "source_name": " ".join(name.split()),
            "weight_percent": str(D(weight)),
            "company_code": None,
        }
        for name, weight in re.findall(r"(.+?)\s+(\d+(?:\.\d+)?)\s+T\s*AIWAN", table, re.S)
    ]
    if len(weights) != 50 or len({row["source_name"] for row in weights}) != 50:
        raise ValueError("50 unique FTSE source names required")
    total = sum((D(row["weight_percent"]) for row in weights), D(0))
    if any(D(row["weight_percent"]) <= 0 for row in weights) or abs(total - 100) > D("0.25"):
        raise ValueError("FTSE two-decimal rounding bound failed")
    review_reader = PdfReader(BytesIO(bodies["ftse-tw50-review-201903"]))
    review_text = "\n".join(page.extract_text() for page in review_reader.pages[:2])
    review_rows = []
    for line in review_text.splitlines():
        match = re.fullmatch(
            r"\s*(\d+|-)\s+(\d{4})\s+(.+?)\s+(\d+\.\d+|-)\s+(\d+\.\d+|-)\s+(-?\d+\.\d+)\s+(\d+\.\d+)\s*",
            line,
        )
        if match:
            rank, code, label, before, after, change, turnover = match.groups()
            review_rows.append(
                {
                    "rank_after": None if rank == "-" else int(rank),
                    "company_code": code,
                    "source_name_and_sector": label,
                    "before_weight_percent": None if before == "-" else before,
                    "after_weight_percent": None if after == "-" else after,
                    "change_percentage_points": change,
                    "two_way_turnover_percent": turnover,
                }
            )
    before = [row for row in review_rows if row["before_weight_percent"] is not None]
    after = [row for row in review_rows if row["after_weight_percent"] is not None]
    if (
        len(before) != 50
        or len(after) != 50
        or len({row["company_code"] for row in review_rows}) != 51
    ):
        raise ValueError("complete review before/after membership required")
    if "08 March 2019" not in review_text or "Effective on Monday 18 March 2019" not in review_text:
        raise ValueError("review data and effective dates must be explicit")
    before_total = sum((D(row["before_weight_percent"]) for row in before), D(0))
    after_total = sum((D(row["after_weight_percent"]) for row in after), D(0))
    if abs(before_total - 100) > D("0.25") or abs(after_total - 100) > D("0.25"):
        raise ValueError("review rounding bound failed")
    manifest = json.loads((proxy / "manifest.json").read_bytes())
    proxy_bodies = {
        name: checked_file(proxy, name, entry["sha256"])
        for name, entry in manifest["files"].items()
    }
    holdings = json.loads(proxy_bodies["holdings.json"])
    raw_dom = proxy_bodies["rendered-dom-stock-section.raw.txt"].decode("utf-8")
    if (
        manifest["universe_kind"] != "0050_fund_holdings_proxy"
        or len(holdings) != 50
        or len({row["company_code"] for row in holdings}) != 50
    ):
        raise ValueError("complete independently labelled fund holdings required")
    for row in holdings:
        line = "\t".join(
            row[key]
            for key in ("company_code", "company_name", "quantity_shares", "weight_percent")
        )
        if line not in raw_dom or D(row["weight_percent"]) <= 0 or D(row["quantity_shares"]) <= 0:
            raise ValueError("proxy parsed row does not match received DOM")
    proxy_total = sum((D(row["weight_percent"]) for row in holdings), D(0))
    if proxy_total != D(manifest["published_weight_sum_percent"]) or manifest["weights_normalized"]:
        raise ValueError("published proxy weights must remain unchanged")
    result = {
        "schema": "universe-evidence-acceptance-v1",
        "acceptance_passed": True,
        "ftse_source_metadata": source_metadata,
        "ftse_indicative_weights": {
            "as_of": "2026-06-30",
            "document_date": "2026-08-19",
            "publication_timestamp_verified": False,
            "source_name_count": 50,
            "weights": weights,
            "weight_sum_percent": str(total),
            "company_code_mapping_verified": False,
            "weights_normalized": False,
            "universe_kind": "TW50-index-indicative-vintage",
            "usable_at_2026_06_30": False,
        },
        "ftse_2019_review": {
            "data_as_of": "2019-03-08",
            "membership_effective_date": "2019-03-18",
            "before_count": len(before),
            "after_count": len(after),
            "before_weight_sum_percent": str(before_total),
            "after_weight_sum_percent": str(after_total),
            "weight_basis": "review-pro-forma-using-2019-03-08-data",
            "publication_timestamp_verified": False,
            "rows": review_rows,
            "weights_normalized": False,
            "actual_2019_03_18_close_weights_verified": False,
        },
        "fund_holdings_proxy": {
            "as_of": manifest["as_of"],
            "universe_kind": manifest["universe_kind"],
            "available_at": max(
                datetime.fromisoformat(manifest["received_at"]),
                datetime.fromisoformat(manifest["recorded_at"]),
            ).isoformat(),
            "offline_integrity_verified": True,
            "stock_count": 50,
            "weight_sum_percent": str(proxy_total),
            "chart_stock_weight_percent": manifest["chart_stock_weight_percent"],
            "difference_percentage_points": manifest[
                "stock_weight_sum_vs_chart_difference_percentage_points"
            ],
            "weights_normalized": False,
            "exact_tw50_index_weights_verified": False,
        },
        "full_historical_universe_ready": False,
        "historical_research_ready": False,
        "aggregate_fundamental_signal_ready": False,
        "profitability_demonstrated": False,
        "target_exposure": None,
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    with output.open("x", encoding="utf-8") as target:
        target.write(serialized)
    print(
        json.dumps(
            {
                "acceptance_passed": True,
                "indicative_count": 50,
                "indicative_sum": str(total),
                "review_before_count": len(before),
                "review_after_count": len(after),
                "proxy_count": 50,
                "proxy_sum": str(proxy_total),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ftse", type=Path, default=ROOT / "data/private/tw50-weights-evidence-20261005"
    )
    parser.add_argument(
        "--proxy", type=Path, default=ROOT / "data/private/0050-holdings-proxy-20261005"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.ftse, args.proxy, args.output)
