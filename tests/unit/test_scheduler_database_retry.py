from sqlalchemy.exc import OperationalError

from dynamic_leverage.services.scheduler import is_retryable_scheduler_failure


def test_database_recovery_failure_keeps_schedule_slot_retryable() -> None:
    error = OperationalError(
        "connect", {}, RuntimeError("database system is not yet accepting connections")
    )
    assert is_retryable_scheduler_failure(error)
