from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# Frozen official annual schedule, shipped with its provenance. Unknown years
# retain weekday scheduling, but freshness must explicitly fail closed.
_CALENDAR = json.loads(
    (Path(__file__).parents[1] / "reference/twse-calendar-2026.json").read_bytes()
)
KNOWN_YEARS = frozenset({int(_CALENDAR["queryYear"])})
KNOWN_HOLIDAYS = frozenset(
    date.fromisoformat(row[0])
    for row in _CALENDAR["data"]
    if "開始交易日" not in row[1] and "最後交易日" not in row[1]
)


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


def next_session_open(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    candidate = current.replace(hour=9, minute=15, second=0, microsecond=0)
    if not is_trading_day(candidate.date()) or current >= candidate:
        day = next_trading_day(current.date())
        candidate = datetime.combine(day, time(9, 15), tzinfo=TAIPEI_TZ)
    return candidate


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
