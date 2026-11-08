"""Point-in-time research features; never produces an order or an exposure target."""

from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal as D


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware availability required")
    return value


def source_number(value: str) -> D | None:
    if value.strip() in {"", "-", "--", "N/A", "不適用"}:
        return None
    number = D(value.replace(",", ""))
    if not number.is_finite():
        raise ValueError("nonfinite fundamental value")
    return number


def roc_month(value: str) -> date:
    if len(value) not in {5, 6} or not value.isdecimal():
        raise ValueError("invalid ROC revenue month")
    year, month = int(value[:-2]) + 1911, int(value[-2:])
    return date(year, month, calendar.monthrange(year, month)[1])


def prior_month(day: date) -> date:
    year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
    return date(year, month, calendar.monthrange(year, month)[1])


@dataclass(frozen=True)
class SourceObservation:
    source_id: str
    source_url: str
    body: bytes
    body_sha256: str
    received_at: datetime
    recorded_at: datetime
    published_at: datetime | None = None

    def __post_init__(self):
        aware(self.received_at)
        aware(self.recorded_at)
        if self.published_at is not None:
            aware(self.published_at)
        if hashlib.sha256(self.body).hexdigest() != self.body_sha256:
            raise ValueError("fundamental source fingerprint mismatch")
        if not self.source_id or not self.source_url:
            raise ValueError("source identity required")

    @property
    def available_at(self) -> datetime:
        # A source table date is not a publication clock. Later downloads cannot
        # become available in historical queries by inserting an old receive time.
        return max(self.received_at, self.recorded_at)


@dataclass(frozen=True)
class FundamentalValue:
    company: str
    period_end: date
    kind: str
    value: D
    source_id: str
    available_at: datetime
    period_basis: str = "monthly"
    scope: str = "source-reported"

    def __post_init__(self):
        aware(self.available_at)
        if not self.value.is_finite() or not self.company or not self.source_id:
            raise ValueError("invalid normalized fundamental record")


def parse_revenue(observation: SourceObservation) -> list[FundamentalValue]:
    rows = json.loads(observation.body)
    if not isinstance(rows, list) or not rows:
        raise ValueError("official revenue response must be a nonempty array")
    values, seen = [], set()
    for row in rows:
        company = str(row["公司代號"]).strip()
        period = roc_month(row["資料年月"])
        key = (company, period)
        if key in seen:
            raise ValueError("duplicate company revenue period")
        seen.add(key)
        growth = source_number(row["營業收入-去年同月增減(%)"])
        current = source_number(row["營業收入-當月營收"])
        prior = source_number(row["營業收入-去年當月營收"])
        if growth is None or current is None or prior is None or prior <= 0 or current < 0:
            continue
        # Both nominal amounts originate in the same row/unit. Cross-check the
        # reported percentage without claiming the unknown currency unit is TWD.
        if abs((current / prior - 1) - growth / 100) > D("0.0001"):
            raise ValueError(f"revenue growth arithmetic mismatch: {company}")
        values.append(
            FundamentalValue(
                company,
                period,
                "revenue_yoy",
                growth / 100,
                observation.source_id,
                observation.available_at,
            )
        )
    return values


@dataclass(frozen=True)
class UniverseObservation:
    kind: str
    effective_at: datetime
    source_id: str
    members: dict[str, D]
    profitability_metrics: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        aware(self.effective_at)


