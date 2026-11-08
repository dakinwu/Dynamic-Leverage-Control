from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from dynamic_leverage.domain.models import StrategyInputs
from dynamic_leverage.services.research_replay import (
    ResearchObservation,
    ResearchReturn,
    replay_comparison,
)


def _observation() -> ResearchObservation:
    return ResearchObservation(
        datetime(2025, 1, 1, 15, tzinfo=UTC),
        datetime(2025, 1, 1, 14, tzinfo=UTC),
        StrategyInputs(
            as_of=date(2025, 1, 1),
            pe_ttm=Decimal("20"),
            pe_percentile=Decimal("0.5"),
            eps_yoy=Decimal("0.1"),
            rv20=Decimal("0.15"),
            taiwan_vix=Decimal("20"),
            taiex_daily_return=Decimal("0"),
        ),
        ("valuation", "index", "vix"),
    )


def _run(observation: ResearchObservation):
    return replay_comparison(
        [observation],
        [
            ResearchReturn(
                datetime(2025, 1, 2, 13, tzinfo=UTC),
                datetime(2025, 1, 3, 13, tzinfo=UTC),
                Decimal("0.01"),
                Decimal("0.017"),
                ("0050", "00631L"),
            )
        ],
        evaluation_start=datetime(2025, 1, 2, tzinfo=UTC),
        frozen_at=datetime(2024, 12, 1, tzinfo=UTC),
    )


def test_next_close_replay_compares_real_returns_and_doubled_costs() -> None:
    result = _run(_observation())
    baseline = result["comparison"]["cost_1x"]["metrics"]["0050_buy_hold"]
    assert baseline["total_return"] == (1 - Decimal("0.001425")) * Decimal("1.01") - 1
    assert (
        result["comparison"]["cost_2x"]["metrics"]["0050_buy_hold"]["total_return"]
        < baseline["total_return"]
    )
    assert result["promotion_allowed"] is False


def test_today_imported_history_cannot_be_used_yesterday() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        _run(replace(_observation(), available_at=datetime(2025, 1, 2, tzinfo=UTC)))


def test_signal_at_execution_close_cannot_earn_that_return() -> None:
    with pytest.raises(ValueError, match="no prior decision"):
        _run(replace(_observation(), decision_at=datetime(2025, 1, 2, 13, tzinfo=UTC)))


def test_missing_signal_does_not_fall_back_to_fixed_target() -> None:
    with pytest.raises(ValueError, match="missing inputs"):
        _run(replace(_observation(), inputs=StrategyInputs(as_of=date(2025, 1, 1))))
