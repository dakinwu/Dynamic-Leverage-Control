from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from dynamic_leverage.domain.ledger import StrategyLedgerState, apply_strategy_fill
from dynamic_leverage.infrastructure.repository import AuditRepository

ALLOWED_LEDGER_INSTRUMENTS = frozenset({"0050", "00631L", "00685L"})


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
    if repository.strategy_ledger_event(event_key) is not None:
        state = repository.get_strategy_ledger_state()
        if state is None:
            raise RuntimeError("strategy fill exists without a state projection")
        return RecordFillResult(created=False, state=state)

    current = repository.get_strategy_ledger_state()
    if current is None:
        raise ValueError("strategy ledger must be initialized by account sync first")
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
    repository.save_strategy_ledger_state(updated)
    repository.append_strategy_ledger_event(
        event_key=event_key,
        event_type="FILL",
        occurred_at=occurred_at,
        payload=payload,
    )
    return RecordFillResult(created=True, state=updated)
