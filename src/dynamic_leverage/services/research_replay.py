"""Strict timed replay. Imported modern revisions cannot impersonate past knowledge."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from dynamic_leverage.domain.backtest import BacktestEngine, BacktestPoint
from dynamic_leverage.domain.execution import OneXTwoXRecipe
from dynamic_leverage.domain.models import PolicyState, StrategyInputs
from dynamic_leverage.domain.strategies import DvlV2Policy


@dataclass(frozen=True)
class ResearchObservation:
    decision_at: datetime
    available_at: datetime
    inputs: StrategyInputs
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResearchReturn:
    starts_at: datetime
    ends_at: datetime
    one_x_total_return: Decimal
    two_x_total_return: Decimal
    source_ids: tuple[str, ...]


def _aware(value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError("research timestamps must have timezones")


def replay_comparison(
    observations: list[ResearchObservation],
    returns: list[ResearchReturn],
    *,
    evaluation_start: datetime,
    frozen_at: datetime,
) -> dict[str, object]:
    """Evaluate frozen policy on a held-out contiguous period, at next close or later.

    A bar is a real instrument total return between two executable closes. A
    signal at or after its start cannot affect that bar. Total returns must
    include distributions/splits; synthetic 2x index returns are inadmissible.
    Source authentication and historical revision evidence are separate gates.
    """
    for value in (evaluation_start, frozen_at):
        _aware(value)
    if frozen_at >= evaluation_start:
        raise ValueError("policy must be frozen before the evaluation period")
    if not observations or not returns:
        raise ValueError("real observations and instrument returns are required")
    state = PolicyState()
    decisions = []
    previous = None
    previous_market_date = None
    for observation in observations:
        _aware(observation.decision_at)
        _aware(observation.available_at)
        if not observation.source_ids or observation.available_at > observation.decision_at:
            raise ValueError("input evidence was unavailable at decision time")
        if previous and observation.decision_at <= previous:
            raise ValueError("decisions must be strictly chronological")
        if observation.inputs.as_of > observation.decision_at.date():
            raise ValueError("future observation date")
        if previous_market_date and observation.inputs.as_of <= previous_market_date:
            raise ValueError("each market date may advance policy only once")
        decision = DvlV2Policy().evaluate(observation.inputs, state)
        if not decision.valid or decision.target_exposure is None:
            raise ValueError("missing inputs: research cannot invent a target")
        state = decision.next_state
        decisions.append((observation.decision_at, decision.target_exposure))
        previous, previous_market_date = observation.decision_at, observation.inputs.as_of
    points = []
    previous_end = None
    for bar in returns:
        _aware(bar.starts_at)
        _aware(bar.ends_at)
        if not bar.source_ids or bar.starts_at >= bar.ends_at:
            raise ValueError("return interval or source evidence is invalid")
        if previous_end is not None and bar.starts_at != previous_end:
            raise ValueError("return intervals must be contiguous without overlap")
        previous_end = bar.ends_at
        if any(
            not value.is_finite() or value <= -1
            for value in (
                bar.one_x_total_return,
                bar.two_x_total_return,
            )
        ):
            raise ValueError("invalid instrument total return")
        if bar.starts_at < evaluation_start:
            continue
        target = next(
            (target for stamp, target in reversed(decisions) if stamp < bar.starts_at), None
        )
        if target is None:
            raise ValueError("no prior decision before the executable return interval")
        points.append(
            BacktestPoint(
                bar.ends_at.date(),
                bar.one_x_total_return,
                bar.two_x_total_return,
                target,
            )
        )
    if not points:
        raise ValueError("no held-out evaluation bars")
    output = {}
    for multiplier in (1, 2):
        scenarios = {}
        for name, target, band in (
            ("dvl_v2", None, Decimal("0.15")),
            ("0050_buy_hold", Decimal("1"), Decimal("Infinity")),
            ("fixed_1_2", Decimal("1.2"), Decimal("0.15")),
            ("00631L_buy_hold", Decimal("2"), Decimal("Infinity")),
        ):
            rows = [
                BacktestPoint(
                    p.trading_date,
                    p.one_x_return,
                    p.two_x_return,
                    target if target is not None else p.target_exposure,
                )
                for p in points
            ]
            result = BacktestEngine(
                no_trade_band=band,
                one_way_cost_rate=Decimal("0.001425") * multiplier,
                sell_tax_rate=Decimal("0.001") * multiplier,
            ).run(rows, OneXTwoXRecipe())
            scenarios[name] = asdict(result.metrics)
        candidate, benchmark = scenarios["dvl_v2"], scenarios["0050_buy_hold"]
        output[f"cost_{multiplier}x"] = {
            "metrics": scenarios,
            "passes_numerical_q7": (
                candidate["total_return"] > benchmark["total_return"]
                and candidate["annualized_volatility"] <= benchmark["annualized_volatility"]
                and abs(candidate["maximum_drawdown"]) <= abs(benchmark["maximum_drawdown"])
                and abs(candidate["maximum_drawdown"]) <= Decimal("0.40")
            ),
        }
    return {
        "policy": DvlV2Policy.version,
        "frozen_at": frozen_at.isoformat(),
        "evaluation_start": evaluation_start.isoformat(),
        "bars": len(points),
        "execution": "first executable close strictly after decision; close-to-close total returns",
        "source_ids": sorted(
            {source for row in (*observations, *returns) for source in row.source_ids}
        ),
        "comparison": output,
        "promotion_allowed": False,
        "limitations": [
            "source_and_revision_authentication_required",
            "slippage_and_liquidity_not_modeled",
            "walk_forward_not_completed",
        ],
    }
