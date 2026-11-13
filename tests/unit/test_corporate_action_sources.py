"""Split facts come from a reviewed registry; dividend facts from captured TWSE tables."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services.corporate_actions import (
    DISTRIBUTION_KIND,
    distribution_actions,
    distribution_url,
    load_split_registry,
)

HEADER = (
    "<tr><th>ETF Code</th><th>ETF Name</th><th>Ex-dividend Date</th>"
    "<th>Dividend Payment Date</th><th>Cash Dividend (NT$/Per beneficiary unit)</th>"
    "<th>Year Announced</th></tr>"
)


def _table(rows: list[tuple[str, str, str, str]]) -> bytes:
    body = "".join(
        f"<tr><td>{s}</td><td>Fund</td><td>{ex}</td><td>{pay}</td><td>{amount}</td>"
        f"<td>{ex[:4]}</td></tr>"
        for s, ex, pay, amount in rows
    )
    return f"<html><body><table>{HEADER}{body}</table></body></html>".encode()


def test_shipped_registry_contains_the_three_reviewed_splits() -> None:
    splits = {split.symbol: split for split in load_split_registry()}

    assert {symbol: split.ratio for symbol, split in splits.items()} == {
        "0050": 4,
        "00631L": 22,
        "00685L": 24,
    }
    assert splits["0050"].resume_date == date(2025, 6, 18)


def _registry(directory: Path, body: bytes, digest: str) -> None:
    (directory / "corporate-actions.json").write_bytes(body)
    (directory / "corporate-actions-provenance.json").write_text(
        json.dumps({"file": "corporate-actions.json", "sha256": digest}), encoding="utf-8"
    )


def test_registry_edited_without_updating_provenance_is_rejected(tmp_path: Path) -> None:
    body = json.dumps({"splits": []}).encode()
    _registry(tmp_path, body, "0" * 64)

    with pytest.raises(ValueError, match="sha256"):
        load_split_registry(tmp_path)


def test_registry_hash_ignores_windows_line_endings(tmp_path: Path) -> None:
    lf = b'{\n  "splits": []\n}\n'
    _registry(tmp_path, lf.replace(b"\n", b"\r\n"), hashlib.sha256(lf).hexdigest())

    assert load_split_registry(tmp_path) == ()


@pytest.fixture
def repo():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with make_session_factory(engine).begin() as session:
        yield AuditRepository(session)
    engine.dispose()


def _capture(repo, symbol: str, body: bytes, *, year: int = 2026, checksum: str = "c") -> None:
    repo.append_raw_snapshot(
        kind=DISTRIBUTION_KIND,
        source=distribution_url(symbol, year),
        observed_at=datetime(2026, 11, 11, 1, 0, tzinfo=UTC),
        received_at=datetime(2026, 11, 11, 1, 0, tzinfo=UTC),
        quality="authoritative",
        checksum=checksum,
        payload={"body_text": body.decode()},
    )


def test_distribution_actions_come_from_the_latest_capture_per_symbol(repo) -> None:
    _capture(repo, "0050", _table([("0050", "2026/07/21", "2026/08/10", "0.6")]))
    _capture(repo, "00631L", _table([]), checksum="empty")

    actions, problems = distribution_actions(repo, ["0050", "00631L"], today=date(2026, 11, 11))

    assert problems == []
    assert [(a.action_id, a.amount_per_unit) for a in actions] == [
        ("0050:DIVIDEND:2026-07-21", Decimal("0.6"))
    ]


def test_missing_or_unparseable_capture_is_reported_not_treated_as_zero(repo) -> None:
    _capture(repo, "0050", b"<html>maintenance</html>")

    actions, problems = distribution_actions(repo, ["0050", "00685L"], today=date(2026, 11, 11))

    assert actions == []
    assert sorted(problem.split(":")[0] for problem in problems) == ["0050", "00685L"]
