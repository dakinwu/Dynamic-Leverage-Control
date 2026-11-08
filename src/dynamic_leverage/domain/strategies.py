from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Protocol

from dynamic_leverage.domain.models import (
    ONE,
    TWO,
    PolicyState,
    SafetyState,
    StrategyDecision,
    StrategyInputs,
    clip_exposure,
    round_exposure,
)


class StrategyPolicy(Protocol):
    version: str

    def evaluate(self, inputs: StrategyInputs, state: PolicyState) -> StrategyDecision: ...


def _volatility_cap(volatility: Decimal) -> Decimal:
    if volatility < Decimal("0.20"):
        return TWO
    if volatility < Decimal("0.25"):
        return Decimal("1.8")
    if volatility < Decimal("0.30"):
        return Decimal("1.6")
    if volatility < Decimal("0.35"):
        return Decimal("1.4")
    if volatility < Decimal("0.45"):
        return Decimal("1.2")
    return ONE


def _apply_persistence(
    proposed: Decimal,
    inputs: StrategyInputs,
    state: PolicyState,
) -> tuple[Decimal, PolicyState, tuple[str, ...]]:
    previous = state.previous_target
    if proposed < previous:
        next_state = replace(
            state,
            previous_target=proposed,
            increase_candidate=None,
            increase_streak=0,
        )
        return proposed, next_state, ("RISK_REDUCTION_IMMEDIATE",)

    if proposed == previous:
        next_state = replace(state, increase_candidate=None, increase_streak=0)
        return previous, next_state, ("TARGET_UNCHANGED",)

    same_candidate = state.increase_candidate == proposed
    streak = state.increase_streak + 1 if same_candidate else 1
    candidate_state = replace(state, increase_candidate=proposed, increase_streak=streak)
    if streak < 3:
        return previous, candidate_state, ("INCREASE_CONFIRMATION_PENDING",)

    iso = inputs.as_of.isocalendar()
    iso_week = (iso.year, iso.week)
    if state.last_increase_iso_week == iso_week:
        return previous, candidate_state, ("WEEKLY_INCREASE_LIMIT",)

    allowed = min(proposed, previous + Decimal("0.2"))
    next_state = replace(
        candidate_state,
        previous_target=allowed,
        increase_candidate=None,
        increase_streak=0,
        last_increase_iso_week=iso_week,
    )
    return allowed, next_state, ("INCREASE_CONFIRMED", "WEEKLY_STEP_APPLIED")


def _invalid_decision(
    version: str,
    inputs: StrategyInputs,
    state: PolicyState,
    *reasons: str,
) -> StrategyDecision:
    return StrategyDecision(
        strategy_version=version,
        as_of=inputs.as_of,
        valid=False,
        raw_target=None,
        target_exposure=None,
        safety_state=SafetyState.DATA_UNSAFE,
        reasons=tuple(reasons),
        next_state=state,
    )


class ConstantExposurePolicy:
    version = "constant-1.2-v1"

    def __init__(
        self,
        target: Decimal = Decimal("1.2"),
        *,
        version: str | None = None,
    ) -> None:
        # Keep the 1.2x baseline backwards compatible while allowing the
        # registry to expose an independent, immutable 2.0x candidate.
        self.version = version or type(self).version
        self.target = clip_exposure(target)

    def evaluate(self, inputs: StrategyInputs, state: PolicyState) -> StrategyDecision:
        if not inputs.critical_data_safe:
            return _invalid_decision(self.version, inputs, state, "CRITICAL_DATA_UNSAFE")
        next_state = replace(state, previous_target=self.target)
        return StrategyDecision(
            strategy_version=self.version,
            as_of=inputs.as_of,
            valid=True,
            raw_target=self.target,
            target_exposure=self.target,
            safety_state=SafetyState.NORMAL,
            reasons=("CONSTANT_EXPOSURE",),
            next_state=next_state,
        )


