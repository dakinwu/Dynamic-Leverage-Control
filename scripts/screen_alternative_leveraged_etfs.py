"""Official monthly trading-condition screen; never estimates strategy profitability."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import time
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

SYMBOLS = ("0050", "00631L", "00675L", "00685L", "00670L", "00647L")


def number(value):
    value = str(value).replace(",", "").strip()
    if value in {"", "--", "-"}:
        return None
    result = D(value)
    if not result.is_finite():
        raise ValueError("finite official amount required")
    return result


def parse_month(body: bytes, symbol: str, month: str) -> list[dict]:
    payload = json.loads(body)
    if payload.get("stat") != "OK" or not payload.get("data"):
        raise ValueError("nonempty successful official month required")
    year, m = int(month[:4]), int(month[4:])
    title = payload.get("title", "")
    if (
        not re.search(rf"\b{re.escape(symbol)}\b", title)
        or f"{year - 1911}年{m:02d}月" not in title
    ):
        raise ValueError("official symbol/month mismatch")
    fields = payload["fields"]
    required = {"日期", "成交股數", "成交金額", "收盤價"}
    if not required.issubset(fields) or len(fields) != len(set(fields)):
        raise ValueError("unique official date/volume/value/close fields required")
    parsed, seen = [], set()
    for row in payload["data"]:
        record = dict(zip(fields, row, strict=True))
        y, mm, dd = (int(x) for x in record["日期"].split("/"))
        day = date(y + 1911, mm, dd)
        if day.year != year or day.month != m or day in seen:
            raise ValueError("unique requested-month dates required")
        seen.add(day)
        shares, turnover, close = (
            number(record[key]) for key in ("成交股數", "成交金額", "收盤價")
        )
        if shares is None or turnover is None or shares < 0 or turnover < 0:
            raise ValueError("nonnegative observed volume and turnover required")
        if shares != shares.to_integral_value():
            raise ValueError("volume must be shares, not fractional or lot-count estimates")
        if turnover > 0 and (shares <= 0 or close is None or close <= 0):
            raise ValueError("positive traded-day price/volume required")
        parsed.append(
            {
                "day": day.isoformat(),
                "shares": str(shares),
                "turnover_twd": str(turnover),
                "unadjusted_close": None if close is None else str(close),
            }
        )
    return sorted(parsed, key=lambda item: item["day"])


def write_new(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    with path.open("x", encoding="utf-8") as target:
        target.write(text)


def capture(folder: Path, month: str):
    folder.mkdir(parents=True, exist_ok=False)
    observations, failures = [], []
    opener = build_opener(ProxyHandler({}))
    for index, symbol in enumerate(SYMBOLS):
        if index:
            time.sleep(1.5)
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={month}01&stockNo={symbol}&response=json"
        try:
            with opener.open(
                Request(url, headers={"User-Agent": "ResearchEvidence/1.0"}), timeout=30
            ) as response:
                body = response.read(5_000_001)
                received = datetime.now(UTC).isoformat()
                content_type = response.headers.get("Content-Type")
            if len(body) > 5_000_000:
                raise ValueError("bounded monthly response required")
            name = f"{symbol}-{month}.raw.json"
            with (folder / name).open("xb") as target:
                target.write(body)
            observations.append(
                {
                    "symbol": symbol,
                    "source_url": url,
                    "raw_file": name,
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                    "received_at": received,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "content_type": content_type,
                }
            )
            parse_month(body, symbol, month)
            print(f"captured {symbol}", flush=True)
        except Exception as error:
            failures.append(
                {
                    "symbol": symbol,
                    "source_url": url,
                    "error_type": type(error).__name__,
                    "message": str(error),
                }
            )
            # Stop instead of retrying/ramping up requests after rate limiting.
            break
    write_new(
        folder / "manifest.json",
        {
            "schema": "alternative-etf-official-month-capture-v1",
            "month": month,
            "observations": observations,
            "failures": failures,
        },
    )
    if failures:
        raise RuntimeError(
            "capture incomplete; inspect preserved manifest, do not infer missing data"
        )


def screen(folder: Path, output: Path):
    if output.exists():
        raise FileExistsError("use a new screen output")
    manifest = json.loads((folder / "manifest.json").read_bytes())
    results, trading_dates = [], []
    if manifest["failures"] or {entry["symbol"] for entry in manifest["observations"]} != set(
        SYMBOLS
    ):
        raise ValueError("all six declared controls/candidates required")
    for entry in manifest["observations"]:
        path = (folder / entry["raw_file"]).resolve()
        if not path.is_relative_to(folder.resolve()):
            raise ValueError("source outside capture")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["body_sha256"]:
            raise ValueError("official month fingerprint mismatch")
        rows = parse_month(body, entry["symbol"], manifest["month"])
        traded = [row for row in rows if D(row["turnover_twd"]) > 0]
        if not traded:
            raise ValueError("at least one positive-turnover date required")
        turnover = statistics.median(D(row["turnover_twd"]) for row in traded)
        median_shares = statistics.median(D(row["shares"]) for row in traded)
        trading_dates.append({row["day"] for row in rows})
        results.append(
            {
                "symbol": entry["symbol"],
                "dates": {"start": rows[0]["day"], "end": rows[-1]["day"]},
                "observed_days": len(rows),
                "positive_turnover_days": len(traded),
                "median_daily_turnover_twd": str(turnover),
                "median_daily_volume_shares": str(median_shares),
                "trade_100000_share_of_median_turnover_percent": str(D(100000) / turnover * 100),
                "trade_500000_share_of_median_turnover_percent": str(D(500000) / turnover * 100),
                "last_unadjusted_close": traded[-1]["unadjusted_close"],
                "source": entry,
            }
        )
    result = {
        "schema": "alternative-etf-trading-condition-screen-v1",
        "route_id": "R17",
        "month": manifest["month"],
        "results": results,
        "all_symbols_have_identical_observed_dates": all(
            days == trading_dates[0] for days in trading_dates
        ),
        "strategy_return_backtest_performed": False,
        "new_out_of_sample": False,
        "corporate_action_and_total_return_history_verified": False,
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "target_exposure": None,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    write_new(output, result)
    print(
        json.dumps(
            {
                "screened": len(results),
                "month": manifest["month"],
                "same_dates": result["all_symbols_have_identical_observed_dates"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--month", default="202609")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("screen output already exists")
    if not re.fullmatch(r"\d{4}(0[1-9]|1[0-2])", args.month):
        raise ValueError("YYYYMM month required")
    if args.capture:
        capture(args.folder, args.month)
    screen(args.folder, args.output)
