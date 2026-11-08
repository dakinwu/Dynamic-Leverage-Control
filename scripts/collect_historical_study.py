"""Download missing official monthly inputs into an isolated research cache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "data/private/research-deps")]

from dynamic_leverage.domain.valuation import validated_valuation_row  # noqa: E402
from dynamic_leverage.infrastructure.providers.official_valuation import (  # noqa: E402
    parse_market_valuation,
)

CACHE = ROOT / "data/private/historical-research-20260918"


def download_supplemental() -> None:
    """Cache public research inputs; credentials travel in headers and are never saved."""
    CACHE.mkdir(parents=True, exist_ok=True)
    queries = [
        ("TaiwanStockPrice", stock, f"finmind-price-{stock}.json") for stock in ["0050", "00631L"]
    ]
    queries += [
        (dataset, stock, dataset + ("-00631L" if stock == "00631L" else "") + ".json")
        for dataset in ["TaiwanStockDividendResult", "TaiwanStockDividend"]
        for stock in ["0050", "00631L"]
    ]
    with httpx.Client(timeout=50, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for dataset, stock, filename in queries:
            path = CACHE / filename
            if path.exists():
                continue
            secret = Path(os.environ["LOCALAPPDATA"]) / "dynamic-leverage-2x/secrets/runtime.env"
            token = next(
                (
                    line.split("=", 1)[1].strip().strip('"')
                    for line in secret.read_text().splitlines()
                    if line.startswith("FINMIND_TOKEN=")
                ),
                "",
            )
            response = client.get(
                "https://api.finmindtrade.com/api/v4/data",
                params={
                    "dataset": dataset,
                    "data_id": stock,
                    "start_date": "2015-01-01",
                    "end_date": "2026-09-17",
                },
                headers={"Authorization": "Bearer " + token},
            )
            if response.status_code != 200 or response.json().get("status") != 200:
                raise RuntimeError(f"supplemental research download unavailable: {filename}")
            path.write_bytes(response.content)
            print("CACHED", filename, flush=True)
        for filename, symbol in [
            ("yahoo0050.raw", "0050.TW"),
            ("yahoo00631L.raw", "00631L.TW"),
            ("yahootaiex.raw", "%5ETWII"),
        ]:
            path = CACHE / filename
            if path.exists():
                continue
            response = client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={
                    "period1": 1104537600,
                    "period2": 1789689600,
                    "interval": "1d",
                    "events": "div,splits",
                },
            )
            response.raise_for_status()
            if not response.json().get("chart", {}).get("result"):
                raise ValueError("vendor crosscheck history unavailable")
            path.write_bytes(response.content)
        for filename, start, end in [
            ("official-dividend-2016.json", "20160101", "20161231"),
            ("official-dividend-probe-startDate.json", "20220701", "20220831"),
        ]:
            path = CACHE / filename
            if path.exists():
                continue
            time.sleep(3)
            response = client.get(
                "https://www.twse.com.tw/exchangeReport/TWT49U",
                params={"startDate": start, "endDate": end, "response": "json"},
            )
            response.raise_for_status()
            if response.json().get("stat") != "OK":
                raise ValueError("official dividend crosscheck unavailable")
            path.write_bytes(response.content)


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    download_supplemental()
    if "--supplemental-only" in sys.argv:
        return
    valuation_path = CACHE / "valuations.json"
    valuations = (
        json.loads(valuation_path.read_text(encoding="utf-8")) if valuation_path.exists() else {}
    )
    db_path = CACHE / "db-history.json"
    snapshots = json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else []
    for snap in snapshots:
        if snap["kind"] != "valuation.taiex.manual":
            continue
        for row in snap["payload"].get("rows", []):
            valid = validated_valuation_row(row)
            if valid and snap["payload"].get("sources", {}).get("twse_market_report"):
                valuations[str(valid[0])[:7]] = {
                    "as_of": str(valid[0]),
                    "pe": str(valid[1]),
                    "source": snap["payload"]["sources"]["twse_market_report"],
                    "database_snapshot_id": snap["snapshot_id"],
                }
    failures = []
    with httpx.Client(
        timeout=40, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
    ) as client:
        for year in range(2005, 2027):
            for month in range(1, 13):
                key = f"{year}-{month:02d}"
                if key > "2026-08" or key in valuations:
                    continue
                path = CACHE / f"pe-{year}{month:02d}.zip"
                url = (
                    "https://www.twse.com.tw/rwd/staticFiles/inspection/inspection/04/001/"
                    f"{year}{month:02d}_C04001.zip"
                )
                try:
                    if not path.exists():
                        time.sleep(1.2)
                        response = client.get(url)
                        response.raise_for_status()
                        parse_market_valuation(response.content, "application/zip")
                        path.write_bytes(response.content)
                    body = path.read_bytes()
                    observed, pe = parse_market_valuation(body, "application/zip")
                    if str(observed)[:7] != key:
                        raise ValueError("report month mismatch")
                    valuations[key] = {
                        "as_of": str(observed),
                        "pe": str(pe),
                        "source": {
                            "url": url,
                            "checksum": hashlib.sha256(body).hexdigest(),
                            "observed_at": datetime.fromtimestamp(
                                path.stat().st_mtime, UTC
                            ).isoformat(),
                        },
                    }
                    print("PE", key, str(pe), flush=True)
                except Exception as exc:
                    failures.append({"month": key, "error": type(exc).__name__})
                    print("FAILED", key, type(exc).__name__, flush=True)
        for month in [6, 7, 8, 9]:
            key = f"2026{month:02d}"
            path = CACHE / f"vix-{key}.txt"
            if not path.exists():
                time.sleep(1.2)
                url = (
                    f"https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{key}new.txt"
                )
                response = client.get(url)
                response.raise_for_status()
                if not re.search(rb"2026\d{4}\s+13450000", response.content):
                    raise ValueError(f"VIX data unavailable: {key}")
                path.write_bytes(response.content)
    (CACHE / "valuations.json").write_text(
        json.dumps(valuations, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (CACHE / "collection-failures.json").write_text(json.dumps(failures), encoding="utf-8")
    print(json.dumps({"months": len(valuations), "failed": failures}), flush=True)
    with httpx.Client(timeout=40, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for year in range(2015, 2027):
            for month in range(1, 13):
                key = f"{year}{month:02d}"
                if key > "202609":
                    continue
                path = CACHE / f"taiex-{key}.json"
                if path.exists():
                    continue
                time.sleep(1.2)
                response = client.get(
                    "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST",
                    params={"date": key + "01", "response": "json"},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("stat") != "OK" or not payload.get("data"):
                    raise ValueError(f"official TAIEX history unavailable: {key}")
                path.write_bytes(response.content)
                print("TAIEX", key, flush=True)
    from run_historical_study import roc_day, yahoo

    official_days = set()
    for path in CACHE.glob("taiex-*.json"):
        official_days.update(roc_day(row[0]) for row in json.loads(path.read_bytes())["data"])
    with httpx.Client(timeout=40, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for stock, name in [("0050", "yahoo0050"), ("00631L", "yahoo00631L")]:
            prices, _, _ = yahoo(name)
            boundary_anchors = set()
            for day in official_days:
                if (
                    "2016-01-01" <= str(day) <= str(max(prices))
                    and (day not in prices or prices[day].volume == 0)
                    and day.day >= 28
                ):
                    ordinal = day.year * 12 + day.month
                    year, month0 = divmod(ordinal, 12)
                    boundary_anchors.add(f"{year}{month0 + 1:02d}")
            months = {
                d.strftime("%Y%m")
                for d in official_days
                if "2016-01-01" <= str(d) <= str(max(prices))
                and (d not in prices or prices[d].volume == 0)
            }
            full_period = {
                f"{year}{month:02d}"
                for year in range(2016, 2027)
                for month in range(1, 13)
                if f"{year}{month:02d}" <= "202609"
            }
            # Optional exhaustive crosscheck may be resumed after a rate-limit response.
            requested = (
                full_period if "--extend-official-prices" in sys.argv else {"202506", "202609"}
            )
            for month in sorted(months | boundary_anchors | requested):
                path = CACHE / f"official-{stock}-{month}.json"
                if path.exists():
                    continue
                time.sleep(1.2)
                response = client.get(
                    "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
                    params={"date": month + "01", "stockNo": stock, "response": "json"},
                )
                response.raise_for_status()
                if response.json().get("stat") != "OK":
                    raise ValueError(f"official ETF price history unavailable: {stock} {month}")
                path.write_bytes(response.content)
                print("ETF", stock, month, flush=True)


if __name__ == "__main__":
    main()
