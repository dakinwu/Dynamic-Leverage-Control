"""Preserve official forward observations in a NEW local research directory only."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path

from dynamic_leverage.services.fundamental_signal import (
    SourceObservation,
    evaluate_signal,
    parse_revenue,
)
from dynamic_leverage.services.research_artifacts import json_report

ROOT = Path(__file__).resolve().parents[2]


ENDPOINTS = (
    "t187ap05_L",
    "t187ap06_L_ci",
    "t187ap06_L_basi",
    "t187ap06_L_fh",
    "t187ap06_L_ins",
    "t187ap06_L_bd",
    "t187ap06_L_mim",
)


def run(output: Path, direct: bool):
    if output.exists():
        raise FileExistsError("choose a new directory; old observations are immutable")
    output.mkdir(parents=True)
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if direct
        else (urllib.request.build_opener())
    )
    sources, observations, values, failures = [], [], [], []
    for endpoint in ENDPOINTS:
        url = f"https://openapi.twse.com.tw/v1/opendata/{endpoint}"
        try:
            with opener.open(url, timeout=45) as response:
                body = response.read()
                received_at = datetime.now(UTC)
            # Preserve even an invalid response as evidence, not a successful dataset.
            with (output / f"{endpoint}.raw.json").open("xb") as target:
                target.write(body)
            recorded_at = datetime.now(UTC)
            digest = hashlib.sha256(body).hexdigest()
            source_id = f"{endpoint}:{digest}:{recorded_at.isoformat()}"
            obs = SourceObservation(source_id, url, body, digest, received_at, recorded_at)
            parsed = json.loads(body)
            if not isinstance(parsed, list) or not parsed:
                raise ValueError("official response is not a nonempty array")
            sources.append(obs)
            observations.append(
                {
                    "source_id": source_id,
                    "source_url": url,
                    "body_sha256": digest,
                    "raw_file": f"{endpoint}.raw.json",
                    "received_at": received_at.isoformat(),
                    "recorded_at": recorded_at.isoformat(),
                    "available_at": obs.available_at.isoformat(),
                    "published_at": None,
                    "source_table_dates": sorted({r.get("出表日期", "") for r in parsed}),
                    "row_count": len(parsed),
                    "period_basis": "monthly" if endpoint == "t187ap05_L" else "unknown",
                    "source_unit": "unknown",
                    "company_codes": sorted({r.get("公司代號", "") for r in parsed}),
                }
            )
            if endpoint == "t187ap05_L":
                values.extend(parse_revenue(obs))
            print(f"captured {endpoint}: {len(parsed)} rows", flush=True)
        except Exception as error:
            failures.append(
                {"endpoint": endpoint, "error_type": type(error).__name__, "message": str(error)}
            )
            print(f"failed {endpoint}: {type(error).__name__}", flush=True)
    cutoff = datetime.now(UTC)
    month = max((v.period_end for v in values), default=date(cutoff.year, 1, 31))
    quarter_month = ((month.month - 1) // 3) * 3
    quarter = date(month.year if quarter_month else month.year - 1, quarter_month or 12, 1)
    import calendar

    quarter = quarter.replace(day=calendar.monthrange(quarter.year, quarter.month)[1])
    signal = evaluate_signal(
        sources, values, None, cutoff=cutoff, revenue_month=month, margin_quarter=quarter
    )
    report = {
        "schema": "fundamental-forward-capture-v1",
        "captured_at": cutoff.isoformat(),
        "proxy_policy": "explicit-direct-for-this-process" if direct else "environment-default",
        "observations": observations,
        "failures": failures,
        "normalized_revenue_rows": len(values),
        "revenue_months": sorted({v.period_end.isoformat() for v in values}),
        "financial_period_semantics_verified": False,
        "historical_publication_evidence_ready": False,
        "historical_research_ready": False,
        "profitability_demonstrated": False,
        "signal": signal,
        "source_sha256": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in (
                "src/dynamic_leverage/services/fundamental_signal.py",
                "scripts/capture_fundamental_evidence.py",
            )
        },
    }
    (output / "observations.json").write_text(
        json.dumps(report, default=json_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    serialized = [
        {
            **vars(v),
            "available_at": v.available_at.isoformat(),
            "value": str(v.value),
            "period_end": v.period_end.isoformat(),
        }
        for v in values
    ]
    (output / "revenue-values.json").write_text(
        json.dumps(serialized, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if failures:
        raise RuntimeError("one or more sources failed; inspect preserved observations.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--direct",
        action="store_true",
        help="explicitly bypass environment proxy for this research process only",
    )
    args = parser.parse_args()
    run(args.output, args.direct)
