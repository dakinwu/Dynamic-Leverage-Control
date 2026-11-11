from decimal import Decimal

from dynamic_leverage.domain.models import SafetyState
from dynamic_leverage.domain.risk import RiskContext, RiskGate


def test_production_order_path_is_always_rejected_in_mvp() -> None:
    decision = RiskGate().evaluate(
        RiskContext(
            requested_exposure=Decimal("1.2"),
            actual_exposure=Decimal("1.0"),
            account_eligible=True,
            production_order_requested=True,
        )
    )
    assert decision.approved is False
    assert "PRODUCTION_ORDER_PATH_DISABLED" in decision.reason_codes


def test_data_unsafe_allows_only_reduction() -> None:
    decision = RiskGate().evaluate(
        RiskContext(
            requested_exposure=Decimal("1.2"),
            actual_exposure=Decimal("1.4"),
            data_safe=False,
            account_eligible=True,
        )
    )
    assert decision.approved is True
    assert decision.allowed_exposure == Decimal("1.2")
    assert decision.risk_reducing_only is True


def test_broker_unsafe_stops_even_risk_reducing_order() -> None:
    decision = RiskGate().evaluate(
        RiskContext(
            requested_exposure=Decimal("1.0"),
            actual_exposure=Decimal("1.4"),
            broker_safe=False,
            account_eligible=True,
        )
    )
    assert decision.approved is False
    assert decision.safety_state == SafetyState.BROKER_UNSAFE


def test_capital_loss_limit_allows_only_exposure_reduction() -> None:
    blocked = RiskGate().evaluate(
        RiskContext(
            requested_exposure=Decimal("1.4"),
            actual_exposure=Decimal("1.2"),
            capital_loss_limit_breached=True,
            account_eligible=True,
        )
    )
    reduction = RiskGate().evaluate(
        RiskContext(
            requested_exposure=Decimal("0.8"),
            actual_exposure=Decimal("1.2"),
            capital_loss_limit_breached=True,
            account_eligible=True,
        )
    )

    assert blocked.approved is False
    assert blocked.allowed_exposure == Decimal("1.2")
    assert "CAPITAL_LOSS_LIMIT" in blocked.reason_codes
    assert reduction.approved is True
    assert reduction.allowed_exposure == Decimal("0.8")
    assert reduction.risk_reducing_only is True


def test_mandatory_reduction_honours_a_lower_requested_target() -> None:
    decision = RiskGate().evaluate(
        RiskContext(
            requested_exposure=Decimal("1.0"),
            actual_exposure=Decimal("2.06"),
            account_eligible=True,
        )
    )
    assert decision.approved is True
    assert decision.allowed_exposure == Decimal("1.0")
    assert decision.reason_codes == ("MANDATORY_EXPOSURE_REDUCTION",)
    assert decision.risk_reducing_only is True


def test_mandatory_reduction_caps_a_higher_requested_target_at_ceiling() -> None:
    decision = RiskGate().evaluate(
        RiskContext(
            requested_exposure=Decimal("2.4"),
            actual_exposure=Decimal("2.06"),
            account_eligible=True,
        )
    )
    assert decision.allowed_exposure == Decimal("2.0")
    assert decision.reason_codes == ("MANDATORY_EXPOSURE_REDUCTION",)


def test_production_order_refusal_is_reported_before_any_state_check() -> None:
    decision = RiskGate().evaluate(
        RiskContext(
            requested_exposure=Decimal("1.2"),
            actual_exposure=Decimal("1.0"),
            broker_safe=False,
            manual_halt=True,
            production_order_requested=True,
        )
    )
    assert decision.approved is False
    assert decision.reason_codes == ("PRODUCTION_ORDER_PATH_DISABLED",)