class DvlV10Policy:
    version = "dvl-tw-v1.0"

    @staticmethod
    def _absolute_pe_target(pe: Decimal) -> Decimal:
        if pe >= Decimal("30"):
            return ONE
        if pe >= Decimal("28"):
            return Decimal("1.2")
        if pe >= Decimal("26"):
            return Decimal("1.4")
        if pe >= Decimal("24"):
            return Decimal("1.6")
        if pe >= Decimal("22"):
            return Decimal("1.8")
        return TWO

    @staticmethod
    def _percentile_target(percentile: Decimal) -> Decimal:
        if percentile >= Decimal("0.85"):
            return ONE
        if percentile >= Decimal("0.70"):
            return Decimal("1.2")
        if percentile >= Decimal("0.50"):
            return Decimal("1.4")
        if percentile >= Decimal("0.30"):
            return Decimal("1.6")
        if percentile >= Decimal("0.15"):
            return Decimal("1.8")
        return TWO

    @staticmethod
    def _eps_modifier(eps_yoy: Decimal) -> Decimal:
        if eps_yoy >= Decimal("0"):
            return Decimal("0")
        if eps_yoy >= Decimal("-0.10"):
            return Decimal("-0.1")
        if eps_yoy >= Decimal("-0.20"):
            return Decimal("-0.2")
        return Decimal("-0.3")

    def evaluate(self, inputs: StrategyInputs, state: PolicyState) -> StrategyDecision:
        required = (
            inputs.pe_ttm,
            inputs.pe_percentile,
            inputs.eps_yoy,
            inputs.rv20,
            inputs.taiwan_vix,
        )
        if not inputs.critical_data_safe or any(value is None for value in required):
            return _invalid_decision(self.version, inputs, state, "V1_0_INPUT_MISSING")

        assert inputs.pe_ttm is not None
        assert inputs.pe_percentile is not None
        assert inputs.eps_yoy is not None
        assert inputs.rv20 is not None
        assert inputs.taiwan_vix is not None

        absolute = self._absolute_pe_target(inputs.pe_ttm)
        percentile = self._percentile_target(inputs.pe_percentile)
        valuation = round_exposure(Decimal("0.7") * absolute + Decimal("0.3") * percentile)
        raw = clip_exposure(valuation + self._eps_modifier(inputs.eps_yoy))
        stress_vol = max(inputs.rv20, inputs.taiwan_vix / Decimal("100"))
        risk_target = min(raw, _volatility_cap(stress_vol))

        panic_trigger = (
            inputs.taiwan_vix >= Decimal("35")
            or inputs.rv20 >= Decimal("0.35")
            or (
                inputs.taiex_daily_return is not None
                and inputs.taiex_daily_return <= Decimal("-0.07")
            )
        )
        panic_active = state.panic_active or panic_trigger
        clear_condition = inputs.taiwan_vix < Decimal("30") and inputs.rv20 < Decimal("0.30")
        clear_streak = state.panic_clear_streak + 1 if panic_active and clear_condition else 0
        if panic_active and clear_streak >= 5:
            panic_active = False
            clear_streak = 0

        proposed = min(risk_target, state.previous_target) if panic_active else risk_target
        panic_state = replace(
            state,
            panic_active=panic_active,
            panic_clear_streak=clear_streak,
        )
        target, next_state, persistence_reasons = _apply_persistence(
            round_exposure(proposed), inputs, panic_state
        )
        safety = SafetyState.PANIC_LOCK if panic_active else SafetyState.NORMAL
        reasons = (
            "P_E_ABSOLUTE_AND_PERCENTILE",
            "EPS_MODIFIER_APPLIED",
            "RV20_VIX_CAP_APPLIED",
            *(("PANIC_TRIGGERED",) if panic_trigger else ()),
            *persistence_reasons,
        )
        return StrategyDecision(
            strategy_version=self.version,
            as_of=inputs.as_of,
            valid=True,
            raw_target=round_exposure(risk_target),
            target_exposure=target,
            safety_state=safety,
            reasons=reasons,
            next_state=next_state,
            diagnostics={
                "absolute_target": str(absolute),
                "percentile_target": str(percentile),
                "valuation_target": str(valuation),
                "stress_vol": str(stress_vol),
            },
        )


