"""Lossless frozen replay inputs and append-only experiment artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from dynamic_leverage.domain.models import SafetyState, StrategyInputs
from dynamic_leverage.services.account_replay import Quote, ReplayDay


def encode_exact(value):
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    raise TypeError(type(value).__name__)


def decode_exact(value):
    if set(value) == {"$decimal"}:
        return Decimal(value["$decimal"])
    if set(value) == {"$date"}:
        return date.fromisoformat(value["$date"])
    return value


def read_dataset(path: Path, expected_sha256: str):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("frozen dataset fingerprint mismatch")
    payload = json.loads(raw, object_hook=decode_exact)
    rows = []
    for item in payload["rows"]:
        item["target"] = Decimal(str(item["target"]))
        item["quotes"] = {k: Quote(**v) for k, v in item["quotes"].items()}
        item["strategy_state"] = SafetyState(item["strategy_state"])
        item["policy_inputs"] = StrategyInputs(**item["policy_inputs"])
        rows.append(ReplayDay(**item))
    return rows, payload


def write_csv(path: Path, rows: list[dict]):
    with path.open("x", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def json_report(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value).__name__)
