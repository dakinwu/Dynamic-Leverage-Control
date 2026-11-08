from pathlib import Path

import pytest

from dynamic_leverage.infrastructure.providers.manual_valuation import load_manual_valuation


def test_load_manual_valuation_preserves_point_in_time_source(tmp_path: Path) -> None:
    path = tmp_path / "valuation.csv"
    path.write_text(
        "as_of,pe_ttm,market_eps,source,source_version\n"
        "2026-07-31,24.5,1754,TWSE-manual,download-2026-08-01\n"
        "2026-08-28,26.0,1800,TWSE-manual,download-2026-08-29\n",
        encoding="utf-8",
    )

    snapshot = load_manual_valuation(path)

    assert len(snapshot.points) == 2
    assert str(snapshot.points[-1].pe_ttm) == "26.0"
    assert snapshot.to_payload()["schema"] == "manual-valuation-v1"
    assert snapshot.observed_at.date().isoformat() == "2026-08-28"


def test_load_manual_valuation_rejects_duplicate_dates(tmp_path: Path) -> None:
    path = tmp_path / "valuation.csv"
    path.write_text(
        "as_of,pe_ttm,market_eps,source,source_version\n"
        "2026-08-28,26,1800,manual,v1\n"
        "2026-08-28,27,1800,manual,v2\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate as_of"):
        load_manual_valuation(path)
