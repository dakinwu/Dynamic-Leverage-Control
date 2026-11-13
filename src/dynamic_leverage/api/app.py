from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from dynamic_leverage.api.schemas import (
    AccountSyncResponse,
    AllocationRequest,
    AllocationResponse,
    AllocationSchema,
    AutomationStatusSchema,
    BacktestMetricsSchema,
    BacktestRequest,
    BacktestResponse,
    DashboardResponse,
    DashboardSnapshotSchema,
    DashboardStrategySchema,
    DividendConfirmationRequest,
    FreshnessStatusSchema,
    ManualHaltRequest,
    ManualHaltSchema,
    OperationsSummaryResponse,
    PaperEvidenceSchema,
    PaperIntentRequest,
    PaperIntentResponse,
    PolicyStateSchema,
    QueueItemSchema,
    QueueTransitionRequest,
    RiskDecisionSchema,
    RiskEvaluateRequest,
    StrategyDecisionSchema,
    StrategyEvaluateRequest,
    StrategyFillRequest,
    StrategyFillResponse,
    StrategyLedgerStateSchema,
    StrategySelectionRequest,
    StrategySelectionSchema,
    SystemStatusResponse,
)
from dynamic_leverage.config import Settings, get_settings
from dynamic_leverage.domain.account import AccountSnapshotReader
from dynamic_leverage.domain.backtest import BacktestEngine, BacktestPoint
from dynamic_leverage.domain.execution import Allocation, OneXTwoXRecipe, TwoXCashRecipe
from dynamic_leverage.domain.models import PolicyState, StrategyInputs
from dynamic_leverage.domain.risk import RiskGate
from dynamic_leverage.domain.strategies import (
    ConstantExposurePolicy,
    DvlV2Policy,
    DvlV10Policy,
    DvlV11Policy,
)
from dynamic_leverage.infrastructure.brokers.shioaji import (
    ShioajiAccountSnapshotReader,
    ShioajiUnavailable,
    load_credentials,
)
from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import (
    MANUAL_EVALUATION_TRIGGER,
    AuditRepository,
    LedgerWriteConflict,
)
from dynamic_leverage.infrastructure.tables import (  # noqa: F401
    AutomationStatusRecord,
    BrokerEventRecord,
    FreshnessContractRecord,
    ManualHaltStateRecord,
    OperatorQueueItemRecord,
    PaperEvidenceRecord,
    PaperFillRecord,
    PaperOrderRecord,
    RawSnapshotRecord,
    RiskEvaluationRecord,
    StrategyEvaluationRecord,
    StrategyLedgerEventRecord,
    StrategyLedgerStateRecord,
    StrategySelectionRecord,
    TradeIntentRecord,
)
from dynamic_leverage.services import notifications
from dynamic_leverage.services.account_sync import (
    mask_binding,
    sync_account_snapshot,
)
from dynamic_leverage.services.freshness import (
    freshness_rows as _freshness_rows,
)
from dynamic_leverage.services.ledger import confirm_dividend_payment, record_strategy_fill
from dynamic_leverage.services.operations_projection import (
    OUTBOX_PENDING,
    build_operations,
    dispatch_notifications,
    enqueue_notification,
)
from dynamic_leverage.services.operations_projection import queue_payload as _queue_payload
from dynamic_leverage.services.research_evidence import REFERENCE, research_evidence
from dynamic_leverage.services.risk_context import (
    ClientRiskClaims,
    gate_allows_target,
    server_risk_state,
)
from dynamic_leverage.services.risk_context import evaluate_risk as evaluate_risk_context


def _send_telegram(path: Path | None, message: str) -> bool:
    # Resolved at call time so tests and the runtime share one replaceable sender.
    return notifications.send_telegram_message(path, message)


def get_session(request: Request, background_tasks: BackgroundTasks):
    """Commit before the response is sent; deliver staged notifications afterwards.

    Used with ``scope="function"`` so a failed commit surfaces as a 5xx instead
    of following an already-sent 200. Outbox rows commit atomically with the
    state they describe, and delivery only ever reads committed rows.
    """
    session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
        notifications_staged = session.info.pop(OUTBOX_PENDING, False)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    if notifications_staged:
        background_tasks.add_task(
            dispatch_notifications,
            request.app.state.session_factory,
            request.app.state.telegram_secret_file,
            sender=_send_telegram,
        )


