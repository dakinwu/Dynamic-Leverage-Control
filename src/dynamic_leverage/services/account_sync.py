from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from dynamic_leverage.domain.account import AccountSnapshotReader, BrokerAccountSnapshot
from dynamic_leverage.domain.corporate_actions import SplitAction
from dynamic_leverage.domain.ledger import (
    StrategyLedgerState,
    bootstrap_strategy_ledger,
    mark_strategy_ledger,
)
from dynamic_leverage.infrastructure.repository import AuditRepository, LedgerWriteConflict
from dynamic_leverage.services.calendar import TAIPEI_TZ, previous_trading_day
from dynamic_leverage.services.corporate_actions import distribution_actions, load_split_registry
from dynamic_leverage.services.ledger import MAX_LEDGER_WRITE_ATTEMPTS
from dynamic_leverage.services.ledger_actions import (
    apply_due_corporate_actions,
    unregistered_ratio_reasons,
)
from dynamic_leverage.services.strategy_pipeline import parse_pcf_exposure

ALLOWED_INSTRUMENTS = frozenset({"0050", "00631L", "00685L"})


@dataclass(frozen=True, slots=True)
class AccountSyncResult:
    snapshot_id: str
    created: bool
    account_mode: str
    account_masked: str
    nav: Decimal
    strategy_nav: Decimal
    strategy_cash: Decimal
    net_contributions: Decimal
    peak_nav: Decimal
    principal_loss_amount: Decimal
    principal_loss_ratio: Decimal
    current_drawdown: Decimal
    maximum_drawdown: Decimal
    pending_settlement: Decimal
    actual_exposure: Decimal | None
    account_exposure: Decimal | None
    exposure_quality: str
    reconciliation_break: bool
    reconciliation_reason: str | None
    quality: str
    external_instruments: tuple[str, ...] = ()


def binding_fingerprint(binding: str) -> str:
    return hashlib.sha256(binding.encode("utf-8")).hexdigest()


def mask_binding(binding: str) -> str:
    broker_id, separator, account_id = binding.partition("-")
    if (
        not separator
        or binding.count("-") != 1
        or not broker_id
        or len(account_id) < 4
        or any(character.isspace() for character in binding)
    ):
        raise ValueError("account binding must use BROKER_ID-ACCOUNT_ID format")
    return f"{broker_id}-***{account_id[-4:]}"


# Both allowed leveraged funds are daily 2x products; a PCF reading outside this
# band is a parsing or data defect, so the exposure is treated as unknown.
TWO_X_PCF_EXPOSURE_BAND = (Decimal("1.8"), Decimal("2.3"))


def _pcf_is_current(trade_date: date, observed_at: datetime) -> bool:
    """The PCF must describe the account date or the session just before it."""
    account_date = observed_at.astimezone(TAIPEI_TZ).date()
    return previous_trading_day(account_date) <= trade_date <= account_date


def _actual_exposure(
    snapshot: BrokerAccountSnapshot,
    repository: AuditRepository,
    strategy_nav: Decimal,
) -> tuple[Decimal | None, Decimal | None, str]:
    if snapshot.nav <= 0 or strategy_nav <= 0:
        return None, None, "invalid-nav"
    numerator = Decimal("0")
    for position in snapshot.positions:
        if position.instrument == "0050":
            numerator += position.market_value
            continue
        if position.instrument in {"00631L", "00685L"}:
            record = repository.latest_raw_snapshot(
                kind=(
                    "market.yuanta_00631l_pcf.official"
                    if position.instrument == "00631L"
                    else "market.capital_00685l_pcf.official"
                )
            )
            pcf = parse_pcf_exposure([record], position.instrument) if record else None
            if pcf is None:
                return None, None, "official-etf-exposure-required"
            if not TWO_X_PCF_EXPOSURE_BAND[0] <= pcf.exposure <= TWO_X_PCF_EXPOSURE_BAND[1]:
                return None, None, "pcf-exposure-out-of-range"
            if not _pcf_is_current(pcf.trade_date, snapshot.observed_at):
                return None, None, "pcf-stale"
            numerator += position.market_value * pcf.exposure
    return numerator / strategy_nav, numerator / snapshot.nav, "authoritative-pcf"


