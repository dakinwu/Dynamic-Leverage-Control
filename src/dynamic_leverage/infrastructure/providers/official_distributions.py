"""Strict parser for the TWSE English ETF distribution table, including payment dates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class Distribution:
    symbol: str
    ex_date: date
    payment_date: date
    amount: Decimal


class _Table(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self.table is not None:
                raise ValueError("nested distribution table")
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


HEADERS = [
    "ETF Code",
    "ETF Name",
    "Ex-dividend Date",
    "Dividend Payment Date",
    "Cash Dividend (NT$/Per beneficiary unit)",
    "Year Announced",
]


def parse_distributions(body: bytes, source: str, symbol: str, start: int, end: int):
    """Empty data is only accepted inside an otherwise valid, explicitly scoped table."""
    url = urlsplit(source)
    query = parse_qs(url.query)
    if (
        url.scheme != "https"
        or url.netloc != "www.twse.com.tw"
        or url.path != "/en/ETFortune-institute/dividendList"
        or query != {"startDate": [str(start)], "endDate": [str(end)], "stkNo": [symbol]}
        or not 2005 <= start <= end
    ):
        raise ValueError("invalid official distribution query")
    parser = _Table()
    parser.feed(body.decode("utf-8-sig"))
    tables = [t for t in parser.tables if t and t[0] == HEADERS]
    if len(tables) != 1 or any(x is not None for x in (parser.table, parser.row, parser.cell)):
        raise ValueError("missing or ambiguous official distribution table")
    result = {}
    for cells in tables[0][1:]:
        if len(cells) != 6 or cells[0] != symbol:
            raise ValueError("invalid distribution row or unexpected symbol")
        ex_date = date.fromisoformat(cells[2].replace("/", "-"))
        payment = date.fromisoformat(cells[3].replace("/", "-"))
        try:
            amount = Decimal(cells[4].replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError("invalid distribution amount") from exc
        if (
            not start <= ex_date.year <= end
            or payment < ex_date
            or not amount.is_finite()
            or amount <= 0
            or ex_date in result
            or int(cells[5]) != ex_date.year
        ):
            raise ValueError("invalid or duplicate distribution event")
        result[ex_date] = Distribution(symbol, ex_date, payment, amount)
    return result
