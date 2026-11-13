"""A manual /strategies/evaluate run is a what-if; it must not become official state."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from dynamic_leverage.api.app import create_app
from dynamic_leverage.config import Settings
from dynamic_leverage.domain.models import PolicyState, SafetyState, StrategyDecision
from dynamic_leverage.infrastructure.repository import AuditRepository

CONSOLE = {"base_url": "http://localhost", "headers": {"X-DVL-Client": "console"}}
VERSION = "dvl-tw-v2-shadow"
DAY = date(2026, 11, 9)


def _app(tmp_path):
    return create_app(
        Settings(
            app_env="test",
            database_url=f"sqlite+pysqlite:///{(tmp_path / 'manual.db').as_posix()}",
            auto_create_schema=True,
        )
    )


def _official(app, *, target: str, state_before: PolicyState) -> None:
    """What the scheduler records: a decision that advances the policy state."""
    with app.state.session_factory.begin() as session:
        AuditRepository(session).append_strategy_decision(
            StrategyDecision(
                strategy_version=VERSION,
                as_of=DAY,
                valid=True,
                raw_target=Decimal(target),
                target_exposure=Decimal(target),
                safety_state=SafetyState.NORMAL,
                reasons=("SCHEDULED",),
                next_state=PolicyState(previous_target=Decimal(target), increase_streak=1),
            ),
            state_before=state_before,
            advance_policy_state=True,
        )


def _manual(client: TestClient) -> None:
    response = client.post(
        "/api/v1/strategies/evaluate",
        json={
            "strategy_version": VERSION,
            "as_of": DAY.isoformat(),
            "pe_percentile": "0.1",
            "eps_yoy": "0.1",
            "rv20": "0.10",
            "taiwan_vix": "12",
        },
    )
    assert response.status_code == 200


def test_manual_evaluation_does_not_replace_the_dashboard_target(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, **CONSOLE) as client:
        _official(app, target="1.6", state_before=PolicyState())
        _manual(client)
        summary = client.get("/api/v1/operations/summary").json()

    assert Decimal(summary["exposure"]["target_exposure"]) == Decimal("1.6")


def test_manual_evaluation_does_not_hijack_same_day_policy_state(tmp_path) -> None:
    app = _app(tmp_path)
    scheduled_before = PolicyState(previous_target=Decimal("1.2"), increase_streak=0)
    with TestClient(app, **CONSOLE) as client:
        _manual(client)  # Earliest same-day row, recorded without a state_before.
        _official(app, target="1.6", state_before=scheduled_before)
        with app.state.session_factory() as session:
            rerun_state = AuditRepository(session).policy_state_for_evaluation(VERSION, DAY)

    # A same-day scheduler rerun must start from the first official run's state.
    assert rerun_state == scheduled_before


def test_manual_evaluation_is_still_auditable_in_history(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app, **CONSOLE) as client:
        _manual(client)
        with app.state.session_factory() as session:
            rows = AuditRepository(session).strategy_decisions(VERSION)

    assert len(rows) == 1
    assert rows[0].diagnostics["trigger"] == "manual-evaluate"
