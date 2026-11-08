from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

ONE = Decimal("1.0")
TWO = Decimal("2.0")
EXPOSURE_STEP = Decimal("0.1")


class SafetyState(StrEnum):
    NORMAL = "NORMAL"
    PANIC_LOCK = "PANIC_LOCK"
    DATA_UNSAFE = "DATA_UNSAFE"
    ETF_ABNORMAL = "ETF_ABNORMAL"
    BROKER_UNSAFE = "BROKER_UNSAFE"
    MANUAL_HALT = "MANUAL_HALT"


class ExecutionMaturity(StrEnum):
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    BROKER_SANDBOX = "BROKER_SANDBOX"
    LIVE_CANARY = "LIVE_CANARY"
    AUTONOMOUS = "AUTONOMOUS"


class DataQuality(StrEnum):
    AUTHORITATIVE = "AUTHORITATIVE"
    SUPPLEMENTAL = "SUPPLEMENTAL"
    STALE = "STALE"
    CONTRADICTORY = "CONTRADICTORY"
    MISSING = "MISSING"


def decimal(value: Decimal | str | int | float) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def round_exposure(value: Decimal) -> Decimal:
    return value.quantize(EXPOSURE_STEP, rounding=ROUND_HALF_UP)


def clip_exposure(value: Decimal, lower: Decimal = ONE, upper: Decimal = TWO) -> Decimal:
    return min(max(value, lower), upper)


@dataclass(frozen=True, slots=True)
class DataPoint:
    name: str
    value: Decimal | str | None
    source: str
    observed_at: datetime
    published_at: datetime | None
    received_at: datetime
    quality: DataQuality
    checksum: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StrategyInputs:
    as_of: date
    pe_ttm: Decimal | None = None
    pe_percentile: Decimal | None = None
    eps_yoy: Decimal | None = None
    rv20: Decimal | None = None
    taiwan_vix: Decimal | None = None
    taiex_daily_return: Decimal | None = None
    # Optional overlays used by the shadow dynamic-leverage policy.  A drawdown
    # is expressed as a negative decimal (for example -0.25 means -25%).
    # Missing optional overlays never fabricate a signal; the policy records
    # the missing input in diagnostics and stays within its conservative cap.
    taiex_drawdown: Decimal | None = None
    funding_cost: Decimal | None = None
    critical_data_safe: bool = True


@dataclass(frozen=True, slots=True)
class PolicyState:
    previous_target: Decimal = ONE
    panic_active: bool = False
    panic_clear_streak: int = 0
    increase_candidate: Decimal | None = None
    increase_streak: int = 0
    last_increase_iso_week: tuple[int, int] | None = None

    def with_target(self, target: Decimal) -> PolicyState:
        return replace(self, previous_target=target)


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    strategy_version: str
    as_of: date
    valid: bool
    raw_target: Decimal | None
    # Invalid evaluations deliberately do not carry forward a target.  A
    # previous target is policy state, not evidence for the current run.
    target_exposure: Decimal | None
    safety_state: SafetyState
    reasons: tuple[str, ...]
    next_state: PolicyState
    diagnostics: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Position:
    instrument: str
    quantity: Decimal
    price: Decimal
    exposure_multiple: Decimal

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    as_of: datetime
    cash: Decimal
    positions: tuple[Position, ...]
    pending_settlements: Decimal = Decimal("0")

    @property
    def nav(self) -> Decimal:
        return self.cash + sum((position.market_value for position in self.positions), Decimal("0"))

    @property
    def actual_exposure(self) -> Decimal:
        if self.nav <= 0:
            return Decimal("0")
        numerator = sum(
            (position.market_value * position.exposure_multiple for position in self.positions),
            Decimal("0"),
        )
        return numerator / self.nav


@dataclass(frozen=True, slots=True)
class TradeIntent:
    intent_id: str
    created_at: datetime
    strategy_version: str
    execution_recipe: str
    current_exposure: Decimal
    requested_exposure: Decimal
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    allowed_exposure: Decimal
    safety_state: SafetyState
    reason_codes: tuple[str, ...]
    risk_reducing_only: bool = False
