from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any

from dynamic_leverage.domain.models import StrategyInputs
from dynamic_leverage.domain.valuation import validated_valuation_row
from dynamic_leverage.infrastructure.repository import AuditRepository


@dataclass(frozen=True, slots=True)
class StrategyPipelineResult:
    inputs: StrategyInputs | None
    snapshot_ids: tuple[str, ...]
    missing: tuple[str, ...]
    diagnostics: dict[str, str]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)

    @property
    def text(self) -> str:
        return " ".join(self.parts)


def _body(record: Any) -> Any:
    payload = record.payload or {}
    if "body" in payload:
        return payload["body"]
    return payload.get("body_text", "")


def _roc_date(value: str) -> date | None:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 8:
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        except ValueError:
            return None
    if len(digits) != 7:
        return None
    try:
        return date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:]))
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    cleaned = str(value).replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned in {"--", "-", "N/A", "null"}:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


@dataclass(frozen=True, slots=True)
class TaiexPoint:
    as_of: date
    close: Decimal
    daily_return: Decimal | None


def parse_taiex_points(records: list[Any]) -> list[TaiexPoint]:
    points: list[TaiexPoint] = []
    for record in records:
        body = _body(record)
        rows: list[dict[str, Any]] = []
        default_date = _roc_date(str(body.get("date", ""))) if isinstance(body, dict) else None
        if isinstance(body, list):
            rows = [row for row in body if isinstance(row, dict)]
        elif isinstance(body, dict) and "MI_5MINS_HIST" in getattr(record, "source", ""):
            fields = body.get("fields", [])
            rows = [
                {**dict(zip(fields, values, strict=False)), "指數": "發行量加權股價指數"}
                for values in body.get("data", [])
            ]
        elif isinstance(body, dict) and isinstance(body.get("tables"), list):
            for table in body["tables"]:
                if not isinstance(table, dict) or not isinstance(table.get("fields"), list):
                    continue
                fields = [str(field) for field in table["fields"]]
                rows.extend(
                    dict(zip(fields, values, strict=False))
                    for values in table.get("data", [])
                    if isinstance(values, list)
                )
        for row in rows:
            if not isinstance(row, dict):
                continue
            index_name = str(row.get("指數", ""))
            if "加權" not in index_name:
                continue
            as_of = _roc_date(str(row.get("日期", ""))) or default_date
            close = _decimal(row.get("收盤指數"))
            if as_of is None or close is None or close <= 0:
                continue
            change = _decimal(row.get("漲跌百分比") or row.get("漲跌百分比(%)"))
            sign = str(row.get("漲跌", "")).strip()
            if change is not None:
                change = change / Decimal("100")
                if sign == "-":
                    change = -abs(change)
            points.append(TaiexPoint(as_of, close, change))
    # Repository records are newest-first; keep the first observation for a
    # date so historical backfill cannot overwrite a fresher daily snapshot.
    unique: dict[date, TaiexPoint] = {}
    for point in points:
        unique.setdefault(point.as_of, point)
    ordered = sorted(unique.values(), key=lambda point: point.as_of)
    return [
        TaiexPoint(
            point.as_of,
            point.close,
            point.daily_return
            if point.daily_return is not None
            else (point.close / ordered[i - 1].close - 1 if i else None),
        )
        for i, point in enumerate(ordered)
    ]


