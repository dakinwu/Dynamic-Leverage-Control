from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services.calendar import next_session_open, next_trading_day, schedule_slot
from dynamic_leverage.services.scheduler import (
    is_retryable_scheduler_failure,
    should_complete_schedule_slot,
)


def test_queue_lifecycle_reopens_after_condition_returns() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        repo = AuditRepository(session)
        repo.reconcile_queue(
            [
                {
                    "item_id": "data:missing",
                    "title": "資料缺失",
                    "detail": "尚無快照",
                    "impact": "阻擋",
                    "next_action": "同步",
                }
            ]
        )
        repo.transition_queue_item(
            "data:missing",
            target_status="ACKNOWLEDGED",
            actor="tester",
            reason="已確認",
        )
        repo.transition_queue_item(
            "data:missing",
            target_status="RESOLVED",
            actor="tester",
            reason="資料補齊",
        )
        rows = repo.reconcile_queue(
            [
                {
                    "item_id": "data:missing",
                    "title": "資料缺失",
                    "detail": "再次缺失",
                    "impact": "阻擋",
                    "next_action": "同步",
                }
            ]
        )
        assert rows[0].status == "REOPENED"
        assert rows[0].reopened_count == 1
    engine.dispose()


def test_manual_halt_is_persistent_and_strategy_selection_is_explicit() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        repo = AuditRepository(session)
        repo.set_manual_halt(active=True, actor="tester", reason="drill")
        repo.set_strategy_selection(
            strategy_version="constant-1.2-v1",
            mode="paper",
            changed_by="tester",
            reason="compare baseline",
        )
    with factory.begin() as session:
        repo = AuditRepository(session)
        assert repo.get_manual_halt().active is True
        assert repo.get_strategy_selection().strategy_version == "constant-1.2-v1"
    engine.dispose()


def test_trading_calendar_skips_weekend() -> None:
    assert next_trading_day(datetime(2026, 9, 4, tzinfo=UTC).date()).isoformat() == "2026-09-07"
    opening = next_session_open(datetime(2026, 9, 4, 16, tzinfo=UTC))
    assert opening.weekday() == 0
    assert opening.hour == 9 and opening.minute == 15
    taipei = ZoneInfo("Asia/Taipei")
    assert schedule_slot(datetime(2026, 9, 4, 8, tzinfo=taipei)) is None
    assert schedule_slot(datetime(2026, 9, 4, 9, 7, tzinfo=taipei)) == "account-sync:2026-09-04"
    assert schedule_slot(datetime(2026, 9, 4, 9, 20, tzinfo=taipei)) is not None


def test_invalid_queue_transition_is_rejected() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        repo = AuditRepository(session)
        repo.reconcile_queue(
            [{"item_id": "x", "title": "x", "detail": "x", "impact": "x", "next_action": "x"}]
        )
        with pytest.raises(ValueError, match="invalid queue transition"):
            repo.transition_queue_item("x", target_status="RESOLVED", actor="tester", reason="skip")
    engine.dispose()


def test_scheduler_retries_the_same_slot_after_a_transport_failure() -> None:
    assert is_retryable_scheduler_failure(httpx.ConnectError("temporary DNS failure")) is True
    assert is_retryable_scheduler_failure(ValueError("source has no new publication")) is False
    assert should_complete_schedule_slot(["twse_quotes"]) is False
    assert should_complete_schedule_slot([]) is True
