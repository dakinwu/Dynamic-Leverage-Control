from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from dynamic_leverage.api.app import create_app
from dynamic_leverage.config import Settings
from dynamic_leverage.domain.account import (
    BrokerAccountSnapshot,
    BrokerPosition,
    BrokerSettlement,
)


class FakeAccountReader:
    def read_account(self, binding: str) -> BrokerAccountSnapshot:
        assert binding == "9A00-1234567"
        return BrokerAccountSnapshot(
            observed_at=datetime(2026, 8, 31, 5, 30, tzinfo=UTC),
            cash=Decimal("80000"),
            positions=(
                BrokerPosition(
                    instrument="0050",
                    quantity=Decimal("1000"),
                    average_price=Decimal("48"),
                    last_price=Decimal("50"),
                    direction="Buy",
                    condition="Cash",
                ),
            ),
            settlements=(
                BrokerSettlement(
                    settlement_date=date(2026, 9, 2),
                    amount=Decimal("-10000"),
                    t=2,
                ),
            ),
        )


def _client() -> TestClient:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        auto_create_schema=True,
    )
    return TestClient(create_app(settings))


def test_system_status_proves_production_orders_are_disabled() -> None:
    with _client() as client:
        response = client.get("/api/v1/system")
    assert response.status_code == 200
    payload = response.json()
    assert payload["production_order_submission"] == "disabled"
    assert payload["eligibility_gate"] == "closed"


def test_system_status_survives_transient_secret_mount_io_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        auto_create_schema=True,
        shioaji_secret_file=tmp_path / "runtime.env",
    )
    app = create_app(settings)

    def raise_io_error(_path: Path) -> bool:
        raise OSError(5, "simulated bind mount I/O error")

    monkeypatch.setattr(Path, "exists", raise_io_error)
    with TestClient(app) as client:
        response = client.get("/api/v1/system")

    assert response.status_code == 200
    assert response.json()["shioaji_secret_present"] is False


def test_strategy_evaluation_is_persisted_for_dashboard() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/strategies/evaluate",
            json={
                "strategy_version": "constant-1.2-v1",
                "as_of": "2026-08-28",
                "critical_data_safe": True,
            },
        )
        assert response.status_code == 200
        dashboard = client.get("/api/v1/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["strategies"][0]["target_exposure"] == "1.200"


def test_constant_strategy_selection_materializes_immediate_target() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/strategies/selection",
            json={
                "strategy_version": "constant-2.0-v1",
                "mode": "paper",
                "reason": "compare high exposure baseline",
                "changed_by": "operator",
            },
        )
        assert response.status_code == 200
        dashboard = client.get("/api/v1/dashboard")

    assert dashboard.status_code == 200
    selected = dashboard.json()["selected_strategy"]
    assert selected["strategy_version"] == "constant-2.0-v1"
    constant = next(
        item for item in dashboard.json()["strategies"] if item["version"] == "constant-2.0-v1"
    )
    assert constant["target_exposure"] == "2.000"
    assert constant["valid"] is True


