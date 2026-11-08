"""Price-index stress models; these are not historical ETF total returns."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

from dynamic_leverage.domain.indicators import percentile_midrank, realized_volatility_20
from dynamic_leverage.domain.models import StrategyInputs
from dynamic_leverage.services.historical_study import month_end


def synthetic_prices(closes: dict[date, D], *, annual_drag: D) -> dict[date, tuple[D, D]]:
    """Daily 2x price returns minus assumed drag, accrued over actual calendar gaps."""
    if not annual_drag.is_finite() or annual_drag < 0 or not closes:
        raise ValueError("nonempty closes and finite nonnegative drag required")
    one, two = D(100), D(100)
    previous_day, previous_close = None, None
    result = {}
    for day, close in sorted(closes.items()):
        if not close.is_finite() or close <= 0:
            raise ValueError("finite positive index closes required")
        if previous_day is not None:
            r = close / previous_close - 1
            factor = 1 + 2 * r - annual_drag * D((day - previous_day).days) / D("365.2425")
            if factor <= 0:
                raise ValueError("synthetic 2x asset exhausted; cannot continue or resurrect NAV")
            one *= 1 + r
            two *= factor
        result[day] = (one, two)
        previous_day, previous_close = day, close
    return result


def crisis_inputs(closes: dict[date, D], valuations: dict[int, D], *, lag_days: int):
    """Return missing fields explicitly, never substitute shorter PE histories."""
    if lag_days < 1:
        raise ValueError("positive assumed publication delay required")
    dates = sorted(closes)
    month_closes = {d.year * 12 + d.month: closes[d] for d in dates}
    result = {}
    for i, day in enumerate(dates):
        if i < 20:
            continue
        ordinal = day.year * 12 + day.month - 1
        while month_end(ordinal) + timedelta(days=lag_days) > day:
            ordinal -= 1
        pe = valuations.get(ordinal)
        eps_yoy = percentile = None
        if pe is not None and pe.is_finite() and pe > 0:
            old_pe = valuations.get(ordinal - 12)
            if (
                old_pe is not None
                and old_pe.is_finite()
                and old_pe > 0
                and all(key in month_closes for key in [ordinal, ordinal - 12])
            ):
                eps_yoy = (month_closes[ordinal] / pe) / (month_closes[ordinal - 12] / old_pe) - 1
            prior = [valuations.get(k) for k in range(ordinal - 120, ordinal)]
            if all(v is not None and v.is_finite() and v > 0 for v in prior):
                percentile = percentile_midrank(pe, prior)
        result[day] = StrategyInputs(
            as_of=day,
            pe_ttm=pe,
            pe_percentile=percentile,
            eps_yoy=eps_yoy,
            rv20=realized_volatility_20([closes[d] for d in dates[i - 20 : i + 1]]),
            taiex_daily_return=closes[day] / closes[dates[i - 1]] - 1,
        )
    return result
