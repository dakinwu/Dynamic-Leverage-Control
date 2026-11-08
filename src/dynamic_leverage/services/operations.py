from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class ActivationItem:
    key: str
    label: str
    status: str
    detail: str


@dataclass(frozen=True)
class AccountPositionSummary:
    instrument: str
    quantity: Decimal
    last_price: Decimal
    market_value: Decimal


@dataclass(frozen=True)
class AccountSummary:
    mode: str
    binding_configured: bool
    planning_nav: Decimal
    strategy_nav: Decimal | None = None
    observed_nav: Decimal | None = None
    strategy_cash: Decimal | None = None
    net_contributions: Decimal | None = None
    peak_nav: Decimal | None = None
    principal_loss_amount: Decimal | None = None
    principal_loss_ratio: Decimal | None = None
    current_drawdown: Decimal | None = None
    maximum_drawdown: Decimal | None = None
    pending_settlement: Decimal | None = None
    account_masked: str | None = None
    quality: str = "unknown"
    observed_at: datetime | None = None
    positions: tuple[AccountPositionSummary, ...] = ()
    external_instruments: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExposureSummary:
    target_exposure: Decimal | None
    actual_exposure: Decimal | None
    account_exposure: Decimal | None
    quality: str
    exposure_ceiling: Decimal = Decimal("2.0")


@dataclass(frozen=True)
class StrategyReadiness:
    strategy_version: str
    ready: bool
    observed_points: int
    required_points: int
    missing_inputs: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class OperationsSummary:
    operator_status: str
    summary: str
    next_action: str
    production_order_submission: str
    account: AccountSummary
    exposure: ExposureSummary
    activation_items: tuple[ActivationItem, ...]
    strategy_readiness: StrategyReadiness


@dataclass(frozen=True)
class OperationsInputs:
    database_ok: bool
    shioaji_secret_present: bool
    shioaji_mode: str
    account_binding_configured: bool
    planning_nav: Decimal
    snapshot_count: int
    target_exposure: Decimal | None
    strategy_nav: Decimal | None = None
    account_snapshot_present: bool = False
    account_reconciled: bool = False
    account_masked: str | None = None
    account_quality: str = "unknown"
    account_observed_at: datetime | None = None
    observed_nav: Decimal | None = None
    strategy_cash: Decimal | None = None
    net_contributions: Decimal | None = None
    peak_nav: Decimal | None = None
    principal_loss_amount: Decimal | None = None
    principal_loss_ratio: Decimal | None = None
    current_drawdown: Decimal | None = None
    maximum_drawdown: Decimal | None = None
    pending_settlement: Decimal | None = None
    actual_exposure: Decimal | None = None
    account_exposure: Decimal | None = None
    exposure_quality: str = "unknown"
    positions: tuple[AccountPositionSummary, ...] = ()
    external_instruments: tuple[str, ...] = ()
    selected_strategy_version: str = ""
    selected_strategy_valid: bool = False
    strategy_missing_inputs: tuple[str, ...] = ()
    valuation_points: int = 0
    valuation_required_points: int = 120
    stale_required_contracts: tuple[str, ...] = ()
    capital_loss_limit_breached: bool = False


