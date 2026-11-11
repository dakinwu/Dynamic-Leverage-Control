"""/risk/evaluate and paper intents must reach the same RiskGate verdict from server state."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dynamic_leverage.api.app import create_app
from dynamic_leverage.api.schemas import FreshnessStatusSchema
from dynamic_leverage.config import Settings
from dynamic_leverage.domain.models import PolicyState, SafetyState, StrategyDecision
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services import freshness

CONSOLE = {"base_url": "http://localhost", "headers": {"X-DVL-Client": "console"}}
STRATEGY = "constant-1.2-v1"


@pytest.fixture
def account_app(tmp_path: Path):
    from test_api import FakeAccountReader

    secret_file = tmp_path / "runtime.env"
    secret_file.write_text("SJ_API_KEY=key\nSJ_SECRET_KEY=secret\n", encoding="utf-8")
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        auto_create_schema=True,
        shioaji_mode="readonly",
        shioaji_secret_file=secret_file,
        shioaji_account_id="9A00-1234567",
    )
    return create_app(settings, account_reader_factory=lambda _path: FakeAccountReader())


@pytest.fixture
def all_fresh(monkeypatch):
    """Every freshness contract FRESH, wherever the API reads freshness from."""
    real = freshness.freshness_rows

    def fresh_rows(repository, **kwargs):
        return [
            FreshnessStatusSchema.model_validate(
                {**row.model_dump(), "status": "FRESH", "reason": "test: fresh"}
            )
            for row in real(repository, **kwargs)
        ]

    import dynamic_leverage.api.app as app_module

    monkeypatch.setattr(freshness, "freshness_rows", fresh_rows)
    if hasattr(app_module, "_freshness_rows"):
        monkeypatch.setattr(app_module, "_freshness_rows", fresh_rows)


def _panic_decision(app) -> None:
    with app.state.session_factory.begin() as session:
        AuditRepository(session).append_strategy_decision(
            StrategyDecision(
                strategy_version=STRATEGY,
                as_of=date(2026, 8, 31),
                valid=True,
                raw_target=Decimal("1.2"),
                target_exposure=Decimal("1.0"),
                safety_state=SafetyState.PANIC_LOCK,
                reasons=("PANIC_TRIGGERED",),
                next_state=PolicyState(panic_active=True),
            )
        )


def _paper(client: TestClient, requested: str) -> dict:
    response = client.post(
        "/api/v1/paper/intents",
        json={
            "strategy_version": STRATEGY,
            "current_exposure": "0.1",
            "requested_exposure": requested,
            "reason": "risk unification test",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    risk = next(item for item in payload["evidence"] if item["kind"] == "risk_decision")
    return {"approved": payload["approved"], **risk["payload"]}


def _risk(client: TestClient, **overrides) -> dict:
    body = {
        "requested_exposure": "0.5",
        "actual_exposure": "0.1",
        "account_eligible": True,
        **overrides,
    }
    response = client.post("/api/v1/risk/evaluate", json=body)
    assert response.status_code == 200
    return response.json()


def test_stale_data_is_data_unsafe_even_when_client_claims_data_safe(account_app) -> None:
    with TestClient(account_app, **CONSOLE) as client:
        assert client.post("/api/v1/account/sync").status_code == 200
        risk = _risk(client, data_safe=True)
        paper = _paper(client, "0.5")

    assert risk["approved"] is False
    assert risk["safety_state"] == "DATA_UNSAFE"
    assert paper["approved"] is False
    assert "DATA_UNSAFE" in paper["reason_codes"]


def test_client_broker_safe_cannot_stand_in_for_a_missing_ledger(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            app_env="test",
            database_url="sqlite+pysqlite:///:memory:",
            auto_create_schema=True,
        )
    )
    with TestClient(app, **CONSOLE) as client:
        risk = _risk(client, broker_safe=True, data_safe=True)

    assert risk["approved"] is False
    assert risk["safety_state"] == "BROKER_UNSAFE"


def test_server_panic_lock_blocks_increases_on_both_paths(account_app, all_fresh) -> None:
    with TestClient(account_app, **CONSOLE) as client:
        assert client.post("/api/v1/account/sync").status_code == 200
        selected = client.post(
            "/api/v1/strategies/selection",
            json={"strategy_version": STRATEGY, "reason": "panic drill"},
        )
        assert selected.status_code == 200
        _panic_decision(account_app)
        risk = _risk(client, strategy_state="NORMAL")
        paper = _paper(client, "0.5")
        reduction = _paper(client, "0.05")

    assert risk["approved"] is False
    assert risk["safety_state"] == "PANIC_LOCK"
    assert paper["approved"] is False
    assert "PANIC_LOCK" in paper["reason_codes"]
    assert reduction["approved"] is True


def test_control_fresh_data_and_normal_state_approve_on_both_paths(account_app, all_fresh) -> None:
    with TestClient(account_app, **CONSOLE) as client:
        assert client.post("/api/v1/account/sync").status_code == 200
        with account_app.state.session_factory.begin() as session:
            from dynamic_leverage.domain.models import StrategyInputs
            from dynamic_leverage.domain.strategies import ConstantExposurePolicy

            decision = ConstantExposurePolicy().evaluate(
                StrategyInputs(as_of=date(2026, 8, 31)), PolicyState()
            )
            AuditRepository(session).append_strategy_decision(decision)
        risk = _risk(client)
        paper = _paper(client, "0.5")

    assert risk["approved"] is True
    assert paper["approved"] is True
