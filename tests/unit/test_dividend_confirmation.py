"""The operator confirms what a dividend payment actually delivered."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from test_ledger_corporate_actions import DIVIDEND_ID, Reader, _capture_distribution, _snapshot

from dynamic_leverage.api.app import create_app
from dynamic_leverage.config import Settings
from dynamic_leverage.services.account_sync import sync_account_snapshot

CONSOLE = {"base_url": "http://localhost", "headers": {"X-DVL-Client": "console"}}
URL = f"/api/v1/strategy-ledger/dividends/{DIVIDEND_ID}/confirm"


def _paid_dividend_app(tmp_path: Path):
    app = create_app(
        Settings(
            app_env="test",
            database_url=f"sqlite+pysqlite:///{(tmp_path / 'div.db').as_posix()}",
            auto_create_schema=True,
        )
    )
    return app


def _seed(app) -> None:
    factory = app.state.session_factory
    _capture_distribution(factory, "0.6")
    for day, price in [
        (date(2026, 7, 15), "50"),
        (date(2026, 7, 21), "49.4"),
        (date(2026, 8, 10), "50"),
    ]:
        with factory.begin() as session:
            sync_account_snapshot(
                session,
                Reader(_snapshot(day, "1000", price)),
                binding="9A00-1234567",
                capital_budget=Decimal("500000"),
            )


def test_confirmation_books_the_received_amount_once(tmp_path: Path) -> None:
    app = _paid_dividend_app(tmp_path)
    with TestClient(app, **CONSOLE) as client:
        _seed(app)
        before = client.get("/api/v1/strategy-ledger").json()
        first = client.post(URL, json={"received_amount": "590"})
        again = client.post(URL, json={"received_amount": "590.00"})
        changed = client.post(URL, json={"received_amount": "600"})
        after = client.get("/api/v1/strategy-ledger").json()

    assert before["unconfirmed_dividends"] == {DIVIDEND_ID: "600.0"}
    assert first.status_code == 200 and first.json()["created"] is True
    assert again.status_code == 200 and again.json()["created"] is False
    assert changed.status_code == 409
    assert after["unconfirmed_dividends"] == {}
    assert Decimal(after["cash"]) == Decimal(before["cash"]) - Decimal("10")


def test_unknown_dividend_cannot_be_confirmed(tmp_path: Path) -> None:
    app = _paid_dividend_app(tmp_path)
    with TestClient(app, **CONSOLE) as client:
        _seed(app)
        response = client.post(
            "/api/v1/strategy-ledger/dividends/0050:DIVIDEND:2026-01-22/confirm",
            json={"received_amount": "100"},
        )

    assert response.status_code == 409
