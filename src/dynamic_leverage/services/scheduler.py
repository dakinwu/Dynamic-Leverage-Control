from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.exc import OperationalError

from dynamic_leverage.config import Settings
from dynamic_leverage.domain.models import StrategyInputs
from dynamic_leverage.domain.strategies import DvlV2Policy
from dynamic_leverage.infrastructure.brokers.shioaji import (
    ShioajiAccountSnapshotReader,
    ShioajiUnavailable,
    load_credentials,
)
from dynamic_leverage.infrastructure.database import make_engine, make_session_factory
from dynamic_leverage.infrastructure.providers.official_indices import OfficialIndicesClient
from dynamic_leverage.infrastructure.providers.official_valuation import OfficialValuationClient
from dynamic_leverage.infrastructure.repository import AuditRepository, LedgerWriteConflict
from dynamic_leverage.services.account_sync import sync_account_snapshot
from dynamic_leverage.services.backfill import run_official_backfill
from dynamic_leverage.services.calendar import (
    account_sync_due,
    next_account_sync,
    next_close_run,
    next_session_open,
    schedule_slot,
)
from dynamic_leverage.services.corporate_actions import DISTRIBUTION_KIND
from dynamic_leverage.services.ingestion import (
    ingest_official_http_snapshot,
    ingest_official_valuation_snapshot,
    ingest_twse_snapshot,
)
from dynamic_leverage.services.notifications import TelegramNotifier
from dynamic_leverage.services.operations_projection import (
    dispatch_notifications,
    reconcile_operations,
)
from dynamic_leverage.services.strategy_pipeline import build_strategy_inputs

DEFAULT_CODES = {"0050", "00631L", "00685L"}
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def _now() -> datetime:
    """Cycle clock (a seam for tests)."""
    return datetime.now(TAIPEI_TZ)


def is_retryable_scheduler_failure(exc: Exception) -> bool:
    """Return whether the current schedule slot should be retried after this failure."""
    return isinstance(
        exc,
        httpx.TransportError
        | TimeoutError
        | OSError
        | ShioajiUnavailable
        | OperationalError
        | LedgerWriteConflict,
    )


def record_shadow_decision(repository: AuditRepository, run_id: str):
    """Evaluate the shadow policy from stored evidence and persist the decision.

    Invalid inputs still persist an invalid run so the dashboard cannot show an
    older target as current; only valid decisions advance the policy state.
    """
    processed = build_strategy_inputs(repository)
    policy_as_of = (
        processed.inputs.as_of
        if processed.inputs is not None
        else datetime.fromisoformat(processed.diagnostics["market_date"]).date()
    )
    policy_state = repository.policy_state_for_evaluation(DvlV2Policy.version, policy_as_of)
    if processed.inputs is None:
        invalid_inputs = StrategyInputs(as_of=policy_as_of, critical_data_safe=False)
        decision = DvlV2Policy().evaluate(invalid_inputs, policy_state)
        decision = replace(
            decision,
            reasons=tuple((*decision.reasons, *processed.missing)),
            diagnostics={**processed.diagnostics, "run_id": run_id},
        )
    else:
        decision = DvlV2Policy().evaluate(processed.inputs, policy_state)
        decision = replace(
            decision,
            diagnostics={**processed.diagnostics, **decision.diagnostics, "run_id": run_id},
        )
    row = repository.append_strategy_decision(
        decision,
        input_snapshot_ids=list(processed.snapshot_ids),
        state_before=policy_state,
        advance_policy_state=True,
    )
    return row, decision, processed


def should_complete_schedule_slot(retryable_failed_jobs: list[str]) -> bool:
    return not retryable_failed_jobs