SessionDependency = Annotated[Session, Depends(get_session, scope="function")]

# The API has no authentication and is local-only. These guards stop another
# web page in the operator's browser from driving it: a foreign Host header
# (DNS rebinding) is refused, and every state-changing request must carry a
# custom header, which a cross-site page cannot send without a CORS preflight
# that only WEB_ORIGIN passes, plus a JSON body (no form or text/plain posts).
LOCAL_HOSTS = ["localhost", "127.0.0.1"]
CONSOLE_HEADER = "X-DVL-Client"
CONSOLE_HEADER_VALUE = "console"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Documentation only (enforcement is the middleware): lets Swagger UI at /docs
# send the header after "Authorize" with the value "console".
_console_header_scheme = APIKeyHeader(
    name=CONSOLE_HEADER,
    auto_error=False,
    description=f"Local console marker; use the value '{CONSOLE_HEADER_VALUE}'.",
)


async def _require_console_json(request: Request, call_next):
    if request.method not in _SAFE_METHODS:
        if request.headers.get(CONSOLE_HEADER) != CONSOLE_HEADER_VALUE:
            return JSONResponse(
                status_code=403,
                content={"detail": f"missing {CONSOLE_HEADER} header"},
            )
        content_type = request.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        has_body = request.headers.get("content-length", "0") != "0" or (
            "transfer-encoding" in request.headers
        )
        if (content_type or has_body) and media_type != "application/json":
            return JSONResponse(
                status_code=415,
                content={"detail": "request body must be application/json"},
            )
    return await call_next(request)


TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def _strategy(version: str):
    strategies = {
        "constant-1.2-v1": ConstantExposurePolicy(),
        "constant-2.0-v1": ConstantExposurePolicy(target=Decimal("2.0"), version="constant-2.0-v1"),
        "dvl-tw-v1.0": DvlV10Policy(),
        "dvl-tw-v1.1-raw": DvlV11Policy(),
        "dvl-tw-v1.1-winsorized": DvlV11Policy(winsorized=True),
        "dvl-tw-v2-shadow": DvlV2Policy(),
    }
    try:
        return strategies[version]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown strategy version") from exc


def _recipe(version: str):
    recipes = {
        "2x-cash-v1": TwoXCashRecipe(),
        "1x-2x-v1": OneXTwoXRecipe(),
    }
    try:
        return recipes[version]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown execution recipe") from exc


def _allocation_payload(allocations: tuple[Allocation, ...]) -> list[dict[str, str]]:
    """Serialize Decimal allocation weights for JSON evidence storage."""
    return [
        {
            "instrument": str(item.instrument),
            "weight": str(item.weight),
            "assumed_exposure": str(item.assumed_exposure),
        }
        for item in allocations
    ]


