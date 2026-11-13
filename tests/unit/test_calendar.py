import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from dynamic_leverage.services import calendar
from dynamic_leverage.services.calendar import load_calendars


def _write_year(directory: Path, year: int, rows: list[list[str]]) -> dict[str, object]:
    name = f"twse-calendar-{year}.json"
    body = json.dumps(
        {"stat": "ok", "data": rows, "queryYear": year, "total": len(rows)},
        ensure_ascii=False,
    ).encode("utf-8")
    (directory / name).write_bytes(body)
    return {
        "year": year,
        "file": name,
        "source": f"https://example.invalid/{year}",
        "retrieved_at": "2026-11-09",
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _write_provenance(directory: Path, entries: list[dict[str, object]]) -> None:
    (directory / "calendar-provenance.json").write_text(
        json.dumps({"calendars": entries, "scope": "test"}), encoding="utf-8"
    )


def test_loads_every_year_listed_in_provenance(tmp_path: Path) -> None:
    _write_provenance(
        tmp_path,
        [
            _write_year(
                tmp_path,
                2026,
                [["2026-10-09", "國慶日", ""], ["2026-01-02", "國曆新年開始交易日", ""]],
            ),
            _write_year(tmp_path, 2027, [["2027-02-08", "農曆春節", ""]]),
        ],
    )

    years, holidays = load_calendars(tmp_path)

    assert years == frozenset({2026, 2027})
    assert holidays == frozenset({date(2026, 10, 9), date(2027, 2, 8)})


def test_rejects_calendar_whose_bytes_differ_from_provenance(tmp_path: Path) -> None:
    entry = _write_year(tmp_path, 2027, [["2027-02-08", "農曆春節", ""]])
    entry["sha256"] = "0" * 64
    _write_provenance(tmp_path, [entry])

    with pytest.raises(ValueError, match="sha256"):
        load_calendars(tmp_path)


def test_rejects_calendar_whose_year_differs_from_provenance(tmp_path: Path) -> None:
    entry = _write_year(tmp_path, 2026, [["2026-10-09", "國慶日", ""]])
    entry["year"] = 2027
    _write_provenance(tmp_path, [entry])

    with pytest.raises(ValueError, match="queryYear"):
        load_calendars(tmp_path)


def test_shipped_calendar_covers_2026_and_treats_unpublished_years_as_unverified() -> None:
    assert calendar.calendar_verified(date(2026, 10, 9)) is True
    assert calendar.is_trading_day(date(2026, 10, 9)) is False
    assert calendar.is_trading_day(date(2026, 1, 2)) is True
    # The official 2027 schedule was not yet published on 2026-11-09.
    assert calendar.calendar_verified(date(2027, 1, 4)) is False