async def run_snapshot_scheduler(
    *,
    database_url: str,
    interval_seconds: int,
    once: bool = False,
    telegram_secret_file: Path | None = None,
    twse_market_valuation_url: str | None = None,
    mops_market_eps_url: str | None = None,
    shioaji_mode: str = "disabled",
    shioaji_secret_file: Path | None = None,
    shioaji_account_id: str | None = None,
    strategy_nav: Decimal | None = None,
) -> None:
    notifier = TelegramNotifier(telegram_secret_file)
    operation_settings = Settings(
        database_url=database_url,
        shioaji_mode=shioaji_mode,
        shioaji_secret_file=shioaji_secret_file,
        shioaji_account_id=shioaji_account_id,
        initial_strategy_nav=int(strategy_nav or 500000),
    )

    def update_operations():
        projection_engine = make_engine(database_url)
        try:
            factory = make_session_factory(projection_engine)
            with factory.begin() as session:
                reconcile_operations(session, operation_settings)
            dispatch_notifications(factory, telegram_secret_file)
        except Exception as exc:  # noqa: BLE001 - retry independently on next heartbeat
            try:
                with make_session_factory(projection_engine).begin() as session:
                    AuditRepository(session).upsert_automation_status(
                        automation_id="operations-projection",
                        status="DEGRADED",
                        failure_reason=f"操作投影更新失敗：{type(exc).__name__}",
                    )
            except Exception:  # noqa: BLE001 - database outage is already recorded below
                pass
            print(
                json.dumps(
                    {"event": "operations_projection_failed", "error_type": type(exc).__name__}
                ),
                flush=True,
            )
        finally:
            projection_engine.dispose()

    last_cycle_slot: str | None = None
    last_account_sync_on = None
    while True:
        update_operations()
        cycle_started = _now()
        cycle_run_id = str(uuid4())
        backfill_created = 0
        try:
            backfill_result = await run_official_backfill(
                database_url=database_url,
                months=121,
                index_days=0,
                max_month_attempts=6,
                twse_market_valuation_url=twse_market_valuation_url,
                mops_market_eps_url=mops_market_eps_url,
            )
            print(
                json.dumps(
                    {"event": "valuation_backfill_batch", **backfill_result},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            remaining = backfill_result["valuation_remaining"]
            backfill_created = int(backfill_result["valuation_created"])
            backfill_completed_at = datetime.now(TAIPEI_TZ)
            backfill_engine = make_engine(database_url)
            backfill_factory = make_session_factory(backfill_engine)
            try:
                with backfill_factory.begin() as session:
                    AuditRepository(session).upsert_automation_status(
                        automation_id="valuation-history-backfill",
                        status=(
                            "SUCCESS"
                            if remaining == 0
                            else "DEGRADED"
                            if backfill_result["valuation_failed"]
                            else "WAITING"
                        ),
                        last_started_at=cycle_started,
                        last_success_at=backfill_completed_at if remaining == 0 else None,
                        next_run_at=(
                            next_close_run(backfill_completed_at)
                            if remaining == 0
                            else backfill_completed_at + timedelta(seconds=interval_seconds)
                        ),
                        last_run_id=cycle_run_id,
                        failure_reason=(
                            None
                            if remaining == 0
                            else f"尚缺 {remaining}/121 個估值月份，等待下一批回補"
                        ),
                        metadata_payload=backfill_result,
                    )
            finally:
                backfill_engine.dispose()
        except Exception as exc:  # noqa: BLE001 - daily ingestion remains independent
            print(
                json.dumps(
                    {
                        "event": "valuation_backfill_failed",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            failure_engine = make_engine(database_url)
            try:
                with make_session_factory(failure_engine).begin() as session:
                    AuditRepository(session).upsert_automation_status(
                        automation_id="valuation-history-backfill",
                        status="DEGRADED",
                        last_started_at=cycle_started,
                        next_run_at=datetime.now(TAIPEI_TZ) + timedelta(seconds=interval_seconds),
                        last_run_id=cycle_run_id,
                        failure_reason=f"回補未完成：{type(exc).__name__}: {exc}",
                    )
            except Exception:  # noqa: BLE001 - database outage must not hide the original log
                pass
            finally:
                failure_engine.dispose()
        slot = "manual" if once else schedule_slot(cycle_started)
        if not once and (slot is None or slot == last_cycle_slot) and not backfill_created:
            next_due = min(
                next_account_sync(cycle_started),
                next_session_open(cycle_started),
                next_close_run(cycle_started),
            )
            status_engine = make_engine(database_url)
            status_factory = make_session_factory(status_engine)
            try:
                with status_factory.begin() as session:
                    AuditRepository(session).upsert_automation_status(
                        automation_id="snapshot-scheduler",
                        status="WAITING",
                        next_run_at=next_due,
                        failure_reason="等待下一個交易日排程視窗",
                        metadata_payload={"schedule": "trading-day-aware", "slot": slot},
                    )
            except Exception as exc:  # noqa: BLE001
                print(
                    json.dumps(
                        {"event": "scheduler_status_failed", "error": str(exc)},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            finally:
                status_engine.dispose()
            await asyncio.sleep(interval_seconds)
            continue
        if slot is None:
            slot = "history-refresh"
        status_engine = make_engine(database_url)
        status_factory = make_session_factory(status_engine)
        try:
            with status_factory.begin() as session:
                AuditRepository(session).upsert_automation_status(
                    automation_id="snapshot-scheduler",
                    status="RUNNING",
                    last_started_at=cycle_started,
                    next_run_at=cycle_started + timedelta(seconds=interval_seconds),
                    last_run_id=cycle_run_id,
                    metadata_payload={
                        "schedule": "trading-day-aware",
                        "next_close_run": next_close_run(cycle_started).isoformat(),
                        "next_session_open": next_session_open(cycle_started).isoformat(),
                    },
                )
        except Exception as exc:  # noqa: BLE001 - status must not stop ingestion
            print(
                json.dumps(
                    {"event": "scheduler_status_failed", "error": str(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        finally:
            status_engine.dispose()
        client = OfficialIndicesClient()
        valuation_client = OfficialValuationClient(
            market_report_url=twse_market_valuation_url
            or "https://www.twse.com.tw/zh/trading/statistics/index04.html",
            market_eps_url=mops_market_eps_url
            or "https://openapi.twse.com.tw/v1/opendata/t187ap14_L",
        )
        failed_jobs: list[str] = []
        retryable_failed_jobs: list[str] = []
        missing_inputs: tuple[str, ...] = ()
        jobs = (
            (
                "twse_quotes",
                ingest_twse_snapshot(database_url=database_url, codes=DEFAULT_CODES),
            ),
            (
                "taiex_index",
                client.taiex_snapshot(),
            ),
            (
                "taifex_vix",
                client.vix_snapshot(),
            ),
            (
                "yuanta_00631l_pcf",
                client.yuanta_00631l_pcf_snapshot(),
            ),
            (
                "capital_00685l_pcf",
                client.capital_00685l_pcf_snapshot(),
            ),
            (
                "taiex_valuation",
                valuation_client.snapshot(),
            ),
            *(
                (
                    f"twse_distributions_{code}",
                    client.twse_distributions_snapshot(code, cycle_started.year),
                )
                for code in sorted(DEFAULT_CODES)
            ),
        )
        for name, job in jobs:
            try:
                source_snapshot = await job
                if name == "twse_quotes":
                    result = source_snapshot
                elif name.startswith("twse_distributions_"):
                    result = await ingest_official_http_snapshot(
                        database_url=database_url,
                        kind=DISTRIBUTION_KIND,
                        snapshot=source_snapshot,
                    )
                elif name == "taiex_valuation":
                    result = await ingest_official_valuation_snapshot(
                        database_url=database_url,
                        snapshot=source_snapshot,
                    )
                else:
                    result = await ingest_official_http_snapshot(
                        database_url=database_url,
                        kind=f"market.{name}.official",
                        snapshot=source_snapshot,
                    )
                event = {
                    "event": f"{name}_ingested",
                    "at": datetime.now(TAIPEI_TZ).isoformat(),
                    "snapshot_id": result.snapshot_id,
                    "created": result.created,
                    "item_count": result.item_count,
                }
            except Exception as exc:  # noqa: BLE001 - worker retries next cycle
                failed_jobs.append(name)
                if is_retryable_scheduler_failure(exc):
                    retryable_failed_jobs.append(name)
                event = {
                    "event": f"{name}_failed",
                    "at": datetime.now(TAIPEI_TZ).isoformat(),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            print(json.dumps(event, ensure_ascii=False), flush=True)

        if (
            shioaji_mode == "readonly"
            and shioaji_secret_file is not None
            and shioaji_account_id
            and (account_sync_due(cycle_started, last_account_sync_on) or slot.startswith("close"))
        ):
            engine = None
            try:
                load_credentials(shioaji_secret_file)
                engine = make_engine(database_url)
                factory = make_session_factory(engine)
                with factory.begin() as session:
                    account_result = sync_account_snapshot(
                        session,
                        ShioajiAccountSnapshotReader(secret_file=shioaji_secret_file),
                        binding=shioaji_account_id,
                        capital_budget=strategy_nav,
                    )
                last_account_sync_on = cycle_started.date()
                print(
                    json.dumps(
                        {
                            "event": "account_sync_ingested",
                            "snapshot_id": account_result.snapshot_id,
                            "created": account_result.created,
                            "quality": account_result.quality,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - like every job: never stop the loop
                failed_jobs.append("account_sync")
                if is_retryable_scheduler_failure(exc):
                    retryable_failed_jobs.append("account_sync")
                print(
                    json.dumps(
                        {
                            "event": "account_sync_failed",
                            "error_type": type(exc).__name__,
                            "message": "唯讀帳戶同步失敗，請檢查憑證與帳戶綁定。",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            finally:
                if engine is not None:
                    engine.dispose()

        # Turn immutable source evidence into a shadow decision.  Missing or
        # stale fields are reported explicitly; no default value is invented.
        engine = make_engine(database_url)
        session_factory = make_session_factory(engine)
        try:
            with session_factory.begin() as session:
                repository = AuditRepository(session)
                row, decision, processed = record_shadow_decision(repository, cycle_run_id)
                if not decision.valid:
                    missing_inputs = processed.missing
                    print(
                        json.dumps(
                            {
                                "event": "strategy_evaluation_skipped",
                                "strategy": DvlV2Policy.version,
                                "reason": "missing_required_inputs",
                                "missing": processed.missing,
                                "evaluation_id": row.evaluation_id,
                                "diagnostics": processed.diagnostics,
                                "at": datetime.now(TAIPEI_TZ).isoformat(),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                else:
                    print(
                        json.dumps(
                            {
                                "event": "strategy_evaluated",
                                "strategy": decision.strategy_version,
                                "evaluation_id": row.evaluation_id,
                                "run_id": cycle_run_id,
                                "target": str(decision.target_exposure),
                                "raw_target": str(decision.raw_target),
                                "safety_state": decision.safety_state.value,
                                "reasons": decision.reasons,
                                "diagnostics": decision.diagnostics,
                                "at": datetime.now(TAIPEI_TZ).isoformat(),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
        except Exception as exc:  # noqa: BLE001 - source worker continues next cycle
            failed_jobs.append("strategy_evaluation")
            if is_retryable_scheduler_failure(exc):
                retryable_failed_jobs.append("strategy_evaluation")
            print(
                json.dumps(
                    {
                        "event": "strategy_evaluation_failed",
                        "strategy": DvlV2Policy.version,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "at": datetime.now(TAIPEI_TZ).isoformat(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        finally:
            engine.dispose()
        status_engine = make_engine(database_url)
        status_factory = make_session_factory(status_engine)
        try:
            final_status = "DEGRADED" if failed_jobs or missing_inputs else "SUCCESS"
            details: list[str] = []
            if failed_jobs:
                details.append(f"失敗工作：{', '.join(failed_jobs)}")
            if missing_inputs:
                details.append(f"資料不足：{', '.join(missing_inputs)}")
            completed_at = datetime.now(TAIPEI_TZ)
            slot_complete = should_complete_schedule_slot(retryable_failed_jobs)
            next_run_at = (
                min(
                    next_account_sync(completed_at),
                    next_session_open(completed_at),
                    next_close_run(completed_at),
                )
                if slot_complete
                else completed_at + timedelta(seconds=interval_seconds)
            )
            with status_factory.begin() as session:
                AuditRepository(session).upsert_automation_status(
                    automation_id="snapshot-scheduler",
                    status=final_status,
                    last_success_at=completed_at if final_status == "SUCCESS" else None,
                    next_run_at=next_run_at,
                    last_run_id=cycle_run_id,
                    failure_reason=("；".join(details) if details else None),
                )
            detail = "；".join(details) if details else "所有權威來源與 Shadow 評估完成。"
            notification_state = (
                f"{final_status}|{','.join(failed_jobs)}|{','.join(missing_inputs)}"
            )
            notifier.notify_state(
                "scheduler",
                notification_state,
                f"DVL Scheduler {final_status}\n{detail}\nRun {cycle_run_id}",
            )
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {"event": "scheduler_status_failed", "error": str(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        finally:
            status_engine.dispose()
        update_operations()
        if should_complete_schedule_slot(retryable_failed_jobs):
            last_cycle_slot = slot
        if once:
            return
        await asyncio.sleep(interval_seconds)
