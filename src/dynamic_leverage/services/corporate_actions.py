"""Where corporate-action facts come from.

Splits: the reviewed registry ``reference/corporate-actions.json``, pinned by
``corporate-actions-provenance.json``. Dividends: the TWSE ETF distribution
table, captured as immutable raw snapshots and parsed strictly. A missing or
unparseable capture is reported, never read as "no dividend".
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dynamic_leverage.domain.corporate_actions import DividendAction, SplitAction
from dynamic_leverage.infrastructure.providers.official_distributions import parse_distributions
from dynamic_leverage.infrastructure.repository import AuditRepository

REFERENCE_DIR = Path(__file__).parents[1] / "reference"
DISTRIBUTION_KIND = "corporate_action.twse_distributions"
DISTRIBUTION_ENDPOINT = "https://www.twse.com.tw/en/ETFortune-institute/dividendList"
DISTRIBUTION_MAX_AGE = timedelta(days=7)


def load_split_registry(reference_dir: Path = REFERENCE_DIR) -> tuple[SplitAction, ...]:
    """Reviewed splits; an edit without updating the provenance hash is refused."""
    provenance = json.loads((reference_dir / "corporate-actions-provenance.json").read_bytes())
    body = (reference_dir / provenance["file"]).read_bytes()
    # Hash with LF line endings so a Windows checkout (core.autocrlf) still verifies.
    if hashlib.sha256(body.replace(b"\r\n", b"\n")).hexdigest() != provenance["sha256"]:
        raise ValueError("corporate-actions.json: sha256 differs from its provenance")
    registry = json.loads(body)
    return tuple(
        SplitAction(
            symbol=entry["symbol"],
            ratio=entry["ratio"],
            last_trade_date=date.fromisoformat(entry["last_trade_date"]),
            halt_start=date.fromisoformat(entry["halt_start"]),
            halt_end=date.fromisoformat(entry["halt_end"]),
            resume_date=date.fromisoformat(entry["resume_date"]),
        )
        for entry in registry["splits"]
    )


def distribution_url(symbol: str, year: int) -> str:
    """Last, current and next year: next year's first ex-date is often announced early."""
    return f"{DISTRIBUTION_ENDPOINT}?startDate={year - 1}&endDate={year + 1}&stkNo={symbol}"


def distribution_actions(
    repository: AuditRepository, symbols: list[str], *, today: date
) -> tuple[list[DividendAction], list[str]]:
    """Dividend facts from the newest capture per symbol, plus problems to surface."""
    latest = {}
    for record in repository.raw_snapshots_for_kind(kind=DISTRIBUTION_KIND, limit=500):
        symbol = parse_qs(urlsplit(record.source).query).get("stkNo", [""])[0]
        if symbol in symbols and symbol not in latest:
            latest[symbol] = record  # Records arrive newest first.
    actions: list[DividendAction] = []
    problems: list[str] = []
    for symbol in symbols:
        record = latest.get(symbol)
        if record is None:
            problems.append(f"{symbol}: no distribution capture")
            continue
        if today - record.received_at.date() > DISTRIBUTION_MAX_AGE:
            problems.append(f"{symbol}: distribution capture older than 7 days")
        query = parse_qs(urlsplit(record.source).query)
        try:
            events = parse_distributions(
                str((record.payload or {}).get("body_text", "")).encode("utf-8"),
                record.source,
                symbol,
                int(query["startDate"][0]),
                int(query["endDate"][0]),
            )
        except (ValueError, KeyError, IndexError) as exc:
            problems.append(f"{symbol}: distribution capture unreadable ({exc})")
            continue
        actions.extend(
            DividendAction(
                symbol=symbol,
                ex_date=event.ex_date,
                payment_date=event.payment_date,
                amount_per_unit=event.amount,
                source=f"raw_snapshot:{record.snapshot_id}",
            )
            for event in events.values()
        )
    return sorted(actions, key=lambda action: (action.ex_date, action.symbol)), problems
