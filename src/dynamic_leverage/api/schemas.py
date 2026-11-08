from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from dynamic_leverage.domain.models import PolicyState, SafetyState, StrategyInputs


class PolicyStateSchema(BaseModel):
    previous_target: Decimal = Decimal("1.0")
    panic_active: bool = False
    panic_clear_streak: int = 0
    increase_candidate: Decimal | None = None
    increase_streak: int = 0
    last_increase_iso_week: tuple[int, int] | None = None

    def to_domain(self) -> PolicyState:
        return PolicyState(**self.model_dump())


class StrategyEvaluateRequest(BaseModel):
    strategy_version: str
    as_of: date
    pe_ttm: Decimal | None = None
    pe_percentile: Decimal | None = Field(default=None, ge=0, le=1)
    eps_yoy: Decimal | None = None
    rv20: Decimal | None = Field(default=None, ge=0)
    taiwan_vix: Decimal | None = Field(default=None, ge=0)
    taiex_daily_return: Decimal | None = None
    taiex_drawdown: Decimal | None = None
    funding_cost: Decimal | None = Field(default=None, ge=0)
    critical_data_safe: bool = True
    state: PolicyStateSchema = Field(default_factory=PolicyStateSchema)

    def inputs(self) -> StrategyInputs:
        values = self.model_dump(exclude={"strategy_version", "state"})
        return StrategyInputs(**values)


class StrategyDecisionSchema(BaseModel):
    strategy_version: str
    as_of: date
    valid: bool
    raw_target: Decimal | None
    target_exposure: Decimal | None
    safety_state: SafetyState
    reasons: tuple[str, ...]
    diagnostics: dict[str, str]
    next_state: PolicyStateSchema


class AllocationRequest(BaseModel):
    recipe: str
    target_exposure: Decimal = Field(ge=0, le=3.2)


class AllocationSchema(BaseModel):
    instrument: str
    weight: Decimal
    assumed_exposure: Decimal


class AllocationResponse(BaseModel):
    recipe: str
    target_exposure: Decimal
    allocations: tuple[AllocationSchema, ...]


class RiskEvaluateRequest(BaseModel):
    requested_exposure: Decimal
    actual_exposure: Decimal
    strategy_state: SafetyState = SafetyState.NORMAL
    data_safe: bool = True
    broker_safe: bool = True
    etf_abnormal: bool = False
    manual_halt: bool = False
    capital_loss_limit_breached: bool = False
    account_eligible: bool = False
    production_order_requested: bool = False


class RiskDecisionSchema(BaseModel):
    approved: bool
    allowed_exposure: Decimal
    safety_state: SafetyState
    reason_codes: tuple[str, ...]
    risk_reducing_only: bool


class BacktestPointSchema(BaseModel):
    trading_date: date
    one_x_return: Decimal
    two_x_return: Decimal
    target_exposure: Decimal = Field(ge=0, le=2)
    two_x_exposure: Decimal = Decimal("2.0")
    cash_return: Decimal = Decimal("0")


class BacktestRequest(BaseModel):
    recipe: str
    points: list[BacktestPointSchema] = Field(min_length=1)
    initial_nav: Decimal = Field(default=Decimal("500000"), gt=0)


class BacktestMetricsSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_return: Decimal
    cagr: Decimal
    annualized_volatility: Decimal
    maximum_drawdown: Decimal
    longest_recovery_days: int
    sortino: Decimal | None
    turnover: Decimal
    total_cost: Decimal
    worst_1_day: Decimal
    worst_5_day: Decimal
    worst_20_day: Decimal


class BacktestResponse(BaseModel):
    metrics: BacktestMetricsSchema
    equity: list[dict[str, str | int]]
    timing_verified: bool = False
    profitability_demonstrated: bool = False
    evidence_note: str = "外部目標的計算結果；未驗證訊號可知時間與成交時間，不能視為策略優勢證據。"


class SystemStatusResponse(BaseModel):
    status: str
    environment: str
    database: str
    production_order_submission: str
    shioaji_mode: str
    shioaji_secret_present: bool
    eligibility_gate: str


class DashboardStrategySchema(BaseModel):
    version: str
    as_of: date
    target_exposure: Decimal | None
    valid: bool
    safety_state: str
    reasons: list[str]
    diagnostics: dict[str, str] = Field(default_factory=dict)


class DashboardSnapshotSchema(BaseModel):
    snapshot_id: str
    kind: str
    source: str
    observed_at: datetime
    received_at: datetime
    quality: str


class DashboardResponse(BaseModel):
    status: str
    account_nav: Decimal
    exposure_ceiling: Decimal
    live_canary_budget: Decimal
    production_orders_enabled: bool
    strategies: list[DashboardStrategySchema]
    snapshots: list[DashboardSnapshotSchema]
    alerts: list[str]
    selected_strategy: StrategySelectionSchema
    market_prices: dict[str, Decimal] = Field(default_factory=dict)


class ActivationItemSchema(BaseModel):
    key: str
    label: str
    status: str
    detail: str


class AccountPositionSummarySchema(BaseModel):
    instrument: str
    quantity: Decimal
    last_price: Decimal
    market_value: Decimal


class AccountSummarySchema(BaseModel):
    mode: str
    binding_configured: bool
    planning_nav: Decimal
    strategy_nav: Decimal | None
    observed_nav: Decimal | None
    strategy_cash: Decimal | None
    net_contributions: Decimal | None
    peak_nav: Decimal | None
    principal_loss_amount: Decimal | None
    principal_loss_ratio: Decimal | None
    current_drawdown: Decimal | None
    maximum_drawdown: Decimal | None
    pending_settlement: Decimal | None
    account_masked: str | None
    quality: str
    observed_at: datetime | None
    positions: list[AccountPositionSummarySchema]
    external_instruments: list[str]


