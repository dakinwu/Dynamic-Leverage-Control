"""One server-side risk context for every endpoint that asks the Risk Gate.

The context is assembled from stored evidence (freshness, ledger, account
snapshot, manual halt, latest strategy run). Values a client submits can only
make the verdict stricter: they are AND-ed with, or OR-ed into, server truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from dynamic_leverage.config import Settings
from dynamic_leverage.domain.models import RiskDecision, SafetyState
from dynamic_leverage.domain.risk import RiskContext, RiskGate, capital_loss_limit_breached
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services import freshness

ACCOUNT_SNAPSHOT_KIND = "broker.account.shioaji"


@dataclass(frozen=True, slots=True)
class ServerRiskState:
    actual_exposure: Decimal | None
    stale_required: tuple[str, ...]
    account_truth_present: bool
    manual_halt: bool
    capital_loss_limit_breached: bool
    strategy_state: SafetyState
    strategy_version: str


@dataclass(frozen=True, slots=True)
class ClientRiskClaims:
    """Request flags. Each one can only tighten the server's view."""

    strategy_state: SafetyState = SafetyState.NORMAL
    data_safe: bool = True
    broker_safe: bool = True
    etf_abnormal: bool = False
    manual_halt: bool = False
    capital_loss_limit_breached: bool = False
    # Paper maturity has no account prerequisites; live order paths are refused
    # separately (production_order_requested) and the live gate stays closed.
    account_eligible: bool = True
    production_order_requested: bool = False


def server_risk_state(
    repository: AuditRepository,
    settings: Settings,
    *,
    strategy_version: str | None = None,
) -> ServerRiskState:
    ledger = repository.get_strategy_ledger_state()
    account_snapshot = repository.latest_raw_snapshot(kind=ACCOUNT_SNAPSHOT_KIND)
    account_truth_present = (
        bool(settings.shioaji_account_id)
        and account_snapshot is not None
        and account_snapshot.quality not in {"error", "unknown"}
        and ledger is not None
        and ledger.reconciled
        and ledger.actual_exposure is not None
    )
    version = strategy_version or repository.get_strategy_selection().strategy_version
    latest = repository.latest_strategy_decision(version)
    panic = latest is not None and latest.safety_state == SafetyState.PANIC_LOCK.value
    return ServerRiskState(
        actual_exposure=ledger.actual_exposure if ledger is not None else None,
        stale_required=freshness.stale_required_contracts(freshness.freshness_rows(repository)),
        account_truth_present=account_truth_present,
        manual_halt=repository.get_manual_halt().active,
        capital_loss_limit_breached=bool(
            ledger
            and capital_loss_limit_breached(ledger.principal_loss_ratio, ledger.maximum_drawdown)
        ),
        strategy_state=SafetyState.PANIC_LOCK if panic else SafetyState.NORMAL,
        strategy_version=version,
    )


def evaluate_risk(
    server: ServerRiskState,
    *,
    requested_exposure: Decimal,
    submitted_actual_exposure: Decimal,
    claims: ClientRiskClaims | None = None,
) -> tuple[RiskDecision, RiskContext]:
    claims = claims or ClientRiskClaims()
    strategy_state = (
        SafetyState.PANIC_LOCK
        if SafetyState.PANIC_LOCK in (server.strategy_state, claims.strategy_state)
        else claims.strategy_state
    )
    context = RiskContext(
        requested_exposure=requested_exposure,
        # Without a reconciled ledger the submitted value is used only for the
        # arithmetic; broker_safe is then False, so nothing can be approved.
        actual_exposure=(
            server.actual_exposure
            if server.actual_exposure is not None
            else submitted_actual_exposure
        ),
        strategy_state=strategy_state,
        data_safe=claims.data_safe and not server.stale_required,
        broker_safe=claims.broker_safe and server.account_truth_present,
        etf_abnormal=claims.etf_abnormal,  # No server-side ETF evidence source yet.
        manual_halt=claims.manual_halt or server.manual_halt,
        capital_loss_limit_breached=(
            claims.capital_loss_limit_breached or server.capital_loss_limit_breached
        ),
        account_eligible=claims.account_eligible,
        production_order_requested=claims.production_order_requested,
    )
    return RiskGate().evaluate(context), context


def gate_allows_target(decision: RiskDecision, requested_exposure: Decimal) -> bool:
    """Whether the gate permits holding exactly the requested (ceiling-capped) target.

    ``approved`` alone means "an executable change exists"; a paper intent to
    maintain exposure under a risk-reducing-only state is permitted, while a
    hard stop (manual halt, broker unsafe) blocks every intent.
    """
    target = min(requested_exposure, RiskGate.exposure_ceiling)
    return decision.allowed_exposure == target and (
        decision.approved or decision.risk_reducing_only
    )
