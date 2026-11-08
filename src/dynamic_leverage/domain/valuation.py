"""Shared numerical and observation-period checks for ingestion and research."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


def validated_valuation_row(row: Any) -> tuple[date, Decimal, Decimal] | None:
    if not isinstance(row, dict):
        return None
    try:
        observed = date.fromisoformat(str(row["as_of"]))
        pe, eps = Decimal(str(row["pe_ttm"])), Decimal(str(row["market_eps"]))
        if not pe.is_finite() or not eps.is_finite() or pe <= 0 or eps <= 0:
            return None
        version = str(row.get("source_version", ""))
        if ":" in version:
            report, index = (date.fromisoformat(value) for value in version.split(":"))
            if (report.year, report.month) != (index.year, index.month):
                return None
            if (observed.year, observed.month) != (report.year, report.month):
                return None
        return observed, pe, eps
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None
