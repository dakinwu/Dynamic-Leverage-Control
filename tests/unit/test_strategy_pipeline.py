from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services.strategy_pipeline import (
    build_strategy_inputs,
    compute_rv20,
    parse_taiwan_vix,
)


def _record(
    repo: AuditRepository,
    *,
    kind: str,
    checksum: str,
    payload: dict,
    observed_at: datetime | None = None,
    source: str | None = None,
) -> str:
    timestamp = observed_at or datetime.now(UTC)
    row, _ = repo.append_raw_snapshot(
        kind=kind,
        source=source or f"test:{kind}",
        observed_at=timestamp,
        received_at=timestamp,
        quality="authoritative",
        checksum=checksum,
        payload=payload,
    )
    return row.snapshot_id


def _vix(stamp: str, value: str) -> dict:
    """Official dated TAIFEX daily file; undated VIX text has no verifiable session."""
    return {
        "source": f"https://www.taifex.com.tw/cht/7/getVixData?filesname={stamp}",
        "payload": {"body_text": f"{stamp}\tLast 1 min AVG\t{value}\r\n"},
    }


def test_pipeline_requires_point_in_time_history() -> None:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        result = build_strategy_inputs(AuditRepository(session))
        assert result.inputs is None
        assert "taiex_close" in result.missing
        assert "rv20" in result.missing
    engine.dispose()


def test_pipeline_builds_inputs_from_authoritative_snapshots() -> None:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        repo = AuditRepository(session)
        start = date(2025, 8, 1)
        for index in range(21):
            current = start + timedelta(days=index)
            roc = f"{current.year - 1911:03d}{current.month:02d}{current.day:02d}"
            _record(
                repo,
                kind="market.taiex_index.official",
                checksum=f"taiex-{index}",
                payload={
                    "schema": "official-http-json-v1",
                    "body": [
                        {
                            "日期": roc,
                            "指數": "發行量加權股價指數",
                            "收盤指數": str(18000 + index * 10),
                            "漲跌": "+",
                            "漲跌百分比": "0.05",
                        }
                    ],
                },
                observed_at=datetime.combine(current, datetime.min.time(), tzinfo=UTC),
            )
        _record(
            repo,
            kind="market.taifex_vix.official",
            checksum="vix-1",
            **_vix("20250821", "18.5"),
            observed_at=datetime(2025, 8, 21, tzinfo=UTC),
        )
        valuation_rows = [
            {
                "as_of": date(2015 + (7 + index) // 12, (7 + index) % 12 + 1, 1).isoformat(),
                "pe_ttm": str(18 + (index % 7) * 0.5),
                "market_eps": str(700 + index),
                "source": "x",
                "source_version": "1",
            }
            for index in range(120)
        ]
        valuation_rows.append(
            {
                "as_of": "2025-08-21",
                "pe_ttm": "18",
                "market_eps": "900",
                "source": "x",
                "source_version": "1",
            }
        )
        _record(
            repo,
            kind="valuation.taiex.manual",
            checksum="valuation-1",
            payload={
                "schema": "manual-valuation-v1",
                "rows": valuation_rows,
            },
            observed_at=datetime(2025, 8, 21, tzinfo=UTC),
        )
        result = build_strategy_inputs(repo)
        assert result.inputs is not None
        assert result.inputs.taiwan_vix == Decimal("18.5")
        assert result.inputs.rv20 is not None
        assert result.inputs.eps_yoy is not None
        assert result.inputs.taiex_drawdown == Decimal("0")
    engine.dispose()


def test_vix_parser_does_not_accept_unlabelled_number() -> None:
    assert parse_taiwan_vix([]) is None
    assert compute_rv20([]) is None


def test_vix_parser_reads_taifex_download_last_minute_average() -> None:
    record = type(
        "Record",
        (),
        {"payload": {"body_text": "20260904\\tLast 1 min AVG\\t\\t\\t24.00\\r\\n"}},
    )()
    assert parse_taiwan_vix([record]) == Decimal("24.00")


def test_pipeline_does_not_count_duplicate_months_as_history() -> None:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        repo = AuditRepository(session)
        rows = [{"as_of": "2025-08-01", "pe_ttm": "20", "market_eps": "900"} for _ in range(121)]
        _record(
            repo,
            kind="valuation.taiex.manual",
            checksum="duplicate-months",
            payload={"rows": rows},
            observed_at=datetime(2025, 8, 1, tzinfo=UTC),
        )
        result = build_strategy_inputs(repo, as_of=date(2025, 8, 1))

        assert result.inputs is None
        assert "pe_percentile" in result.missing
        assert result.diagnostics["valuation_points"] == "0"
    engine.dispose()


def test_pipeline_ignores_vix_received_after_decision_date() -> None:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        repo = AuditRepository(session)
        _record(
            repo,
            kind="market.taifex_vix.official",
            checksum="known-vix",
            **_vix("20250801", "18"),
            observed_at=datetime(2025, 8, 1, tzinfo=UTC),
        )
        _record(
            repo,
            kind="market.taifex_vix.official",
            checksum="future-vix",
            **_vix("20250802", "40"),
            observed_at=datetime(2025, 8, 2, tzinfo=UTC),
        )
        result = build_strategy_inputs(repo, as_of=date(2025, 8, 1))

        assert result.diagnostics["taiwan_vix"] == "18"
    engine.dispose()


def test_pipeline_excludes_later_same_day_evidence() -> None:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with make_session_factory(engine).begin() as session:
        repo = AuditRepository(session)
        for hour, vix in [(12, "18"), (14, "40")]:
            _record(
                repo,
                kind="market.taifex_vix.official",
                checksum=f"hour-{hour}",
                **_vix("20250801", vix),
                observed_at=datetime(2025, 8, 1, hour, tzinfo=UTC),
            )
        result = build_strategy_inputs(
            repo, as_of=date(2025, 8, 1), decision_at=datetime(2025, 8, 1, 13, tzinfo=UTC)
        )
        assert result.diagnostics["taiwan_vix"] == "18"
        assert len(result.snapshot_ids) == 1
    engine.dispose()


def test_older_extra_month_does_not_fill_gap_in_required_window() -> None:
    from types import SimpleNamespace

    from dynamic_leverage.services.strategy_pipeline import _valuation_inputs

    rows = [
        {
            "as_of": date(2015 + i // 12, i % 12 + 1, 1).isoformat(),
            "pe_ttm": "20",
            "market_eps": "800",
        }
        for i in range(122)
        if i != 40
    ]
    record = SimpleNamespace(payload={"rows": rows}, snapshot_id="history")
    result = _valuation_inputs([record], date(2025, 2, 1))
    assert result[1] is None
    assert result[-1] == 119
