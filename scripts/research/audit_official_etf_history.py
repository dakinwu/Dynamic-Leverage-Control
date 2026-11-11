"""Append missing official quote months without changing frozen research inputs."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data/private/historical-research-20260918"
OUT = ROOT / "data/private/official-etf-audit-20260928"
REPORT = ROOT / "docs/research/official-etf-audit-20260928.json"


def official_rows(payload, stock, month):
    if payload.get("stat") != "OK" or stock not in payload.get("title", ""):
        raise ValueError("official symbol or status mismatch")
    if payload.get("date", "")[:6] != month or not payload.get("data"):
        raise ValueError("official requested month mismatch")
    result = {}
    for row in payload["data"]:
        y, m, d = map(int, row[0].split("/"))
        day = date(y + 1911, m, d).isoformat()
        if day[:7].replace("-", "") != month or day in result:
            raise ValueError("mixed month or duplicate official date")
        result[day] = (Decimal(row[6].replace(",", "")), int(row[1].replace(",", "")))
    return result


def main():
    if REPORT.exists():
        raise FileExistsError("audit report is immutable; select a new batch")
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "symbols": {},
        "evidence": {},
        "frozen_research_changed": False,
        "historical_publication_verified": False,
    }
    evidence = report["evidence"]
    with httpx.Client(timeout=35, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for stock in ("0050", "00631L"):
            vendor_path = CACHE / f"finmind-price-{stock}.json"
            vendor = {
                r["date"]: (Decimal(str(r["close"])), int(r["Trading_Volume"]))
                for r in json.loads(vendor_path.read_bytes())["data"]
                if "2016-01-01" <= r["date"] <= "2026-09-17"
            }
            evidence[vendor_path.relative_to(ROOT).as_posix()] = {
                "sha256": hashlib.sha256(vendor_path.read_bytes()).hexdigest()
            }
            original_days, official, errors = set(), {}, []
            for month in sorted({day[:7].replace("-", "") for day in vendor}):
                name = f"official-{stock}-{month}.json"
                old, new = CACHE / name, OUT / name
                path = old if old.exists() else new
                url = (
                    "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
                    f"?date={month}01&stockNo={stock}&response=json"
                )
                try:
                    if not path.exists():
                        time.sleep(1.5)
                        response = client.get(url)
                        response.raise_for_status()
                        official_rows(response.json(), stock, month)
                        with path.open("xb") as f:
                            f.write(response.content)
                        (OUT / (name + ".meta.json")).write_text(
                            json.dumps(
                                {
                                    "url": url,
                                    "received_at": datetime.now(UTC).isoformat(),
                                    "sha256": hashlib.sha256(response.content).hexdigest(),
                                    "published_at": None,
                                }
                            ),
                            encoding="utf-8",
                        )
                    rows = official_rows(json.loads(path.read_bytes()), stock, month)
                    official.update({d: v for d, v in rows.items() if d in vendor})
                    if path == old:
                        original_days.update(d for d in rows if d in vendor)
                    evidence[path.relative_to(ROOT).as_posix()] = {
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "url": url,
                    }
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    errors.append({"month": month, "error": type(exc).__name__})
                print(stock, month, len(official), "verified days", flush=True)
            differences = [
                {
                    "date": d,
                    "vendor_price": str(vendor[d][0]),
                    "official_price": str(v[0]),
                    "vendor_volume": vendor[d][1],
                    "official_volume": v[1],
                }
                for d, v in sorted(official.items())
                if v != vendor[d]
            ]
            report["symbols"][stock] = {
                "quote_days": len(vendor),
                "previous_official_days": len(original_days),
                "official_days": len(official),
                "missing_days": sorted(vendor.keys() - official.keys()),
                "price_differences": [
                    r for r in differences if r["vendor_price"] != r["official_price"]
                ],
                "volume_differences": [
                    r for r in differences if r["vendor_volume"] != r["official_volume"]
                ],
                "request_errors": errors,
            }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REPORT", REPORT)


if __name__ == "__main__":
    main()
