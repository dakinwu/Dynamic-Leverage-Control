"""Version-bound company diagnostics; partial coverage never becomes a TW50 signal."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal as D
from html.parser import HTMLParser

from dynamic_leverage.services.financial_periods import QuarterMargin
from dynamic_leverage.services.fundamental_signal import (
    FundamentalValue,
    SourceObservation,
    aware,
    prior_month,
    source_number,
)


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.stack = [], []
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.stack.append([])
        elif tag == "tr" and self.stack:
            self.stack[-1].append([])
        elif tag in {"td", "th"} and self.stack:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            if self.stack and self.stack[-1]:
                self.stack[-1][-1].append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "table" and self.stack:
            self.tables.append(self.stack.pop())


def parse_mops_monthly_revenue(source: SourceObservation, period: date) -> list[FundamentalValue]:
    """Parse the legacy domestic/foreign archive tables; availability stays current.

    Period labels and mathematical YoY agreement do not verify insurance accounting
    comparability or historical first publication. Scope remains source-reported.
    """
    import calendar

    expected = (
        f"https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_{period.year - 1911}_{period.month}_"
    )
    if source.source_url not in {expected + "0.html", expected + "1.html"}:
        raise ValueError("reviewed MOPS source and requested month must match")
    if period.day != calendar.monthrange(period.year, period.month)[1]:
        raise ValueError("month end required")
    text = source.body.decode("cp950")
    plain = re.sub(r"<[^>]+>", " ", text)
    compact = "".join(plain.split())
    if (
        f"上市公司{period.year - 1911}年{period.month}月份" not in compact
        or "單位：千元" not in compact
    ):
        raise ValueError("official MOPS period and unit header required")
    parser = _Tables()
    parser.feed(text)
    expected_header = [
        "公司代號",
        "公司名稱",
        "當月營收",
        "上月營收",
        "去年當月營收",
        "上月比較增減(%)",
        "去年同月增減(%)",
        "當月累計營收",
        "去年累計營收",
        "前期比較增減(%)",
    ]
    values, seen = [], set()
    for table in parser.tables:
        if not any(row[:10] == expected_header for row in table):
            continue
        for row in table:
            if len(row) != 11 or not row[0].isdecimal():
                continue
            if row[0] in seen:
                raise ValueError("duplicate MOPS company/month")
            seen.add(row[0])
            current, previous, reported = (source_number(row[index]) for index in (2, 4, 6))
            if (
                any(value is None for value in (current, previous, reported))
                or current < 0
                or previous <= 0
            ):
                continue
            exact = current / previous - 1
            # Legacy tables truncate percentages to two decimals; retain exact
            # amount-derived growth after checking the displayed percentage.
            if abs(exact - reported / 100) > D("0.0001"):
                raise ValueError(f"MOPS monthly YoY arithmetic mismatch: {row[0]}")
            values.append(
                FundamentalValue(
                    row[0], period, "revenue_yoy", exact, source.source_id, source.available_at
                )
            )
    if not seen:
        raise ValueError("nonempty reviewed company table required")
    return values


def parse_tsmc_sec_revenue(source: SourceObservation, period: date) -> FundamentalValue:
    """Read the exact NT$-thousands table, never the rounded million-dollar table.

    This adapter is deliberately issuer-specific, using a reviewed SEC filing
    layout. It rejects unknown identities, periods, units and ambiguous tables.
    """
    if not source.source_url.startswith("https://www.sec.gov/Archives/edgar/data/1046179/"):
        raise ValueError("reviewed TSMC SEC issuer source required")
    text = source.body.decode("utf-8")
    if "Taiwan Semiconductor Manufacturing Company Limited" not in text:
        raise ValueError("TSMC company identity missing")
    parser = _Tables()
    parser.feed(text)
    plain = re.sub(r"<[^>]+>", " ", text)
    if not re.search(r"Revenue\s*\(in NT\$ thousands\)", plain):
        raise ValueError("verified thousands unit required")
    month_labels = (
        "Jan.",
        "Feb.",
        "Mar.",
        "Apr.",
        "May",
        "Jun.",
        "Jul.",
        "Aug.",
        "Sep.",
        "Oct.",
        "Nov.",
        "Dec.",
    )
    label = month_labels[period.month - 1]
    matches = []
    for table in parser.tables:
        rows = [[cell for cell in row if cell] for row in table]
        if ["Period", "Items", str(period.year), str(period.year - 1)] not in rows:
            continue
        matches.extend(row for row in rows if len(row) == 4 and row[:2] == [label, "Net Revenue"])
    if len(matches) != 1:
        raise ValueError("unique exact monthly revenue table required")
    import calendar

    if period.day != calendar.monthrange(period.year, period.month)[1]:
        raise ValueError("month end required")
    current, previous = (D(value.replace(",", "")) for value in matches[0][2:])
    if not current.is_finite() or not previous.is_finite() or current < 0 or previous <= 0:
        raise ValueError("positive comparable revenue denominator required")
    return FundamentalValue(
        "2330",
        period,
        "revenue_yoy",
        current / previous - 1,
        source.source_id,
        source.available_at,
        "monthly",
        "consolidated",
    )


def company_diagnostic(
    current: FundamentalValue,
    previous: FundamentalValue,
    margin: QuarterMargin,
    previous_margin: QuarterMargin,
    sources: dict[str, SourceObservation],
    cutoff: datetime,
) -> dict:
    """Apply the frozen improvement hypothesis to one company, without allocation."""
    aware(cutoff)
    records = (current, previous, margin.value, previous_margin.value)
    if len({value.company for value in records}) != 1:
        raise ValueError("same company required")
    if (
        current.kind != "revenue_yoy"
        or previous.kind != "revenue_yoy"
        or current.period_basis != "monthly"
        or previous.period_basis != "monthly"
        or previous.period_end != prior_month(current.period_end)
        or margin.value.kind != "gross_margin"
        or previous_margin.value.kind != "gross_margin"
        or margin.value.period_basis != "single-quarter-verified"
        or previous_margin.value.period_basis != "single-quarter-verified"
        or previous_margin.value.period_end
        != prior_month(prior_month(prior_month(margin.value.period_end)))
        or len({value.scope for value in records}) != 1
        or margin.value.scope not in {"consolidated", "standalone"}
        or not margin.accounting_basis_id
        or not margin.comparability_id
        or margin.accounting_basis_id != previous_margin.accounting_basis_id
        or margin.comparability_id != previous_margin.comparability_id
    ):
        raise ValueError("verified comparable adjacent periods required")
    dependencies = tuple(
        dict.fromkeys(
            (
                current.source_id,
                previous.source_id,
                *margin.dependency_source_ids,
                *previous_margin.dependency_source_ids,
            )
        )
    )
    for value in records:
        source = sources.get(value.source_id)
        if source is None or value.available_at < source.available_at:
            raise ValueError("normalized value lacks source availability evidence")
    if any(key not in sources for key in dependencies):
        raise ValueError("complete source dependency chain required")
    available = max(
        *(value.available_at for value in records),
        *(sources[key].available_at for key in dependencies),
    )
    blocked = available > cutoff or any(value.period_end > cutoff.date() for value in records)
    acceleration = current.value - previous.value
    margin_change = margin.value.value - previous_margin.value.value
    return {
        "company": current.company,
        "status": "unavailable" if blocked else "company_diagnostic_only",
        "available_at": available,
        "source_ids": dependencies,
        "revenue_acceleration": None if blocked else acceleration,
        "gross_margin_change": None if blocked else margin_change,
        "improving": None if blocked else acceleration > 0 and margin_change >= 0,
        "target_exposure": None,
        "index_feature_ready": False,
        "profitability_demonstrated": False,
    }
