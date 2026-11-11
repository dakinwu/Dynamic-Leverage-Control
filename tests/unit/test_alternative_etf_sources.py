import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "research" / f"{name}.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


screen = module("screen_alternative_leveraged_etfs")
nav = module("audit_alternative_nav")


def month_payload():
    return {
        "stat": "OK",
        "title": "115年09月 00670L synthetic fixture",
        "fields": ["日期", "成交股數", "成交金額", "收盤價"],
        "data": [["115/09/01", "1,000", "100,000", "100"]],
    }


def test_official_month_shares_and_twd_are_preserved():
    result = screen.parse_month(json.dumps(month_payload()).encode(), "00670L", "202609")
    assert result[0]["shares"] == "1000"
    assert result[0]["turnover_twd"] == "100000"
    assert result[0]["day"] == "2026-09-01"


@pytest.mark.parametrize("change", ["symbol", "month", "duplicate-date", "price", "nonfinite"])
def test_mismatched_or_invalid_month_evidence_rejected(change):
    payload = month_payload()
    if change == "symbol":
        payload["title"] = "115年09月 00675L"
    elif change == "month":
        payload["data"][0][0] = "115/08/31"
    elif change == "duplicate-date":
        payload["data"] *= 2
    elif change == "price":
        payload["data"][0][-1] = "--"
    else:
        payload["data"][0][1] = "NaN"
    with pytest.raises(ValueError):
        screen.parse_month(json.dumps(payload).encode(), "00670L", "202609")


def test_nav_parser_preserves_exact_dates_and_units_without_return_inference():
    rows = nav.parse_nav(b"var PNavStr='2017,02,14,20.000000,2017,02,15,20.010000';")
    assert rows[0] == {"day": "2017-02-14", "reported_unit_nav": "20.000000"}
    assert len(rows) == 2


@pytest.mark.parametrize(
    "value", ["2017,02,14", "2017,02,14,0", "2017,02,14,NaN", "2017,02,14,20,2017,02,14,21"]
)
def test_incomplete_invalid_or_duplicate_nav_is_rejected(value):
    with pytest.raises(ValueError):
        nav.parse_nav(f"var PNavStr='{value}';".encode())


@pytest.mark.parametrize(
    "body", [b"var stkId='00675L';var PNavStr='2017,02,14,20';", b"var PNavStr='2017,02,14,20';"]
)
def test_issuer_cannot_return_another_fund_or_no_identity(body):
    with pytest.raises(ValueError, match="identity"):
        nav.parse_nav(body, "00670L")
