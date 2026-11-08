"""Evidence report: numerical coverage is separate from historical research readiness."""

from __future__ import annotations

from datetime import UTC, date, datetime

from dynamic_leverage.domain.valuation import validated_valuation_row
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services.backfill import _requested_months
from dynamic_leverage.services.research_evidence import research_evidence
from dynamic_leverage.services.strategy_pipeline import build_strategy_inputs


def audit_data(repository: AuditRepository, *, anchor: date | None = None) -> dict[str, object]:
    records = repository.raw_snapshots_for_kind(kind="valuation.taiex.manual", limit=100000)
    months: dict[tuple[int, int], dict[str, object]] = {}
    rejected = []
    for record in records:
        for row in record.payload.get("rows", []):
            value = validated_valuation_row(row)
            if value is None or value[0] > date.today():
                rejected.append(
                    {"snapshot_id": record.snapshot_id, "reason": "invalid_value_or_period"}
                )
                continue
            observed, pe, eps = value
            key = (observed.year, observed.month)
            if key not in months:
                months[key] = {
                    "month": f"{key[0]:04d}-{key[1]:02d}",
                    "as_of": observed.isoformat(),
                    "pe_ttm": str(pe),
                    "market_eps": str(eps),
                    "snapshot_id": record.snapshot_id,
                    "quality": record.quality,
                    "received_at": record.received_at.isoformat(),
                    "published_at": record.published_at.isoformat()
                    if record.published_at
                    else None,
                    "eps_methodology": record.payload.get("eps_methodology", "unspecified"),
                    "sources": record.payload.get("sources", {}),
                }
    end = anchor or (date(*max(months), 1) if months else date.today().replace(day=1))
    required = _requested_months(121, end)
    missing = required - months.keys()
    pipeline = build_strategy_inputs(repository)
    represented = [months[key] for key in sorted(months.keys() & required)]
    no_publication = [row["month"] for row in represented if row["published_at"] is None]
    issues = []
    if missing:
        issues.append("valuation_window_incomplete")
    if no_publication:
        issues.append("historical_first_publication_not_verified")
    if any(str(row["eps_methodology"]).startswith("derived") for row in represented):
        issues.append("derived_eps_requires_methodology_validation")
    evidence = research_evidence(repository.get_strategy_selection().strategy_version)
    if evidence["evidence_status"] == "verified-archive":
        issues.extend(
            [
                "offline_replay_completed_but_candidate_failed_objective",
                "historical_vix_coverage_insufficient_for_v2",
                "new_out_of_sample_evidence_insufficient",
                "instrument_history_not_integrated_into_runtime_read_model",
            ]
        )
    else:
        issues.append("research_evidence_missing_or_invalid")
    return {
        "schema": "data-acceptance-v2",
        "research_evidence": evidence,
        "checked_at": datetime.now(UTC).isoformat(),
        "anchor_month": end.strftime("%Y-%m"),
        "required_months_including_current": 121,
        "valid_months_in_window": len(represented),
        "missing_months": [f"{y:04d}-{m:02d}" for y, m in sorted(missing)],
        "numerical_history_ready": not missing,
        "monitoring_inputs_ready": pipeline.inputs is not None and not missing,
        "monitoring_missing_inputs": list(pipeline.missing),
        "monitoring_diagnostics": pipeline.diagnostics,
        "freshness_and_account_risk_gate": "check_operations_summary_separately",
        "historical_research_ready": not issues,
        "profitability_demonstrated": False,
        "research_blockers": issues,
        "months_without_publication_evidence": no_publication,
        "rejected_rows": rejected,
        "months": represented,
        "input_snapshot_ids": list(pipeline.snapshot_ids),
    }
