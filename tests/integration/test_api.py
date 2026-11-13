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

CONSOLE = {"base_url": "http://localhost", "headers": {"X-DVL-Client": "console"}}


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
    return TestClient(create_app(settings), **CONSOLE)


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
    with TestClient(app, **CONSOLE) as client:
        response = client.get("/api/v1/system")

    assert response.status_code == 200
    assert response.json()["shioaji_secret_present"] is False


def test_manual_strategy_evaluation_is_audited_but_not_a_dashboard_target() -> None:
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
        history = client.get("/api/v1/strategies/constant-1.2-v1/runs")
    assert dashboard.status_code == 200
    assert dashboard.json()["strategies"] == []
    assert history.status_code == 200
    assert history.json()[0]["target_exposure"] == "1.200"


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

    with TestClient(app, **CONSOLE) as client:
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
    with TestClient(app, **CONSOLE) as client:
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


def test_paper_intent_above_cash_ceiling_is_recorded_as_blocked() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/paper/intents",
            json={
                "strategy_version": "dvl-tw-v2-shadow",
                "current_exposure": "1.0",
                "requested_exposure": "3.2",
                "reason": "v2 shadow candidate above the cash ceiling",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["approved"] is False
        assert payload["allocations"] == []
        risk = next(item for item in payload["evidence"] if item["kind"] == "risk_decision")
        assert "EXPOSURE_CEILING" in risk["payload"]["reason_codes"]
        assert risk["payload"]["requested_exposure"] == "3.2"


def test_paper_intent_rejects_exposure_above_v2_candidate_ceiling() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/paper/intents",
            json={
                "strategy_version": "dvl-tw-v2-shadow",
                "current_exposure": "1.0",
                "requested_exposure": "3.3",
                "reason": "beyond any strategy target",
            },
        )
    assert response.status_code == 422


def test_paper_intent_increase_is_blocked_without_fresh_inputs_or_strategy_run() -> None:
    # A fresh database has no verified market data and no strategy evaluation.
    with _client() as client:
        response = client.post(
            "/api/v1/paper/intents",
            json={
                "strategy_version": "constant-1.2-v1",
                "current_exposure": "1.0",
                "requested_exposure": "1.2",
                "reason": "increase on empty inputs",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["approved"] is False
    risk = next(item for item in payload["evidence"] if item["kind"] == "risk_decision")
    # The Risk Gate reports its first blocker (no reconciled broker account);
    # the stale inputs remain in the evidence. DATA_UNSAFE with a reconciled
    # account is covered in test_risk_unification.py.
    assert "BROKER_UNSAFE" in risk["payload"]["reason_codes"]
    assert "STRATEGY_UNEVALUATED" in risk["payload"]["reason_codes"]
    assert risk["payload"]["stale_required_contracts"]


def _ledger_app(tmp_path: Path):
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


LEDGER_FILL = {
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


def test_fill_replay_with_different_details_is_a_conflict(tmp_path: Path) -> None:
    with TestClient(_ledger_app(tmp_path), **CONSOLE) as client:
        assert client.post("/api/v1/account/sync").status_code == 200
        assert client.post("/api/v1/strategy-ledger/fills", json=LEDGER_FILL).status_code == 200
        changed = client.post(
            "/api/v1/strategy-ledger/fills", json={**LEDGER_FILL, "quantity": "200"}
        )
        ledger = client.get("/api/v1/strategy-ledger")

    assert changed.status_code == 409
    assert ledger.json()["position_revision"] == 1


def test_fill_insert_race_returns_the_idempotent_result(tmp_path: Path, monkeypatch) -> None:
    from dynamic_leverage.infrastructure.repository import AuditRepository

    real_lookup = AuditRepository.strategy_ledger_event
    hidden = {"calls": 0}

    def lookup_missing_during_race(self, event_key):
        # Model a concurrent writer whose insert lands between our existence
        # checks and our own insert: the first two lookups miss it.
        if event_key.startswith("FILL:") and hidden["calls"] < 2:
            hidden["calls"] += 1
            return None
        return real_lookup(self, event_key)

    with TestClient(_ledger_app(tmp_path), **CONSOLE) as client:
        assert client.post("/api/v1/account/sync").status_code == 200
        assert client.post("/api/v1/strategy-ledger/fills", json=LEDGER_FILL).status_code == 200
        monkeypatch.setattr(AuditRepository, "strategy_ledger_event", lookup_missing_during_race)
        raced = client.post("/api/v1/strategy-ledger/fills", json=LEDGER_FILL)
        monkeypatch.undo()
        ledger = client.get("/api/v1/strategy-ledger")

    assert raced.status_code == 200
    assert raced.json()["created"] is False
    assert ledger.json()["position_revision"] == 1
