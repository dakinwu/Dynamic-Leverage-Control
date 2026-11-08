"""Explicit statement contexts and single-quarter conversion for research inputs."""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal as D

from dynamic_leverage.services.fundamental_signal import (
    FundamentalValue,
    SourceObservation,
    aware,
    source_number,
)


@dataclass(frozen=True)
class ReportedStatement:
    company: str
    fiscal_year_label: str
    quarter: int
    kind: str
    values: dict[str, D | None]
    source_id: str
    body_sha256: str
    available_at: datetime


@dataclass(frozen=True)
class FinancialContext:
    """Manually verified official period evidence, bound to a concrete source version.

    An OpenAPI table's quarter label alone cannot construct this context. Evidence
    observations must be retained separately and cross-checked before supplying it.
    """

    company: str
    statement_source_id: str
    statement_body_sha256: str
    evidence_source_id: str
    period_start: date
    period_end: date
    basis: str
    amount_unit: str
    scope: str
    accounting_basis_id: str
    comparability_id: str
    verified_at: datetime


@dataclass(frozen=True)
class QuarterMargin:
    value: FundamentalValue
    dependency_source_ids: tuple[str, ...]
    accounting_basis_id: str
    comparability_id: str


def parse_statements(observation: SourceObservation, kind: str) -> list[ReportedStatement]:
    if kind not in {"ci", "basi", "fh", "ins", "bd", "mim"}:
        raise ValueError("unknown official statement kind")
    rows = json.loads(observation.body)
    if not isinstance(rows, list) or not rows:
        raise ValueError("nonempty official statement array required")
    parsed, seen = [], set()
    labels = {"出表日期", "年度", "季別", "公司代號", "公司名稱"}
    for row in rows:
        company = str(row["公司代號"]).strip()
        year, quarter = str(row["年度"]), int(row["季別"])
        if not company or not year.isdecimal() or quarter not in {1, 2, 3, 4}:
            raise ValueError("invalid reported fiscal period")
        key = (company, year, quarter)
        if key in seen:
            raise ValueError("duplicate reported company period")
        seen.add(key)
        # Preserve income statement fields by exact official name, including EPS.
        # Missing fields remain missing; financial companies are not mapped to ci.
        amounts = {key: source_number(value) for key, value in row.items() if key not in labels}
        parsed.append(
            ReportedStatement(
                company,
                year,
                quarter,
                kind,
                amounts,
                observation.source_id,
                observation.body_sha256,
                observation.available_at,
            )
        )
    return parsed


def verify_context(
    statement: ReportedStatement,
    context: FinancialContext,
    sources: dict[str, SourceObservation],
    cutoff: datetime,
) -> datetime:
    aware(cutoff)
    aware(context.verified_at)
    if not context.accounting_basis_id or not context.comparability_id:
        raise ValueError("verified accounting and comparison version required")
    source = sources.get(statement.source_id)
    evidence = sources.get(context.evidence_source_id)
    if (
        context.company != statement.company
        or context.statement_source_id != statement.source_id
        or context.statement_body_sha256 != statement.body_sha256
        or source is None
        or source.body_sha256 != statement.body_sha256
        or evidence is None
    ):
        raise ValueError("context lacks source-version evidence")
    if context.evidence_source_id == statement.source_id:
        raise ValueError("quarter label cannot be its own period/unit evidence")
    if context.basis not in {"single-quarter", "year-to-date"}:
        raise ValueError("verified single-quarter or year-to-date basis required")
    if context.amount_unit not in {"TWD", "TWD-thousand", "TWD-million"}:
        raise ValueError("verified amount unit required")
    if context.scope not in {"consolidated", "standalone"}:
        raise ValueError("verified statement scope required")
    end = context.period_end
    if (
        context.period_start > end
        or end.month not in {3, 6, 9, 12}
        or end.day != (calendar.monthrange(end.year, end.month)[1])
    ):
        raise ValueError("verified calendar fiscal quarter required")
    if end.year != int(statement.fiscal_year_label) + 1911 or end.month != statement.quarter * 3:
        raise ValueError("context does not match reported calendar fiscal period")
    start_month = 1 if context.basis == "year-to-date" else end.month - 2
    if context.period_start != date(end.year, start_month, 1):
        raise ValueError("context start does not match stated period basis")
    available = max(
        statement.available_at, source.available_at, evidence.available_at, context.verified_at
    )
    if available > cutoff:
        raise ValueError("statement or context was not available at cutoff")
    return available


def general_quarter_margin(
    statement: ReportedStatement,
    context: FinancialContext,
    sources: dict[str, SourceObservation],
    cutoff: datetime,
    *,
    preceding: tuple[ReportedStatement, FinancialContext] | None = None,
) -> QuarterMargin:
    """Only gross profit and revenue may be differenced; EPS is never subtracted."""
    if statement.kind != "ci":
        raise ValueError("financial-industry income mapping is unverified")
    available = verify_context(statement, context, sources, cutoff)
    fields = ("營業收入", "營業毛利（毛損）淨額")
    amounts = [statement.values.get(key) for key in fields]
    if any(value is None for value in amounts):
        raise ValueError("complete comparable revenue and gross profit required")
    source_ids = [statement.source_id, context.evidence_source_id]
    if context.basis == "year-to-date" and statement.quarter > 1:
        if preceding is None:
            raise ValueError("preceding year-to-date quarter required")
        old, old_context = preceding
        old_available = verify_context(old, old_context, sources, cutoff)
        if (
            old.company != statement.company
            or old.kind != statement.kind
            or old.fiscal_year_label != statement.fiscal_year_label
            or old.quarter != statement.quarter - 1
            or old_context.basis != "year-to-date"
            or old_context.amount_unit != context.amount_unit
            or old_context.scope != context.scope
            or old_context.accounting_basis_id != context.accounting_basis_id
            or old_context.comparability_id != context.comparability_id
        ):
            raise ValueError("preceding quarter is not comparable")
        prior = [old.values.get(key) for key in fields]
        if any(value is None for value in prior):
            raise ValueError("preceding quarter lacks required amounts")
        amounts = [current - prior for current, prior in zip(amounts, prior, strict=True)]
        available = max(available, old_available)
        source_ids.extend((old.source_id, old_context.evidence_source_id))
    revenue, gross_profit = amounts
    if revenue <= 0:
        raise ValueError("positive single-quarter revenue required")
    # Preserve all four original source dependencies, not just the current table.
    return QuarterMargin(
        FundamentalValue(
            statement.company,
            context.period_end,
            "gross_margin",
            gross_profit / revenue,
            statement.source_id,
            available,
            "single-quarter-verified",
            context.scope,
        ),
        tuple(dict.fromkeys(source_ids)),
        context.accounting_basis_id,
        context.comparability_id,
    )
