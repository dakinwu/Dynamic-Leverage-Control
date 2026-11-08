"""Read-only operational projection and explicit durable reconciliation command."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from dynamic_leverage.api.schemas import (
    AutomationStatusSchema,
    ExposureHistoryPointSchema,
    ManualHaltSchema,
    OperationsSummaryResponse,
    QueueItemSchema,
    StrategySelectionSchema,
)
from dynamic_leverage.infrastructure.brokers.shioaji import load_credentials
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.infrastructure.tables import NotificationOutboxRecord, OperationsStateRecord
from dynamic_leverage.services.account_sync import binding_fingerprint
from dynamic_leverage.services.freshness import ensure_freshness_contracts, freshness_rows
from dynamic_leverage.services.notifications import send_telegram_message
from dynamic_leverage.services.operations import (
    AccountPositionSummary,
    OperationsInputs,
    derive_operations_summary,
)
from dynamic_leverage.services.research_evidence import research_evidence


def queue_payload(row) -> QueueItemSchema:
    return QueueItemSchema(
        item_id=row.item_id,
        severity=row.severity,
        status=row.status,
        title=row.title,
        detail=row.detail,
        impact=row.impact,
        next_action=row.next_action,
        evidence_refs=list(row.evidence_refs or []),
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
        reopened_count=row.reopened_count,
    )


def build_operations(repository, settings, *, system=None, now=None):
    if system is None:
        present = False
        if settings.shioaji_secret_file:
            try:
                load_credentials(settings.shioaji_secret_file)
                present = True
            except (OSError, ValueError):
                pass
        system = SimpleNamespace(
            database="ok", shioaji_mode=settings.shioaji_mode, shioaji_secret_present=present
        )
    decisions = repository.latest_strategy_decisions()
    selected = repository.get_strategy_selection()
    recent_snapshots = repository.latest_raw_snapshots(limit=10)
    market_snapshot_count = sum(
        snapshot.kind != "broker.account.shioaji" for snapshot in recent_snapshots
    )
    selected_latest = next(
        (row for row in decisions if row.strategy_version == selected.strategy_version),
        None,
    )
    # The selected strategy is the sole source of the current target.  Do
    # not silently fall back to a valid historical strategy when the
    # selected candidate is invalid; that creates a false buy/sell signal.
    preferred = selected_latest
    account_snapshot = None
    if settings.shioaji_account_id:
        source = f"shioaji:account:{binding_fingerprint(settings.shioaji_account_id)}"
        account_snapshot = repository.latest_raw_snapshot(
            kind="broker.account.shioaji",
            source=source,
        )
    account_payload = account_snapshot.payload if account_snapshot is not None else {}
    ledger = repository.get_strategy_ledger_state()
    freshness = freshness_rows(repository, now=now)
    stale_required = tuple(
        item.contract_key
        for item in freshness
        if item.status != "FRESH" and item.contract_key != "capital_00685l_pcf"
    )
    dvl_latest = next(
        (row for row in decisions if row.strategy_version == "dvl-tw-v2-shadow"),
        None,
    )
    missing_text = dvl_latest.diagnostics.get("missing_inputs", "") if dvl_latest else ""
    dvl_missing = tuple(reason for reason in str(missing_text).split(",") if reason)
    if dvl_latest and not dvl_latest.valid and not dvl_missing:
        dvl_missing = tuple(
            reason
            for reason in dvl_latest.reasons
            if reason in {"pe_percentile", "eps_yoy", "rv20", "taiwan_vix", "taiex_close"}
        )
    summary = derive_operations_summary(
        OperationsInputs(
            database_ok=system.database == "ok",
            shioaji_secret_present=system.shioaji_secret_present,
            shioaji_mode=system.shioaji_mode,
            account_binding_configured=bool(settings.shioaji_account_id),
            planning_nav=Decimal(settings.initial_strategy_nav),
            snapshot_count=market_snapshot_count,
            target_exposure=preferred.target_exposure if preferred else None,
            strategy_nav=ledger.current_nav if ledger is not None else None,
            account_snapshot_present=account_snapshot is not None,
            account_reconciled=bool(
                ledger and ledger.reconciled and ledger.actual_exposure is not None
            ),
            account_masked=account_payload.get("account_masked"),
            account_quality=(
                account_snapshot.quality if account_snapshot is not None else "unknown"
            ),
            account_observed_at=(
                account_snapshot.observed_at if account_snapshot is not None else None
            ),
            observed_nav=(
                Decimal(account_payload["nav"]) if account_payload.get("nav") is not None else None
            ),
            strategy_cash=ledger.cash if ledger is not None else None,
            net_contributions=ledger.net_contributions if ledger is not None else None,
            peak_nav=ledger.peak_nav if ledger is not None else None,
            principal_loss_amount=(ledger.principal_loss_amount if ledger is not None else None),
            principal_loss_ratio=(ledger.principal_loss_ratio if ledger is not None else None),
            current_drawdown=ledger.current_drawdown if ledger is not None else None,
            maximum_drawdown=ledger.maximum_drawdown if ledger is not None else None,
            capital_loss_limit_breached=bool(
                ledger
                and (
                    ledger.principal_loss_ratio >= Decimal("0.40")
                    or ledger.maximum_drawdown >= Decimal("0.40")
                )
            ),
            pending_settlement=(
                Decimal(account_payload["pending_settlement"])
                if account_payload.get("pending_settlement") is not None
                else None
            ),
            actual_exposure=ledger.actual_exposure if ledger is not None else None,
            account_exposure=ledger.account_exposure if ledger is not None else None,
            exposure_quality=ledger.exposure_quality if ledger is not None else "unknown",
            positions=tuple(
                AccountPositionSummary(
                    instrument=position["instrument"],
                    quantity=Decimal(position["quantity"]),
                    last_price=Decimal(position["last_price"]),
                    market_value=Decimal(position["market_value"]),
                )
                for position in account_payload.get("positions", [])
            ),
            external_instruments=tuple(account_payload.get("external_instruments", [])),
            selected_strategy_version=selected.strategy_version,
            selected_strategy_valid=bool(preferred and preferred.valid),
            strategy_missing_inputs=(
                dvl_missing if selected.strategy_version == "dvl-tw-v2-shadow" else ()
            ),
            valuation_points=(
                int(dvl_latest.diagnostics.get("valuation_points", 0)) if dvl_latest else 0
            ),
            valuation_required_points=120,
            stale_required_contracts=stale_required,
        )
    )
    exposure_history: list[ExposureHistoryPointSchema] = []
    for ledger_event in reversed(repository.strategy_ledger_events(limit=30)):
        event_payload = ledger_event.payload or {}
        actual_value = event_payload.get("actual_exposure")
        if actual_value is None:
            continue
        exposure_history.append(
            ExposureHistoryPointSchema(
                observed_at=ledger_event.occurred_at,
                actual_exposure=Decimal(str(actual_value)),
                target_exposure=(preferred.target_exposure if preferred is not None else None),
            )
        )
    active_queue: list[dict[str, object]] = []
    for item in summary.activation_items:
        if item.status == "ready":
            continue
        active_queue.append(
            {
                "item_id": f"activation:{item.key}",
                "severity": "BLOCKER" if item.status == "blocked" else "ACTION",
                "title": item.label,
                "detail": item.detail,
                "impact": "阻止可信任的策略判定或 Paper 動作。",
                "next_action": summary.next_action,
                "evidence_refs": [],
            }
        )
    for item in freshness:
        if item.status != "FRESH" and item.contract_key != "capital_00685l_pcf":
            active_queue.append(
                {
                    "item_id": f"freshness:{item.contract_key}",
                    "severity": "BLOCKER",
                    "title": f"資料來源 {item.owner} {item.status}",
                    "detail": item.reason,
                    "impact": "資料未達 Freshness Contract，禁止增加曝險。",
                    "next_action": "重新執行資料同步，並確認來源時間與 checksum。",
                    "evidence_refs": [item.snapshot_id] if item.snapshot_id else [],
                }
            )
    halt = repository.get_manual_halt()
    if halt.active:
        active_queue.append(
            {
                "item_id": "risk:manual-halt",
                "severity": "BLOCKER",
                "title": "Manual Halt 已啟用",
                "detail": halt.reason or "操作員暫停策略動作。",
                "impact": "禁止新的紙上交易模擬；資料同步與對帳仍會繼續。",
                "next_action": "確認原因後於風控頁解除 Manual Halt。",
                "evidence_refs": [],
            }
        )
    if (
        summary.exposure.target_exposure is not None
        and summary.exposure.actual_exposure is not None
        and abs(summary.exposure.actual_exposure - summary.exposure.target_exposure)
        > Decimal("0.05")
        and not halt.active
    ):
        direction = (
            "增加"
            if summary.exposure.actual_exposure < summary.exposure.target_exposure
            else "降低"
        )
        active_queue.append(
            {
                "item_id": "exposure:rebalance-review",
                "severity": "ACTION",
                "title": f"曝險偏離目標，建議{direction}曝險",
                "detail": (
                    f"Target {summary.exposure.target_exposure}x / "
                    f"Actual {summary.exposure.actual_exposure}x。"
                ),
                "impact": "僅建立紙上交易模擬，不會送出正式委託。",
                "next_action": "查看策略工作表與 Risk Gate 證據，再決定是否模擬這次調整。",
                "evidence_refs": [],
            }
        )
    queue = repository.queue_items()
    automation = repository.list_automation_status()
    if not automation:
        automation_payload = [
            AutomationStatusSchema(
                automation_id="snapshot-scheduler",
                status="NOT_STARTED",
                last_started_at=None,
                last_success_at=None,
                next_run_at=None,
                last_run_id=None,
                failure_reason="尚未收到 Scheduler 心跳。",
            )
        ]
    else:
        automation_payload = [
            AutomationStatusSchema(
                automation_id=row.automation_id,
                status=row.status,
                last_started_at=row.last_started_at,
                last_success_at=row.last_success_at,
                next_run_at=row.next_run_at,
                last_run_id=row.last_run_id,
                failure_reason=row.failure_reason,
            )
            for row in automation
        ]
    payload = asdict(summary)
    payload["research"] = research_evidence(selected.strategy_version)
    payload.update(
        {
            "strategy": StrategySelectionSchema(
                strategy_version=selected.strategy_version,
                mode=selected.mode,
                changed_at=selected.changed_at,
                changed_by=selected.changed_by,
                reason=selected.reason,
            ).model_dump(),
            "queue": [queue_payload(row).model_dump() for row in queue],
            "freshness": [item.model_dump() for item in freshness],
            "automation": [item.model_dump() for item in automation_payload],
            "manual_halt": ManualHaltSchema(
                active=halt.active,
                reason=halt.reason,
                actor=halt.actor,
                enabled_at=halt.enabled_at,
                released_at=halt.released_at,
                release_reason=halt.release_reason,
            ).model_dump(),
            "exposure_history": [item.model_dump() for item in exposure_history],
        }
    )
    projection = repository.session.get(OperationsStateRecord, "projection")
    payload["operations_updated_at"] = projection.updated_at if projection else None
    return OperationsSummaryResponse.model_validate(payload), active_queue


def reconcile_operations(session, settings, *, now=None):
    """Persist conditions and outbox atomically; caller commits before dispatch."""
    now = now or datetime.now(UTC)
    repo = AuditRepository(session)
    ensure_freshness_contracts(repo)
    before = {row.item_id: row.status for row in repo.queue_items()}
    summary, active = build_operations(repo, settings, now=now)
    queue = repo.reconcile_queue(active)
    unsafe = sorted(
        item.contract_key
        for item in summary.freshness
        if item.status != "FRESH" and item.contract_key != "capital_00685l_pcf"
    )
    value = "UNSAFE:" + ",".join(unsafe) if unsafe else "FRESH"
    state = session.get(OperationsStateRecord, "freshness")
    if state is None or state.state != value:
        session.add(
            NotificationOutboxRecord(
                event_id=str(uuid4()),
                channel="data_freshness",
                message=f"DVL 資料狀態 {value}\n必要日期或資料未驗證時禁止增加曝險。",
                created_at=now,
                attempts=0,
            )
        )
        if state is None:
            state = OperationsStateRecord(key="freshness")
            session.add(state)
        state.state, state.updated_at = value, now
    for row in queue:
        if before.get(row.item_id) != row.status:
            session.add(
                NotificationOutboxRecord(
                    event_id=str(uuid4()),
                    channel=f"queue:{row.item_id}",
                    message=f"DVL Operator Queue {row.status}\n{row.title}\n{row.detail}",
                    created_at=now,
                    attempts=0,
                )
            )
    state = session.get(OperationsStateRecord, "projection")
    if state is None:
        state = OperationsStateRecord(key="projection")
        session.add(state)
    state.state, state.updated_at = summary.operator_status, now
    repo.upsert_automation_status(
        automation_id="operations-projection",
        status="SUCCESS",
        last_success_at=now,
        failure_reason=None,
    )
    session.flush()
    return summary


def dispatch_notifications(factory, secret_file, *, sender=send_telegram_message):
    """Deliver only committed events. Acknowledged sends survive worker restarts.

    Network delivery and database commit cannot be atomic: a crash in that
    narrow window can duplicate a message. Failed sends remain pending.
    """
    if secret_file is None:
        return 0
    sent = 0
    with factory.begin() as session:
        rows = session.scalars(
            select(NotificationOutboxRecord)
            .where(NotificationOutboxRecord.delivered_at.is_(None))
            .order_by(NotificationOutboxRecord.created_at, NotificationOutboxRecord.event_id)
            .limit(20)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            row.attempts += 1
            if sender(secret_file, row.message):
                row.delivered_at = datetime.now(UTC)
                sent += 1
    return sent