class DvlV11Policy:
    def __init__(self, *, winsorized: bool = False) -> None:
        self.winsorized = winsorized
        self.version = "dvl-tw-v1.1-winsorized" if winsorized else "dvl-tw-v1.1-raw"

    def evaluate(self, inputs: StrategyInputs, state: PolicyState) -> StrategyDecision:
        required = (inputs.pe_percentile, inputs.rv20)
        if not inputs.critical_data_safe or any(value is None for value in required):
            return _invalid_decision(self.version, inputs, state, "V1_1_INPUT_MISSING")

        assert inputs.pe_percentile is not None
        assert inputs.rv20 is not None
        percentile = inputs.pe_percentile
        if self.winsorized:
            percentile = min(max(percentile, Decimal("0.05")), Decimal("0.95"))

        pe_target = round_exposure(clip_exposure(TWO - percentile))
        risk_target = min(pe_target, _volatility_cap(inputs.rv20))
        panic_trigger = inputs.rv20 >= Decimal("0.35") or (
            inputs.taiex_daily_return is not None and inputs.taiex_daily_return <= Decimal("-0.07")
        )
        panic_active = state.panic_active or panic_trigger
        clear_condition = inputs.rv20 < Decimal("0.30")
        clear_streak = state.panic_clear_streak + 1 if panic_active and clear_condition else 0
        if panic_active and clear_streak >= 5:
            panic_active = False
            clear_streak = 0

        proposed = min(risk_target, state.previous_target) if panic_active else risk_target
        panic_state = replace(
            state,
            panic_active=panic_active,
            panic_clear_streak=clear_streak,
        )
        target, next_state, persistence_reasons = _apply_persistence(
            round_exposure(proposed), inputs, panic_state
        )
        reasons = (
            "P_E_PERCENTILE",
            "RV20_CAP_APPLIED",
            *(("PERCENTILE_WINSORIZED",) if self.winsorized else ()),
            *(("PANIC_TRIGGERED",) if panic_trigger else ()),
            *persistence_reasons,
        )
        return StrategyDecision(
            strategy_version=self.version,
            as_of=inputs.as_of,
            valid=True,
            raw_target=round_exposure(risk_target),
            target_exposure=target,
            safety_state=SafetyState.PANIC_LOCK if panic_active else SafetyState.NORMAL,
            reasons=reasons,
            next_state=next_state,
            diagnostics={
                "pe_percentile": str(percentile),
                "pe_target": str(pe_target),
                "rv20_cap": str(_volatility_cap(inputs.rv20)),
            },
        )