def create_app(
    settings: Settings | None = None,
    account_reader_factory: Callable[[Path], AccountSnapshotReader] | None = None,
) -> FastAPI:
    runtime_settings = settings or get_settings()
    make_account_reader = account_reader_factory or (
        lambda secret_file: ShioajiAccountSnapshotReader(secret_file=secret_file)
    )
    engine = make_engine(runtime_settings.database_url)
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if runtime_settings.auto_create_schema:
            Base.metadata.create_all(engine)
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.telegram_secret_file = runtime_settings.telegram_secret_file
        yield
        engine.dispose()

    app = FastAPI(
        title="Dynamic Leverage Control",
        version="0.1.0",
        lifespan=lifespan,
        dependencies=[Security(_console_header_scheme)],
    )
    # Starlette runs the last-added middleware first: Host check, then CORS
    # (so guard rejections stay readable by the console), then the POST guard.
    app.middleware("http")(_require_console_json)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[runtime_settings.web_origin],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", CONSOLE_HEADER],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)

    @app.get(
        "/api/v1/system",
        response_model=SystemStatusResponse,
        responses={503: {"model": SystemStatusResponse, "description": "database unavailable"}},
    )
    def system_status(session: SessionDependency) -> SystemStatusResponse | JSONResponse:
        database = "ok"
        try:
            session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            database = "unavailable"
        secret_present = False
        if runtime_settings.shioaji_secret_file:
            try:
                if runtime_settings.shioaji_secret_file.exists():
                    load_credentials(runtime_settings.shioaji_secret_file)
                    secret_present = True
            except (OSError, ValueError):
                secret_present = False
        status = SystemStatusResponse(
            status="ok" if database == "ok" else "degraded",
            environment=runtime_settings.app_env,
            database=database,
            production_order_submission="disabled",
            shioaji_mode=runtime_settings.shioaji_mode,
            shioaji_secret_present=secret_present,
            eligibility_gate="closed",
        )
        if database != "ok":
            # Health checks and monitors must see the outage, not a 200.
            session.rollback()
            return JSONResponse(status_code=503, content=status.model_dump(mode="json"))
        return status

    @app.get("/api/v1/dashboard", response_model=DashboardResponse)
    def dashboard(session: SessionDependency) -> DashboardResponse:
        repository = AuditRepository(session)
        selected = repository.get_strategy_selection()
        decisions = repository.latest_strategy_decisions()
        recent_snapshots = repository.latest_raw_snapshots(limit=10)
        market_snapshots = [
            snapshot for snapshot in recent_snapshots if snapshot.kind != "broker.account.shioaji"
        ]
        strategies = [
            DashboardStrategySchema(
                version=row.strategy_version,
                as_of=row.as_of,
                target_exposure=row.target_exposure,
                valid=row.valid,
                safety_state=row.safety_state,
                reasons=row.reasons,
                diagnostics=row.diagnostics,
            )
            for row in decisions
        ]
        snapshots = [
            DashboardSnapshotSchema(
                snapshot_id=row.snapshot_id,
                kind=row.kind,
                source=(
                    "shioaji:account:<redacted>"
                    if row.kind == "broker.account.shioaji"
                    else row.source
                ),
                observed_at=row.observed_at,
                received_at=row.received_at,
                quality=row.quality,
            )
            for row in recent_snapshots
        ]
        quote_snapshot = repository.latest_raw_snapshot(kind="market.twse.daily_quotes")
        dashboard_ledger = repository.get_strategy_ledger_state()
        market_prices: dict[str, Decimal] = {}
        if quote_snapshot is not None:
            for quote in (quote_snapshot.payload or {}).get("quotes", []):
                try:
                    market_prices[str(quote["code"])] = Decimal(str(quote["close"]))
                except (KeyError, TypeError, ValueError):
                    continue
        alerts = ["生產下單路徑已硬鎖；目前僅允許 Shadow / Paper。"]
        if not market_snapshots:
            alerts.append("尚未匯入權威資料快照；所有資料型策略維持 fail-closed。")
        if not strategies:
            alerts.append("尚未產生策略評估；請先匯入資料，再執行 Shadow 評估。")
        selected_run = next(
            (item for item in strategies if item.version == selected.strategy_version),
            None,
        )
        if selected_run is not None and not selected_run.valid:
            alerts.append(
                f"目前選定策略 {selected.strategy_version} 尚未完成評估；不會沿用舊目標。"
            )
        return DashboardResponse(
            status=(
                "AWAITING_DATA"
                if (
                    not market_snapshots
                    or not strategies
                    or (selected_run is not None and not selected_run.valid)
                )
                else "MONITORING"
            ),
            account_nav=(
                dashboard_ledger.current_nav
                if dashboard_ledger is not None
                else Decimal(runtime_settings.initial_strategy_nav)
            ),
            exposure_ceiling=Decimal("2.0"),
            live_canary_budget=Decimal("10000"),
            production_orders_enabled=False,
            strategies=strategies,
            snapshots=snapshots,
            alerts=alerts,
            selected_strategy=StrategySelectionSchema(
                strategy_version=selected.strategy_version,
                mode=selected.mode,
                changed_at=selected.changed_at,
                changed_by=selected.changed_by,
                reason=selected.reason,
            ),
            market_prices=market_prices,
        )

    @app.get("/api/v1/data/acceptance")
    def data_acceptance(session: SessionDependency) -> dict[str, object]:
        from dynamic_leverage.services.data_acceptance import audit_data

        return audit_data(AuditRepository(session))

    @app.get("/api/v1/operations/summary", response_model=OperationsSummaryResponse)
    def operations_summary(session: SessionDependency) -> OperationsSummaryResponse:
        summary, _ = build_operations(
            AuditRepository(session), runtime_settings, system=system_status(session)
        )
        return summary

    @app.get("/api/v1/research")
    def research_registry(session: SessionDependency):
        return research_evidence(AuditRepository(session).get_strategy_selection().strategy_version)

    @app.get("/api/v1/research/rearm/report", response_class=PlainTextResponse)
    def research_report():
        if research_evidence("")["evidence_status"] != "verified-archive":
            raise HTTPException(status_code=409, detail="research evidence invalid")
        return (REFERENCE / "rearm-findings.md").read_text(encoding="utf-8")

    @app.get("/api/v1/operations/queue", response_model=list[QueueItemSchema])
    def operations_queue(session: SessionDependency) -> list[QueueItemSchema]:
        return [_queue_payload(row) for row in AuditRepository(session).queue_items()]

    @app.post("/api/v1/operations/queue/{item_id}/acknowledge", response_model=QueueItemSchema)
    def acknowledge_queue_item(
        item_id: str,
        payload: QueueTransitionRequest,
        session: SessionDependency,
    ) -> QueueItemSchema:
        try:
            row = AuditRepository(session).transition_queue_item(
                item_id,
                target_status="ACKNOWLEDGED",
                actor=payload.actor,
                reason=payload.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if row is None:
            raise HTTPException(status_code=404, detail="queue item not found")
        enqueue_notification(
            session,
            f"queue:{row.item_id}",
            (
                f"DVL Operator Queue {row.status}\n{row.title}\n"
                f"處理人：{payload.actor}\n原因：{payload.reason}"
            ),
        )
        return _queue_payload(row)

    @app.post("/api/v1/operations/queue/{item_id}/resolve", response_model=QueueItemSchema)
    def resolve_queue_item(
        item_id: str,
        payload: QueueTransitionRequest,
        session: SessionDependency,
    ) -> QueueItemSchema:
        try:
            row = AuditRepository(session).transition_queue_item(
                item_id,
                target_status="RESOLVED",
                actor=payload.actor,
                reason=payload.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if row is None:
            raise HTTPException(status_code=404, detail="queue item not found")
        enqueue_notification(
            session,
            f"queue:{row.item_id}",
            (
                f"DVL Operator Queue {row.status}\n{row.title}\n"
                f"處理人：{payload.actor}\n原因：{payload.reason}"
            ),
        )
        return _queue_payload(row)

    @app.get("/api/v1/data/freshness", response_model=list[FreshnessStatusSchema])
    def data_freshness(session: SessionDependency) -> list[FreshnessStatusSchema]:
        repository = AuditRepository(session)
        return _freshness_rows(repository)

    @app.get("/api/v1/automation/status", response_model=list[AutomationStatusSchema])
    def automation_status(session: SessionDependency) -> list[AutomationStatusSchema]:
        rows = AuditRepository(session).list_automation_status()
        return [
            AutomationStatusSchema(
                automation_id=row.automation_id,
                status=row.status,
                last_started_at=row.last_started_at,
                last_success_at=row.last_success_at,
                next_run_at=row.next_run_at,
                last_run_id=row.last_run_id,
                failure_reason=row.failure_reason,
            )
            for row in rows
        ]

    @app.post("/api/v1/risk/manual-halt", response_model=ManualHaltSchema)
    def enable_manual_halt(
        payload: ManualHaltRequest,
        session: SessionDependency,
    ) -> ManualHaltSchema:
        repository = AuditRepository(session)
        was_active = repository.get_manual_halt().active
        row = repository.set_manual_halt(
            active=True,
            actor=payload.actor,
            reason=payload.reason,
        )
        if not was_active:
            enqueue_notification(
                session,
                "manual_halt",
                f"DVL Manual Halt 已啟用\n原因：{payload.reason}\n操作者：{payload.actor}",
                state="ACTIVE",
            )
        return ManualHaltSchema(
            active=row.active,
            reason=row.reason,
            actor=row.actor,
            enabled_at=row.enabled_at,
            released_at=row.released_at,
            release_reason=row.release_reason,
        )

    @app.post("/api/v1/risk/manual-halt/release", response_model=ManualHaltSchema)
    def release_manual_halt(
        payload: ManualHaltRequest,
        session: SessionDependency,
    ) -> ManualHaltSchema:
        repository = AuditRepository(session)
        was_active = repository.get_manual_halt().active
        row = repository.set_manual_halt(
            active=False,
            actor=payload.actor,
            reason=payload.reason,
        )
        if was_active:
            enqueue_notification(
                session,
                "manual_halt",
                f"DVL Manual Halt 已解除\n原因：{payload.reason}\n操作者：{payload.actor}",
                state="RELEASED",
            )
        return ManualHaltSchema(
            active=row.active,
            reason=row.reason,
            actor=row.actor,
            enabled_at=row.enabled_at,
            released_at=row.released_at,
            release_reason=row.release_reason,
        )

    @app.get("/api/v1/strategies/selection", response_model=StrategySelectionSchema)
    def strategy_selection(session: SessionDependency) -> StrategySelectionSchema:
        row = AuditRepository(session).get_strategy_selection()
        return StrategySelectionSchema(
            strategy_version=row.strategy_version,
            mode=row.mode,
            changed_at=row.changed_at,
            changed_by=row.changed_by,
            reason=row.reason,
        )

    @app.post("/api/v1/strategies/selection", response_model=StrategySelectionSchema)
    def set_strategy_selection(
        payload: StrategySelectionRequest,
        session: SessionDependency,
    ) -> StrategySelectionSchema:
        policy = _strategy(payload.strategy_version)
        mode = "shadow" if payload.strategy_version == "dvl-tw-v2-shadow" else payload.mode
        repository = AuditRepository(session)
        row = repository.set_strategy_selection(
            strategy_version=payload.strategy_version,
            mode=mode,
            changed_by=payload.changed_by,
            reason=payload.reason,
        )
        # Constant baselines are data-independent.  Materialize a fresh,
        # immutable run at selection time so the overview can immediately show
        # its target instead of waiting for the next scheduler heartbeat.
        if payload.strategy_version in {"constant-1.2-v1", "constant-2.0-v1"}:
            decision = policy.evaluate(
                StrategyInputs(as_of=date.today(), critical_data_safe=True),
                PolicyState(),
            )
            decision = replace(
                decision,
                diagnostics={
                    **decision.diagnostics,
                    "run_id": str(uuid4()),
                    "trigger": "strategy-selection",
                },
            )
            repository.append_strategy_decision(decision, input_snapshot_ids=[])
        return StrategySelectionSchema(
            strategy_version=row.strategy_version,
            mode=row.mode,
            changed_at=row.changed_at,
            changed_by=row.changed_by,
            reason=row.reason,
        )

    @app.get("/api/v1/strategies/{version}/runs", response_model=list[DashboardStrategySchema])
    def strategy_runs(version: str, session: SessionDependency) -> list[DashboardStrategySchema]:
        if version not in {
            "constant-1.2-v1",
            "constant-2.0-v1",
            "dvl-tw-v1.0",
            "dvl-tw-v1.1-raw",
            "dvl-tw-v1.1-winsorized",
            "dvl-tw-v2-shadow",
        }:
            raise HTTPException(status_code=404, detail="unknown strategy version")
        rows = AuditRepository(session).strategy_decisions(version)
        return [
            DashboardStrategySchema(
                version=row.strategy_version,
                as_of=row.as_of,
                target_exposure=row.target_exposure,
                valid=row.valid,
                safety_state=row.safety_state,
                reasons=row.reasons,
                diagnostics={**row.diagnostics, "evaluation_id": row.evaluation_id},
            )
            for row in rows
        ]

    @app.post("/api/v1/paper/intents", response_model=PaperIntentResponse)
    def create_paper_intent(
        payload: PaperIntentRequest,
        session: SessionDependency,
    ) -> PaperIntentResponse:
        _strategy(payload.strategy_version)
        recipe = _recipe(payload.recipe)
        # Targets above the cash ceiling have no executable recipe; the intent is
        # still recorded so the EXPOSURE_CEILING block leaves an audit trail.
        allocations = (
            recipe.allocate(payload.requested_exposure)
            if payload.requested_exposure <= RiskGate.exposure_ceiling
            else ()
        )
        repository = AuditRepository(session)
        server = server_risk_state(
            repository, runtime_settings, strategy_version=payload.strategy_version
        )
        decision, context = evaluate_risk_context(
            server,
            requested_exposure=payload.requested_exposure,
            submitted_actual_exposure=payload.current_exposure,
        )
        effective_current_exposure = context.actual_exposure
        run_id = str(uuid4())
        evidence: list[PaperEvidenceRecord] = []
        strategy_record = repository.latest_strategy_decision(payload.strategy_version)
        evidence.append(
            repository.append_paper_evidence(
                run_id=run_id,
                kind="strategy_run",
                payload={
                    "strategy_version": payload.strategy_version,
                    "evaluation_id": strategy_record.evaluation_id if strategy_record else None,
                    "target_exposure": str(payload.requested_exposure),
                    "allocations": _allocation_payload(allocations),
                },
                formula_or_rule="immutable strategy evaluation + operator request",
            )
        )
        increase_requested = payload.requested_exposure > effective_current_exposure
        # Paper-only checks, then the shared Risk Gate verdict (first blocking
        # reason plus its safety state, e.g. RISK_REDUCING_ONLY + DATA_UNSAFE).
        reason_codes = ["PAPER_ONLY"]
        if payload.requested_exposure > RiskGate.exposure_ceiling:
            reason_codes.append("EXPOSURE_CEILING")
        if increase_requested and (strategy_record is None or not strategy_record.valid):
            reason_codes.append("STRATEGY_UNEVALUATED")
        if not gate_allows_target(decision, payload.requested_exposure):
            for code in (*decision.reason_codes, decision.safety_state.value):
                if code not in reason_codes and code != "NORMAL":
                    reason_codes.append(code)
        approved = len(reason_codes) == 1
        risk_state = "BLOCKED:" + ",".join(reason_codes) if not approved else "APPROVED"
        enqueue_notification(
            session,
            "risk_gate",
            (
                f"DVL Paper Risk Gate {'通過' if approved else '阻擋'}\n"
                f"目前曝險：{effective_current_exposure}x；要求：{payload.requested_exposure}x\n"
                f"原因：{', '.join(reason_codes)}\nRun {run_id}"
            ),
            state=risk_state,
        )
        risk = repository.append_paper_evidence(
            run_id=run_id,
            kind="risk_decision",
            parent_id=evidence[-1].evidence_id,
            payload={
                "approved": approved,
                "current_exposure": str(effective_current_exposure),
                "submitted_current_exposure": str(payload.current_exposure),
                "requested_exposure": str(payload.requested_exposure),
                "reason_codes": reason_codes,
                "risk_gate": {
                    "approved": decision.approved,
                    "allowed_exposure": str(decision.allowed_exposure),
                    "safety_state": decision.safety_state.value,
                    "reason_codes": list(decision.reason_codes),
                },
                "stale_required_contracts": list(server.stale_required),
                "account_truth_present": server.account_truth_present,
                "capital_loss_limit_breached": server.capital_loss_limit_breached,
                "strategy_state": context.strategy_state.value,
            },
            formula_or_rule="shared RiskGate on server risk context; cash ceiling 2.0x",
        )
        evidence.append(risk)
        intent = repository.append_paper_evidence(
            run_id=run_id,
            kind="trade_intent",
            parent_id=risk.evidence_id,
            payload={
                "strategy_version": payload.strategy_version,
                "recipe": payload.recipe,
                "current_exposure": str(effective_current_exposure),
                "requested_exposure": str(payload.requested_exposure),
                "reason": payload.reason,
                "allocations": _allocation_payload(allocations),
            },
            formula_or_rule=(
                "Paper only; no broker order submission; piecewise 0050/00631L/cash allocation"
            ),
        )
        evidence.append(intent)
        recommendation = (
            "已建立紙上調整紀錄：增加曝險"
            if payload.requested_exposure > effective_current_exposure
            else "已建立紙上調整紀錄：降低曝險"
            if payload.requested_exposure < effective_current_exposure
            else "已建立紙上調整紀錄：維持"
        )
        if not approved:
            recommendation = "紙上調整已記錄，但被 Risk Gate 阻擋"
        return PaperIntentResponse(
            run_id=run_id,
            approved=approved,
            maturity="PAPER",
            recommendation=recommendation,
            evidence=[
                PaperEvidenceSchema.model_validate(row, from_attributes=True) for row in evidence
            ],
            allocations=[
                AllocationSchema(
                    instrument=item.instrument,
                    weight=item.weight,
                    assumed_exposure=item.assumed_exposure,
                )
                for item in allocations
            ],
        )

    @app.get("/api/v1/paper/evidence/{run_id}", response_model=list[PaperEvidenceSchema])
    def paper_evidence(run_id: str, session: SessionDependency) -> list[PaperEvidenceSchema]:
        rows = AuditRepository(session).paper_evidence(run_id)
        if not rows:
            raise HTTPException(status_code=404, detail="paper run not found")
        return [PaperEvidenceSchema.model_validate(row, from_attributes=True) for row in rows]

    @app.post(
        "/api/v1/account/sync",
        response_model=AccountSyncResponse,
        responses={503: {"description": "read-only broker account sync is unavailable"}},
    )
    def sync_account(session: SessionDependency) -> AccountSyncResponse | JSONResponse:
        if runtime_settings.shioaji_mode != "readonly":
            raise HTTPException(status_code=409, detail="Shioaji mode must be readonly")
        secret_file = runtime_settings.shioaji_secret_file
        if secret_file is None or runtime_settings.shioaji_account_id is None:
            raise HTTPException(status_code=409, detail="account binding is incomplete")
        try:
            mask_binding(runtime_settings.shioaji_account_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="account binding format is invalid",
            ) from exc
        try:
            load_credentials(secret_file)
            result = sync_account_snapshot(
                session,
                make_account_reader(secret_file),
                binding=runtime_settings.shioaji_account_id,
                capital_budget=Decimal(runtime_settings.initial_strategy_nav),
            )
        except (OSError, ValueError, ShioajiUnavailable) as exc:
            # Discard any partial sync, but commit the failure notice itself;
            # raising would roll the notice back together with the sync.
            session.rollback()
            enqueue_notification(
                session,
                "account_sync",
                (
                    "DVL 唯讀帳戶同步失敗\n"
                    f"錯誤類型：{type(exc).__name__}\n"
                    "請確認 Shioaji 連線、憑證與帳戶綁定。"
                ),
                state=f"FAILED:{type(exc).__name__}",
            )
            return JSONResponse(
                status_code=503,
                content={"detail": "read-only broker account sync is unavailable"},
            )
        enqueue_notification(
            session,
            "account_sync",
            (
                "DVL 唯讀帳戶同步恢復\n"
                f"帳戶：{result.account_masked}\n"
                f"Strategy NAV：{result.strategy_nav}"
            ),
            state="SUCCESS",
        )
        return AccountSyncResponse.model_validate(asdict(result))

    @app.get("/api/v1/strategy-ledger", response_model=StrategyLedgerStateSchema)
    def strategy_ledger(session: SessionDependency) -> StrategyLedgerStateSchema:
        state = AuditRepository(session).get_strategy_ledger_state()
        if state is None:
            raise HTTPException(status_code=404, detail="strategy ledger is not initialized")
        return StrategyLedgerStateSchema.model_validate(asdict(state))

    @app.post("/api/v1/strategy-ledger/fills", response_model=StrategyFillResponse)
    def record_fill(
        payload: StrategyFillRequest,
        session: SessionDependency,
    ) -> StrategyFillResponse:
        if payload.occurred_at.utcoffset() is None:
            raise HTTPException(status_code=422, detail="occurred_at must include a timezone")
        try:
            try:
                result = record_strategy_fill(session, **payload.model_dump())
            except IntegrityError:
                # A concurrent request inserted this execution_id first. Discard
                # our attempt and answer from the committed event: idempotent if
                # identical, 409 if the details differ.
                session.rollback()
                result = record_strategy_fill(session, **payload.model_dump())
        except (ValueError, LedgerWriteConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return StrategyFillResponse(
            created=result.created,
            ledger=StrategyLedgerStateSchema.model_validate(asdict(result.state)),
        )

    @app.post(
        "/api/v1/strategy-ledger/dividends/{action_id}/confirm",
        response_model=StrategyFillResponse,
    )
    def confirm_dividend(
        action_id: str,
        payload: DividendConfirmationRequest,
        session: SessionDependency,
    ) -> StrategyFillResponse:
        try:
            result = confirm_dividend_payment(
                session,
                action_id=action_id,
                received_amount=payload.received_amount,
                note=payload.note,
                confirmed_at=datetime.now(TAIPEI_TZ),
            )
        except (ValueError, LedgerWriteConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return StrategyFillResponse(
            created=result.created,
            ledger=StrategyLedgerStateSchema.model_validate(asdict(result.state)),
        )

    @app.post("/api/v1/strategies/evaluate", response_model=StrategyDecisionSchema)
    def evaluate_strategy(
        payload: StrategyEvaluateRequest,
        session: SessionDependency,
    ) -> StrategyDecisionSchema:
        policy = _strategy(payload.strategy_version)
        decision = policy.evaluate(payload.inputs(), payload.state.to_domain())
        decision = replace(
            decision,
            diagnostics={
                **decision.diagnostics,
                "run_id": str(uuid4()),
                # A what-if with caller-supplied inputs and state: kept for audit,
                # never treated as the current target or policy state.
                "trigger": MANUAL_EVALUATION_TRIGGER,
            },
        )
        AuditRepository(session).append_strategy_decision(decision)
        return StrategyDecisionSchema(
            strategy_version=decision.strategy_version,
            as_of=decision.as_of,
            valid=decision.valid,
            raw_target=decision.raw_target,
            target_exposure=decision.target_exposure,
            safety_state=decision.safety_state,
            reasons=decision.reasons,
            diagnostics=decision.diagnostics,
            next_state=PolicyStateSchema(**asdict(decision.next_state)),
        )

    @app.post("/api/v1/execution/allocate", response_model=AllocationResponse)
    def allocate(payload: AllocationRequest) -> AllocationResponse:
        if payload.target_exposure > Decimal("2.0"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "targets above 2.0x are shadow/paper only; "
                    "no cash-account execution recipe is enabled"
                ),
            )
        recipe = _recipe(payload.recipe)
        allocations = recipe.allocate(payload.target_exposure)
        return AllocationResponse(
            recipe=recipe.version,
            target_exposure=payload.target_exposure,
            allocations=tuple(AllocationSchema(**asdict(item)) for item in allocations),
        )

    @app.post("/api/v1/risk/evaluate", response_model=RiskDecisionSchema)
    def evaluate_risk(
        payload: RiskEvaluateRequest,
        session: SessionDependency,
    ) -> RiskDecisionSchema:
        repository = AuditRepository(session)
        claims = payload.model_dump(exclude={"requested_exposure", "actual_exposure"})
        decision, context = evaluate_risk_context(
            server_risk_state(repository, runtime_settings),
            requested_exposure=payload.requested_exposure,
            submitted_actual_exposure=payload.actual_exposure,
            claims=ClientRiskClaims(**claims),
        )
        actual_exposure = context.actual_exposure
        risk_state = (
            "BLOCKED:" + ",".join(decision.reason_codes) if not decision.approved else "APPROVED"
        )
        enqueue_notification(
            session,
            "risk_gate",
            (
                f"DVL Risk Gate {'通過' if decision.approved else '阻擋'}\n"
                f"目前曝險：{actual_exposure}x；要求：{payload.requested_exposure}x\n"
                f"原因：{', '.join(decision.reason_codes)}"
            ),
            state=risk_state,
        )
        return RiskDecisionSchema(**asdict(decision))

    @app.post("/api/v1/backtests/run", response_model=BacktestResponse)
    def run_backtest(payload: BacktestRequest) -> BacktestResponse:
        recipe = _recipe(payload.recipe)
        points = [BacktestPoint(**point.model_dump()) for point in payload.points]
        result = BacktestEngine(initial_nav=payload.initial_nav).run(points, recipe)
        equity = [
            {
                "trading_date": item.trading_date.isoformat(),
                "nav": str(item.nav),
                "daily_return": str(item.daily_return),
                "actual_exposure": str(item.actual_exposure),
                "turnover": str(item.turnover),
                "cost": str(item.cost),
            }
            for item in result.equity
        ]
        return BacktestResponse(
            metrics=BacktestMetricsSchema.model_validate(result.metrics),
            equity=equity,
        )

    return app


app = create_app()
