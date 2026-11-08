from datetime import date, timedelta
from decimal import Decimal

from dynamic_leverage.domain.models import PolicyState, SafetyState, StrategyInputs
from dynamic_leverage.domain.strategies import (
    ConstantExposurePolicy,
    DvlV2Policy,
    DvlV10Policy,
    DvlV11Policy,
)


def test_constant_exposure_is_invalid_when_critical_data_is_unsafe() -> None:
    decision = ConstantExposurePolicy().evaluate(
        StrategyInputs(as_of=date(2026, 8, 28), critical_data_safe=False),
        PolicyState(previous_target=Decimal("1.1")),
    )
    assert decision.valid is False
    assert decision.target_exposure is None
    assert decision.safety_state == SafetyState.DATA_UNSAFE


def test_v11_cheap_market_is_capped_by_high_rv20() -> None:
    decision = DvlV11Policy().evaluate(
        StrategyInputs(
            as_of=date(2026, 8, 28),
            pe_percentile=Decimal("0.1"),
            rv20=Decimal("0.32"),
        ),
        PolicyState(previous_target=Decimal("1.8")),
    )
    assert decision.raw_target == Decimal("1.4")
    assert decision.target_exposure == Decimal("1.4")


def test_v10_panic_lock_never_increases_exposure() -> None:
    decision = DvlV10Policy().evaluate(
        StrategyInputs(
            as_of=date(2026, 8, 28),
            pe_ttm=Decimal("20"),
            pe_percentile=Decimal("0.10"),
            eps_yoy=Decimal("0.05"),
            rv20=Decimal("0.15"),
            taiwan_vix=Decimal("36"),
            taiex_daily_return=Decimal("0.01"),
        ),
        PolicyState(previous_target=Decimal("1.2")),
    )
    assert decision.target_exposure == Decimal("1.2")
    assert decision.safety_state == SafetyState.PANIC_LOCK


def test_v11_releases_panic_after_five_safe_days() -> None:
    policy = DvlV11Policy()
    state = PolicyState(previous_target=Decimal("1.2"), panic_active=True)
    current_date = date(2026, 8, 24)
    decision = None
    for offset in range(5):
        decision = policy.evaluate(
            StrategyInputs(
                as_of=current_date + timedelta(days=offset),
                pe_percentile=Decimal("0.8"),
                rv20=Decimal("0.20"),
            ),
            state,
        )
        state = decision.next_state
    assert decision is not None
    assert decision.next_state.panic_active is False


def test_v2_cheap_low_volatility_can_propose_shadow_target_above_two_x() -> None:
    decision = DvlV2Policy().evaluate(
        StrategyInputs(
            as_of=date(2026, 8, 28),
            pe_percentile=Decimal("0.10"),
            eps_yoy=Decimal("0.05"),
            rv20=Decimal("0.15"),
            taiwan_vix=Decimal("18"),
            taiex_drawdown=Decimal("-0.40"),
        ),
        PolicyState(previous_target=Decimal("3.2")),
    )
    assert decision.valid is True
    assert decision.raw_target == Decimal("3.2")
    assert decision.target_exposure == Decimal("3.2")
    assert decision.diagnostics["candidate_ceiling"] == "3.2"
    assert "SHADOW_ONLY_ABOVE_2X" in decision.reasons


def test_v2_drawdown_cap_keeps_expensive_market_at_two_x_or_less() -> None:
    decision = DvlV2Policy().evaluate(
        StrategyInputs(
            as_of=date(2026, 8, 28),
            pe_percentile=Decimal("0.90"),
            eps_yoy=Decimal("0.05"),
            rv20=Decimal("0.15"),
            taiwan_vix=Decimal("18"),
            taiex_drawdown=Decimal("0"),
        ),
        PolicyState(previous_target=Decimal("1.6")),
    )
    assert decision.raw_target == Decimal("1.6")
    assert decision.target_exposure == Decimal("1.6")
