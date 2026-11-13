"""Pinned, read-only research evidence; never grants trading eligibility."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dynamic_leverage.services.benchmark_comparison import compare_stages

REFERENCE = Path(__file__).parents[1] / "reference"


def _content_sha256(path: Path) -> str:
    """Digest of LF-normalized bytes, so git's per-platform line endings do not matter."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def research_evidence(selected_version: str, *, root: Path = REFERENCE) -> dict:
    try:
        registry = json.loads((root / "research-registry.json").read_bytes())
        for name, digest in registry["evidence_files"].items():
            if Path(name).name != name:
                raise ValueError("invalid evidence path")
            if _content_sha256(root / name) != digest:
                raise ValueError("research evidence fingerprint mismatch")
        for record in registry["records"]:
            identity = json.dumps(record["identity"], sort_keys=True).encode()
            if hashlib.sha256(identity).hexdigest() != record["identity_sha256"]:
                raise ValueError("research identity fingerprint mismatch")
        return {
            "evidence_status": "verified-archive",
            "research_date": registry["research_date"],
            "selected_strategy_version": selected_version,
            # Runtime currently has no immutable config/dataset promotion identity.
            # Name matches alone must never confer research qualification.
            "selected_status": "unverified",
            "selected_detail": "日常版本尚無完整匹配的參數／資料研究資格；資料可算不代表獲利。",
            "profitability_demonstrated": False,
            "records": registry["records"],
            "comparison": compare_stages(json.loads((root / "rearm-results.json").read_bytes())),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {
            "evidence_status": "invalid-or-missing",
            "selected_strategy_version": selected_version,
            "selected_status": "unverified",
            "selected_detail": "研究檔案遺失或指紋不符，不能沿用舊資格。",
            "profitability_demonstrated": False,
            "records": [],
        }