def derive_operations_summary(inputs: OperationsInputs) -> OperationsSummary:
    account_ready = inputs.account_snapshot_present and inputs.account_reconciled
    strategy_ready = inputs.selected_strategy_valid and inputs.target_exposure is not None
    strategy_readiness = StrategyReadiness(
        strategy_version=inputs.selected_strategy_version,
        ready=strategy_ready,
        observed_points=inputs.valuation_points,
        required_points=inputs.valuation_required_points,
        missing_inputs=inputs.strategy_missing_inputs,
        detail=(
            "目前選定策略已完成必要輸入。"
            if strategy_ready
            else (
                f"目前只有 {inputs.valuation_points}/{inputs.valuation_required_points} "
                "個有效估值月點，且缺少：" + "、".join(inputs.strategy_missing_inputs)
                if inputs.strategy_missing_inputs
                else "目前選定策略尚未產生有效目標。"
            )
        ),
    )
    activation_items = (
        ActivationItem(
            key="database",
            label="PostgreSQL 稽核資料庫",
            status="ready" if inputs.database_ok else "blocked",
            detail="可連線" if inputs.database_ok else "連線失敗",
        ),
        ActivationItem(
            key="runtime_secrets",
            label="Runtime secrets",
            status="ready" if inputs.shioaji_secret_present else "required",
            detail="已掛載且可讀" if inputs.shioaji_secret_present else "尚未掛載完整金鑰",
        ),
        ActivationItem(
            key="broker_mode",
            label="Shioaji 執行模式",
            status="ready" if inputs.shioaji_mode == "readonly" else "required",
            detail=inputs.shioaji_mode,
        ),
        ActivationItem(
            key="account_binding",
            label="Strategy Account Binding",
            status="ready" if account_ready else "required",
            detail=(
                f"已驗證 {inputs.account_masked}"
                if account_ready
                else "已設定，等待唯讀同步驗證"
                if inputs.account_binding_configured
                else "尚未設定策略帳戶 ID"
            ),
        ),
        ActivationItem(
            key="market_snapshots",
            label="市場資料快照",
            status="ready" if inputs.snapshot_count > 0 else "required",
            detail=f"已保存 {inputs.snapshot_count} 筆" if inputs.snapshot_count else "尚無快照",
        ),
        ActivationItem(
            key="strategy_run",
            label="Strategy Run",
            status="ready" if strategy_ready else "required",
            detail="已有最新有效決策" if strategy_ready else strategy_readiness.detail,
        ),
        ActivationItem(
            key="production_lock",
            label="正式下單路徑",
            status="ready",
            detail="disabled",
        ),
        ActivationItem(
            key="capital_risk",
            label="40% 資本風險上限",
            status="blocked" if inputs.capital_loss_limit_breached else "ready",
            detail=(
                "本金虧損或歷史最大回撤已觸及 40%"
                if inputs.capital_loss_limit_breached
                else "未觸及"
            ),
        ),
    )

    if not inputs.database_ok:
        operator_status = "BLOCKED"
        summary = "稽核資料庫不可用，平台已停止信任衍生狀態。"
        next_action = "恢復資料庫連線並重新確認快照。"
    elif inputs.account_snapshot_present and not inputs.account_reconciled:
        operator_status = "BLOCKED"
        summary = "策略台帳與券商快照無法對帳，系統已關閉曝險判定。"
        next_action = "補登實際成交或處理不支援的部位條件，然後重新同步。"
    elif inputs.capital_loss_limit_breached:
        operator_status = "BLOCKED"
        summary = "策略已觸及 40% 資本風險上限，禁止維持或增加曝險。"
        next_action = "只允許降低曝險；完成損失檢討前不得恢復正常調整。"
    elif not account_ready:
        operator_status = "ACTION_REQUIRED"
        summary = "目前只有規劃資產，尚未建立可信任的券商帳戶事實。"
        next_action = "完成 Strategy Account Binding，然後執行唯讀帳戶同步。"
    elif inputs.stale_required_contracts:
        operator_status = "ATTENTION"
        summary = "必要資料已過期，禁止增加曝險。"
        next_action = "先重新同步權威資料；資料恢復前只能研究或降低曝險。"
    elif inputs.snapshot_count == 0 or not strategy_ready:
        operator_status = "ACTION_REQUIRED"
        summary = "帳戶已驗證，但目前選定策略尚未完成評估。"
        next_action = strategy_readiness.detail + " 到資料頁執行歷史回補。"
    else:
        operator_status = "NORMAL"
        summary = "唯讀監控資料已完成基本對帳。"
        next_action = "持續監控資料品質與曝險偏差。"

    return OperationsSummary(
        operator_status=operator_status,
        summary=summary,
        next_action=next_action,
        production_order_submission="disabled",
        account=AccountSummary(
            mode="observed" if inputs.account_snapshot_present else "planning",
            binding_configured=inputs.account_binding_configured,
            planning_nav=inputs.planning_nav,
            strategy_nav=inputs.strategy_nav,
            observed_nav=inputs.observed_nav,
            strategy_cash=inputs.strategy_cash,
            net_contributions=inputs.net_contributions,
            peak_nav=inputs.peak_nav,
            principal_loss_amount=inputs.principal_loss_amount,
            principal_loss_ratio=inputs.principal_loss_ratio,
            current_drawdown=inputs.current_drawdown,
            maximum_drawdown=inputs.maximum_drawdown,
            pending_settlement=inputs.pending_settlement,
            account_masked=inputs.account_masked,
            quality=inputs.account_quality,
            observed_at=inputs.account_observed_at,
            positions=inputs.positions,
            external_instruments=inputs.external_instruments,
        ),
        exposure=ExposureSummary(
            target_exposure=inputs.target_exposure,
            actual_exposure=inputs.actual_exposure,
            account_exposure=inputs.account_exposure,
            quality=inputs.exposure_quality,
        ),
        activation_items=activation_items,
        strategy_readiness=strategy_readiness,
    )
