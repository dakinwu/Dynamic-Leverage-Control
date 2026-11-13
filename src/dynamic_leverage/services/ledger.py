from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from dynamic_leverage.domain.ledger import (
    StrategyLedgerState,
    apply_strategy_fill,
    confirm_dividend,
)
from dynamic_leverage.infrastructure.repository import AuditRepository, LedgerWriteConflict
from dynamic_leverage.services.corporate_actions import load_split_registry
from dynamic_leverage.services.ledger_actions import fill_conflict

ALLOWED_LEDGER_INSTRUMENTS = frozenset({"0050", "00631L", "00685L"})
MAX_LEDGER_WRITE_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class RecordFillResult:
    created: bool
    state: StrategyLedgerState


def record_strategy_fill(
    session: Session,
    *,
    execution_id: str,
    instrument: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    commission: Decimal,
    tax: Decimal,
    occurred_at: datetime,
    note: str,
) -> RecordFillResult:
    if instrument not in ALLOWED_LEDGER_INSTRUMENTS:
        raise ValueError("instrument is not part of the strategy execution recipes")
    repository = AuditRepository(session)
    event_key = f"FILL:{execution_id}"
    fill = {
        "instrument": instrument,
        "side": side.upper(),
        "quantity": quantity,
        "price": price,
        "commission": commission,
        "tax": tax,
    }
    for _attempt in range(MAX_LEDGER_WRITE_ATTEMPTS):
        existing = repository.strategy_ledger_event(event_key)
        if existing is not None:
            if not _same_fill(existing, fill, occurred_at):
                raise ValueError(
                    f"execution_id {execution_id} is already recorded with a different fill"
                )
            state = repository.get_strategy_ledger_state(refresh=True)
            if state is None:
                raise RuntimeError("strategy fill exists without a state projection")
            return RecordFillResult(created=False, state=state)

        current = repository.get_strategy_ledger_state(refresh=True)
        if current is None:
            raise ValueError("strategy ledger must be initialized by account sync first")
        conflict = fill_conflict(
            repository,
            current,
            instrument=instrument,
            occurred_at=occurred_at,
            splits=load_split_registry(),
        )
        if conflict:
            raise ValueError(conflict)
        try:
            return _book_fill(
                repository,
                current,
                event_key=event_key,
                execution_id=execution_id,
                fill=fill,
                occurred_at=occurred_at,
                note=note,
            )
        except LedgerWriteConflict:
            continue  # Another writer committed first: re-read and re-apply.
    raise LedgerWriteConflict("strategy ledger kept changing; retry the fill")


def confirm_dividend_payment(
    session: Session,
    *,
    action_id: str,
    received_amount: Decimal,
    note: str,
    confirmed_at: datetime,
) -> RecordFillResult:
    """Book what a dividend payment actually delivered, once per action."""
    repository = AuditRepository(session)
    event_key = f"CA:{action_id}:CONFIRMATION"
    for _attempt in range(MAX_LEDGER_WRITE_ATTEMPTS):
        existing = repository.strategy_ledger_event(event_key)
        if existing is not None:
            if Decimal(str(existing.payload.get("received_amount"))) != received_amount:
                raise ValueError(
                    f"dividend {action_id} was already confirmed with a different amount"
                )
            state = repository.get_strategy_ledger_state(refresh=True)
            return RecordFillResult(created=False, state=state)
        current = repository.get_strategy_ledger_state(refresh=True)
        if current is None or action_id not in current.unconfirmed_dividends:
            raise ValueError(f"no paid, unconfirmed dividend {action_id}")
        credited = current.unconfirmed_dividends[action_id]
        updated = confirm_dividend(
            current, action_id=action_id, received=received_amount, at=confirmed_at
        )
        try:
            repository.save_strategy_ledger_state(updated, expected=current)
        except LedgerWriteConflict:
            continue
        repository.append_strategy_ledger_event(
            event_key=event_key,
            event_type="DIVIDEND_CONFIRMATION",
            occurred_at=confirmed_at,
            payload={
                "action_id": action_id,
                "credited_amount": str(credited),
                "received_amount": str(received_amount),
                "difference": str(received_amount - credited),
                "note": note,
                "state_after": repository.ledger_state_payload(updated),
            },
        )
        return RecordFillResult(created=True, state=updated)
    raise LedgerWriteConflict("strategy ledger kept changing; retry the confirmation")


def _same_fill(event, fill: dict[str, object], occurred_at: datetime) -> bool:
    """A replay must describe the identical booking (numbers compared as values)."""
    stored = event.payload or {}
    try:
        return (
            stored.get("instrument") == fill["instrument"]
            and str(stored.get("side", "")).upper() == fill["side"]
            and all(
                Decimal(str(stored.get(key))) == fill[key]
                for key in ("quantity", "price", "commission", "tax")
            )
            and event.occurred_at == occurred_at
        )
    except (ArithmeticError, ValueError, TypeError):
        return False


def _book_fill(
    repository: AuditRepository,
    current: StrategyLedgerState,
    *,
    event_key: str,
    execution_id: str,
    fill: dict,
    occurred_at: datetime,
    note: str,
) -> RecordFillResult:
    instrument, side = fill["instrument"], fill["side"]
    quantity, price = fill["quantity"], fill["price"]
    commission, tax = fill["commission"], fill["tax"]
    updated = apply_strategy_fill(
        current,
        instrument=instrument,
        side=side,
        quantity=quantity,
        price=price,
        commission=commission,
        tax=tax,
        occurred_at=occurred_at,
    )
    payload = {
        "execution_id": execution_id,
        "instrument": instrument,
        "side": side.upper(),
        "quantity": str(quantity),
        "price": str(price),
        "commission": str(commission),
        "tax": str(tax),
        "note": note,
        "state_after": repository.ledger_state_payload(updated),
    }
    repository.save_strategy_ledger_state(updated, expected=current)
    repository.append_strategy_ledger_event(
        event_key=event_key,
        event_type="FILL",
        occurred_at=occurred_at,
        payload=payload,
    )
    return RecordFillResult(created=True, state=updated)
