"""Cache public TWSE crisis inputs separately; no database or broker writes."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import io
import json
import re
import time
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal as D
from decimal import InvalidOperation
from pathlib import Path

import httpx

from dynamic_leverage.infrastructure.providers.official_valuation import (
    parse_market_valuation,
)

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data/private/oos-crisis-20260921"


def parse_archive(body):
    """Legacy research layout has an explicit all-listed-stocks row, not an average."""
    try:
        observed, pe = parse_market_valuation(body, "application/zip")
        return observed, pe, "market-row"
    except ValueError:
        if body[:2] != b"PK":
            raise
    import xlrd

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".xls"):
                continue
            book = xlrd.open_workbook(file_contents=archive.read(name))
            for sheet in book.sheets():
                dates = [
                    xlrd.xldate_as_datetime(cell.value, book.datemode).date()
                    for row in sheet.get_rows()
                    for cell in row
                    if cell.ctype == xlrd.XL_CELL_DATE
                ]
                if not dates:
                    for row in sheet.get_rows():
                        for cell in row:
                            match = re.search(
                                r"民\s*國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月", str(cell.value)
                            )
                            if match:
                                year, month = int(match[1]) + 1911, int(match[2])
                                dates.append(date(year, month, calendar.monthrange(year, month)[1]))
                for row in sheet.get_rows():
                    if "".join(str(row[0].value).split()) != "全部上市股票":
                        continue
                    # Match the PE header; do not scan across an empty PE into yield/PBR.
                    columns = [
                        value_col
                        for header_col, value_col in [(2, 2), (4, 5)]
                        if any(
                            len(header) > header_col
                            and "".join(str(header[header_col].value).split()) == "本益比"
                            for header in sheet.get_rows()
                        )
                    ]
                    if len(columns) != 1:
                        raise ValueError("unknown legacy PE header layout")
                    column = columns[0]
                    pe = D(str(row[column].value))
                    if len(set(dates)) != 1 or not pe.is_finite() or pe <= 0:
                        raise ValueError("ambiguous legacy date or PE")
                    return dates[0], pe, "legacy-all-listed-stocks-historical-accounting"
    raise ValueError("no explicit market PE in legacy workbook")


def collect(probe=False, old_valuations=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    requests = []
    months = (
        []
        if old_valuations
        else ["199901", "200001", "200801"]
        if probe
        else [f"{y}{m:02}" for y in range(1999, 2011) for m in range(1, 13)]
    )
    for month in months:
        requests.append(
            (
                f"taiex-{month}.json",
                "index",
                month,
                f"https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={month}01&response=json",
            )
        )
    pe_months = (
        [f"{y}{m:02}" for y in range(1999, 2005) for m in range(1, 13) if f"{y}{m:02}" >= "199903"]
        if old_valuations
        else ["199901", "200001", "200801"]
        if probe
        else []
    )
    for month in pe_months:
        requests.append(
            (
                f"pe-{month}.zip",
                "pe",
                month,
                "https://www.twse.com.tw/rwd/staticFiles/inspection/inspection/04/001/"
                f"{month}_C04001.zip",
            )
        )
    if old_valuations:
        for month in [f"{y}{m:02}" for y in range(2007, 2011) for m in range(1, 13)]:
            requests.append(
                (
                    f"tw50-{month}.json",
                    "index",
                    month,
                    f"https://www.twse.com.tw/rwd/zh/FTSE/TAI50I?date={month}01&response=json",
                )
            )
    if not probe and not old_valuations:
        requests.append(
            (
                "taiex-202609.json",
                "index",
                "202609",
                "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date=20260901&response=json",
            )
        )
        for stock in ["0050", "00631L"]:
            requests.append(
                (
                    f"official-{stock}-202609.json",
                    "price",
                    "202609",
                    "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
                    f"?date=20260901&stockNo={stock}&response=json",
                )
            )
    manifest_path = CACHE / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    with httpx.Client(
        timeout=40, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
    ) as client:
        for name, kind, month, url in requests:
            path = CACHE / name
            if path.exists() and manifest.get(name, {}).get("valid"):
                continue
            status = None
            body = b""
            previous_entry = manifest.get(name, {})
            failed = CACHE / (name + ".failed")
            reused = kind == "pe" and failed.exists() and failed.read_bytes()[:2] == b"PK"
            try:
                if reused:
                    body, status = failed.read_bytes(), previous_entry.get("status")
                else:
                    time.sleep(1.3)
                    r = client.get(url)
                    status, body = r.status_code, r.content
                    r.raise_for_status()
                if kind == "pe":
                    observed, pe, method = parse_archive(body)
                    if observed.strftime("%Y%m") != month:
                        raise ValueError("month mismatch")
                    detail = {"as_of": str(observed), "pe": str(pe), "method": method}
                else:
                    payload = json.loads(body)
                    if payload.get("stat") != "OK" or not payload.get("data"):
                        raise ValueError("official endpoint has no usable data")
                    for row in payload["data"]:
                        y, m, _ = map(int, row[0].split("/"))
                        if f"{y + 1911}{m:02}" != month:
                            raise ValueError("month mismatch")
                    detail = {"rows": len(payload["data"])}
                path.write_bytes(body)
                entry = {"valid": True, **detail}
                print("CACHED", name, detail, flush=True)
            except (httpx.HTTPError, ValueError, KeyError, InvalidOperation) as exc:
                (CACHE / (name + ".failed")).write_bytes(body)
                entry = {"valid": False, "error": str(exc)}
                print("UNAVAILABLE", name, type(exc).__name__, status, flush=True)
            manifest[name] = {
                **entry,
                "url": url,
                "status": status,
                "retrieved_at": previous_entry["retrieved_at"]
                if reused
                else datetime.now(UTC).isoformat(),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            if status in {403, 428, 429}:
                raise RuntimeError("source throttled; stop instead of bypassing its restriction")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--old-valuations", action="store_true")
    args = parser.parse_args()
    collect(args.probe, args.old_valuations)
