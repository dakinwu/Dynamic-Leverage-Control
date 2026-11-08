from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from dynamic_leverage.infrastructure.database import Base


class OperationsStateRecord(Base):
    __tablename__ = "operations_projection_state"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    state: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NotificationOutboxRecord(Base):
    __tablename__ = "notification_outbox"
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    channel: Mapped[str] = mapped_column(String(180))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)


class RawSnapshotRecord(Base):
    __tablename__ = "raw_snapshots"
    __table_args__ = (UniqueConstraint("source", "checksum", name="uq_raw_source_checksum"),)

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str] = mapped_column(String(255), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    quality: Mapped[str] = mapped_column(String(32))
    checksum: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class StrategyEvaluationRecord(Base):
    __tablename__ = "strategy_evaluations"

    evaluation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    strategy_version: Mapped[str] = mapped_column(String(80), index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid: Mapped[bool] = mapped_column(Boolean)
    raw_target: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    target_exposure: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    safety_state: Mapped[str] = mapped_column(String(32))
    reasons: Mapped[list[str]] = mapped_column(JSON)
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSON)
    input_snapshot_ids: Mapped[list[str]] = mapped_column(JSON)
    state_before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    state_after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class StrategyPolicyStateRecord(Base):
    """Current projection of a policy's state; immutable evaluations remain the audit trail."""

    __tablename__ = "strategy_policy_state"

    strategy_version: Mapped[str] = mapped_column(String(80), primary_key=True)
    last_advanced_as_of: Mapped[date] = mapped_column(Date, index=True)
    state_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class StrategyLedgerStateRecord(Base):
    """Current projection of capital assigned only to this strategy."""

    __tablename__ = "strategy_ledger_state"

    ledger_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    capital_budget: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    net_contributions: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    cash: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    positions: Mapped[dict[str, str]] = mapped_column(JSON)
    last_prices: Mapped[dict[str, str]] = mapped_column(JSON)
    current_nav: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    peak_nav: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    principal_loss_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    principal_loss_ratio: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    current_drawdown: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    maximum_drawdown: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    position_revision: Mapped[int] = mapped_column(default=0)
    reconciled: Mapped[bool] = mapped_column(Boolean, index=True)
    reconciliation_reason: Mapped[str | None] = mapped_column(Text)
    last_account_snapshot_id: Mapped[str | None] = mapped_column(String(36), index=True)
    initialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actual_exposure: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    account_exposure: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    exposure_quality: Mapped[str] = mapped_column(String(64))


class StrategyLedgerEventRecord(Base):
    """Append-only evidence used to reconstruct the strategy ledger."""

    __tablename__ = "strategy_ledger_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    account_snapshot_id: Mapped[str | None] = mapped_column(String(36), index=True)
    event_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class RiskEvaluationRecord(Base):
    __tablename__ = "risk_evaluations"

    evaluation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    strategy_evaluation_id: Mapped[str] = mapped_column(String(36), index=True)
    approved: Mapped[bool] = mapped_column(Boolean)
    allowed_exposure: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    safety_state: Mapped[str] = mapped_column(String(32))
    risk_reducing_only: Mapped[bool] = mapped_column(Boolean)
    reasons: Mapped[list[str]] = mapped_column(JSON)


class TradeIntentRecord(Base):
    __tablename__ = "trade_intents"

    intent_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    strategy_version: Mapped[str] = mapped_column(String(80))
    execution_recipe: Mapped[str] = mapped_column(String(80))
    current_exposure: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    requested_exposure: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    reason_codes: Mapped[list[str]] = mapped_column(JSON)
    maturity: Mapped[str] = mapped_column(String(32))


class BrokerEventRecord(Base):
    __tablename__ = "broker_events"
    __table_args__ = (UniqueConstraint("broker", "broker_event_id", name="uq_broker_event"),)

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    broker: Mapped[str] = mapped_column(String(32), index=True)
    broker_event_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PaperOrderRecord(Base):
    __tablename__ = "paper_orders"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    instrument: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int]
    limit_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text)


class PaperFillRecord(Base):
    __tablename__ = "paper_fills"

    fill_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    instrument: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int]
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    commission: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    tax: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    settlement_date: Mapped[date] = mapped_column(Date, index=True)


class FreshnessContractRecord(Base):
    """Mutable freshness contract used by the operator read model."""

    __tablename__ = "freshness_contracts"

    contract_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str] = mapped_column(String(255), index=True)
    owner: Mapped[str] = mapped_column(String(120))
    max_age_minutes: Mapped[int] = mapped_column()
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AutomationStatusRecord(Base):
    """Latest status for a scheduler automation lane."""

    __tablename__ = "automation_status"

    automation_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OperatorQueueItemRecord(Base):
    """Current projection of an operator issue; history lives in events."""

    __tablename__ = "operator_queue_items"

    item_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text)
    impact: Mapped[str] = mapped_column(Text)
    next_action: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(120))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(120))
    reopened_count: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OperatorQueueEventRecord(Base):
    __tablename__ = "operator_queue_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(120), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)


class ManualHaltStateRecord(Base):
    __tablename__ = "manual_halt_state"

    halt_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(120))
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ManualHaltEventRecord(Base):
    __tablename__ = "manual_halt_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    actor: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class StrategySelectionRecord(Base):
    __tablename__ = "strategy_selection"

    selection_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    strategy_version: Mapped[str] = mapped_column(String(80))
    mode: Mapped[str] = mapped_column(String(16), default="paper")
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    changed_by: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text)


class PaperEvidenceRecord(Base):
    """Append-only links for the Paper decision evidence chain."""

    __tablename__ = "paper_evidence"

    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    formula_or_rule: Mapped[str | None] = mapped_column(String(255))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
