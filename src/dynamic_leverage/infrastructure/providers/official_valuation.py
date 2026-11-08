from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Set
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot, HttpSnapshotClient

# Both feeds are published by the TWSE/MOPS stack.  The market report URL is
# configurable because TWSE occasionally changes the report's download route.
TWSE_MARKET_VALUATION_URL = "https://www.twse.com.tw/zh/trading/statistics/index04.html"
MOPS_MARKET_EPS_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap14_L"
TWSE_MONTHLY_REPORT_INDEX = "https://www.twse.com.tw/rwd/zh/statistics/download"
TWSE_TAIEX_INDEX_URL = "https://openapi.twse.com.tw/v1/indicesReport/MI_INDEX"
TWSE_ARCHIVE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/zip,*/*",
    "Referer": "https://www.twse.com.tw/zh/trading/statistics/index04.html",
}
TWSE_API_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"}


@dataclass(frozen=True, slots=True)
class OfficialValuationSnapshot:
    valuation: HttpSnapshot
    eps: HttpSnapshot
    index: HttpSnapshot
    rows: tuple[dict[str, str], ...]
    as_of: date
    eps_methodology: str
    eps_error: str | None = None

    @property
    def checksum(self) -> str:
        return hashlib.sha256(
            f"{self.valuation.checksum}:{self.eps.checksum}:{self.index.checksum}".encode("ascii")
        ).hexdigest()

    @property
    def observed_at(self) -> datetime:
        return datetime.combine(
            self.as_of,
            datetime.min.time(),
            tzinfo=self.valuation.received_at.tzinfo,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "official-taiex-valuation-v1",
            "methodology": "TWSE monthly market P/E + MOPS EPS validation / TAIEX-derived fallback",
            "eps_methodology": self.eps_methodology,
            "eps_error": self.eps_error,
            "rows": list(self.rows),
            "sources": {
                "twse_market_report": {
                    "url": self.valuation.source,
                    "checksum": self.valuation.checksum,
                    "observed_at": self.valuation.received_at.isoformat(),
                },
                "mops_eps": {
                    "url": self.eps.source,
                    "checksum": self.eps.checksum,
                    "observed_at": self.eps.received_at.isoformat(),
                },
                "taiex_index": {
                    "url": self.index.source,
                    "checksum": self.index.checksum,
                    "observed_at": self.index.received_at.isoformat(),
                },
            },
        }


def _number(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("−", "-")
    if not text or text in {"-", "--", "N/A", "無"}:
        return None
    try:
        number = Decimal(text.rstrip("%倍"))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def _date(value: Any) -> date | None:
    text = str(value or "").strip()
    # TWSE OpenAPI commonly returns compact ROC dates such as 1150831.
    compact = re.fullmatch(r"(\d{3})(\d{2})(\d{2})", text)
    if compact:
        try:
            return date(int(compact.group(1)) + 1911, int(compact.group(2)), int(compact.group(3)))
        except ValueError:
            return None
    match = re.search(r"(?<!\d)(\d{4})[-/]?(\d{1,2})[-/]?(\d{1,2})?(?!\d)", text)
    if not match:
        match = re.search(r"(?<!\d)(\d{3})[年/-]?(\d{1,2})[月/-]?(\d{1,2})?(?!\d)", text)
    if not match:
        return None
    year = int(match.group(1))
    if year < 1911:
        year += 1911
    month = int(match.group(2))
    day = int(match.group(3) or 1)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _find_metric(row: dict[str, Any], names: tuple[str, ...]) -> Decimal | None:
    for key, value in row.items():
        key_text = str(key).lower()
        if any(name.lower() in key_text for name in names):
            result = _number(value)
            if result is not None:
                return result
    return None


def _has_market_label(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text).lower()
    return any(token.lower() in normalized for token in ("大盤", "加權", "市場", "taiex"))


class _TableParser(HTMLParser):
    """Small dependency-free parser for TWSE HTML table exports."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def parse_market_valuation(body: bytes, content_type: str) -> tuple[date, Decimal]:
    """Extract the explicit market/TAIEX P/E row from a TWSE report.

    We intentionally reject a report that only contains individual stocks.  A
    simple average of stock P/Es is not the TWSE market P/E and would create a
    false strategy signal.
    """
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            workbook_name = next(
                (name for name in archive.namelist() if name.lower().endswith(".xls")),
                None,
            )
            if workbook_name is None:
                raise ValueError("TWSE market report ZIP has no XLS workbook")
            try:
                import xlrd

                workbook = xlrd.open_workbook(file_contents=archive.read(workbook_name))
            except ImportError as exc:  # pragma: no cover - dependency is in the image
                raise RuntimeError("xlrd is required to parse TWSE monthly reports") from exc
            report_date: date | None = None
            for sheet in workbook.sheets():
                for values in sheet.get_rows():
                    cells = [str(cell.value).strip() for cell in values]
                    for cell in values:
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            report_date = xlrd.xldate_as_datetime(
                                cell.value, workbook.datemode
                            ).date()
                    line = " ".join(cells)
                    if not _has_market_label(line):
                        continue
                    pe = next((_number(cell) for cell in cells if _number(cell)), None)
                    as_of = next((_date(cell) for cell in cells if _date(cell)), None)
                    if pe is not None:
                        # Monthly reports represent the period as YYYYMM; use
                        # the first day because the effective date is month-level.
                        return as_of or report_date or date.today().replace(day=1), pe
        raise ValueError("TWSE market report XLS has no explicit 大盤/TAIEX P/E row")

    text = body.decode("utf-8-sig", errors="replace")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if decoded is not None:
        # Some TWSE responses use {fields: [...], data: [[...], ...]}.
        if isinstance(decoded, dict) and isinstance(decoded.get("fields"), list):
            fields = [str(field) for field in decoded["fields"]]
            for values in decoded.get("data", []):
                if isinstance(values, list):
                    decoded.setdefault("_rows", []).append(dict(zip(fields, values, strict=False)))
        for row in _walk(decoded):
            label = " ".join(str(v) for v in row.values())
            if not _has_market_label(label):
                continue
            pe = _find_metric(row, ("本益比", "pe", "per"))
            as_of = next((_date(v) for v in row.values() if _date(v)), None)
            if pe is not None and as_of is not None:
                return as_of, pe
    # CSV/HTML exports often contain a simple row with a market label and a
    # P/E column.  Keep the parser dependency-free for the scheduler image.
    if "html" in content_type.lower() or "<table" in text.lower():
        table_parser = _TableParser()
        table_parser.feed(text)
        rows = table_parser.rows
    else:
        rows = csv.reader(io.StringIO(text)) if "csv" in content_type.lower() else []
    for cells in rows:
        line = " ".join(cells)
        if not _has_market_label(line):
            continue
        numbers = [_number(cell) for cell in cells]
        numbers = [number for number in numbers if number is not None]
        as_of = _date(line)
        if as_of is not None and numbers:
            # The report's market P/E is the first positive metric after the
            # label in the published table layout.
            return as_of, numbers[0]
    raise ValueError("TWSE market report has no explicit 大盤/TAIEX P/E row")


def parse_market_eps(body: bytes, content_type: str) -> tuple[date, Decimal]:
    """Extract an explicit aggregate EPS row from the MOPS EPS feed."""
    text = body.decode("utf-8-sig", errors="replace")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    candidates = list(_walk(decoded)) if decoded is not None else []
    if "csv" in content_type.lower() or not candidates:
        candidates = list(csv.DictReader(io.StringIO(text)))
    for row in candidates:
        label = " ".join(str(v) for v in row.values())
        if not any(token in label for token in ("大盤", "全體", "合計", "上市公司")):
            continue
        eps = _find_metric(row, ("基本每股盈餘", "market_eps", "eps"))
        as_of = _date(row.get("日期") or row.get("date"))
        if as_of is None:
            as_of = _date(decoded.get("date")) if isinstance(decoded, dict) else None
        if as_of is None:
            as_of = next((_date(v) for v in row.values() if _date(v)), None)
        if eps is not None and as_of is not None:
            return as_of, eps
    raise ValueError("MOPS EPS feed has no explicit aggregate/全體 EPS row")


def parse_taiex_index(body: bytes, content_type: str) -> tuple[date, Decimal]:
    """Read the official TAIEX close used only for the transparent EPS derivation."""
    try:
        decoded = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("TAIEX index response is not JSON") from exc
    rows: list[dict[str, Any]] = []
    if isinstance(decoded, dict) and isinstance(decoded.get("tables"), list):
        for table in decoded["tables"]:
            if not isinstance(table, dict) or not isinstance(table.get("fields"), list):
                continue
            fields = [str(field) for field in table["fields"]]
            for values in table.get("data", []):
                if isinstance(values, list):
                    rows.append(dict(zip(fields, values, strict=False)))
    rows.extend(_walk(decoded))
    for row in rows:
        label = " ".join(str(v) for v in row.values())
        if "發行量加權股價指數" not in label and "TAIEX" not in label:
            continue
        close = _find_metric(row, ("收盤指數", "close", "index"))
        as_of = _date(row.get("日期") or row.get("date"))
        if as_of is None and isinstance(decoded, dict):
            as_of = _date(decoded.get("date"))
        if as_of is None:
            as_of = next((_date(v) for v in row.values() if _date(v)), None)
        if close is not None and as_of is not None:
            return as_of, close
    raise ValueError("TAIEX index response has no close")


def parse_monthly_index(body: bytes, year: int, month: int) -> list[tuple[date, Decimal]]:
    decoded = json.loads(body.decode("utf-8-sig"))
    if decoded.get("stat") != "OK":
        raise ValueError("monthly index is unavailable")
    fields = decoded.get("fields", [])
    if "日期" not in fields or "收盤指數" not in fields:
        raise ValueError("monthly index fields are missing")
    points = []
    for values in decoded.get("data", []):
        row = dict(zip(fields, values, strict=True))
        observed, close = _date(row["日期"]), _number(row["收盤指數"])
        if observed is None or close is None or (observed.year, observed.month) != (year, month):
            raise ValueError("monthly index contains an invalid value or a different month")
        points.append((observed, close))
    if not points or len({point[0] for point in points}) != len(points):
        raise ValueError("monthly index is empty or has duplicate dates")
    return sorted(points)


class OfficialValuationClient:
    def __init__(
        self,
        snapshot_client: HttpSnapshotClient | None = None,
        *,
        market_report_url: str = TWSE_MARKET_VALUATION_URL,
        market_eps_url: str = MOPS_MARKET_EPS_URL,
    ) -> None:
        self.http = snapshot_client or HttpSnapshotClient()
        self.market_report_url = market_report_url
        self.market_eps_url = market_eps_url
        self.backfill_failures: dict[str, str] = {}

    async def latest_published_month(self) -> date:
        report = await self._fetch_monthly_report()
        match = re.search(r"/(\d{4})(\d{2})_C04001\.zip", report.source)
        if match:
            return date(int(match[1]), int(match[2]), 1)
        return parse_market_valuation(report.body, report.content_type)[0].replace(day=1)

    async def historical_monthly_index(self, year: int, month: int) -> HttpSnapshot:
        path = f"/rwd/zh/TAIEX/MI_5MINS_HIST?date={year:04d}{month:02d}01&response=json"
        snapshot = await self.http.fetch(f"https://www.twse.com.tw{path}", headers=TWSE_API_HEADERS)
        parse_monthly_index(snapshot.body, year, month)
        return snapshot

    async def snapshot(self) -> OfficialValuationSnapshot:
        if self.market_report_url.endswith("index04.html"):
            month = await self.latest_published_month()
            snapshots = await self.backfill_months(months=1, end=month)
            if not snapshots:
                raise ValueError(f"latest monthly valuation unavailable: {self.backfill_failures}")
            return snapshots[0]
        valuation, eps, index, eps_error = await self._fetch_pair()
        valuation_date, pe = parse_market_valuation(valuation.body, valuation.content_type)
        try:
            eps_date, market_eps = parse_market_eps(eps.body, eps.content_type)
            eps_methodology = "mops-explicit-aggregate"
            as_of = min(valuation_date, eps_date)
        except ValueError:
            index_date, index_value = parse_taiex_index(index.body, index.content_type)
            if (valuation_date.year, valuation_date.month) != (index_date.year, index_date.month):
                raise ValueError("cannot derive EPS from different observation months") from None
            market_eps = index_value / pe
            eps_methodology = "derived-taiex-close-divided-by-twse-market-pe"
            as_of = min(valuation_date, index_date)
            eps_date = index_date
        return OfficialValuationSnapshot(
            valuation=valuation,
            eps=eps,
            index=index,
            rows=(
                {
                    "as_of": as_of.isoformat(),
                    "pe_ttm": str(pe),
                    "market_eps": str(market_eps),
                    "source": "twse-monthly-mops",
                    "source_version": f"{valuation_date.isoformat()}:{eps_date.isoformat()}",
                },
            ),
            as_of=as_of,
            eps_methodology=eps_methodology,
            eps_error=eps_error,
        )

    async def _fetch_pair(
        self,
    ) -> tuple[HttpSnapshot, HttpSnapshot, HttpSnapshot, str | None]:
        # Kept as a method so tests can provide deterministic responses without
        # making network calls.
        valuation = await self._fetch_monthly_report()
        index = await self.http.fetch(TWSE_TAIEX_INDEX_URL)
        try:
            eps = await self.http.fetch(self.market_eps_url)
            eps_error = None
        except Exception as exc:  # MOPS is optional when the transparent fallback is usable.
            eps_error = f"{type(exc).__name__}: {exc}".strip()
            eps = HttpSnapshot(
                source=self.market_eps_url,
                received_at=datetime.now(ZoneInfo("Asia/Taipei")),
                body=b"[]",
                content_type="application/json",
                checksum=hashlib.sha256(b"[]").hexdigest(),
            )
        return valuation, eps, index, eps_error

    async def _fetch_monthly_report(self) -> HttpSnapshot:
        # The human-facing page is JavaScript-rendered.  Its official backing
        # endpoint returns the current year's report index and stable ZIP URLs.
        if self.market_report_url.endswith("index04.html"):
            # Avoid the JS/index route (which intermittently returns 307/428)
            # by probing the newest six archive months directly.
            current = date.today().year * 12 + date.today().month
            for offset in range(0, 7):
                ordinal = current - offset
                year, month = divmod(ordinal - 1, 12)
                month += 1
                try:
                    report = await self.historical_monthly_report(year, month)
                    if report is None:
                        continue
                    # Some unpublished archive paths return an HTTP 200 HTML
                    # error page.  Validate the payload before accepting it so
                    # the first invalid month cannot mask a later valid report.
                    parse_market_valuation(report.body, report.content_type)
                    return report
                except ValueError:
                    continue
                except Exception:  # noqa: BLE001 - unpublished month is skipped
                    continue
            raise ValueError("TWSE monthly report archive has no recent published P/E ZIP")
        return await self.http.fetch(self.market_report_url, headers=TWSE_ARCHIVE_HEADERS)

    async def historical_monthly_report(self, year: int, month: int) -> HttpSnapshot | None:
        """Fetch one published TWSE P/E report for historical backfill."""
        if month < 1 or month > 12:
            return None
        path = f"/rwd/staticFiles/inspection/inspection/04/001/{year:04d}{month:02d}_C04001.zip"
        last_error: Exception | None = None
        for host in ("www.twse.com.tw", "wwwc.twse.com.tw"):
            try:
                return await self.http.fetch(f"https://{host}{path}", headers=TWSE_ARCHIVE_HEADERS)
            except Exception as exc:  # noqa: BLE001 - try the official mirror
                last_error = exc
        if last_error is not None:
            raise last_error
        return None

    async def historical_taiex_index(self, as_of: date) -> HttpSnapshot:
        """Fetch the official daily index response for a requested date."""
        path = f"/rwd/zh/afterTrading/MI_INDEX?date={as_of:%Y%m%d}&type=IND&response=json"
        last_error: Exception | None = None
        for host in ("www.twse.com.tw", "wwwc.twse.com.tw"):
            try:
                return await self.http.fetch(f"https://{host}{path}", headers=TWSE_API_HEADERS)
            except Exception as exc:  # noqa: BLE001 - try the official mirror
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("no TWSE historical index host configured")

    async def backfill_months(
        self,
        *,
        months: int = 121,
        end: date | None = None,
        pause_seconds: float = 1.0,
        exclude_months: Set[tuple[int, int]] | None = None,
        max_month_attempts: int | None = None,
    ) -> list[OfficialValuationSnapshot]:
        """Build historical monthly valuation snapshots for percentile/YoY inputs."""
        import asyncio

        end = end or await self.latest_published_month()
        self.backfill_failures = {}
        first_month = end.year * 12 + end.month - max(months - 1, 0)
        excluded = exclude_months or set()
        results: list[OfficialValuationSnapshot] = []
        attempted = 0
        for offset in range(max(months, 1)):
            ordinal = first_month + offset
            year, month = divmod(ordinal - 1, 12)
            month += 1
            if (year, month) in excluded:
                continue
            if max_month_attempts is not None and attempted >= max_month_attempts:
                break
            attempted += 1
            try:
                report = await self.historical_monthly_report(year, month)
            except Exception as exc:  # noqa: BLE001 - report individual month failures
                self.backfill_failures[f"{year:04d}-{month:02d}"] = type(exc).__name__
                continue
            finally:
                if pause_seconds:
                    await asyncio.sleep(pause_seconds)
            if report is None:
                self.backfill_failures[f"{year:04d}-{month:02d}"] = "report_unavailable"
                continue
            try:
                _parsed_date, pe = parse_market_valuation(report.body, report.content_type)
            except ValueError as exc:
                self.backfill_failures[f"{year:04d}-{month:02d}"] = str(exc)
                continue
            # The workbook may contain a publication date unrelated to the
            # observation period.  The archive path is the authoritative month.
            report_date = date(year, month, calendar.monthrange(year, month)[1])
            try:
                index_snapshot = await self.historical_monthly_index(year, month)
                index_date, index_value = parse_monthly_index(index_snapshot.body, year, month)[-1]
            except Exception as exc:  # noqa: BLE001 - do not substitute an earlier close on error
                self.backfill_failures[f"{year:04d}-{month:02d}"] = (
                    f"month_end_index_unavailable:{type(exc).__name__}"
                )
                continue
            finally:
                if pause_seconds:
                    await asyncio.sleep(pause_seconds)
            placeholder = HttpSnapshot(
                source=f"{self.market_eps_url}#historical-{report_date.isoformat()}",
                received_at=report.received_at,
                body=b"[]",
                content_type="application/json",
                checksum=hashlib.sha256(
                    f"historical-{report_date.isoformat()}".encode()
                ).hexdigest(),
            )
            as_of = min(report_date, index_date)
            results.append(
                OfficialValuationSnapshot(
                    valuation=report,
                    eps=placeholder,
                    index=index_snapshot,
                    rows=(
                        {
                            "as_of": as_of.isoformat(),
                            "pe_ttm": str(pe),
                            "market_eps": str(index_value / pe),
                            "source": "twse-monthly-taiex-derived",
                            "source_version": f"{report_date.isoformat()}:{index_date.isoformat()}",
                        },
                    ),
                    as_of=as_of,
                    eps_methodology="derived-taiex-close-divided-by-twse-market-pe",
                    eps_error=(
                        "MOPS historical aggregate EPS unavailable; derived from "
                        "official TAIEX close / P-E"
                    ),
                )
            )
        return results
