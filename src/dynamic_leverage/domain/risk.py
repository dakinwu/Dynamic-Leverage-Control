from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from dynamic_leverage.domain.models import RiskDecision, SafetyState, clip_exposure

# Strategy capital may lose at most this fraction (principal or peak drawdown)
# before only exposure reductions are allowed.
CAPITAL_LOSS_LIMIT = Decimal("0.40")


def capital_loss_limit_breached(principal_loss_ratio: Decimal, maximum_drawdown: Decimal) -> bool:
    return principal_loss_ratio >= CAPITAL_LOSS_LIMIT or maximum_drawdown >= CAPITAL_LOSS_LIMIT


@dataclass(frozen=True, slots=True)
class RiskContext:
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


class RiskGate:
    exposure_ceiling = Decimal("2.0")

    def evaluate(self, context: RiskContext) -> RiskDecision:
        requested = clip_exposure(
            context.requested_exposure,
            lower=Decimal("0"),
            upper=self.exposure_ceiling,
        )

        # Unconditional: no system state can open the production order path.
        if context.production_order_requested:
            return RiskDecision(
                approved=False,
                allowed_exposure=context.actual_exposure,
                safety_state=SafetyState.MANUAL_HALT,
                reason_codes=("PRODUCTION_ORDER_PATH_DISABLED",),
            )

        if context.manual_halt:
            return RiskDecision(
                approved=False,
                allowed_exposure=context.actual_exposure,
                safety_state=SafetyState.MANUAL_HALT,
                reason_codes=("MANUAL_HALT",),
            )

        if not context.broker_safe:
            return RiskDecision(
                approved=False,
                allowed_exposure=context.actual_exposure,
                safety_state=SafetyState.BROKER_UNSAFE,
                reason_codes=("BROKER_RECONCILIATION_REQUIRED",),
            )

        if not context.account_eligible:
            return RiskDecision(
                approved=False,
                allowed_exposure=context.actual_exposure,
                safety_state=SafetyState.MANUAL_HALT,
                reason_codes=("ELIGIBILITY_GATE_CLOSED",),
            )

        if context.capital_loss_limit_breached:
            allowed = min(requested, context.actual_exposure)
            return RiskDecision(
                approved=allowed < context.actual_exposure,
                allowed_exposure=allowed,
                safety_state=SafetyState.PANIC_LOCK,
                reason_codes=("CAPITAL_LOSS_LIMIT", "RISK_REDUCING_ONLY"),
                risk_reducing_only=True,
            )

        risk_reducing_only = (
            not context.data_safe
            or context.etf_abnormal
            or context.strategy_state == SafetyState.PANIC_LOCK
        )
        if risk_reducing_only:
            allowed = min(requested, context.actual_exposure)
            state = (
                SafetyState.ETF_ABNORMAL
                if context.etf_abnormal
                else SafetyState.DATA_UNSAFE
                if not context.data_safe
                else SafetyState.PANIC_LOCK
            )
            return RiskDecision(
                approved=allowed < context.actual_exposure,
                allowed_exposure=allowed,
                safety_state=state,
                reason_codes=("RISK_REDUCING_ONLY",),
                risk_reducing_only=True,
            )

        if context.actual_exposure > self.exposure_ceiling:
            return RiskDecision(
                approved=True,
                allowed_exposure=min(requested, self.exposure_ceiling),
                safety_state=SafetyState.NORMAL,
                reason_codes=("MANDATORY_EXPOSURE_REDUCTION",),
                risk_reducing_only=True,
            )

        return RiskDecision(
            approved=True,
            allowed_exposure=requested,
            safety_state=SafetyState.NORMAL,
            reason_codes=("RISK_GATE_APPROVED",),
        )
