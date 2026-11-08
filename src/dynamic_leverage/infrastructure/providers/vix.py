"""Verify the market date from the official daily file, never its download time."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse


def parse_vix_close(body: bytes | str, source: str) -> tuple[date, Decimal]:
    url = urlparse(source)
    if url.scheme != "https" or url.hostname != "www.taifex.com.tw":
        raise ValueError("VIX source is not the official download")
    if url.path != "/cht/7/getVixData":
        raise ValueError("VIX source is not a dated daily file")
    names = parse_qs(url.query).get("filesname", [])
    if len(names) != 1 or not re.fullmatch(r"\d{8}", names[0]):
        raise ValueError("VIX download date missing")
    requested = datetime.strptime(names[0], "%Y%m%d").date()
    raw = body.decode("ascii", errors="replace") if isinstance(body, bytes) else body
    dates = set(re.findall(r"(?m)^\s*(\d{8})\s+", raw))
    matches = re.findall(r"(?m)^\s*(\d{8})\s+Last\s+1\s+min\s+AVG\s+(\d+(?:\.\d+)?)\s*$", raw)
    if dates != {names[0]} or len(matches) != 1 or matches[0][0] != names[0]:
        raise ValueError("VIX body date mismatch or closing average missing")
    value = Decimal(matches[0][1])
    if not value.is_finite() or not Decimal(5) <= value < Decimal(200):
        raise ValueError("VIX close outside validated range")
    return requested, value