def compute_rv20(points: list[TaiexPoint]) -> Decimal | None:
    if len(points) < 21:
        return None
    closes = [point.close for point in points[-21:]]
    # Product specification: simple close-to-close returns (not log returns).
    returns = [float(closes[i] / closes[i - 1] - Decimal("1")) for i in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    return Decimal(str(math.sqrt(variance * 252)))


def parse_taiwan_vix(records: list[Any]) -> Decimal | None:
    """Extract the official last-minute average from a TAIFEX VIX download.

    TAIFEX's public HTML page lists download links; the downloaded Big5 text
    file contains a row such as ``Last 1 min AVG ... 24.00``.  The raw snapshot
    is decoded with replacement characters by ingestion, but this row's ASCII
    label and numeric value remain intact.
    """
    for record in records:
        raw = str(_body(record))
        download_candidates = re.findall(
            r"Last\s+1\s+min\s+AVG[^0-9]{0,40}(\d{1,3}(?:\.\d+)?)",
            raw,
            flags=re.IGNORECASE,
        )
        for candidate in download_candidates:
            value = _decimal(candidate)
            if value is not None and Decimal("5") <= value < Decimal("200"):
                return value
        parser = _TextExtractor()
        try:
            parser.feed(raw)
            text = parser.text
        except Exception:  # noqa: BLE001 - malformed vendor HTML is simply missing
            text = raw
        candidates = re.findall(
            r"(?:VIX|波動率|臺股波動率)[^0-9]{0,80}(\d{1,3}(?:\.\d+)?)",
            text,
            flags=re.IGNORECASE,
        )
        for candidate in candidates:
            value = _decimal(candidate)
            # The page also contains unrelated version/date numbers.  A
            # single-digit match is not a plausible Taiwan VIX observation,
            # so fail closed instead of turning a page chrome number into a
            # volatility signal.
            if value is not None and Decimal("5") <= value < Decimal("200"):
                return value
    return None


def parse_pcf_exposure(records: list[Any], code: str) -> Decimal | None:
    """Best-effort PCF exposure extraction for diagnostics, never for pricing."""
    for record in records:
        raw = str(_body(record))
        parser = _TextExtractor()
        try:
            parser.feed(raw)
            text = parser.text
        except Exception:  # noqa: BLE001
            text = raw
        patterns = (
            r"(?:實際)?曝險[^0-9]{0,40}(\d{2,3}(?:\.\d+)?)\s*%",
            r"(?:TX|臺指)[^0-9]{0,80}(\d{2,3}(?:\.\d+)?)\s*%",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                value = _decimal(match.group(1))
                if value is not None and Decimal("0") < value <= Decimal("400"):
                    return value / Decimal("100")
    return None


def _valuation_inputs(
    records: list[Any], as_of: date
) -> tuple[Decimal | None, Decimal | None, Decimal | None, str | None, int]:
    by_month: dict[tuple[int, int], tuple[date, Decimal, Decimal, str]] = {}
    for record in records:
        payload = record.payload or {}
        for row in payload.get("rows", []) if isinstance(payload, dict) else []:
            validated = validated_valuation_row(row)
            if validated is None:
                continue
            row_date, pe, eps = validated
            if row_date > as_of:
                continue
            key = (row_date.year, row_date.month)
            existing = by_month.get(key)
            # Records arrive newest-first. For the same represented date, keep
            # the first known version; within a month, use its latest valid date.
            if existing is None or row_date > existing[0]:
                by_month[key] = (row_date, pe, eps, record.snapshot_id)
    points = sorted(by_month.values(), key=lambda point: point[0])
    if not points:
        return None, None, None, None, 0
    current_date, current_pe, current_eps, source_id = points[-1]
    # Percentile uses the previous 120 valid observations and excludes the
    # current month. Mid-rank ties make the result deterministic.
    current_ordinal = current_date.year * 12 + current_date.month
    required = {
        divmod(ordinal - 1, 12) for ordinal in range(current_ordinal - 120, current_ordinal)
    }
    history = [point[1] for key, point in by_month.items() if (key[0], key[1] - 1) in required]
    percentile = None
    if len(history) == 120:
        less = sum(pe < current_pe for pe in history)
        equal = sum(pe == current_pe for pe in history)
        percentile = (Decimal(less) + Decimal(equal) / Decimal("2")) / Decimal(len(history))
    prior = min(
        (
            point
            for point in points[:-1]
            if point[0].year == current_date.year - 1 and point[0].month == current_date.month
        ),
        key=lambda point: abs((current_date - point[0]).days - 365),
        default=None,
    )
    eps_yoy = None if prior is None else current_eps / prior[2] - Decimal("1")
    return current_pe, percentile, eps_yoy, source_id, len(history)


def _records_available_on(records: list[Any], as_of: date) -> list[Any]:
    """Keep records observed and received by the end of the decision date."""
    available = []
    for record in records:
        received_at = getattr(record, "received_at", None)
        observed_at = getattr(record, "observed_at", None)
        published_at = getattr(record, "published_at", None)
        if received_at is not None and received_at.date() > as_of:
            continue
        if observed_at is not None and observed_at.date() > as_of:
            continue
        if published_at is not None and published_at.date() > as_of:
            continue
        available.append(record)
    return available


def build_strategy_inputs(
    repository: AuditRepository,
    *,
    as_of: date | None = None,
    decision_at: datetime | None = None,
) -> StrategyPipelineResult:
    if decision_at is not None and decision_at.tzinfo is None:
        raise ValueError("decision_at must include a timezone")
    cutoff = decision_at or (
        datetime.combine(as_of, time.max, tzinfo=UTC) if as_of else datetime.now(UTC)
    )

    def available(records: list[Any]) -> list[Any]:
        return [
            record
            for record in records
            if all(
                timestamp is None
                or (timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)) <= cutoff
                for timestamp in (record.received_at, record.observed_at, record.published_at)
            )
        ]

    taiex_records = repository.raw_snapshots_for_kind(kind="market.taiex_index.official")
    taiex_records += repository.raw_snapshots_for_kind(
        kind="history.taiex_index.official", limit=10000
    )
    vix_records = repository.raw_snapshots_for_kind(kind="market.taifex_vix.official")
    valuation_records = repository.raw_snapshots_for_kind(
        kind="valuation.taiex.manual", limit=10000
    )
    taiex_records, vix_records, valuation_records = (
        available(records) for records in (taiex_records, vix_records, valuation_records)
    )
    points = parse_taiex_points(taiex_records)
    market_date = as_of or (points[-1].as_of if points else date.today())
    points = parse_taiex_points(taiex_records)
    current = next((point for point in reversed(points) if point.as_of <= market_date), None)
    rv20 = compute_rv20([point for point in points if point.as_of <= market_date])
    vix = parse_taiwan_vix(vix_records)
    pe, pe_percentile, eps_yoy, valuation_id, valuation_points = _valuation_inputs(
        valuation_records, market_date
    )
    drawdown = None
    if current is not None:
        high = max(point.close for point in points if point.as_of <= market_date)
        drawdown = current.close / high - Decimal("1")
    missing = tuple(
        name
        for name, value in (
            ("taiex_close", current),
            ("pe_percentile", pe_percentile),
            ("eps_yoy", eps_yoy),
            ("rv20", rv20),
            ("taiwan_vix", vix),
        )
        if value is None
    )
    snapshot_ids = tuple(
        record.snapshot_id for record in (*taiex_records, *vix_records, *valuation_records)
    )
    diagnostics = {
        "taiex_points": str(len(points)),
        "market_date": market_date.isoformat(),
        "decision_at": cutoff.isoformat(),
        "historical_publication_evidence": "not_verified",
        "valuation_snapshot": valuation_id or "missing",
        "valuation_points": str(valuation_points),
        "valuation_required_points": "120",
        "vix_source": vix_records[0].source if vix_records else "missing",
    }
    # Keep the raw inputs visible to the operator.  The dashboard must show
    # what the policy actually consumed, rather than only the final target.
    if pe is not None:
        diagnostics["pe_ttm"] = str(pe)
    if pe_percentile is not None:
        diagnostics["pe_percentile"] = str(pe_percentile)
    if eps_yoy is not None:
        diagnostics["eps_yoy"] = str(eps_yoy)
    if rv20 is not None:
        diagnostics["rv20"] = str(rv20)
    if vix is not None:
        diagnostics["taiwan_vix"] = str(vix)
    if drawdown is not None:
        diagnostics["taiex_drawdown"] = str(drawdown)
    if missing:
        diagnostics["missing_inputs"] = ",".join(missing)
    if missing:
        return StrategyPipelineResult(None, snapshot_ids, missing, diagnostics)
    assert current is not None and pe is not None and pe_percentile is not None
    assert eps_yoy is not None and rv20 is not None and vix is not None
    return StrategyPipelineResult(
        StrategyInputs(
            as_of=market_date,
            pe_ttm=pe,
            pe_percentile=pe_percentile,
            eps_yoy=eps_yoy,
            rv20=rv20,
            taiwan_vix=vix,
            taiex_daily_return=current.daily_return,
            taiex_drawdown=drawdown,
            critical_data_safe=True,
        ),
        snapshot_ids,
        (),
        diagnostics,
    )
