"""Writes must be committed before the response, and notifications sent only after commit."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from dynamic_leverage.api.app import create_app
from dynamic_leverage.config import Settings
from dynamic_leverage.infrastructure.brokers.shioaji import ShioajiUnavailable
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services import notifications

CONSOLE = {"base_url": "http://localhost", "headers": {"X-DVL-Client": "console"}}


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "app_env": "test",
        # A file database: a second connection sees only committed rows.
        "database_url": f"sqlite+pysqlite:///{(tmp_path / 'api.db').as_posix()}",
        "auto_create_schema": True,
        "telegram_secret_file": tmp_path / "telegram.env",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def sent(monkeypatch) -> list[str]:
    messages: list[str] = []

    def fake_send(_path, message, **_kwargs) -> bool:
        messages.append(message)
        return True

    monkeypatch.setattr(notifications, "send_telegram_message", fake_send)
    return messages


def _fail_commits(app) -> None:
    real_factory = app.state.session_factory

    def failing_factory():
        session = real_factory()

        def commit() -> None:
            raise OperationalError("COMMIT", {}, Exception("database is locked"))

        session.commit = commit
        return session

    app.state.session_factory = failing_factory


def test_commit_failure_returns_5xx_and_sends_no_notification(tmp_path, sent) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app, raise_server_exceptions=False, **CONSOLE) as client:
        _fail_commits(app)
        response = client.post(
            "/api/v1/risk/manual-halt",
            json={"actor": "ops", "reason": "commit failure drill"},
        )

    assert response.status_code >= 500
    assert sent == []


def test_notification_is_sent_only_after_the_state_is_committed(tmp_path, monkeypatch) -> None:
    app = create_app(_settings(tmp_path))
    committed_when_sent: list[bool] = []

    def fake_send(_path, message, **_kwargs) -> bool:
        with app.state.session_factory() as reader:
            committed_when_sent.append(AuditRepository(reader).get_manual_halt().active)
        return True

    monkeypatch.setattr(notifications, "send_telegram_message", fake_send)
    with TestClient(app, **CONSOLE) as client:
        response = client.post(
            "/api/v1/risk/manual-halt",
            json={"actor": "ops", "reason": "volatility review"},
        )
        assert response.status_code == 200
        with app.state.session_factory() as reader:
            assert AuditRepository(reader).get_manual_halt().active is True

    assert committed_when_sent == [True]


def test_unchanged_risk_gate_state_is_notified_once(tmp_path, sent) -> None:
    app = create_app(_settings(tmp_path))
    body = {"requested_exposure": "1.2", "actual_exposure": "1.0"}
    with TestClient(app, **CONSOLE) as client:
        assert client.post("/api/v1/risk/evaluate", json=body).status_code == 200
        assert client.post("/api/v1/risk/evaluate", json=body).status_code == 200

    assert len(sent) == 1
    assert "DVL Risk Gate" in sent[0]


def test_account_sync_failure_is_still_notified(tmp_path, sent) -> None:
    secret_file = tmp_path / "runtime.env"
    secret_file.write_text("SJ_API_KEY=key\nSJ_SECRET_KEY=secret\n", encoding="utf-8")

    class BrokenReader:
        def read_account(self, binding: str):
            raise ShioajiUnavailable("gateway down")

    app = create_app(
        _settings(
            tmp_path,
            shioaji_mode="readonly",
            shioaji_secret_file=secret_file,
            shioaji_account_id="9A00-1234567",
        ),
        account_reader_factory=lambda _path: BrokenReader(),
    )
    with TestClient(app, **CONSOLE) as client:
        response = client.post("/api/v1/account/sync")

    assert response.status_code == 503
    assert response.json()["detail"] == "read-only broker account sync is unavailable"
    assert len(sent) == 1
    assert "唯讀帳戶同步失敗" in sent[0]
