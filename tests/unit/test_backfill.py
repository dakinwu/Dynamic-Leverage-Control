from datetime import date

from dynamic_leverage.infrastructure.database import Base, make_engine
from dynamic_leverage.services.backfill import (
    _pending_months,
    _record_attempt,
    _requested_months,
    _valuation_month,
)


def test_requested_months_spans_year_boundary() -> None:
    assert _requested_months(3, date(2020, 2, 1)) == {
        (2019, 12),
        (2020, 1),
        (2020, 2),
    }


def test_valuation_month_reads_normalized_as_of() -> None:
    assert _valuation_month(
        {"rows": [{"as_of": "2020-02-28", "pe_ttm": "20", "market_eps": "800"}]}
    ) == (2020, 2)
    assert _valuation_month({"rows": []}) is None


def test_incomplete_month_must_be_retried() -> None:
    assert _valuation_month({"rows": [{"as_of": "2020-02-28"}]}) is None


def test_cross_period_derived_eps_must_be_retried() -> None:
    assert (
        _valuation_month(
            {
                "rows": [
                    {
                        "as_of": "2020-02-28",
                        "pe_ttm": "20",
                        "market_eps": "800",
                        "source_version": "2020-02-29:2020-03-10",
                    }
                ]
            }
        )
        is None
    )


def test_failed_first_batch_does_not_starve_later_months(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'backfill.db'}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    missing = {(2020, month) for month in range(1, 9)}
    first = _pending_months(database_url, missing)[:6]
    for year, month in first:
        _record_attempt(database_url, year, month)
    # A fresh call simulates another scheduler process after all first six failed.
    assert _pending_months(database_url, missing)[:2] == [(2020, 7), (2020, 8)]


def test_window_has_120_months_before_latest_published_month() -> None:
    requested = _requested_months(121, date(2026, 8, 1))
    assert min(requested) == (2016, 8)
    assert max(requested) == (2026, 8)
    assert len(requested - {(2026, 8)}) == 120
