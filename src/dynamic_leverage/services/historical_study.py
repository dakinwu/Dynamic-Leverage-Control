"""Retrospective research only. Never manufactures historical availability evidence."""

from __future__ import annotations

import calendar
from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from dynamic_leverage.domain.backtest import BacktestPoint
from dynamic_leverage.domain.indicators import percentile_midrank, realized_volatility_20
from dynamic_leverage.domain.models import PolicyState, StrategyInputs
from dynamic_leverage.domain.strategies import DvlV2Policy, DvlV11Policy

D = Decimal


@dataclass(frozen=True)
class HistoricalPrice:
    adjusted_close: Decimal
    volume: int


def official_total_returns(
    quotes: dict[date, tuple[Decimal, int]],
    dividends: dict[date, Decimal],
    splits: dict[date, Decimal],
    market_dates: list[date],
    suspended: set[date],
) -> dict[date, HistoricalPrice]:
    """Actual close plus cash dividend, reinvested at ex-date close before personal tax.

    A split changes units, not wealth. Only documented suspensions may carry a price.
    The resulting index measures actual instrument returns; it is not a price forecast.
    """
    if not market_dates or set(dividends) - set(market_dates):
        raise ValueError("dividend dates must belong to the complete study calendar")
    result = {}
    index = D(100)
    previous_close = None
    for day in sorted(market_dates):
        if day not in quotes:
            if day not in suspended or previous_close is None:
                raise ValueError(f"missing official ETF close on {day}")
            close, volume = previous_close, 0
        else:
            close, volume = quotes[day]
        if not close.is_finite() or close <= 0 or volume < 0:
            raise ValueError("invalid official ETF price or volume")
        split = splits.get(day, D(1))
        dividend = dividends.get(day, D(0))
        if not split.is_finite() or split <= 0 or not dividend.is_finite() or dividend < 0:
            raise ValueError("invalid corporate action")
        if previous_close is not None:
            index *= (close + dividend) * split / previous_close
        result[day] = HistoricalPrice(index, volume)
        previous_close = close
    return result


def month_end(ordinal: int) -> date:
    year, month0 = divmod(ordinal - 1, 12)
    return date(year, month0 + 1, calendar.monthrange(year, month0 + 1)[1])


def research_inputs(
    closes: dict[date, Decimal],
    valuations: dict[int, Decimal],
    vix: dict[date, Decimal],
    *,
    publication_lag_days: int,
) -> dict[date, StrategyInputs]:
    """Use assumed publication delays, explicitly separate from observed published_at."""
    if publication_lag_days < 1:
        raise ValueError("a positive publication delay is required")
    dates = sorted(closes)
    month_closes = {day.year * 12 + day.month: closes[day] for day in dates}
    result = {}
    peak = D(0)
    for index, day in enumerate(dates):
        peak = max(peak, closes[day])
        if index < 20:
            continue
        ordinal = day.year * 12 + day.month - 1
        while month_end(ordinal) + timedelta(days=publication_lag_days) > day:
            ordinal -= 1
        prior = [valuations.get(key) for key in range(ordinal - 120, ordinal)]
        current = valuations.get(ordinal)
        if current is None or any(value is None for value in prior):
            continue  # caller requires all study-period dates; warm-up may be absent
        eps_yoy = None
        if all(key in month_closes and key in valuations for key in [ordinal, ordinal - 12]):
            eps = month_closes[ordinal] / current
            previous_eps = month_closes[ordinal - 12] / valuations[ordinal - 12]
            eps_yoy = eps / previous_eps - 1
        result[day] = StrategyInputs(
            as_of=day,
            pe_ttm=current,
            pe_percentile=percentile_midrank(current, prior),
            eps_yoy=eps_yoy,
            rv20=realized_volatility_20([closes[d] for d in dates[index - 20 : index + 1]]),
            taiwan_vix=vix.get(day),
            taiex_daily_return=closes[day] / closes[dates[index - 1]] - 1,
            taiex_drawdown=closes[day] / peak - 1,
        )
    return result


def research_targets(
    inputs: dict[date, StrategyInputs],
    *,
    version: str,
    start: date,
    end: date,
    market_dates: list[date],
) -> tuple[dict[date, Decimal], int]:
    if version not in {"v11", "v2_cash_cap2"}:
        raise ValueError("unsupported research policy")
    policy = DvlV11Policy() if version == "v11" else DvlV2Policy()
    state = PolicyState()
    targets = {}
    capped = 0
    for day in sorted(d for d in market_dates if start <= d <= end):
        if day not in inputs:
            raise ValueError(f"missing valuation / volatility input on {day}")
        decision = policy.evaluate(inputs[day], state)
        if not decision.valid or decision.target_exposure is None:
            raise ValueError(f"invalid {version} historical decision on {day}")
        target = decision.target_exposure
        if target > 2:
            capped += 1
        target = min(target, D(2))
        # This is a named research variant, not a change to the production policy.
        state = replace(decision.next_state, previous_target=target)
        targets[day] = target
    return targets, capped


def lagged_points(
    one_x: dict[date, HistoricalPrice],
    two_x: dict[date, HistoricalPrice],
    targets: dict[date, Decimal],
    *,
    start: date,
    end: date,
) -> list[BacktestPoint]:
    """Close D signal -> close D+1 execution -> return ending close D+2."""
    dates = sorted(set(one_x) & set(two_x))
    signal_dates = sorted(targets)
    result = []
    for opening, closing in zip(dates, dates[1:], strict=False):
        if opening < start or closing > end:
            continue
        index = bisect_left(signal_dates, opening) - 1
        if index < 0:
            raise ValueError(f"no prior-close decision for execution on {opening}")
        for prices in (one_x, two_x):
            if any(
                not prices[d].adjusted_close.is_finite() or prices[d].adjusted_close <= 0
                for d in (opening, closing)
            ):
                raise ValueError("invalid adjusted close")
        result.append(
            BacktestPoint(
                trading_date=closing,
                one_x_return=one_x[closing].adjusted_close / one_x[opening].adjusted_close - 1,
                two_x_return=two_x[closing].adjusted_close / two_x[opening].adjusted_close - 1,
                target_exposure=targets[signal_dates[index]],
                rebalance_allowed=one_x[opening].volume > 0 and two_x[opening].volume > 0,
            )
        )
    if not result:
        raise ValueError("no common historical return intervals")
    return result
