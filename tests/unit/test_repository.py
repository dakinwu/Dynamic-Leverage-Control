from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from dynamic_leverage.domain.models import StrategyInputs
from dynamic_leverage.domain.strategies import DvlV2Policy
from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import AuditRepository


def test_raw_snapshot_append_is_idempotent() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime.now(UTC)

    with factory.begin() as session:
        repository = AuditRepository(session)
        first, first_created = repository.append_raw_snapshot(
            kind="market.test",
            source="https://example.test/data",
            observed_at=now,
            received_at=now,
            quality="authoritative",
            checksum="a" * 64,
            payload={"value": 1},
        )
        second, second_created = repository.append_raw_snapshot(
            kind="market.test",
            source="https://example.test/data",
            observed_at=now,
            received_at=now,
            quality="authoritative",
            checksum="a" * 64,
            payload={"value": 1},
        )

    assert first.snapshot_id == second.snapshot_id
    assert first_created is True
    assert second_created is False
    engine.dispose()


def _cheap_market(on_date: date) -> StrategyInputs:
    return StrategyInputs(
        as_of=on_date,
        pe_percentile=Decimal("0.10"),
        eps_yoy=Decimal("0.05"),
        rv20=Decimal("0.15"),
        taiwan_vix=Decimal("18"),
        taiex_drawdown=Decimal("-0.40"),
    )


def test_policy_state_advances_once_per_effective_date() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    policy = DvlV2Policy()
    first_date = date(2026, 8, 24)

    with factory.begin() as session:
        repository = AuditRepository(session)
        targets = []
        for offset in range(3):
            as_of = first_date + timedelta(days=offset)
            state = repository.policy_state_for_evaluation(policy.version, as_of)
            decision = policy.evaluate(_cheap_market(as_of), state)
            repository.append_strategy_decision(
                decision,
                state_before=state,
                advance_policy_state=True,
            )
            targets.append(decision.target_exposure)

            if offset == 0:
                rerun_state = repository.policy_state_for_evaluation(policy.version, as_of)
                rerun = policy.evaluate(_cheap_market(as_of), rerun_state)
                repository.append_strategy_decision(
                    rerun,
                    state_before=rerun_state,
                    advance_policy_state=True,
                )
                assert rerun.next_state.increase_streak == 1

        assert targets == [Decimal("1.0"), Decimal("1.0"), Decimal("1.2")]
        next_state = repository.policy_state_for_evaluation(
            policy.version, first_date + timedelta(days=3)
        )
        assert next_state.previous_target == Decimal("1.2")
        assert next_state.increase_streak == 0
    engine.dispose()