class ExposureSummarySchema(BaseModel):
    target_exposure: Decimal | None
    actual_exposure: Decimal | None
    account_exposure: Decimal | None
    quality: str
    exposure_ceiling: Decimal


class ExposureHistoryPointSchema(BaseModel):
    observed_at: datetime
    actual_exposure: Decimal
    target_exposure: Decimal | None


class OperationsSummaryResponse(BaseModel):
    operations_updated_at: datetime | None = None
    research: dict[str, object] = Field(default_factory=dict)
    operator_status: str
    summary: str
    next_action: str
    production_order_submission: str
    account: AccountSummarySchema
    exposure: ExposureSummarySchema
    activation_items: list[ActivationItemSchema]
    strategy: StrategySelectionSchema
    queue: list[QueueItemSchema] = Field(default_factory=list)
    freshness: list[FreshnessStatusSchema] = Field(default_factory=list)
    automation: list[AutomationStatusSchema] = Field(default_factory=list)
    manual_halt: ManualHaltSchema
    exposure_history: list[ExposureHistoryPointSchema] = Field(default_factory=list)
    strategy_readiness: StrategyReadinessSchema


class StrategySelectionSchema(BaseModel):
    strategy_version: str
    mode: str
    changed_at: datetime
    changed_by: str
    reason: str


class StrategyReadinessSchema(BaseModel):
    strategy_version: str
    ready: bool
    observed_points: int
    required_points: int
    missing_inputs: list[str] = Field(default_factory=list)
    detail: str


class QueueItemSchema(BaseModel):
    item_id: str
    severity: str
    status: str
    title: str
    detail: str
    impact: str
    next_action: str
    evidence_refs: list[str] = Field(default_factory=list)
    first_seen_at: datetime
    last_seen_at: datetime
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    reopened_count: int = 0


class FreshnessStatusSchema(BaseModel):
    contract_key: str
    kind: str
    source: str
    owner: str
    status: str
    observed_at: datetime | None
    age_minutes: int | None
    max_age_minutes: int
    reason: str
    snapshot_id: str | None = None
    effective_at: datetime | None = None
    run_id: str | None = None
    formula_or_rule: str | None = None
    source_trade_date: date | None = None
    expected_trade_date: date | None = None
    received_at: datetime | None = None
    published_at: datetime | None = None


class AutomationStatusSchema(BaseModel):
    automation_id: str
    status: str
    last_started_at: datetime | None
    last_success_at: datetime | None
    next_run_at: datetime | None
    last_run_id: str | None
    failure_reason: str | None


class ManualHaltSchema(BaseModel):
    active: bool
    reason: str | None
    actor: str | None
    enabled_at: datetime | None
    released_at: datetime | None
    release_reason: str | None


class QueueTransitionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    actor: str = Field(default="operator", min_length=1, max_length=120)


class ManualHaltRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    actor: str = Field(default="operator", min_length=1, max_length=120)


class StrategySelectionRequest(BaseModel):
    strategy_version: str
    mode: str = Field(default="shadow", pattern="^(shadow|paper)$")
    reason: str = Field(min_length=1, max_length=500)
    changed_by: str = Field(default="operator", min_length=1, max_length=120)


class PaperEvidenceSchema(BaseModel):
    evidence_id: str
    run_id: str
    parent_id: str | None
    kind: str
    created_at: datetime
    formula_or_rule: str | None
    evidence_hash: str
    payload: dict[str, object]


class PaperIntentRequest(BaseModel):
    strategy_version: str
    recipe: str = "1x-2x-v1"
    current_exposure: Decimal = Field(ge=0)
    requested_exposure: Decimal = Field(ge=0, le=2)
    reason: str = Field(min_length=1, max_length=500)


class PaperIntentResponse(BaseModel):
    run_id: str
    approved: bool
    maturity: str
    recommendation: str
    evidence: list[PaperEvidenceSchema]
    allocations: list[AllocationSchema] = Field(default_factory=list)


OperationsSummaryResponse.model_rebuild()
DashboardResponse.model_rebuild()


class AccountSyncResponse(BaseModel):
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


class StrategyFillRequest(BaseModel):
    execution_id: str = Field(min_length=1, max_length=120)
    instrument: str = Field(pattern="^(0050|00631L|00685L)$")
    side: str = Field(pattern="^(BUY|SELL|buy|sell)$")
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    commission: Decimal = Field(default=Decimal("0"), ge=0)
    tax: Decimal = Field(default=Decimal("0"), ge=0)
    occurred_at: datetime
    note: str = Field(default="", max_length=500)


class StrategyLedgerStateSchema(BaseModel):
    capital_budget: Decimal
    net_contributions: Decimal
    cash: Decimal
    positions: dict[str, Decimal]
    last_prices: dict[str, Decimal]
    current_nav: Decimal
    peak_nav: Decimal
    principal_loss_amount: Decimal
    principal_loss_ratio: Decimal
    current_drawdown: Decimal
    maximum_drawdown: Decimal
    position_revision: int
    reconciled: bool
    reconciliation_reason: str | None
    last_account_snapshot_id: str | None
    initialized_at: datetime
    updated_at: datetime
    actual_exposure: Decimal | None
    account_exposure: Decimal | None
    exposure_quality: str


class StrategyFillResponse(BaseModel):
    created: bool
    ledger: StrategyLedgerStateSchema
