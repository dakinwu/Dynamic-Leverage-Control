from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import TypeAdapter

from dynamic_leverage.config import get_settings
from dynamic_leverage.domain.execution import OneXTwoXRecipe, TwoXCashRecipe
from dynamic_leverage.domain.models import PolicyState, StrategyInputs
from dynamic_leverage.domain.strategies import (
    ConstantExposurePolicy,
    DvlV2Policy,
    DvlV10Policy,
    DvlV11Policy,
)
from dynamic_leverage.infrastructure.brokers.shioaji import ShioajiReadOnlyAdapter
from dynamic_leverage.infrastructure.database import make_engine, make_session_factory
from dynamic_leverage.infrastructure.providers.manual_valuation import load_manual_valuation
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services.account_sync import mask_binding
from dynamic_leverage.services.backfill import run_official_backfill
from dynamic_leverage.services.data_acceptance import audit_data
from dynamic_leverage.services.ingestion import ingest_twse_snapshot
from dynamic_leverage.services.research_replay import (
    ResearchObservation,
    ResearchReturn,
    replay_comparison,
)
from dynamic_leverage.services.scheduler import run_snapshot_scheduler


def _evaluate_demo() -> None:
    inputs = StrategyInputs(
        as_of=date.today(),
        pe_ttm=Decimal("26"),
        pe_percentile=Decimal("0.65"),
        eps_yoy=Decimal("0.02"),
        rv20=Decimal("0.18"),
        taiwan_vix=Decimal("20"),
        taiex_daily_return=Decimal("0"),
    )
    policies = [ConstantExposurePolicy(), DvlV10Policy(), DvlV11Policy(), DvlV2Policy()]
    rows = []
    for policy in policies:
        decision = policy.evaluate(inputs, PolicyState())
        rows.append(
            {
                "strategy": decision.strategy_version,
                "valid": decision.valid,
                "target": str(decision.target_exposure),
                "state": decision.safety_state.value,
                "reasons": decision.reasons,
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def _show_recipes(target: Decimal) -> None:
    for recipe in (TwoXCashRecipe(), OneXTwoXRecipe()):
        print(recipe.version)
        for item in recipe.allocate(target):
            print(f"  {item.instrument}: weight={item.weight}, exposure={item.assumed_exposure}")


def _repository() -> tuple[object, object]:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    return engine, make_session_factory(engine)


async def _ingest_twse(codes: set[str]) -> None:
    result = await ingest_twse_snapshot(
        database_url=get_settings().database_url,
        codes=codes,
    )
    print(
        json.dumps(
            {
                "snapshot_id": result.snapshot_id,
                "created": result.created,
                "item_count": result.item_count,
            },
            indent=2,
        )
    )


def _ingest_valuation(path: Path) -> None:
    snapshot = load_manual_valuation(path)
    # File name only: an absolute path would expose the operator's account name
    # and folder layout in the Data page and in screenshots.
    source = f"manual-file:{path.name}"
    engine, session_factory = _repository()
    try:
        with session_factory.begin() as session:
            row, created = AuditRepository(session).append_raw_snapshot(
                kind="valuation.taiex.manual",
                source=source,
                observed_at=snapshot.observed_at,
                received_at=snapshot.received_at,
                quality="manual-reviewed",
                checksum=snapshot.checksum,
                payload=snapshot.to_payload(),
            )
        print(json.dumps({"snapshot_id": row.snapshot_id, "created": created}, indent=2))
    finally:
        engine.dispose()


def _readonly_secret_file() -> Path:
    """Every broker login goes through here: SHIOAJI_MODE=disabled never logs in."""
    settings = get_settings()
    if settings.shioaji_mode != "readonly":
        raise RuntimeError("set SHIOAJI_MODE=readonly to use read-only Shioaji commands")
    if settings.shioaji_secret_file is None:
        raise RuntimeError("SHIOAJI_SECRET_FILE is not configured")
    return settings.shioaji_secret_file


def _check_shioaji(codes: tuple[str, ...]) -> None:
    secret_file = _readonly_secret_file()
    adapter = ShioajiReadOnlyAdapter(secret_file=secret_file)
    try:
        adapter.connect()
        result = {
            "mode": "readonly",
            "account_count": len(adapter.accounts()),
            "contracts": adapter.contract_status(codes),
            "production_order_submission": "disabled",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        adapter.disconnect()


def _list_shioaji_accounts(index: int | None) -> None:
    adapter = ShioajiReadOnlyAdapter(secret_file=_readonly_secret_file())
    output_lines: list[str] = []
    try:
        adapter.connect()
        candidates = adapter.account_candidates()
        if index is not None:
            if index < 0 or index >= len(candidates):
                raise RuntimeError("stock account selection is out of range")
            output_lines.append(str(candidates[index]["binding"]))
        else:
            for position, candidate in enumerate(candidates):
                masked = mask_binding(str(candidate["binding"]))
                signed = "signed" if candidate["signed"] else "needs-signing"
                output_lines.append(f"{position}\t{masked}\t{signed}")
    finally:
        adapter.disconnect()
    print("\n".join(output_lines))


def main() -> None:
    parser = argparse.ArgumentParser(prog="dvl")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("evaluate-demo")
    recipe_parser = subparsers.add_parser("show-recipes")
    recipe_parser.add_argument("target", type=Decimal)
    twse_parser = subparsers.add_parser("ingest-twse")
    twse_parser.add_argument(
        "--codes",
        nargs="+",
        default=["0050", "00631L", "00685L"],
        help="TWSE instrument codes to retain in the immutable snapshot",
    )
    valuation_parser = subparsers.add_parser("ingest-valuation")
    valuation_parser.add_argument("csv_path", type=Path)
    backfill_parser = subparsers.add_parser(
        "backfill-official",
        help="回補官方月報估值與歷史 TAIEX 指數，供 percentile/EPS YoY/RV20 使用",
    )
    backfill_parser.add_argument("--months", type=int, default=121)
    backfill_parser.add_argument("--index-days", type=int, default=90)
    backfill_parser.add_argument("--max-month-attempts", type=int, default=None)
    audit_parser = subparsers.add_parser("audit-data", help="驗收數值涵蓋與歷史研究證據")
    audit_parser.add_argument("--output", type=Path)
    audit_parser.add_argument("--anchor", type=date.fromisoformat)
    research_parser = subparsers.add_parser("research-replay", help="以明確時間和來源重播研究比較")
    research_parser.add_argument("dataset", type=Path)
    research_parser.add_argument("--output", type=Path, required=True)
    shioaji_parser = subparsers.add_parser("shioaji-check")
    shioaji_parser.add_argument("--codes", nargs="+", default=["0050", "00631L", "00685L"])
    accounts_parser = subparsers.add_parser("shioaji-accounts")
    accounts_parser.add_argument("--index", type=int)
    scheduler_parser = subparsers.add_parser("scheduler")
    scheduler_parser.add_argument("--once", action="store_true")
    scheduler_parser.add_argument("--interval-seconds", type=int, default=3600)
    args = parser.parse_args()
    if args.command == "evaluate-demo":
        _evaluate_demo()
    elif args.command == "show-recipes":
        _show_recipes(args.target)
    elif args.command == "ingest-twse":
        asyncio.run(_ingest_twse(set(args.codes)))
    elif args.command == "ingest-valuation":
        _ingest_valuation(args.csv_path)
    elif args.command == "backfill-official":
        if args.months < 1 or args.months > 240:
            parser.error("--months must be between 1 and 240")
        if args.index_days < 0 or args.index_days > 400:
            parser.error("--index-days must be between 0 and 400")
        print(
            json.dumps(
                asyncio.run(
                    run_official_backfill(
                        database_url=get_settings().database_url,
                        months=args.months,
                        index_days=args.index_days,
                        max_month_attempts=args.max_month_attempts,
                        twse_market_valuation_url=get_settings().twse_market_valuation_url,
                        mops_market_eps_url=get_settings().mops_market_eps_url,
                    )
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "research-replay":
        dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
        report = replay_comparison(
            TypeAdapter(list[ResearchObservation]).validate_python(dataset["observations"]),
            TypeAdapter(list[ResearchReturn]).validate_python(dataset["returns"]),
            evaluation_start=datetime.fromisoformat(dataset["evaluation_start"]),
            frozen_at=datetime.fromisoformat(dataset["frozen_at"]),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, default=str, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(args.output)
    elif args.command == "audit-data":
        engine, factory = _repository()
        try:
            with factory() as session:
                report = audit_data(AuditRepository(session), anchor=args.anchor)
            output = json.dumps(report, ensure_ascii=False, indent=2)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output, encoding="utf-8")
            print(output)
        finally:
            engine.dispose()
    elif args.command == "shioaji-check":
        _check_shioaji(tuple(args.codes))
    elif args.command == "shioaji-accounts":
        _list_shioaji_accounts(args.index)
    elif args.command == "scheduler":
        if args.interval_seconds < 60:
            parser.error("--interval-seconds must be at least 60")
        asyncio.run(
            run_snapshot_scheduler(
                database_url=get_settings().database_url,
                interval_seconds=args.interval_seconds,
                once=args.once,
                telegram_secret_file=get_settings().telegram_secret_file,
                twse_market_valuation_url=get_settings().twse_market_valuation_url,
                mops_market_eps_url=get_settings().mops_market_eps_url,
                shioaji_mode=get_settings().shioaji_mode,
                shioaji_secret_file=get_settings().shioaji_secret_file,
                shioaji_account_id=get_settings().shioaji_account_id,
                strategy_nav=Decimal(get_settings().initial_strategy_nav),
            )
        )


if __name__ == "__main__":
    main()