class DvlV2Policy:
    """Shadow/Paper candidate for the article-inspired dynamic leverage rule.

    This policy deliberately separates a *candidate* ceiling (3.2x) from the
    live cash-account ceiling (2.0x).  A target above 2.0x is therefore useful
    for replay and paper analysis, but cannot be interpreted as an executable
    order plan.  All thresholds are explicit hypotheses and are exposed in
    diagnostics so a later walk-forward run can reject or replace them.
    """

    version = "dvl-tw-v2-shadow"
    candidate_ceiling = Decimal("3.2")
    executable_cash_ceiling = TWO

    @staticmethod
    def _valuation_target(percentile: Decimal) -> Decimal:
        if percentile <= Decimal("0.20"):
            return Decimal("3.2")
        if percentile <= Decimal("0.40"):
            return Decimal("2.8")
        if percentile <= Decimal("0.60"):
            return Decimal("2.4")
        if percentile <= Decimal("0.80"):
            return Decimal("2.0")
        return Decimal("1.6")

    @staticmethod
    def _eps_modifier(eps_yoy: Decimal) -> Decimal:
        if eps_yoy >= Decimal("0"):
            return Decimal("0")
        if eps_yoy >= Decimal("-0.10"):
            return Decimal("-0.2")
        if eps_yoy >= Decimal("-0.20"):
            return Decimal("-0.4")
        return Decimal("-0.6")

    @staticmethod
    def _volatility_cap(rv20: Decimal, taiwan_vix: Decimal) -> Decimal:
        stress_vol = max(rv20, taiwan_vix / Decimal("100"))
        if stress_vol < Decimal("0.20"):
            return Decimal("3.2")
        if stress_vol < Decimal("0.25"):
            return Decimal("2.8")
        if stress_vol < Decimal("0.30"):
            return Decimal("2.4")
        if stress_vol < Decimal("0.35"):
            return Decimal("2.0")
        if stress_vol < Decimal("0.45"):
            return Decimal("1.6")
        return ONE

    @staticmethod
    def _drawdown_cap(drawdown: Decimal | None) -> Decimal:
        # Deep drawdowns unlock more of the candidate range, but never force a
        # purchase.  This is an allocation ceiling, not a buy-the-dip signal.
        if drawdown is None or drawdown > Decimal("-0.10"):
            return TWO
        if drawdown > Decimal("-0.20"):
            return Decimal("2.4")
        if drawdown > Decimal("-0.35"):
            return Decimal("2.8")
        return Decimal("3.2")

    @staticmethod
    def _funding_cap(funding_cost: Decimal | None) -> Decimal:
        if funding_cost is None or funding_cost <= Decimal("0.03"):
            return Decimal("3.2")
        if funding_cost <= Decimal("0.05"):
            return Decimal("2.0")
        return ONE

    def evaluate(self, inputs: StrategyInputs, state: PolicyState) -> StrategyDecision:
        required = (inputs.pe_percentile, inputs.eps_yoy, inputs.rv20, inputs.taiwan_vix)
        if not inputs.critical_data_safe or any(value is None for value in required):
            return _invalid_decision(self.version, inputs, state, "V2_INPUT_MISSING")

        assert inputs.pe_percentile is not None
        assert inputs.eps_yoy is not None
        assert inputs.rv20 is not None
        assert inputs.taiwan_vix is not None

        valuation = self._valuation_target(inputs.pe_percentile)
        eps_adjusted = clip_exposure(
            valuation + self._eps_modifier(inputs.eps_yoy),
            upper=self.candidate_ceiling,
        )
        vol_cap = self._volatility_cap(inputs.rv20, inputs.taiwan_vix)
        drawdown_cap = self._drawdown_cap(inputs.taiex_drawdown)
        funding_cap = self._funding_cap(inputs.funding_cost)
        candidate = min(eps_adjusted, vol_cap, drawdown_cap, funding_cap)

        panic_trigger = (
            inputs.taiwan_vix >= Decimal("35")
            or inputs.rv20 >= Decimal("0.35")
            or (
                inputs.taiex_daily_return is not None
                and inputs.taiex_daily_return <= Decimal("-0.07")
            )
        )
        panic_active = state.panic_active or panic_trigger
        clear_condition = inputs.taiwan_vix < Decimal("30") and inputs.rv20 < Decimal("0.30")
        clear_streak = state.panic_clear_streak + 1 if panic_active and clear_condition else 0
        if panic_active and clear_streak >= 5:
            panic_active = False
            clear_streak = 0

        # During panic lock only reduce; do not automatically increase after a
        # drawdown.  Persistence still applies to increases on normal days.
        proposed = min(candidate, state.previous_target) if panic_active else candidate
        panic_state = replace(state, panic_active=panic_active, panic_clear_streak=clear_streak)
        target, next_state, persistence_reasons = _apply_persistence(
            round_exposure(proposed), inputs, panic_state
        )
        reasons = (
            "P_E_PERCENTILE_CANDIDATE",
            "EPS_MODIFIER_APPLIED",
            "RV20_VIX_CAP_APPLIED",
            "DRAWDOWN_CAP_APPLIED",
            "FUNDING_CAP_APPLIED",
            "SHADOW_ONLY_ABOVE_2X",
            *(("PANIC_TRIGGERED",) if panic_trigger else ()),
            *persistence_reasons,
        )
        return StrategyDecision(
            strategy_version=self.version,
            as_of=inputs.as_of,
            valid=True,
            raw_target=round_exposure(candidate),
            target_exposure=target,
            safety_state=SafetyState.PANIC_LOCK if panic_active else SafetyState.NORMAL,
            reasons=reasons,
            next_state=next_state,
            diagnostics={
                "pe_percentile": str(inputs.pe_percentile),
                "eps_yoy": str(inputs.eps_yoy),
                "rv20": str(inputs.rv20),
                "taiwan_vix": str(inputs.taiwan_vix),
                "valuation_target": str(valuation),
                "eps_adjusted_target": str(eps_adjusted),
                "volatility_cap": str(vol_cap),
                "drawdown_cap": str(drawdown_cap),
                "funding_cap": str(funding_cap),
                "candidate_ceiling": str(self.candidate_ceiling),
                "executable_cash_ceiling": str(self.executable_cash_ceiling),
                "stress_vol": str(max(inputs.rv20, inputs.taiwan_vix / Decimal("100"))),
                "drawdown_input": (
                    "missing" if inputs.taiex_drawdown is None else str(inputs.taiex_drawdown)
                ),
                "funding_cost_input": (
                    "missing" if inputs.funding_cost is None else str(inputs.funding_cost)
                ),
            },
        )
