import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.services.financial_periods import (
    FinancialContext,
    general_quarter_margin,
    parse_statements,
)
from dynamic_leverage.services.fundamental_signal import SourceObservation

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def source(name, body=b"synthetic context fixture", available=NOW):
    return SourceObservation(
        name,
        "https://official.example/fixture",
        body,
        hashlib.sha256(body).hexdigest(),
        available,
        available,
    )


def statement(quarter, revenue, gross, basis="year-to-date"):
    body = json.dumps(
        [
            {
                "出表日期": "1151005",
                "年度": "115",
                "季別": str(quarter),
                "公司代號": "2330",
                "公司名稱": "synthetic fixture",
                "營業收入": str(revenue),
                "營業毛利（毛損）淨額": str(gross),
                "基本每股盈餘（元）": "15",
            }
        ]
    ).encode()
    observation = source(f"q{quarter}", body)
    evidence = source(f"context-q{quarter}")
    parsed = parse_statements(observation, "ci")[0]
    month = quarter * 3
    import calendar

    context = FinancialContext(
        "2330",
        observation.source_id,
        observation.body_sha256,
        evidence.source_id,
        date(2026, 1 if basis == "year-to-date" else month - 2, 1),
        date(2026, month, calendar.monthrange(2026, month)[1]),
        basis,
        "TWD-thousand",
        "consolidated",
        "synthetic-accounting-basis",
        "synthetic-comparable-series",
        NOW,
    )
    return parsed, context, {observation.source_id: observation, evidence.source_id: evidence}


def test_ytd_amounts_convert_to_single_quarter_not_ytd_margin_change():
    q1, c1, sources1 = statement(1, 50, 10)
    q2, c2, sources2 = statement(2, 200, 60)
    result = general_quarter_margin(q2, c2, {**sources1, **sources2}, NOW, preceding=(q1, c1))
    assert result.value.value == D(50) / 150
    assert result.value.value != D(60) / 200
    assert result.value.period_end == date(2026, 6, 30)
    assert len(result.dependency_source_ids) == 4
    assert result.value.kind == "gross_margin"  # EPS is not differenced into a new EPS.


def test_verified_single_quarter_does_not_require_preceding_statement():
    row, context, sources = statement(2, 150, 50, basis="single-quarter")
    assert general_quarter_margin(row, context, sources, NOW).value.value == D(50) / 150


@pytest.mark.parametrize(
    "change",
    [
        {"basis": "unknown"},
        {"amount_unit": "unknown"},
        {"scope": "unknown"},
        {"period_start": date(2026, 4, 1)},
        {"statement_body_sha256": "wrong"},
    ],
)
def test_unverified_or_mismatched_context_does_not_produce_signal(change):
    row, context, sources = statement(2, 200, 60)
    with pytest.raises(ValueError):
        general_quarter_margin(row, replace(context, **change), sources, NOW)


def test_period_label_cannot_be_its_own_verification_document():
    row, context, sources = statement(2, 200, 60)
    with pytest.raises(ValueError, match="its own"):
        general_quarter_margin(
            row, replace(context, evidence_source_id=row.source_id), sources, NOW
        )


def test_ytd_requires_available_comparable_preceding_version():
    row, context, sources = statement(2, 200, 60)
    with pytest.raises(ValueError, match="preceding year-to-date"):
        general_quarter_margin(row, context, sources, NOW)
    old, old_context, old_sources = statement(1, 50, 10)
    with pytest.raises(ValueError, match="not comparable"):
        general_quarter_margin(
            row,
            context,
            {**sources, **old_sources},
            NOW,
            preceding=(old, replace(old_context, scope="standalone")),
        )


def test_future_context_evidence_not_used_before_available_time():
    row, context, sources = statement(1, 50, 10)
    sources[context.evidence_source_id] = source(
        context.evidence_source_id, available=NOW + timedelta(days=1)
    )
    with pytest.raises(ValueError, match="not available"):
        general_quarter_margin(row, context, sources, NOW)
    assert general_quarter_margin(
        row, context, sources, NOW + timedelta(days=1)
    ).value.available_at == NOW + timedelta(days=1)


def test_banks_do_not_inherit_general_company_gross_margin_mapping():
    row, context, sources = statement(1, 50, 10)
    with pytest.raises(ValueError, match="financial-industry"):
        general_quarter_margin(replace(row, kind="basi"), context, sources, NOW)


def test_zero_quarter_revenue_rejected():
    row, context, sources = statement(1, 0, 10)
    with pytest.raises(ValueError, match="positive"):
        general_quarter_margin(row, context, sources, NOW)


def test_different_accounting_versions_cannot_be_differenced():
    old, old_context, old_sources = statement(1, 50, 10)
    row, context, sources = statement(2, 200, 60)
    with pytest.raises(ValueError, match="not comparable"):
        general_quarter_margin(
            row,
            context,
            {**sources, **old_sources},
            NOW,
            preceding=(old, replace(old_context, comparability_id="unreconciled-revision")),
        )


def test_context_not_used_before_manual_verification():
    row, context, sources = statement(1, 50, 10)
    later = NOW + timedelta(days=1)
    with pytest.raises(ValueError, match="not available"):
        general_quarter_margin(row, replace(context, verified_at=later), sources, NOW)
    assert (
        general_quarter_margin(
            row, replace(context, verified_at=later), sources, later
        ).value.available_at
        == later
    )
