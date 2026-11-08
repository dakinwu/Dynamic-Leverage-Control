from decimal import Decimal

import pytest

from dynamic_leverage.domain.indicators import percentile_midrank, realized_volatility_20
from dynamic_leverage.domain.models import round_exposure


def test_round_exposure_uses_half_up() -> None:
    assert round_exposure(Decimal("1.25")) == Decimal("1.3")
    assert round_exposure(Decimal("1.15")) == Decimal("1.2")


def test_percentile_uses_midrank() -> None:
    history = [Decimal(index) for index in range(119)] + [Decimal("60")]
    percentile = percentile_midrank(Decimal("60"), history)
    assert percentile == Decimal("61") / Decimal("120")


def test_percentile_requires_full_point_in_time_window() -> None:
    with pytest.raises(ValueError, match="120"):
        percentile_midrank(Decimal("20"), [Decimal("20")] * 119)


def test_rv20_requires_21_positive_closes() -> None:
    with pytest.raises(ValueError, match="21"):
        realized_volatility_20([Decimal("100")] * 20)
