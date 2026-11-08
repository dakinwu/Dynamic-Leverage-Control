import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from dynamic_leverage.services.fundamental_signal import (
    FundamentalValue,
    SourceObservation,
    UniverseObservation,
    evaluate_signal,
    parse_revenue,
    roc_month,
)

NOW = datetime(2026, 10, 5, 10, tzinfo=UTC)


def source(body=b"[]", source_id="source", received=NOW, recorded=NOW):
    return SourceObservation(
        source_id,
        "https://openapi.twse.com.tw/example",
        body,
        hashlib.sha256(body).hexdigest(),
        received,
        recorded,
    )


def fixture():
    obs = source()
    universe = UniverseObservation(
        "TW50-index", NOW, "source", {str(i): D("0.02") for i in range(50)}
    )
    values = [
        FundamentalValue(
            str(i),
            period,
            kind,
            value,
            "source",
            NOW,
            basis,
            "consolidated" if kind == "gross_margin" else "source-reported",
        )
        for i in range(50)
        for period, kind, value, basis in (
            (date(2026, 8, 31), "revenue_yoy", D("0.15"), "monthly"),
            (date(2026, 7, 31), "revenue_yoy", D("0.05"), "monthly"),
            (date(2026, 6, 30), "gross_margin", D("0.3"), "single-quarter-verified"),
            (date(2026, 3, 31), "gross_margin", D("0.3"), "single-quarter-verified"),
        )
    ]
    return obs, universe, values


def evaluate(obs, universe, values, cutoff=NOW):
    return evaluate_signal(
        [obs],
        values,
        universe,
        cutoff=cutoff,
        revenue_month=date(2026, 8, 31),
        margin_quarter=date(2026, 6, 30),
    )


def test_full_verified_fixture_produces_features_but_never_exposure():
    obs, universe, values = fixture()
    result = evaluate(obs, universe, values)
    assert result["status"] == "research_feature_only"
    assert result["weighted_revenue_acceleration"] == D("0.1")
    assert result["improving_weight"] == 1
    assert result["target_exposure"] is None
    assert not result["profitability_demonstrated"]


def test_missing_company_does_not_renormalize_coverage():
    obs, universe, values = fixture()
    result = evaluate(obs, universe, values[:-4])
    assert result["covered_weight"] == D("0.98")
    assert result["status"] == "unavailable"
    assert result["weighted_revenue_acceleration"] is None


def test_latest_revision_only_applies_after_its_actual_availability():
    obs, universe, values = fixture()
    revision = source(source_id="revision", recorded=NOW + timedelta(days=1))
    later = replace(
        values[0], source_id="revision", value=D("-0.5"), available_at=revision.available_at
    )
    result = evaluate_signal(
        [obs, revision],
        values + [later],
        universe,
        cutoff=NOW,
        revenue_month=date(2026, 8, 31),
        margin_quarter=date(2026, 6, 30),
    )
    assert result["improving_weight"] == 1
    result = evaluate_signal(
        [obs, revision],
        values + [later],
        universe,
        cutoff=NOW + timedelta(days=1),
        revenue_month=date(2026, 8, 31),
        margin_quarter=date(2026, 6, 30),
    )
    assert result["improving_weight"] == D("0.98")


def test_old_receive_stamp_does_not_backdate_new_observation():
    obs = source(received=NOW - timedelta(days=365))
    assert obs.available_at == NOW
    assert evaluate(obs, None, [], cutoff=NOW - timedelta(seconds=1))["status"] == "unavailable"


@pytest.mark.parametrize("kind", ["0050_fund_holdings_proxy", "TW50-current-backfill"])
def test_wrong_universe_cannot_claim_index_signal(kind):
    obs, universe, values = fixture()
    result = evaluate(obs, replace(universe, kind=kind), values)
    assert "INVALID_OR_PARTIAL_TW50_UNIVERSE" in result["blockers"]


def test_future_universe_is_not_used_at_previous_cutoff():
    obs, universe, values = fixture()
    universe = replace(universe, effective_at=NOW + timedelta(days=1))
    assert "UNIVERSE_NOT_AVAILABLE" in evaluate(obs, universe, values)["blockers"]


def test_cumulative_or_unknown_quarter_cannot_generate_margin_improvement():
    obs, universe, values = fixture()
    values[2] = replace(values[2], period_basis="unknown")
    assert "UNVERIFIED_PERIOD_OR_SCOPE:0" in evaluate(obs, universe, values)["blockers"]


def test_conflicting_versions_at_same_time_rejected():
    obs, universe, values = fixture()
    with pytest.raises(ValueError, match="conflicting"):
        evaluate(obs, universe, values + [replace(values[0], value=D("0.2"))])


def test_record_without_matching_raw_source_rejected():
    obs, universe, values = fixture()
    values[0] = replace(values[0], source_id="missing")
    with pytest.raises(ValueError, match="availability evidence"):
        evaluate(obs, universe, values)


def test_revenue_percent_is_normalized_and_source_date_not_publication():
    body = json.dumps(
        [
            {
                "公司代號": "2330",
                "資料年月": "11508",
                "出表日期": "1150917",
                "營業收入-當月營收": "115",
                "營業收入-去年當月營收": "100",
                "營業收入-去年同月增減(%)": "15",
            }
        ]
    ).encode()
    value = parse_revenue(source(body))[0]
    assert value.value == D("0.15")
    assert value.period_end == date(2026, 8, 31)
    assert value.available_at == NOW


def test_missing_or_zero_base_does_not_become_zero_growth():
    row = {
        "公司代號": "2330",
        "資料年月": "11508",
        "營業收入-當月營收": "100",
        "營業收入-去年當月營收": "0",
        "營業收入-去年同月增減(%)": "-",
    }
    assert parse_revenue(source(json.dumps([row]).encode())) == []


def test_tampered_source_hash_rejected():
    with pytest.raises(ValueError, match="fingerprint"):
        replace(source(), body=b"changed")


@pytest.mark.parametrize("value", ["2026-08", "11513", "", "115AA"])
def test_invalid_month_rejected(value):
    with pytest.raises(ValueError):
        roc_month(value)


def test_naive_timestamp_rejected():
    with pytest.raises(ValueError, match="timezone"):
        source(recorded=NOW.replace(tzinfo=None))


def test_financial_company_uses_explicitly_mapped_comparable_net_margin():
    obs, universe, values = fixture()
    universe = replace(universe, profitability_metrics={"0": "net_margin"})
    values[2] = replace(values[2], kind="net_margin")
    values[3] = replace(values[3], kind="net_margin")
    result = evaluate(obs, universe, values)
    assert result["status"] == "research_feature_only"
    assert result["components"][0]["profitability_metric"] == "net_margin"


def test_unknown_statement_scope_does_not_pass_margin_validation():
    obs, universe, values = fixture()
    values[2] = replace(values[2], scope="unknown")
    assert "UNVERIFIED_PERIOD_OR_SCOPE:0" in evaluate(obs, universe, values)["blockers"]