def evaluate_signal(
    sources: list[SourceObservation],
    values: list[FundamentalValue],
    universe: UniverseObservation | None,
    *,
    cutoff: datetime,
    revenue_month: date,
    margin_quarter: date,
) -> dict:
    """Frozen v1 hypothesis: positive revenue acceleration AND nondeclining margin.

    Requires all 50 contemporary companies. General companies use gross margin;
    financial companies require explicitly mapped net margin and verified income
    semantics. Unsupported/partial coverage yields no renormalized aggregate score.
    """
    aware(cutoff)
    blockers = []
    by_source = {s.source_id: s for s in sources}
    if len(by_source) != len(sources):
        raise ValueError("duplicate source identity")
    if revenue_month.day != calendar.monthrange(revenue_month.year, revenue_month.month)[1]:
        raise ValueError("revenue signal requires a month end")
    if (
        margin_quarter.month not in {3, 6, 9, 12}
        or margin_quarter.day != calendar.monthrange(margin_quarter.year, margin_quarter.month)[1]
    ):
        raise ValueError("margin signal requires a quarter end")
    if revenue_month > cutoff.date() or margin_quarter > cutoff.date():
        blockers.append("FUTURE_PERIOD")
    if universe is None:
        blockers.append("MISSING_CONTEMPORANEOUS_UNIVERSE")
    elif (
        universe.kind != "TW50-index"
        or len(universe.members) != 50
        or any(not w.is_finite() or w <= 0 for w in universe.members.values())
        or abs(sum(universe.members.values(), D(0)) - 1) > D("0.000001")
        or any(
            company not in universe.members or metric not in {"gross_margin", "net_margin"}
            for company, metric in universe.profitability_metrics.items()
        )
    ):
        blockers.append("INVALID_OR_PARTIAL_TW50_UNIVERSE")
    elif (
        universe.effective_at > cutoff
        or universe.source_id not in by_source
        or by_source[universe.source_id].available_at > cutoff
    ):
        blockers.append("UNIVERSE_NOT_AVAILABLE")
    latest = {}
    for v in values:
        source = by_source.get(v.source_id)
        if source is None or v.available_at < source.available_at:
            raise ValueError("value lacks matching source availability evidence")
        if v.available_at > cutoff or source.available_at > cutoff:
            continue
        key = (v.company, v.period_end, v.kind)
        old = latest.get(key)
        if (
            old
            and old.available_at == v.available_at
            and (old.value != v.value or old.period_basis != v.period_basis or old.scope != v.scope)
        ):
            raise ValueError("conflicting contemporaneous source versions")
        if old is None or old.available_at < v.available_at:
            latest[key] = v
    components = []
    if not blockers:
        previous_quarter = prior_month(prior_month(prior_month(margin_quarter)))
        for company, weight in universe.members.items():
            metric = universe.profitability_metrics.get(company, "gross_margin")
            records = [
                latest.get((company, period, kind))
                for period, kind in (
                    (revenue_month, "revenue_yoy"),
                    (prior_month(revenue_month), "revenue_yoy"),
                    (margin_quarter, metric),
                    (previous_quarter, metric),
                )
            ]
            if any(r is None for r in records):
                blockers.append(f"MISSING_COMPARABLE_PERIODS:{company}")
                continue
            current, previous, margin, previous_margin = records
            if (
                current.period_basis != "monthly"
                or previous.period_basis != "monthly"
                or current.scope != previous.scope
                or margin.period_basis != "single-quarter-verified"
                or previous_margin.period_basis != "single-quarter-verified"
                or margin.scope != previous_margin.scope
                or margin.scope not in {"consolidated", "standalone"}
            ):
                blockers.append(f"UNVERIFIED_PERIOD_OR_SCOPE:{company}")
                continue
            acceleration = current.value - previous.value
            margin_change = margin.value - previous_margin.value
            components.append(
                {
                    "company": company,
                    "weight": weight,
                    "revenue_acceleration": acceleration,
                    "margin_change": margin_change,
                    "profitability_metric": metric,
                    "improving": acceleration > 0 and margin_change >= 0,
                    "source_ids": [r.source_id for r in records],
                }
            )
    return {
        "schema": "fundamental-improvement-hypothesis-v1",
        "cutoff": cutoff,
        "status": "unavailable" if blockers else "research_feature_only",
        "blockers": blockers,
        "covered_companies": len(components),
        "covered_weight": sum((c["weight"] for c in components), D(0)),
        "weighted_revenue_acceleration": None
        if blockers
        else sum((c["weight"] * c["revenue_acceleration"] for c in components), D(0)),
        "improving_weight": None
        if blockers
        else sum((c["weight"] for c in components if c["improving"]), D(0)),
        "components": components,
        "target_exposure": None,
        "profitability_demonstrated": False,
    }
