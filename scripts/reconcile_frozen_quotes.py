"""Reconcile all real-ETF frozen replay quotes with the expanded official audit."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dynamic_leverage.services.research_artifacts import read_dataset  # noqa: E402


def main():
    audit_path = ROOT / "docs/research/official-etf-audit-20260928.json"
    audit = json.loads(audit_path.read_bytes())
    official = {"0050": {}, "00631L": {}}
    for name, evidence in audit["evidence"].items():
        path = ROOT / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != evidence["sha256"]:
            raise ValueError(f"audit source changed: {name}")
        if not path.name.startswith("official-"):
            continue
        stock = path.name.split("-")[1]
        for row in json.loads(path.read_bytes())["data"]:
            y, m, d = map(int, row[0].split("/"))
            official[stock][date(y + 1911, m, d)] = (
                Decimal(row[6].replace(",", "")),
                int(row[1].replace(",", "")),
            )
    dataset = ROOT / "data/private/rearm-dataset-20260928"
    manifest = json.loads((dataset / "manifest.json").read_bytes())
    report = {
        "audit_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
        "groups": {},
        "frozen_data_changed": False,
    }
    for group in ("real-etf-base", "real-etf-stress"):
        item = manifest["datasets"][group]
        rows, _ = read_dataset(dataset / item["file"], item["sha256"])
        differences, closures, checked = [], [], 0
        for row in rows:
            for stock, q in row.quotes.items():
                expected = official[stock].get(row.day)
                if expected is None:
                    record = {"date": str(row.day), "stock": stock}
                    if q.volume == 0 and not q.buy_allowed and not q.sell_allowed:
                        closures.append(record)
                    else:
                        differences.append(
                            {**record, "reason": "tradable quote without official day"}
                        )
                elif expected != (q.close, q.volume):
                    differences.append(
                        {"date": str(row.day), "stock": stock, "reason": "price or volume mismatch"}
                    )
                else:
                    checked += 1
        used_days = {row.day for row in rows}
        extra = {
            stock: sorted(
                str(d) for d in values if rows[0].day <= d <= rows[-1].day and d not in used_days
            )
            for stock, values in official.items()
        }
        report["groups"][group] = {
            "matched_quotes": checked,
            "nontradable_days": closures,
            "differences": differences,
            "official_days_absent_from_replay": extra,
        }
        if differences or any(extra.values()):
            raise ValueError("frozen quote reconciliation failed; withhold comparison conclusions")
    path = ROOT / "docs/research/frozen-quote-reconciliation-20260928.json"
    with path.open("x", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(
        json.dumps(
            {
                g: {
                    "matched_quotes": a["matched_quotes"],
                    "nontradable": len(a["nontradable_days"]),
                }
                for g, a in report["groups"].items()
            }
        )
    )


if __name__ == "__main__":
    main()
