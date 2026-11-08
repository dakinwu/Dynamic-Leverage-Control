from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

REQUIRED_COLUMNS = {"as_of", "pe_ttm", "market_eps", "source", "source_version"}


@dataclass(frozen=True, slots=True)
class ManualValuationPoint:
    as_of: date
    pe_ttm: Decimal
    market_eps: Decimal
    source: str
    source_version: str

    def to_payload(self) -> dict[str, str]:
        values = asdict(self)
        values["as_of"] = self.as_of.isoformat()
        values["pe_ttm"] = str(self.pe_ttm)
        values["market_eps"] = str(self.market_eps)
        return values


@dataclass(frozen=True, slots=True)
class ManualValuationSnapshot:
    path: Path
    received_at: datetime
    checksum: str
    points: tuple[ManualValuationPoint, ...]

    @property
    def observed_at(self) -> datetime:
        latest = max(point.as_of for point in self.points)
        return datetime.combine(latest, datetime.min.time(), tzinfo=ZoneInfo("Asia/Taipei"))

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "manual-valuation-v1",
            "rows": [point.to_payload() for point in self.points],
        }


def load_manual_valuation(path: Path) -> ManualValuationSnapshot:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("valuation CSV must be UTF-8 encoded") from exc

    reader = csv.DictReader(io.StringIO(text))
    columns = set(reader.fieldnames or [])
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise ValueError(f"missing valuation columns: {', '.join(sorted(missing))}")

    points: list[ManualValuationPoint] = []
    seen_dates: set[date] = set()
    for line_number, row in enumerate(reader, start=2):
        try:
            as_of = date.fromisoformat((row["as_of"] or "").strip())
            pe_ttm = Decimal((row["pe_ttm"] or "").strip())
            market_eps = Decimal((row["market_eps"] or "").strip())
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"invalid valuation value on CSV line {line_number}") from exc
        source = (row["source"] or "").strip()
        source_version = (row["source_version"] or "").strip()
        if pe_ttm <= 0 or market_eps <= 0:
            raise ValueError(f"valuation values must be positive on CSV line {line_number}")
        if not source or not source_version:
            raise ValueError(f"source fields are required on CSV line {line_number}")
        if as_of in seen_dates:
            raise ValueError(f"duplicate as_of date on CSV line {line_number}")
        seen_dates.add(as_of)
        points.append(
            ManualValuationPoint(
                as_of=as_of,
                pe_ttm=pe_ttm,
                market_eps=market_eps,
                source=source,
                source_version=source_version,
            )
        )

    if not points:
        raise ValueError("valuation CSV must contain at least one row")
    if points != sorted(points, key=lambda point: point.as_of):
        raise ValueError("valuation CSV must be ordered by as_of ascending")

    return ManualValuationSnapshot(
        path=path,
        received_at=datetime.now(ZoneInfo("Asia/Taipei")),
        checksum=hashlib.sha256(raw).hexdigest(),
        points=tuple(points),
    )