def _strategy_positions(
    snapshot: BrokerAccountSnapshot,
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    quantities: dict[str, Decimal] = {}
    market_values: dict[str, Decimal] = {}
    for position in snapshot.positions:
        if position.instrument not in ALLOWED_INSTRUMENTS:
            continue
        quantities[position.instrument] = (
            quantities.get(position.instrument, Decimal("0")) + position.quantity
        )
        market_values[position.instrument] = (
            market_values.get(position.instrument, Decimal("0")) + position.market_value
        )
    quantities = {
        instrument: quantity for instrument, quantity in quantities.items() if quantity != 0
    }
    prices = {
        instrument: market_values[instrument] / quantity
        for instrument, quantity in quantities.items()
    }
    return quantities, prices


def _event_payload(state: StrategyLedgerState) -> dict[str, object]:
    return {
        "capital_budget": str(state.capital_budget),
        "net_contributions": str(state.net_contributions),
        "cash": str(state.cash),
        "positions": {key: str(value) for key, value in sorted(state.positions.items())},
        "last_prices": {key: str(value) for key, value in sorted(state.last_prices.items())},
        "current_nav": str(state.current_nav),
        "peak_nav": str(state.peak_nav),
        "principal_loss_amount": str(state.principal_loss_amount),
        "principal_loss_ratio": str(state.principal_loss_ratio),
        "current_drawdown": str(state.current_drawdown),
        "maximum_drawdown": str(state.maximum_drawdown),
        "position_revision": state.position_revision,
        "reconciled": state.reconciled,
        "reconciliation_reason": state.reconciliation_reason,
        "actual_exposure": (
            str(state.actual_exposure) if state.actual_exposure is not None else None
        ),
        "account_exposure": (
            str(state.account_exposure) if state.account_exposure is not None else None
        ),
        "exposure_quality": state.exposure_quality,
    }


def _reconcile_strategy_ledger(**kwargs) -> StrategyLedgerState:
    """Reconcile, re-reading and retrying if a fill commits between read and write."""
    for _attempt in range(MAX_LEDGER_WRITE_ATTEMPTS):
        try:
            return _reconcile_strategy_ledger_once(**kwargs)
        except LedgerWriteConflict:
            continue
    raise LedgerWriteConflict("strategy ledger kept changing during account sync")


def _reconcile_strategy_ledger_once(
    *,
    repository: AuditRepository,
    snapshot: BrokerAccountSnapshot,
    account_snapshot_id: str,
    capital_budget: Decimal,
    position_structure_break: bool,
    splits: tuple[SplitAction, ...],
) -> StrategyLedgerState:
    current = repository.get_strategy_ledger_state(refresh=True)
    revision = current.position_revision if current is not None else 0
    leveraged_instruments = sorted(
        {
            position.instrument
            for position in snapshot.positions
            if position.instrument in {"00631L", "00685L"}
        }
    )
    exposure_evidence = []
    for instrument in leveraged_instruments:
        record = repository.latest_raw_snapshot(
            kind=(
                "market.yuanta_00631l_pcf.official"
                if instrument == "00631L"
                else "market.capital_00685l_pcf.official"
            )
        )
        exposure_evidence.append(record.snapshot_id if record is not None else "missing")
    evidence_hash = hashlib.sha256("|".join(exposure_evidence).encode("utf-8")).hexdigest()[:16]
    event_key = f"ACCOUNT:{account_snapshot_id}:REVISION:{revision}:EVIDENCE:{evidence_hash}"
    if repository.strategy_ledger_event(event_key) is not None:
        if current is None:
            raise RuntimeError("strategy ledger event exists without a state projection")
        return current

    positions, prices = _strategy_positions(snapshot)
    corporate_events = []
    if current is None:
        if position_structure_break:
            raise ValueError("cannot initialize strategy ledger from unsupported account positions")
        updated = bootstrap_strategy_ledger(
            capital_budget=capital_budget,
            positions=positions,
            prices=prices,
            observed_at=snapshot.observed_at,
            account_snapshot_id=account_snapshot_id,
        )
        event_type = "BOOTSTRAP"
    elif position_structure_break:
        updated = replace(
            current,
            reconciled=False,
            reconciliation_reason="unsupported-direction-or-settlement-condition",
            last_account_snapshot_id=account_snapshot_id,
            updated_at=snapshot.observed_at,
            actual_exposure=None,
            account_exposure=None,
            exposure_quality="reconciliation-break",
        )
        event_type = "RECONCILIATION_BREAK"
    else:
        # Corporate actions first, so the mark compares like-for-like units.
        dividends, _problems = distribution_actions(
            repository,
            sorted(ALLOWED_INSTRUMENTS),
            today=snapshot.observed_at.astimezone(TAIPEI_TZ).date(),
        )
        adjusted, corporate_events, corporate_breaks = apply_due_corporate_actions(
            repository,
            current,
            at=snapshot.observed_at,
            broker_positions=positions,
            splits=splits,
            dividends=dividends,
        )
        updated = mark_strategy_ledger(
            adjusted,
            positions=positions,
            prices=prices,
            observed_at=snapshot.observed_at,
            account_snapshot_id=account_snapshot_id,
        )
        breaks = list(corporate_breaks)
        if not updated.reconciled and str(updated.reconciliation_reason).startswith(
            "unrecorded-position-change"
        ):
            breaks += unregistered_ratio_reasons(adjusted.positions, positions)
        if breaks:
            reasons = [*breaks]
            if not updated.reconciled and updated.reconciliation_reason:
                reasons.append(updated.reconciliation_reason)
            updated = replace(
                updated,
                reconciled=False,
                reconciliation_reason="; ".join(reasons),
                actual_exposure=None,
                account_exposure=None,
                exposure_quality="reconciliation-break",
            )
        event_type = "MARK" if updated.reconciled else "RECONCILIATION_BREAK"

    if updated.reconciled:
        actual, account, exposure_quality = _actual_exposure(
            snapshot,
            repository,
            updated.current_nav,
        )
        updated = replace(
            updated,
            actual_exposure=actual,
            account_exposure=account,
            exposure_quality=exposure_quality,
        )

    repository.save_strategy_ledger_state(updated, expected=current)
    for pending in corporate_events:
        repository.append_strategy_ledger_event(
            event_key=pending.event_key,
            event_type=pending.event_type,
            occurred_at=snapshot.observed_at,
            account_snapshot_id=account_snapshot_id,
            payload={**pending.payload, "state_after": _event_payload(updated)},
        )
    repository.append_strategy_ledger_event(
        event_key=event_key,
        event_type=event_type,
        occurred_at=snapshot.observed_at,
        account_snapshot_id=account_snapshot_id,
        payload=_event_payload(updated),
    )
    return updated


def sync_account_snapshot(
    session: Session,
    reader: AccountSnapshotReader,
    *,
    binding: str,
    capital_budget: Decimal | None = None,
    splits: tuple[SplitAction, ...] | None = None,
) -> AccountSyncResult:
    account_masked = mask_binding(binding)
    fingerprint = binding_fingerprint(binding)
    snapshot = reader.read_account(binding)
    external_instruments = tuple(
        sorted(
            {
                position.instrument
                for position in snapshot.positions
                if position.instrument not in ALLOWED_INSTRUMENTS
            }
        )
    )
    position_structure_break = any(
        position.direction.lower() != "buy" or position.condition.lower() != "cash"
        for position in snapshot.positions
    )
    raw_quality = (
        "unsupported-position-structure"
        if position_structure_break
        else "observed-with-external-positions"
        if external_instruments
        else "authoritative"
    )
    payload = {
        "schema": "shioaji-account-snapshot-v2",
        "account_masked": account_masked,
        "binding_fingerprint": fingerprint,
        "observed_at": snapshot.observed_at.isoformat(),
        "cash": str(snapshot.cash),
        "nav": str(snapshot.nav),
        "pending_settlement": str(snapshot.pending_settlement),
        "positions": [
            {
                "instrument": position.instrument,
                "quantity": str(position.quantity),
                "average_price": str(position.average_price),
                "last_price": str(position.last_price),
                "direction": position.direction,
                "condition": position.condition,
                "market_value": str(position.market_value),
            }
            for position in snapshot.positions
        ],
        "settlements": [
            {
                "date": settlement.settlement_date.isoformat(),
                "amount": str(settlement.amount),
                "t": settlement.t,
            }
            for settlement in snapshot.settlements
        ],
        "position_structure_break": position_structure_break,
        "external_instruments": list(external_instruments),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    repository = AuditRepository(session)
    row, created = repository.append_raw_snapshot(
        kind="broker.account.shioaji",
        source=f"shioaji:account:{fingerprint}",
        observed_at=snapshot.observed_at,
        received_at=snapshot.observed_at,
        quality=raw_quality,
        checksum=checksum,
        payload=payload,
    )
    ledger = _reconcile_strategy_ledger(
        repository=repository,
        snapshot=snapshot,
        account_snapshot_id=row.snapshot_id,
        capital_budget=capital_budget if capital_budget is not None else snapshot.nav,
        position_structure_break=position_structure_break,
        splits=load_split_registry() if splits is None else splits,
    )
    quality = raw_quality if ledger.reconciled else "reconciliation-break"
    return AccountSyncResult(
        snapshot_id=row.snapshot_id,
        created=created,
        account_mode="observed",
        account_masked=account_masked,
        nav=snapshot.nav,
        strategy_nav=ledger.current_nav,
        strategy_cash=ledger.cash,
        net_contributions=ledger.net_contributions,
        peak_nav=ledger.peak_nav,
        principal_loss_amount=ledger.principal_loss_amount,
        principal_loss_ratio=ledger.principal_loss_ratio,
        current_drawdown=ledger.current_drawdown,
        maximum_drawdown=ledger.maximum_drawdown,
        pending_settlement=snapshot.pending_settlement,
        actual_exposure=ledger.actual_exposure,
        account_exposure=ledger.account_exposure,
        exposure_quality=ledger.exposure_quality,
        reconciliation_break=not ledger.reconciled,
        reconciliation_reason=ledger.reconciliation_reason,
        quality=quality,
        external_instruments=external_instruments,
    )
