"""Apply due corporate actions to the strategy ledger, and guard fills against them.

Runs inside account sync, before the broker mark, in one compare-and-swap write:
registered splits are applied only when the broker already shows the new units;
official dividends become receivables at the ex-date and cash at the payment
date. Every phase has a deterministic event key, so reruns never double-book.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from dynamic_leverage.domain.corporate_actions import (
    DividendAction,
    FillRecord,
    SplitAction,
    units_held_at_open,
)
from dynamic_leverage.domain.ledger import (
    StrategyLedgerState,
    accrue_dividend,
    apply_split,
    pay_dividend,
)
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.infrastructure.tables import StrategyLedgerEventRecord
from dynamic_leverage.services.calendar import next_trading_day

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
# A registered split must appear at the broker within this many sessions of resume.
SPLIT_REFLECTION_SESSIONS = 3


@dataclass(frozen=True, slots=True)
class PendingEvent:
    event_key: str
    event_type: str
    payload: dict[str, object]


def _taipei_day(moment: datetime) -> date:
    return moment.astimezone(TAIPEI_TZ).date()


def fill_records(repository: AuditRepository) -> list[FillRecord]:
    rows = repository.session.scalars(
        select(StrategyLedgerEventRecord).where(StrategyLedgerEventRecord.event_type == "FILL")
    )
    return [
        FillRecord(
            symbol=str(row.payload["instrument"]),
            side=str(row.payload["side"]),
            quantity=Decimal(str(row.payload["quantity"])),
            occurred_on=_taipei_day(row.occurred_at),
        )
        for row in rows
    ]


def _split_deadline(split: SplitAction) -> date:
    day = split.resume_date
    for _ in range(SPLIT_REFLECTION_SESSIONS):
        day = next_trading_day(day)
    return day


def apply_due_corporate_actions(
    repository: AuditRepository,
    state: StrategyLedgerState,
    *,
    at: datetime,
    broker_positions: dict[str, Decimal],
    splits: tuple[SplitAction, ...],
    dividends: list[DividendAction],
) -> tuple[StrategyLedgerState, list[PendingEvent], list[str]]:
    """Return the updated state, the events to append, and reconciliation breaks."""
    sync_day = _taipei_day(at)
    started_on = _taipei_day(state.initialized_at)
    events: list[PendingEvent] = []
    breaks: list[str] = []

    def booked(event_key: str, facts: dict[str, str], action_id: str) -> bool:
        existing = repository.strategy_ledger_event(event_key)
        if existing is None:
            return False
        if existing.payload.get("facts") != facts:
            breaks.append(f"corporate-action-facts-changed ({action_id})")
        return True

    def event(key: str, event_type: str, action_id: str, kind: str, symbol: str, **fields):
        events.append(
            PendingEvent(
                event_key=key,
                event_type=event_type,
                payload={"action_id": action_id, "kind": kind, "symbol": symbol, **fields},
            )
        )

    applied: list[SplitAction] = []
    for split in sorted(splits, key=lambda item: item.halt_start):
        key = f"CA:{split.action_id}:SPLIT"
        if booked(key, split.facts(), split.action_id):
            applied.append(split)
            continue
        held = state.positions.get(split.symbol, Decimal("0"))
        if split.halt_start <= started_on or held == 0 or sync_day < split.halt_start:
            continue  # Ledger began after it, never held it, or it has not started.
        if broker_positions.get(split.symbol) == held * split.ratio:
            state = apply_split(state, symbol=split.symbol, ratio=split.ratio, at=at)
            applied.append(split)
            event(
                key,
                "SPLIT",
                split.action_id,
                "SPLIT",
                split.symbol,
                facts=split.facts(),
                units_before=str(held),
                units_after=str(state.positions[split.symbol]),
                source="reference/corporate-actions.json",
            )
        elif sync_day > _split_deadline(split):
            breaks.append(f"split-not-reflected-by-broker ({split.action_id})")

    fills = fill_records(repository)
    for dividend in dividends:
        if dividend.ex_date <= started_on or dividend.ex_date > sync_day:
            continue
        action_id = dividend.action_id
        entitlement_key = f"CA:{action_id}:ENTITLEMENT"
        if booked(entitlement_key, dividend.facts(), action_id):
            if any(action_id in reason for reason in breaks):
                continue  # Facts changed after booking: do not pay on stale facts.
        else:
            units = units_held_at_open(
                dividend.ex_date,
                symbol=dividend.symbol,
                current_units=state.positions.get(dividend.symbol, Decimal("0")),
                fills=fills,
                splits=applied,
            )
            if units == 0:
                continue
            amount = units * dividend.amount_per_unit
            state = accrue_dividend(state, action_id=action_id, amount=amount, at=at)
            event(
                entitlement_key,
                "DIVIDEND_ENTITLEMENT",
                action_id,
                "DIVIDEND",
                dividend.symbol,
                facts=dividend.facts(),
                record_units=str(units),
                amount=str(amount),
                source=dividend.source,
            )
        payment_key = f"CA:{action_id}:PAYMENT"
        if (
            dividend.payment_date <= sync_day
            and action_id in state.receivables
            and repository.strategy_ledger_event(payment_key) is None
        ):
            amount = state.receivables[action_id]
            state = pay_dividend(state, action_id=action_id, at=at)
            event(
                payment_key,
                "DIVIDEND_PAYMENT",
                action_id,
                "DIVIDEND",
                dividend.symbol,
                facts=dividend.facts(),
                amount=str(amount),
                confirmed=False,
                source=dividend.source,
            )
    return state, events, breaks


def unregistered_ratio_reasons(
    ledger_positions: dict[str, Decimal], broker_positions: dict[str, Decimal]
) -> list[str]:
    """Whole multiples the registry does not explain: likely an unregistered split."""
    reasons = []
    for symbol, held in sorted(ledger_positions.items()):
        observed = broker_positions.get(symbol, Decimal("0"))
        if held > 0 and observed > held and observed % held == 0:
            reasons.append(f"unregistered-quantity-ratio ({symbol} x{observed // held})")
    return reasons


def fill_conflict(
    repository: AuditRepository,
    state: StrategyLedgerState,
    *,
    instrument: str,
    occurred_at: datetime,
    splits: tuple[SplitAction, ...],
) -> str | None:
    """Why a fill at this time cannot be booked without corrupting applied actions."""
    occurred_on = _taipei_day(occurred_at)
    rows = repository.session.scalars(
        select(StrategyLedgerEventRecord).where(
            StrategyLedgerEventRecord.event_key.startswith(f"CA:{instrument}:"),
            StrategyLedgerEventRecord.event_type.in_(("SPLIT", "DIVIDEND_ENTITLEMENT")),
        )
    )
    for row in rows:
        facts = row.payload.get("facts", {})
        action_id = row.payload.get("action_id")
        if row.event_type == "SPLIT" and occurred_on <= date.fromisoformat(
            facts["last_trade_date"]
        ):
            return f"fill predates applied corporate action {action_id} (pre-split units)"
        if row.event_type == "DIVIDEND_ENTITLEMENT" and occurred_on < date.fromisoformat(
            facts["ex_date"]
        ):
            return f"fill predates applied corporate action {action_id} (booked entitlement)"
    started_on = _taipei_day(state.initialized_at)
    for split in splits:
        if (
            split.symbol == instrument
            and split.halt_start <= occurred_on
            and split.halt_start > started_on
            and state.positions.get(instrument, Decimal("0")) > 0
            and repository.strategy_ledger_event(f"CA:{split.action_id}:SPLIT") is None
        ):
            return (
                f"corporate action {split.action_id} is not applied yet; "
                "sync the account before booking post-split fills"
            )
    return None
