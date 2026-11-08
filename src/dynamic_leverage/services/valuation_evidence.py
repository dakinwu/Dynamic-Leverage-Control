"""Immutable valuation response observations; never infer historical publication times."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from dynamic_leverage.infrastructure.providers.http_snapshot import HttpSnapshot
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.infrastructure.tables import RawSnapshotRecord

KIND = "valuation.source.observation"


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("valuation evidence requires timezone-aware timestamps")
    return value.astimezone(UTC)


def _identity(role: str, snapshot: HttpSnapshot) -> str:
    value = [
        KIND,
        role,
        snapshot.source,
        snapshot.checksum,
        aware(snapshot.received_at).isoformat(),
    ]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def append_observation(session: Session, snapshot: HttpSnapshot, role: str) -> str:
    """Same fetch is idempotent; a later fetch or changed bytes creates another observation."""
    if role not in {"valuation", "index", "eps"}:
        raise ValueError("unknown valuation evidence role")
    received = aware(snapshot.received_at)
    if hashlib.sha256(snapshot.body).hexdigest() != snapshot.checksum:
        raise ValueError("valuation source checksum does not match raw bytes")
    archived = datetime.now(UTC)
    payload = {
        "schema": "valuation-source-observation-v1",
        "role": role,
        "body_base64": base64.b64encode(snapshot.body).decode("ascii"),
        "body_sha256": snapshot.checksum,
        "content_type": snapshot.content_type,
        "received_at": received.isoformat(),
        "archived_at": archived.isoformat(),
        "available_at": max(received, archived).isoformat(),
        "published_at": None,
        "publication_evidence": "first_publication_unknown",
    }
    row, _ = AuditRepository(session).append_raw_snapshot(
        kind=KIND,
        source=snapshot.source,
        checksum=_identity(role, snapshot),
        observed_at=received,
        received_at=received,
        quality="authoritative",
        payload=payload,
    )
    return row.snapshot_id


def observation_inventory(session: Session, *, cutoff: datetime) -> dict:
    """Read only. Each source is independent; no inference that its value was known earlier."""
    cutoff = aware(cutoff)
    sources = {}
    rows = session.scalars(select(RawSnapshotRecord).where(RawSnapshotRecord.kind == KIND)).all()
    eligible = []
    for row in rows:
        p = row.payload
        received = aware(datetime.fromisoformat(p["received_at"]))
        archived = aware(datetime.fromisoformat(p["archived_at"]))
        available = aware(datetime.fromisoformat(p["available_at"]))
        if available != max(received, archived):
            raise ValueError("valuation availability timestamp mismatch")
        if available > cutoff:
            continue
        body = base64.b64decode(p["body_base64"], validate=True)
        snap = HttpSnapshot(row.source, received, body, p["content_type"], p["body_sha256"])
        if (
            p["schema"] != "valuation-source-observation-v1"
            or hashlib.sha256(body).hexdigest() != p["body_sha256"]
            or _identity(p["role"], snap) != row.checksum
            or row.published_at is not None
            or p["published_at"] is not None
        ):
            raise ValueError("valuation observation integrity failure")
        eligible.append((available, row.snapshot_id, row))
    for available, _, row in sorted(eligible):
        p = row.payload
        key = (row.source, p["role"])
        entry = sources.setdefault(
            key,
            {
                "source": row.source,
                "role": p["role"],
                "versions": {},
                "observations": 0,
                "published_at": None,
            },
        )
        if (
            entry.get("latest_available_at") == available.isoformat()
            and entry.get("latest_body_sha256") != p["body_sha256"]
        ):
            raise ValueError("ambiguous valuation versions at the same availability timestamp")
        entry["observations"] += 1
        version = entry["versions"].setdefault(
            p["body_sha256"],
            {
                "body_sha256": p["body_sha256"],
                "first_available_at": available.isoformat(),
                "first_received_at": p["received_at"],
                "observations": 0,
            },
        )
        version["observations"] += 1
        version["last_available_at"] = available.isoformat()
        entry["latest_body_sha256"] = p["body_sha256"]
        entry["latest_observation_id"] = row.snapshot_id
        entry["latest_available_at"] = available.isoformat()
    result = []
    for _, entry in sorted(sources.items()):
        entry["versions"] = list(entry["versions"].values())
        entry["content_changed"] = len(entry["versions"]) > 1
        result.append(entry)
    return {
        "schema": "valuation-evidence-inventory-v1",
        "cutoff": cutoff.isoformat(),
        "observation_count": len(eligible),
        "sources": result,
        "historical_first_publication_proven": False,
        "historical_research_ready": False,
        "availability_definition": "max(response received time, evidence archived time)",
        "limitations": [
            "Only observed versions are retained; changes between polls may be missed.",
            "Legacy parsed snapshots cannot recover discarded raw bodies or earlier availability.",
            "Identical source bytes do not establish EPS methodology or strategy profitability.",
        ],
    }
