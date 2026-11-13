from __future__ import annotations

from contextlib import suppress
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from dynamic_leverage.api.schemas import FreshnessStatusSchema
from dynamic_leverage.infrastructure.providers.vix import parse_vix_close
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services.calendar import calendar_verified, expected_close_date

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
FRESHNESS_CONTRACTS = (
    ("twse_quotes", "market.twse.daily_quotes", "TWSE", 24 * 60, True),
    ("taiex_index", "market.taiex_index.official", "TWSE", 24 * 60, True),
    ("taifex_vix", "market.taifex_vix.official", "TAIFEX", 24 * 60, True),
    (
        "taiex_valuation",
        "valuation.taiex.manual",
        "TWSE/MOPS",
        45 * 24 * 60,
        True,
    ),
    ("yuanta_00631l_pcf", "market.yuanta_00631l_pcf.official", "Yuanta", 7 * 24 * 60, True),
    ("capital_00685l_pcf", "market.capital_00685l_pcf.official", "Capital", 7 * 24 * 60, False),
    ("account_sync", "broker.account.shioaji", "Shioaji", 24 * 60, True),
)


def stale_required_contracts(rows: list[FreshnessStatusSchema]) -> tuple[str, ...]:
    """Contracts whose non-FRESH status must block exposure increases."""
    return tuple(row.contract_key for row in rows if row.required and row.status != "FRESH")


def contract_max_age_minutes(contract_key: str) -> int:
    return next(age for key, _kind, _owner, age, _req in FRESHNESS_CONTRACTS if key == contract_key)


def ensure_freshness_contracts(repository: AuditRepository) -> None:
    for key, kind, owner, max_age, required in FRESHNESS_CONTRACTS:
        repository.upsert_freshness_contract(
            contract_key=key,
            kind=kind,
            source=owner,
            owner=owner,
            max_age_minutes=max_age,
            required=required,
        )


def freshness_rows(
    repository: AuditRepository, *, now: datetime | None = None
) -> list[FreshnessStatusSchema]:
    now = now or datetime.now(TAIPEI_TZ)
    rows: list[FreshnessStatusSchema] = []
    contracts = repository.list_freshness_contracts()
    if not contracts:
        contracts = [
            SimpleNamespace(
                contract_key=k, kind=t, source=o, owner=o, max_age_minutes=m, required=r
            )
            for k, t, o, m, r in FRESHNESS_CONTRACTS
        ]
    for contract in contracts:
        snapshot = repository.latest_raw_snapshot(kind=contract.kind)
        if snapshot is None:
            rows.append(
                FreshnessStatusSchema(
                    contract_key=contract.contract_key,
                    kind=contract.kind,
                    source=contract.source,
                    owner=contract.owner,
                    status="MISSING",
                    observed_at=None,
                    age_minutes=None,
                    max_age_minutes=contract.max_age_minutes,
                    reason="尚未收到快照",
                    formula_or_rule=f"required snapshot for {contract.kind}",
                    required=getattr(contract, "required", True),
                )
            )
            continue
        observed = snapshot.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        age = max(0, int((now - observed).total_seconds() // 60))
        stale = age > contract.max_age_minutes
        date_reason = None
        actual_date = None
        expected = None
        unknown = False
        if contract.kind in {
            "market.taiex_index.official",
            "market.twse.daily_quotes",
            "market.taifex_vix.official",
        }:
            if contract.kind == "market.taiex_index.official":
                from dynamic_leverage.services.strategy_pipeline import parse_taiex_points

                points = parse_taiex_points([snapshot])
                actual_date = points[-1].as_of if points else None
            elif contract.kind == "market.taifex_vix.official":
                # Also verify legacy immutable bodies; do not rewrite their download times.
                with suppress(ValueError, TypeError):
                    actual_date, _ = parse_vix_close(
                        snapshot.payload.get("body_text", ""), getattr(snapshot, "source", "")
                    )
            else:
                with suppress(ValueError, TypeError):
                    actual_date = date.fromisoformat(snapshot.payload.get("source_trade_date", ""))
            expected = expected_close_date(now)
            unknown = (
                actual_date is None
                or not calendar_verified(expected)
                or not calendar_verified(now.astimezone(TAIPEI_TZ).date())
            )
            stale = unknown or actual_date != expected
            date_reason = f"行情日期 {actual_date or '未知'}；預期完成日 {expected}"
            if unknown:
                date_reason += "；日期或交易日曆證據不足，禁止加碼"
        if observed > now:
            # A future observation is a clock or timezone defect, never fresh evidence.
            unknown = stale = True
            date_reason = f"觀測時間 {observed.isoformat()} 晚於現在，時間證據不可信，禁止加碼"
        rows.append(
            FreshnessStatusSchema(
                contract_key=contract.contract_key,
                kind=contract.kind,
                source=contract.source,
                owner=contract.owner,
                status="UNKNOWN" if unknown else "STALE" if stale else "FRESH",
                observed_at=observed,
                age_minutes=age,
                max_age_minutes=contract.max_age_minutes,
                reason=(
                    date_reason
                    or (
                        f"超過有效期限 {contract.max_age_minutes} 分鐘" if stale else "在有效期限內"
                    )
                ),
                snapshot_id=snapshot.snapshot_id,
                required=getattr(contract, "required", True),
                effective_at=observed if not unknown else None,
                source_trade_date=actual_date,
                expected_trade_date=expected,
                received_at=getattr(snapshot, "received_at", None),
                published_at=getattr(snapshot, "published_at", None),
                formula_or_rule=(
                    "source_trade_date == expected_published_session (15:00 Asia/Taipei)"
                    if date_reason
                    else f"age_minutes <= {contract.max_age_minutes}"
                ),
            )
        )
    return rows
