import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.services.comparable_fundamentals import (
    company_diagnostic,
    parse_mops_monthly_revenue,
    parse_tsmc_sec_revenue,
)
from dynamic_leverage.services.financial_periods import QuarterMargin
from dynamic_leverage.services.fundamental_signal import FundamentalValue, SourceObservation

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def source(body=b"synthetic fixture", name="raw", available=NOW):
    return SourceObservation(
        name,
        "https://www.sec.gov/Archives/edgar/data/1046179/synthetic-fixture.htm",
        body,
        hashlib.sha256(body).hexdigest(),
        available,
        available,
    )


def html(current="150", prior="100"):
    return (
        "Taiwan Semiconductor Manufacturing Company Limited"
        "<p>1. Revenue (in NT$ thousands)</p><table>"
        "<tr><td>Period</td><td>Items</td><td>2026</td><td>2025</td></tr>"
        f"<tr><td>Aug.</td><td>Net Revenue</td><td>{current}</td><td>{prior}</td></tr>"
        "<tr><td>Jan.~Aug.</td><td>Net Revenue</td><td>900</td><td>900</td></tr>"
        "</table>"
    ).encode()


def fixture():
    raw, review = source(), source(name="review")
    values = [
        FundamentalValue("2330", period, kind, value, "raw", NOW, basis, "consolidated")
        for period, kind, value, basis in (
            (date(2026, 8, 31), "revenue_yoy", D("0.5"), "monthly"),
            (date(2026, 7, 31), "revenue_yoy", D("0.4"), "monthly"),
            (date(2026, 6, 30), "gross_margin", D("0.6"), "single-quarter-verified"),
            (date(2026, 3, 31), "gross_margin", D("0.5"), "single-quarter-verified"),
        )
    ]
    margins = [QuarterMargin(value, ("raw", "review"), "basis", "series") for value in values[2:]]
    return values[:2], margins, {"raw": raw, "review": review}


def test_exact_monthly_amounts_not_rounded_or_cumulative_table():
    row = parse_tsmc_sec_revenue(source(html()), date(2026, 8, 31))
    assert row.value == D("0.5")
    assert row.scope == "consolidated"


@pytest.mark.parametrize(
    "body", [html(prior="0"), html() + html(), html().replace(b"thousands", b"million")]
)
def test_invalid_denominator_ambiguous_or_unverified_unit_rejected(body):
    with pytest.raises(ValueError):
        parse_tsmc_sec_revenue(source(body), date(2026, 8, 31))


def test_wrong_issuer_or_period_is_rejected():
    with pytest.raises(ValueError, match="issuer"):
        parse_tsmc_sec_revenue(
            replace(source(html()), source_url="https://other.example/filing"), date(2026, 8, 31)
        )
    with pytest.raises(ValueError, match="unique"):
        parse_tsmc_sec_revenue(source(html()), date(2026, 7, 31))


def test_one_company_improvement_never_becomes_allocation():
    revenues, margins, sources = fixture()
    result = company_diagnostic(*revenues, *margins, sources, NOW)
    assert result["improving"]
    assert result["revenue_acceleration"] == D("0.1")
    assert not result["index_feature_ready"]
    assert result["target_exposure"] is None


def test_late_context_dependency_blocks_old_cutoff():
    revenues, margins, sources = fixture()
    sources["review"] = source(name="review", available=NOW + timedelta(days=1))
    result = company_diagnostic(*revenues, *margins, sources, NOW)
    assert result["status"] == "unavailable"
    assert result["improving"] is None


@pytest.mark.parametrize(
    "changes", [{"comparability_id": "restated"}, {"accounting_basis_id": "new-standard"}]
)
def test_cross_quarter_revision_and_accounting_basis_must_match(changes):
    revenues, margins, sources = fixture()
    with pytest.raises(ValueError, match="comparable"):
        company_diagnostic(*revenues, margins[0], replace(margins[1], **changes), sources, NOW)


def test_missing_dependency_is_not_silently_dropped():
    revenues, margins, sources = fixture()
    del sources["review"]
    with pytest.raises(ValueError, match="dependency"):
        company_diagnostic(*revenues, *margins, sources, NOW)


def mops_fixture():
    text = "上市公司115年7月份 單位：千元<table><tr>"
    headers = [
        "公司代號",
        "公司名稱",
        "當月營收",
        "上月營收",
        "去年當月營收",
        "上月比較增減(%)",
        "去年同月增減(%)",
        "當月累計營收",
        "去年累計營收",
        "前期比較增減(%)",
    ]
    text += "".join(f"<td>{value}</td>" for value in headers) + "</tr><tr>"
    text += (
        "".join(
            f"<td>{value}</td>"
            for value in [
                "2330",
                "synthetic",
                "150",
                "140",
                "100",
                "7.14",
                "50.00",
                "900",
                "500",
                "80.00",
                "-",
            ]
        )
        + "</tr></table>"
    )
    body = text.encode("cp950")
    return SourceObservation(
        "mops",
        "https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_115_7_0.html",
        body,
        hashlib.sha256(body).hexdigest(),
        NOW,
        NOW,
    )


def test_mops_exact_amount_growth_and_true_availability():
    result = parse_mops_monthly_revenue(mops_fixture(), date(2026, 7, 31))
    assert result[0].value == D("0.5")
    assert result[0].available_at == NOW
    assert result[0].scope == "source-reported"


def test_mops_requested_month_and_header_must_match():
    with pytest.raises(ValueError, match="requested month"):
        parse_mops_monthly_revenue(mops_fixture(), date(2026, 8, 31))


def test_mops_duplicate_company_is_rejected():
    original = mops_fixture()
    body = original.body + original.body
    repeated = replace(original, body=body, body_sha256=hashlib.sha256(body).hexdigest())
    with pytest.raises(ValueError, match="duplicate"):
        parse_mops_monthly_revenue(repeated, date(2026, 7, 31))


def test_mops_wrong_growth_does_not_silently_generate_feature():
    original = mops_fixture()
    body = original.body.replace(b"50.00", b"70.00")
    changed = replace(original, body=body, body_sha256=hashlib.sha256(body).hexdigest())
    with pytest.raises(ValueError, match="arithmetic mismatch"):
        parse_mops_monthly_revenue(changed, date(2026, 7, 31))
