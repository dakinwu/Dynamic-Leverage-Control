"""Verify saved issuer NAV tables; no total-return or execution claim is made."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path


def parse_nav(body: bytes, expected_symbol: str | None = None) -> list[dict]:
    text = body.decode("utf-8")
    if expected_symbol is not None:
        identities = re.findall(r"var\s+stkId\s*=\s*[\'\"]([^\'\"]+)[\'\"]", text)
        if identities != [expected_symbol]:
            raise ValueError("issuer response fund identity mismatch")
    matches = re.findall(r"var\s+PNavStr\s*=\s*['\"]([^'\"]+)['\"]", text)
    if len(matches) != 1:
        raise ValueError("unique reviewed issuer NAV array required")
    tokens = matches[0].split(",")
    if len(tokens) % 4:
        raise ValueError("complete date/NAV tuples required")
    rows, seen = [], set()
    for index in range(0, len(tokens), 4):
        y, m, d, raw_nav = tokens[index : index + 4]
        day = date(int(y), int(m), int(d))
        nav = D(raw_nav)
        if day in seen or not nav.is_finite() or nav <= 0:
            raise ValueError("unique dates and positive finite issuer NAV required")
        seen.add(day)
        rows.append({"day": day.isoformat(), "reported_unit_nav": str(nav)})
    if not rows or rows != sorted(rows, key=lambda row: row["day"]):
        raise ValueError("nonempty chronological issuer NAV array required")
    return rows


def run(folder: Path, output: Path):
    if output.exists():
        raise FileExistsError("use a new NAV evidence output")
    manifest = json.loads((folder / "manifest.json").read_bytes())
    expected = {"00670L": "58", "00675L": "60", "00662": "55"}
    if len(manifest["observations"]) != len(expected):
        raise ValueError("all declared series required")
    results = []
    for entry in manifest["observations"]:
        if entry["symbol"] not in expected or expected[entry["symbol"]] != entry["fund_id"]:
            raise ValueError("reviewed fund identity required")
        if (
            entry["source_url"]
            != f"https://www.fubon.com/asset-management/fund/info/value?Fd={entry['fund_id']}&range=0"
        ):
            raise ValueError("reviewed issuer source required")
        path = (folder / entry["raw_file"]).resolve()
        if not path.is_relative_to(folder.resolve()):
            raise ValueError("raw file outside capture")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["body_sha256"]:
            raise ValueError("NAV source fingerprint mismatch")
        rows = parse_nav(body, entry["symbol"])
        steps = []
        for previous, current in zip(rows, rows[1:], strict=False):
            change = D(current["reported_unit_nav"]) / D(previous["reported_unit_nav"]) - 1
            if abs(change) >= D("0.30"):
                steps.append(
                    {"previous": previous, "current": current, "raw_step_fraction": str(change)}
                )
        results.append(
            {
                "symbol": entry["symbol"],
                "source": entry,
                "rows": len(rows),
                "first": rows[0],
                "last": rows[-1],
                "available_at": max(
                    datetime.fromisoformat(entry["received_at"]),
                    datetime.fromisoformat(entry["recorded_at"]),
                ).isoformat(),
                "large_raw_steps_requiring_review": steps,
                "before_listing_rows_must_not_be_traded": True,
                "total_return_series_verified": False,
                "corporate_actions_verified": False,
                "nav_publication_clock_verified": False,
            }
        )
    if {entry["symbol"] for entry in results} != set(expected):
        raise ValueError("unique declared fund set required")
    result = {
        "schema": "alternative-issuer-nav-acceptance-v1",
        "route_id": "R17",
        "acceptance_passed": True,
        "series": results,
        "total_reported_nav_rows": sum(entry["rows"] for entry in results),
        "strategy_return_backtest_performed": False,
        "profitability_demonstrated": False,
        "historical_research_ready": False,
        "target_exposure": None,
        "limitations": [
            "parse acceptance is not split/distribution adjustment acceptance",
            "no large raw step does not prove absence of corporate actions",
            "NAV is not executable exchange price",
            "foreign-market NAV date is not a verified publication clock",
            "00670L and 00662 historical hedging ratios not verified comparable",
        ],
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    with output.open("x", encoding="utf-8") as target:
        target.write(serialized)
    print(
        json.dumps(
            {
                "funds": len(results),
                "reported_rows": result["total_reported_nav_rows"],
                "parse_acceptance_passed": True,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.folder, args.output)
