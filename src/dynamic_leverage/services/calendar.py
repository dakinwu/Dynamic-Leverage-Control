from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
REFERENCE_DIR = Path(__file__).parents[1] / "reference"


def load_calendars(reference_dir: Path = REFERENCE_DIR) -> tuple[frozenset[int], frozenset[date]]:
    """Load every frozen official annual schedule listed in the provenance file.

    Each year must match its recorded sha256 and ``queryYear``; a mismatch is a
    packaging defect and raises instead of silently trusting edited data.
    """
    provenance = json.loads((reference_dir / "calendar-provenance.json").read_bytes())
    years: set[int] = set()
    holidays: set[date] = set()
    for entry in provenance["calendars"]:
        body = (reference_dir / entry["file"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise ValueError(f"{entry['file']}: sha256 differs from calendar provenance")
        schedule = json.loads(body)
        if int(schedule["queryYear"]) != int(entry["year"]):
            raise ValueError(f"{entry['file']}: queryYear differs from calendar provenance")
        years.add(int(entry["year"]))
        holidays.update(
            date.fromisoformat(row[0])
            for row in schedule["data"]
            if "開始交易日" not in row[1] and "最後交易日" not in row[1]
        )
    return frozenset(years), frozenset(holidays)


# Frozen official annual schedules, shipped with their provenance. Unknown years
# retain weekday scheduling, but freshness must explicitly fail closed.
KNOWN_YEARS, KNOWN_HOLIDAYS = load_calendars()


def calendar_verified(value: date) -> bool:
    return value.year in KNOWN_YEARS


def expected_close_date(now: datetime | None = None) -> date:
    """Latest session due by the conservative 15:00 publication cutoff."""
    current = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    day = current.date() if current.hour >= 15 else current.date() - timedelta(days=1)
    while not is_trading_day(day):
        day -= timedelta(days=1)
    return day


def is_trading_day(value: date) -> bool:
    return value.weekday() < 5 and value not in KNOWN_HOLIDAYS


def next_trading_day(value: date) -> date:
    candidate = value + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def previous_trading_day(value: date) -> date:
    candidate = value - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def next_session_open(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    candidate = current.replace(hour=9, minute=15, second=0, microsecond=0)
    if not is_trading_day(candidate.date()) or current >= candidate:
        day = next_trading_day(current.date())
        candidate = datetime.combine(day, time(9, 15), tzinfo=TAIPEI_TZ)
    return candidate


ACCOUNT_SYNC_OPENS = time(9, 5)


def account_sync_due(now: datetime, last_synced_on: date | None) -> bool:
    """Once per trading day from 09:05 Taipei, until it has succeeded that day.

    Unlike the short ``account-sync`` slot, this catches up on the first cycle
    after a missed window, whatever the scheduler interval is.
    """
    current = now.astimezone(TAIPEI_TZ)
    return (
        is_trading_day(current.date())
        and current.time() >= ACCOUNT_SYNC_OPENS
        and last_synced_on != current.date()
    )


def next_account_sync(now: datetime | None = None) -> datetime:
    """Return the next read-only broker sync window (09:05 Taipei time)."""
    current = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    candidate = current.replace(hour=9, minute=5, second=0, microsecond=0)
    if not is_trading_day(candidate.date()) or current >= candidate:
        day = next_trading_day(current.date())
        candidate = datetime.combine(day, time(9, 5), tzinfo=TAIPEI_TZ)
    return candidate


def next_close_run(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    candidate = current.replace(hour=15, minute=0, second=0, microsecond=0)
    if not is_trading_day(candidate.date()) or current >= candidate:
        day = next_trading_day(current.date())
        candidate = datetime.combine(day, time(15, 0), tzinfo=TAIPEI_TZ)
    return candidate


def schedule_slot(now: datetime | None = None) -> str | None:
    """Return the one daily execution window that is currently due."""
    current = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    if not is_trading_day(current.date()):
        return None
    if current.hour > 15 or (current.hour == 15 and current.minute >= 0):
        return f"close:{current.date().isoformat()}"
    if current.hour == 9 and current.minute >= 5 and current.minute < 15:
        return f"account-sync:{current.date().isoformat()}"
    if current.hour > 9 or (current.hour == 9 and current.minute >= 15):
        return f"paper:{current.date().isoformat()}"
    return None
