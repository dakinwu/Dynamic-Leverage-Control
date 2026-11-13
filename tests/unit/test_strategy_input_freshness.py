"""Stale VIX or valuation inputs must yield an invalid decision without advancing policy state."""

from datetime import UTC, date, datetime, timedelta

import pytest

from dynamic_leverage.domain.strategies import DvlV2Policy
from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.infrastructure.tables import StrategyPolicyStateRecord
from dynamic_leverage.services.scheduler import record_shadow_decision

MARKET_DATE = date(2025, 8, 21)


def _at(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


def _add(repo: AuditRepository, *, kind, source, checksum, payload, observed: date) -> None:
    repo.append_raw_snapshot(
        kind=kind,
        source=source,
        observed_at=_at(observed),
        received_at=_at(observed),
        quality="authoritative",
        checksum=checksum,
        payload=payload,
    )


def _seed(repo: AuditRepository, *, vix_date: date, valuation_end: date) -> None:
    for offset in range(21):
        day = MARKET_DATE - timedelta(days=20 - offset)
        roc = f"{day.year - 1911:03d}{day.month:02d}{day.day:02d}"
        _add(
            repo,
            kind="market.taiex_index.official",
            source="test:taiex",
            checksum=f"taiex-{offset}",
            payload={
                "body": [
                    {
                        "日期": roc,
                        "指數": "發行量加權股價指數",
                        "收盤指數": str(18000 + offset * 10),
                        "漲跌": "+",
                        "漲跌百分比": "0.05",
                    }
                ]
            },
            observed=day,
        )
    stamp = vix_date.strftime("%Y%m%d")
    _add(
        repo,
        kind="market.taifex_vix.official",
        source=f"https://www.taifex.com.tw/cht/7/getVixData?filesname={stamp}",
        checksum=f"vix-{stamp}",
        payload={"body_text": f"{stamp}\tLast 1 min AVG\t18.50\r\n"},
        observed=vix_date,
    )
    end_ordinal = valuation_end.year * 12 + valuation_end.month - 1
    rows = []
    for index in range(121):
        year, month0 = divmod(end_ordinal - 120 + index, 12)
        rows.append(
            {
                "as_of": date(year, month0 + 1, 1).isoformat(),
                "pe_ttm": str(18 + (index % 7) * 0.5),
                "market_eps": str(700 + index),
            }
        )
    _add(
        repo,
        kind="valuation.taiex.manual",
        source="test:valuation",
        checksum=f"valuation-{valuation_end}",
        payload={"rows": rows},
        observed=valuation_end,
    )


@pytest.fixture
def repo():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with make_session_factory(engine).begin() as session:
        yield AuditRepository(session)
    engine.dispose()


def _policy_state(repo: AuditRepository):
    return repo.session.get(StrategyPolicyStateRecord, DvlV2Policy.version)


def test_control_fresh_inputs_produce_valid_decision_and_advance_state(repo) -> None:
    _seed(repo, vix_date=MARKET_DATE, valuation_end=date(2025, 8, 1))

    _row, decision, processed = record_shadow_decision(repo, "run-fresh")

    assert processed.inputs is not None and processed.inputs.critical_data_safe is True
    assert decision.valid is True
    assert _policy_state(repo) is not None


def test_vix_one_session_behind_taiex_is_invalid_and_freezes_policy_state(repo) -> None:
    _seed(repo, vix_date=MARKET_DATE - timedelta(days=1), valuation_end=date(2025, 8, 1))

    row, decision, processed = record_shadow_decision(repo, "run-vix-lag")

    assert decision.valid is False
    assert row.valid is False
    assert "taiwan_vix_stale" in decision.reasons
    assert processed.diagnostics["critical_data_safe"] == "false"
    assert _policy_state(repo) is None


def test_stale_valuation_is_invalid_and_freezes_policy_state(repo) -> None:
    # Latest valuation month is 81 days before the market date.
    _seed(repo, vix_date=MARKET_DATE, valuation_end=date(2025, 6, 1))

    row, decision, processed = record_shadow_decision(repo, "run-valuation-stale")

    assert decision.valid is False
    assert row.valid is False
    assert "valuation_stale" in decision.reasons
    assert processed.diagnostics["critical_data_safe"] == "false"
    assert _policy_state(repo) is None