def test_operations_summary_never_presents_planning_nav_as_observed() -> None:
    with _client() as client:
        response = client.get("/api/v1/operations/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["account"]["mode"] == "planning"
    assert payload["account"]["planning_nav"] == "500000"
    assert payload["account"]["observed_nav"] is None
    assert payload["exposure"]["actual_exposure"] is None
    assert payload["exposure"]["quality"] == "unknown"


def test_operations_summary_exposes_account_binding_as_required_setup() -> None:
    with _client() as client:
        response = client.get("/api/v1/operations/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["operator_status"] == "ACTION_REQUIRED"
    account_binding = next(
        item for item in payload["activation_items"] if item["key"] == "account_binding"
    )
    assert account_binding["status"] == "required"
    assert payload["production_order_submission"] == "disabled"


def test_read_only_account_sync_becomes_observed_account_state(tmp_path: Path) -> None:
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
    app = create_app(settings, account_reader_factory=lambda _path: FakeAccountReader())

    with TestClient(app) as client:
        sync = client.post("/api/v1/account/sync")
        summary = client.get("/api/v1/operations/summary")

    assert sync.status_code == 200
    assert sync.json()["account_masked"] == "9A00-***4567"
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["account"]["mode"] == "observed"
    assert payload["account"]["observed_nav"] == "130000"
    assert payload["account"]["strategy_cash"] == "450000.00"
    assert payload["account"]["pending_settlement"] == "-10000"
    assert Decimal(payload["exposure"]["actual_exposure"]) == Decimal("0.1")
    assert payload["account"]["strategy_nav"] == "500000.00"
    assert payload["account"]["net_contributions"] == "500000.00"
    assert payload["account"]["principal_loss_amount"] == "0.00"
    assert payload["account"]["maximum_drawdown"] == "0E-8"
    assert payload["exposure"]["account_exposure"].startswith("0.384615")
    assert payload["account"]["account_masked"] == "9A00-***4567"


def test_actual_fill_is_audited_and_requires_broker_reconciliation(tmp_path: Path) -> None:
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
    app = create_app(settings, account_reader_factory=lambda _path: FakeAccountReader())

    fill = {
        "execution_id": "broker-fill-001",
        "instrument": "0050",
        "side": "BUY",
        "quantity": "100",
        "price": "50",
        "commission": "100",
        "tax": "0",
        "occurred_at": "2026-08-31T14:00:00+08:00",
        "note": "operator confirmed broker fill",
    }
    with TestClient(app) as client:
        assert client.post("/api/v1/account/sync").status_code == 200
        recorded = client.post("/api/v1/strategy-ledger/fills", json=fill)
        duplicate = client.post("/api/v1/strategy-ledger/fills", json=fill)
        ledger = client.get("/api/v1/strategy-ledger")

    assert recorded.status_code == 200
    assert recorded.json()["created"] is True
    assert recorded.json()["ledger"]["cash"] == "444900.00"
    assert recorded.json()["ledger"]["current_nav"] == "499900.00"
    assert recorded.json()["ledger"]["principal_loss_amount"] == "100.00"
    assert recorded.json()["ledger"]["reconciled"] is False
    assert duplicate.status_code == 200
    assert duplicate.json()["created"] is False
    assert ledger.json()["position_revision"] == 1


def test_risk_endpoint_rejects_production_order() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/risk/evaluate",
            json={
                "requested_exposure": "1.2",
                "actual_exposure": "1.0",
                "account_eligible": True,
                "production_order_requested": True,
            },
        )
    assert response.status_code == 200
    assert response.json()["approved"] is False
    assert "PRODUCTION_ORDER_PATH_DISABLED" in response.json()["reason_codes"]


def test_v2_strategy_is_available_and_marks_above_two_x_as_shadow() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/strategies/evaluate",
            json={
                "strategy_version": "dvl-tw-v2-shadow",
                "as_of": "2026-08-28",
                "pe_percentile": "0.10",
                "eps_yoy": "0.05",
                "rv20": "0.15",
                "taiwan_vix": "18",
                "taiex_drawdown": "-0.40",
                "state": {"previous_target": "3.2"},
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["strategy_version"] == "dvl-tw-v2-shadow"
        assert payload["raw_target"] == "3.2"
        assert "SHADOW_ONLY_ABOVE_2X" in payload["reasons"]


def test_execution_rejects_shadow_target_above_cash_ceiling() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/execution/allocate",
            json={"recipe": "2x-cash-v1", "target_exposure": "2.4"},
        )
    assert response.status_code == 409


def test_operations_state_and_manual_halt_api_are_auditable() -> None:
    with _client() as client:
        summary = client.get("/api/v1/operations/summary")
        assert summary.status_code == 200
        assert "freshness" in summary.json()
        assert "queue" in summary.json()

        enabled = client.post("/api/v1/risk/manual-halt", json={"reason": "maintenance"})
        assert enabled.status_code == 200
        assert enabled.json()["active"] is True

        blocked = client.post(
            "/api/v1/risk/evaluate",
            json={
                "requested_exposure": "1.2",
                "actual_exposure": "1.0",
                "account_eligible": True,
            },
        )
        assert blocked.json()["safety_state"] == "MANUAL_HALT"

        released = client.post("/api/v1/risk/manual-halt/release", json={"reason": "done"})
        assert released.status_code == 200
        assert released.json()["active"] is False


def test_paper_intent_creates_evidence_chain_without_broker_order() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/paper/intents",
            json={
                "strategy_version": "constant-1.2-v1",
                "current_exposure": "1.0",
                "requested_exposure": "1.2",
                "reason": "rebalance review",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["maturity"] == "PAPER"
        assert len(payload["evidence"]) == 3
        assert [item["kind"] for item in payload["evidence"]] == [
            "strategy_run",
            "risk_decision",
            "trade_intent",
        ]
        evidence = client.get(f"/api/v1/paper/evidence/{payload['run_id']}")
        assert evidence.status_code == 200
