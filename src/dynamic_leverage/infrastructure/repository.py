from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from dynamic_leverage.domain.ledger import StrategyLedgerState
from dynamic_leverage.domain.models import (
    PolicyState,
    RiskDecision,
    SafetyState,
    StrategyDecision,
    TradeIntent,
)
from dynamic_leverage.infrastructure.tables import (
    AutomationStatusRecord,
    FreshnessContractRecord,
    ManualHaltEventRecord,
    ManualHaltStateRecord,
    OperatorQueueEventRecord,
    OperatorQueueItemRecord,
    PaperEvidenceRecord,
    RawSnapshotRecord,
    RiskEvaluationRecord,
    StrategyEvaluationRecord,
    StrategyLedgerEventRecord,
    StrategyLedgerStateRecord,
    StrategyPolicyStateRecord,
    StrategySelectionRecord,
    TradeIntentRecord,
)


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append_raw_snapshot(
        self,
        *,
        kind: str,
        source: str,
        observed_at: datetime,
        received_at: datetime,
        quality: str,
        checksum: str,
        payload: dict[str, Any],
        published_at: datetime | None = None,
    ) -> tuple[RawSnapshotRecord, bool]:
        """Append an immutable source snapshot, idempotently by source and checksum."""
        existing = self.session.scalar(
            select(RawSnapshotRecord).where(
                RawSnapshotRecord.source == source,
                RawSnapshotRecord.checksum == checksum,
            )
        )
        if existing is not None:
            return existing, False

        row = RawSnapshotRecord(
            snapshot_id=str(uuid4()),
            kind=kind,
            source=source,
            observed_at=observed_at,
            published_at=published_at,
            received_at=received_at,
            quality=quality,
            checksum=checksum,
            payload=payload,
        )
        self.session.add(row)
        self.session.flush()
        return row, True

    @staticmethod
    def ledger_state_payload(state: StrategyLedgerState) -> dict[str, Any]:
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
            "last_account_snapshot_id": state.last_account_snapshot_id,
            "initialized_at": state.initialized_at.isoformat(),
            "updated_at": state.updated_at.isoformat(),
            "actual_exposure": (
                str(state.actual_exposure) if state.actual_exposure is not None else None
            ),
            "account_exposure": (
                str(state.account_exposure) if state.account_exposure is not None else None
            ),
            "exposure_quality": state.exposure_quality,
        }

    @staticmethod
    def _ledger_state_from_row(row: StrategyLedgerStateRecord) -> StrategyLedgerState:
        return StrategyLedgerState(
            capital_budget=Decimal(row.capital_budget),
            net_contributions=Decimal(row.net_contributions),
            cash=Decimal(row.cash),
            positions={key: Decimal(value) for key, value in row.positions.items()},
            last_prices={key: Decimal(value) for key, value in row.last_prices.items()},
            current_nav=Decimal(row.current_nav),
            peak_nav=Decimal(row.peak_nav),
            principal_loss_amount=Decimal(row.principal_loss_amount),
            principal_loss_ratio=Decimal(row.principal_loss_ratio),
            current_drawdown=Decimal(row.current_drawdown),
            maximum_drawdown=Decimal(row.maximum_drawdown),
            position_revision=row.position_revision,
            reconciled=row.reconciled,
            reconciliation_reason=row.reconciliation_reason,
            last_account_snapshot_id=row.last_account_snapshot_id,
            initialized_at=row.initialized_at,
            updated_at=row.updated_at,
            actual_exposure=(
                Decimal(row.actual_exposure) if row.actual_exposure is not None else None
            ),
            account_exposure=(
                Decimal(row.account_exposure) if row.account_exposure is not None else None
            ),
            exposure_quality=row.exposure_quality,
        )

    def get_strategy_ledger_state(self) -> StrategyLedgerState | None:
        row = self.session.get(StrategyLedgerStateRecord, "primary")
        return self._ledger_state_from_row(row) if row is not None else None

    def save_strategy_ledger_state(self, state: StrategyLedgerState) -> StrategyLedgerStateRecord:
        row = self.session.get(StrategyLedgerStateRecord, "primary")
        values = self.ledger_state_payload(state)
        values["initialized_at"] = state.initialized_at
        values["updated_at"] = state.updated_at
        if row is None:
            row = StrategyLedgerStateRecord(ledger_key="primary", **values)
            self.session.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        self.session.flush()
        return row

    def strategy_ledger_event(self, event_key: str) -> StrategyLedgerEventRecord | None:
        return self.session.scalar(
            select(StrategyLedgerEventRecord).where(
                StrategyLedgerEventRecord.event_key == event_key
            )
        )

    def append_strategy_ledger_event(
        self,
        *,
        event_key: str,
        event_type: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        account_snapshot_id: str | None = None,
    ) -> tuple[StrategyLedgerEventRecord, bool]:
        existing = self.strategy_ledger_event(event_key)
        if existing is not None:
            return existing, False
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        row = StrategyLedgerEventRecord(
            event_id=str(uuid4()),
            event_key=event_key,
            event_type=event_type,
            occurred_at=occurred_at,
            recorded_at=datetime.now(ZoneInfo("Asia/Taipei")),
            account_snapshot_id=account_snapshot_id,
            event_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            payload=payload,
        )
        self.session.add(row)
        self.session.flush()
        return row, True

    def strategy_ledger_events(self, *, limit: int = 100) -> list[StrategyLedgerEventRecord]:
        return list(
            self.session.scalars(
                select(StrategyLedgerEventRecord)
                .order_by(StrategyLedgerEventRecord.occurred_at.desc())
                .limit(limit)
            )
        )

    def append_strategy_decision(
        self,
        decision: StrategyDecision,
        *,
        input_snapshot_ids: list[str] | None = None,
        state_before: PolicyState | None = None,
        advance_policy_state: bool = False,
    ) -> StrategyEvaluationRecord:
        row = StrategyEvaluationRecord(
            evaluation_id=str(uuid4()),
            strategy_version=decision.strategy_version,
            as_of=decision.as_of,
            recorded_at=datetime.now(ZoneInfo("Asia/Taipei")),
            valid=decision.valid,
            raw_target=decision.raw_target,
            target_exposure=decision.target_exposure,
            safety_state=decision.safety_state.value,
            reasons=list(decision.reasons),
            diagnostics=decision.diagnostics,
            input_snapshot_ids=input_snapshot_ids or [],
            state_before=self._policy_state_payload(state_before) if state_before else None,
            state_after=self._policy_state_payload(decision.next_state),
        )
        self.session.add(row)
        self.session.flush()
        if advance_policy_state and decision.valid:
            self._advance_policy_state(decision)
        return row

    @staticmethod
    def _policy_state_payload(state: PolicyState) -> dict[str, Any]:
        return {
            "previous_target": str(state.previous_target),
            "panic_active": state.panic_active,
            "panic_clear_streak": state.panic_clear_streak,
            "increase_candidate": (
                str(state.increase_candidate) if state.increase_candidate is not None else None
            ),
            "increase_streak": state.increase_streak,
            "last_increase_iso_week": (
                list(state.last_increase_iso_week)
                if state.last_increase_iso_week is not None
                else None
            ),
        }

    @staticmethod
    def _policy_state_from_payload(payload: dict[str, Any]) -> PolicyState:
        iso_week = payload.get("last_increase_iso_week")
        return PolicyState(
            previous_target=Decimal(str(payload.get("previous_target", "1.0"))),
            panic_active=bool(payload.get("panic_active", False)),
            panic_clear_streak=int(payload.get("panic_clear_streak", 0)),
            increase_candidate=(
                Decimal(str(payload["increase_candidate"]))
                if payload.get("increase_candidate") is not None
                else None
            ),
            increase_streak=int(payload.get("increase_streak", 0)),
            last_increase_iso_week=(
                (int(iso_week[0]), int(iso_week[1])) if iso_week is not None else None
            ),
        )

    def policy_state_for_evaluation(self, strategy_version: str, as_of: date) -> PolicyState:
        """Return a stable pre-decision state, including for same-day reruns."""
        same_day = self.session.scalar(
            select(StrategyEvaluationRecord)
            .where(
                StrategyEvaluationRecord.strategy_version == strategy_version,
                StrategyEvaluationRecord.as_of == as_of,
                StrategyEvaluationRecord.state_before.is_not(None),
            )
            .order_by(StrategyEvaluationRecord.recorded_at.asc())
            .limit(1)
        )
        if same_day is not None and same_day.state_before is not None:
            return self._policy_state_from_payload(same_day.state_before)

        projection = self.session.get(StrategyPolicyStateRecord, strategy_version)
        if projection is not None:
            if as_of < projection.last_advanced_as_of:
                raise ValueError("cannot evaluate policy state before its latest advanced date")
            return self._policy_state_from_payload(projection.state_payload)

        # Preserve the last known target when migrating an existing database.
        legacy = self.session.scalar(
            select(StrategyEvaluationRecord)
            .where(
                StrategyEvaluationRecord.strategy_version == strategy_version,
                StrategyEvaluationRecord.as_of < as_of,
                StrategyEvaluationRecord.valid.is_(True),
                StrategyEvaluationRecord.target_exposure.is_not(None),
            )
            .order_by(
                StrategyEvaluationRecord.as_of.desc(),
                StrategyEvaluationRecord.recorded_at.desc(),
            )
            .limit(1)
        )
        if legacy is None or legacy.target_exposure is None:
            return PolicyState()
        return PolicyState(
            previous_target=Decimal(legacy.target_exposure),
            panic_active=legacy.safety_state == SafetyState.PANIC_LOCK.value,
        )

    def _advance_policy_state(self, decision: StrategyDecision) -> None:
        row = self.session.get(StrategyPolicyStateRecord, decision.strategy_version)
        if row is not None and decision.as_of <= row.last_advanced_as_of:
            return
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        payload = self._policy_state_payload(decision.next_state)
        if row is None:
            row = StrategyPolicyStateRecord(
                strategy_version=decision.strategy_version,
                last_advanced_as_of=decision.as_of,
                state_payload=payload,
                updated_at=now,
            )
            self.session.add(row)
        else:
            row.last_advanced_as_of = decision.as_of
            row.state_payload = payload
            row.updated_at = now
        self.session.flush()

    def append_risk_decision(
        self,
        strategy_evaluation_id: str,
        decision: RiskDecision,
    ) -> RiskEvaluationRecord:
        row = RiskEvaluationRecord(
            evaluation_id=str(uuid4()),
            recorded_at=datetime.now(ZoneInfo("Asia/Taipei")),
            strategy_evaluation_id=strategy_evaluation_id,
            approved=decision.approved,
            allowed_exposure=decision.allowed_exposure,
            safety_state=decision.safety_state.value,
            risk_reducing_only=decision.risk_reducing_only,
            reasons=list(decision.reason_codes),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def append_trade_intent(self, intent: TradeIntent, maturity: str) -> TradeIntentRecord:
        payload = asdict(intent)
        row = TradeIntentRecord(
            intent_id=payload["intent_id"],
            created_at=payload["created_at"],
            strategy_version=payload["strategy_version"],
            execution_recipe=payload["execution_recipe"],
            current_exposure=payload["current_exposure"],
            requested_exposure=payload["requested_exposure"],
            reason_codes=list(payload["reason_codes"]),
            maturity=maturity,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def latest_strategy_decisions(self) -> list[StrategyEvaluationRecord]:
        versions = select(StrategyEvaluationRecord.strategy_version).distinct()
        results = []
        for version in self.session.scalars(versions):
            query = (
                select(StrategyEvaluationRecord)
                .where(StrategyEvaluationRecord.strategy_version == version)
                .order_by(StrategyEvaluationRecord.recorded_at.desc())
                .limit(1)
            )
            row = self.session.scalar(query)
            if row is not None:
                results.append(row)
        return sorted(results, key=lambda item: item.strategy_version)

    def latest_raw_snapshots(self, *, limit: int = 20) -> list[RawSnapshotRecord]:
        query = (
            select(RawSnapshotRecord).order_by(RawSnapshotRecord.received_at.desc()).limit(limit)
        )
        return list(self.session.scalars(query))

    def raw_snapshots_for_kind(
        self,
        *,
        kind: str,
        limit: int = 500,
    ) -> list[RawSnapshotRecord]:
        """Return immutable observations for one source, newest first."""
        query = (
            select(RawSnapshotRecord)
            .where(RawSnapshotRecord.kind == kind)
            .order_by(RawSnapshotRecord.observed_at.desc(), RawSnapshotRecord.received_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(query))

    def latest_strategy_decision(self, version: str) -> StrategyEvaluationRecord | None:
        query = (
            select(StrategyEvaluationRecord)
            .where(StrategyEvaluationRecord.strategy_version == version)
            .order_by(StrategyEvaluationRecord.recorded_at.desc())
            .limit(1)
        )
        return self.session.scalar(query)

    def strategy_decisions(
        self,
        version: str,
        *,
        limit: int = 100,
    ) -> list[StrategyEvaluationRecord]:
        query = (
            select(StrategyEvaluationRecord)
            .where(StrategyEvaluationRecord.strategy_version == version)
            .order_by(StrategyEvaluationRecord.recorded_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(query))

    def latest_raw_snapshot(
        self,
        *,
        kind: str,
        source: str | None = None,
    ) -> RawSnapshotRecord | None:
        query = select(RawSnapshotRecord).where(RawSnapshotRecord.kind == kind)
        if source is not None:
            query = query.where(RawSnapshotRecord.source == source)
        query = query.order_by(RawSnapshotRecord.received_at.desc()).limit(1)
        return self.session.scalar(query)

    def upsert_automation_status(
        self,
        *,
        automation_id: str,
        status: str,
        last_started_at: datetime | None = None,
        last_success_at: datetime | None = None,
        next_run_at: datetime | None = None,
        last_run_id: str | None = None,
        failure_reason: str | None = None,
        metadata_payload: dict[str, Any] | None = None,
    ) -> AutomationStatusRecord:
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        row = self.session.get(AutomationStatusRecord, automation_id)
        if row is None:
            row = AutomationStatusRecord(
                automation_id=automation_id,
                status=status,
                last_started_at=last_started_at,
                last_success_at=last_success_at,
                next_run_at=next_run_at,
                last_run_id=last_run_id,
                failure_reason=failure_reason,
                metadata_payload=metadata_payload or {},
                updated_at=now,
            )
            self.session.add(row)
        else:
            row.status = status
            row.last_started_at = last_started_at or row.last_started_at
            row.last_success_at = last_success_at or row.last_success_at
            row.next_run_at = next_run_at
            row.last_run_id = last_run_id or row.last_run_id
            row.failure_reason = failure_reason
            if metadata_payload is not None:
                row.metadata_payload = metadata_payload
            row.updated_at = now
        self.session.flush()
        return row

    def list_automation_status(self) -> list[AutomationStatusRecord]:
        return list(
            self.session.scalars(
                select(AutomationStatusRecord).order_by(AutomationStatusRecord.automation_id)
            )
        )

    def upsert_freshness_contract(
        self,
        *,
        contract_key: str,
        kind: str,
        source: str,
        owner: str,
        max_age_minutes: int,
        required: bool = True,
    ) -> FreshnessContractRecord:
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        row = self.session.get(FreshnessContractRecord, contract_key)
        if row is None:
            row = FreshnessContractRecord(
                contract_key=contract_key,
                kind=kind,
                source=source,
                owner=owner,
                max_age_minutes=max_age_minutes,
                required=required,
                updated_at=now,
            )
            self.session.add(row)
        else:
            row.kind = kind
            row.source = source
            row.owner = owner
            row.max_age_minutes = max_age_minutes
            row.required = required
            row.updated_at = now
        self.session.flush()
        return row

    def list_freshness_contracts(self) -> list[FreshnessContractRecord]:
        return list(
            self.session.scalars(
                select(FreshnessContractRecord).order_by(FreshnessContractRecord.contract_key)
            )
        )

    def reconcile_queue(self, active_items: list[dict[str, Any]]) -> list[OperatorQueueItemRecord]:
        """Project current conditions while retaining an append-only event trail."""
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        active_keys = {str(item["item_id"]) for item in active_items}
        current = {
            row.item_id: row for row in self.session.scalars(select(OperatorQueueItemRecord))
        }
        for item in active_items:
            item_id = str(item["item_id"])
            row = current.get(item_id)
            if row is None:
                row = OperatorQueueItemRecord(
                    item_id=item_id,
                    severity=str(item.get("severity", "ACTION")),
                    status="OPEN",
                    title=str(item["title"]),
                    detail=str(item.get("detail", "")),
                    impact=str(item.get("impact", "")),
                    next_action=str(item.get("next_action", "")),
                    evidence_refs=list(item.get("evidence_refs", [])),
                    first_seen_at=now,
                    last_seen_at=now,
                    reopened_count=0,
                    updated_at=now,
                )
                self.session.add(row)
                self._append_queue_event(row, "OPEN", "system", "condition detected")
            else:
                if row.status == "RESOLVED":
                    row.status = "REOPENED"
                    row.reopened_count += 1
                    self._append_queue_event(row, "REOPENED", "system", "condition detected again")
                row.severity = str(item.get("severity", row.severity))
                row.title = str(item["title"])
                row.detail = str(item.get("detail", row.detail))
                row.impact = str(item.get("impact", row.impact))
                row.next_action = str(item.get("next_action", row.next_action))
                row.evidence_refs = list(item.get("evidence_refs", row.evidence_refs))
                row.last_seen_at = now
                row.updated_at = now
        for row in current.values():
            if row.item_id not in active_keys and row.status != "RESOLVED":
                row.status = "RESOLVED"
                row.resolved_at = now
                row.resolved_by = "system"
                row.updated_at = now
                self._append_queue_event(row, "RESOLVED", "system", "condition cleared")
        self.session.flush()
        return self.queue_items()

    def _append_queue_event(
        self,
        row: OperatorQueueItemRecord,
        event_type: str,
        actor: str,
        reason: str,
    ) -> OperatorQueueEventRecord:
        event = OperatorQueueEventRecord(
            event_id=str(uuid4()),
            item_id=row.item_id,
            event_type=event_type,
            status=row.status,
            actor=actor,
            reason=reason,
            created_at=datetime.now(ZoneInfo("Asia/Taipei")),
            evidence_refs=list(row.evidence_refs),
        )
        self.session.add(event)
        return event

    def queue_items(self, *, unresolved_only: bool = False) -> list[OperatorQueueItemRecord]:
        query = select(OperatorQueueItemRecord).order_by(
            OperatorQueueItemRecord.severity,
            OperatorQueueItemRecord.updated_at.desc(),
        )
        if unresolved_only:
            query = query.where(OperatorQueueItemRecord.status != "RESOLVED")
        return list(self.session.scalars(query))

    def transition_queue_item(
        self,
        item_id: str,
        *,
        target_status: str,
        actor: str,
        reason: str,
    ) -> OperatorQueueItemRecord | None:
        row = self.session.get(OperatorQueueItemRecord, item_id)
        if row is None:
            return None
        allowed = {
            "OPEN": {"ACKNOWLEDGED"},
            "REOPENED": {"ACKNOWLEDGED"},
            "ACKNOWLEDGED": {"RESOLVED"},
            "RESOLVED": set(),
        }
        if target_status not in allowed.get(row.status, set()):
            raise ValueError(f"invalid queue transition {row.status}->{target_status}")
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        row.status = target_status
        row.updated_at = now
        if target_status == "ACKNOWLEDGED":
            row.acknowledged_at = now
            row.acknowledged_by = actor
        else:
            row.resolved_at = now
            row.resolved_by = actor
        self._append_queue_event(row, target_status, actor, reason)
        self.session.flush()
        return row

    def get_manual_halt(self) -> ManualHaltStateRecord:
        row = self.session.get(ManualHaltStateRecord, "global")
        if row is None:
            row = ManualHaltStateRecord(
                halt_key="global",
                active=False,
                updated_at=datetime.now(ZoneInfo("Asia/Taipei")),
            )
        return row

    def set_manual_halt(self, *, active: bool, actor: str, reason: str) -> ManualHaltStateRecord:
        row = self.get_manual_halt()
        self.session.add(row)
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        row.active = active
        row.updated_at = now
        if active:
            row.reason = reason
            row.actor = actor
            row.enabled_at = now
            row.released_at = None
            row.release_reason = None
            event_type = "ENABLED"
        else:
            row.release_reason = reason
            row.released_at = now
            event_type = "RELEASED"
        self.session.add(
            ManualHaltEventRecord(
                event_id=str(uuid4()),
                event_type=event_type,
                actor=actor,
                reason=reason,
                created_at=now,
            )
        )
        self.session.flush()
        return row

    def get_strategy_selection(self) -> StrategySelectionRecord:
        row = self.session.get(StrategySelectionRecord, "primary")
        if row is None:
            row = StrategySelectionRecord(
                selection_key="primary",
                strategy_version="dvl-tw-v2-shadow",
                mode="shadow",
                changed_at=datetime.now(ZoneInfo("Asia/Taipei")),
                changed_by="system",
                reason="default candidate",
            )
        return row

    def set_strategy_selection(
        self,
        *,
        strategy_version: str,
        mode: str,
        changed_by: str,
        reason: str,
    ) -> StrategySelectionRecord:
        row = self.get_strategy_selection()
        self.session.add(row)
        row.strategy_version = strategy_version
        row.mode = mode
        row.changed_at = datetime.now(ZoneInfo("Asia/Taipei"))
        row.changed_by = changed_by
        row.reason = reason
        self.session.flush()
        return row

    def append_paper_evidence(
        self,
        *,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        parent_id: str | None = None,
        formula_or_rule: str | None = None,
    ) -> PaperEvidenceRecord:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        row = PaperEvidenceRecord(
            evidence_id=str(uuid4()),
            run_id=run_id,
            parent_id=parent_id,
            kind=kind,
            created_at=datetime.now(ZoneInfo("Asia/Taipei")),
            formula_or_rule=formula_or_rule,
            evidence_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            payload=payload,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def paper_evidence(self, run_id: str) -> list[PaperEvidenceRecord]:
        query = (
            select(PaperEvidenceRecord)
            .where(PaperEvidenceRecord.run_id == run_id)
            .order_by(PaperEvidenceRecord.created_at)
        )
        return list(self.session.scalars(query))
