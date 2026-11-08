"""Read-only experiment indexing and duplicate-run preflight checks."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

SCHEMA = "research-experiment-registry-v1"
METRIC_KEYS = (
    "calendar_cagr",
    "annualized_time_weighted_return",
    "money_weighted_return",
    "final_nav",
    "net_contributions",
    "net_profit",
    "annualized_volatility",
    "maximum_drawdown",
    "maximum_principal_loss",
    "total_cost",
    "fill_count",
    "mean_actual_exposure",
    "reentry_signal_date",
    "first_reentry_buy_date",
    "reentry_buy_count",
    "reentry_blocked_days",
    "reentry_executed",
    "capital_exit_trigger_date",
    "market_reentry_date",
    "permanent_40pct_exit_date",
    "permanent_principal_stop_date",
    "numerical_40pct_limits_pass",
    "numerical_objective_pass",
    "q7_pass",
    "q7_numeric_pass",
)


def digest(value) -> str:
    def exact(item):
        if isinstance(item, bool) or item is None or isinstance(item, str):
            return item
        if isinstance(item, int | float | Decimal):
            number = Decimal(str(item))
            if not number.is_finite():
                raise ValueError("finite numeric research parameters required")
            return {"$number": str(number.normalize())}
        if isinstance(item, dict):
            return {key: exact(v) for key, v in item.items()}
        if isinstance(item, list | tuple):
            return [exact(v) for v in item]
        raise TypeError(type(item).__name__)

    return hashlib.sha256(
        json.dumps(exact(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cashflow_signature(manifest_sha: str, specification: dict, engines: dict) -> str:
    """Exclude wrapper/report wording; retain input, cashflow/cost/risk spec and engines."""
    required = {
        "src/dynamic_leverage/services/account_replay.py",
        "src/dynamic_leverage/services/cashflow_research.py",
    }
    if not required.issubset(engines):
        raise ValueError("complete scientific engine fingerprints required")
    return digest(
        {
            "family": "cashflow-signal-research-v1",
            "manifest": manifest_sha,
            "specification": specification,
            "engines": {key: engines[key] for key in sorted(required)},
        }
    )


def report_signature(report: dict, name: str) -> str:
    if report.get("schema") == "alternative-nav-proxy-study-v1":
        return digest(report["experiment_identity"])
    if report.get("schema") == "cashflow-signal-research-v1":
        return cashflow_signature(
            report["manifest_sha256"], report["registered_specification"], report["source_sha256"]
        )
    # Older formats do not all expose complete structured parameters. Retain source
    # artifact identity rather than falsely declaring equivalent requests from metrics.
    return digest({"legacy_artifact": name, "report": report})


def revision_status(name: str) -> str:
    if name.startswith("core-satellite-vol20-results-") and not name.endswith("-v7"):
        return "archived_revision"
    if name == "budget-diagnostics-results-20260928":
        return "superseded_by_verified_acceptance"
    return "reference"


def route_family(name: str, report: dict | None = None) -> str:
    for prefix, family in (
        ("alternative-nav-results", "alternative-markets-nav-proxy"),
        ("historical-results", "original-valuation-and-static-controls"),
        ("risk-replay-results", "risk-gates-exposure-ceilings"),
        ("cash-reserve-results", "allocation-recipes-cash-reserve"),
        ("valuation-reserve-results", "valuation-staging-and-defense"),
        ("oos-crisis-results", "synthetic-crises-and-forward-availability"),
        ("drawdown-budget-results", "drawdown-budget-recovery"),
        ("strategy-value-results", "valuation-earnings-ablation"),
        ("return-attribution-results", "return-attribution-zero-cost-controls"),
        ("recovery-rearm-results", "recovery-rearm-ablation"),
        ("budget-diagnostics-results", "risk-budget-attribution"),
        ("core-satellite-vol20-results", "core-satellite-volatility-and-exits"),
        ("cashflow-salary-example-results", "matched-new-deposits-example"),
        ("cashflow-signal-results", "staging-and-executed-reentry-diagnostics"),
    ):
        if name.startswith(prefix):
            return family
    if report and report.get("schema") == "cashflow-signal-research-v1":
        return (
            "matched-new-deposits-example"
            if report["registered_specification"]["monthly_contribution"]
            else "staging-and-executed-reentry-diagnostics"
        )
    return "unclassified-results-require-review"


def extract_cases(value, study: str, pointer="", period=None):
    period = dict(period or {})
    if isinstance(value, dict):
        for source, target in (
            ("start", "start"),
            ("end", "end"),
            ("start_close", "start"),
            ("end_close", "end"),
        ):
            if source in value and isinstance(value[source], str):
                period[target] = value[source]
        if isinstance(value.get("dates"), dict):
            period.update(value["dates"])
        if "calendar_cagr" in value and "final_nav" in value:
            metrics = {k: value[k] for k in METRIC_KEYS if k in value}
            synthetic = any(word in pointer for word in ("dotcom-", "gfc-"))
            blocked_old = (
                study.startswith("core-satellite-vol20-results-")
                and "exit30-reentry60d5" in pointer
                and "gfc-" in pointer
            )
            yield {
                "case_id": f"{study}#{pointer}",
                "json_pointer": pointer,
                "period": period,
                "data_kind": value.get(
                    "data_kind", "synthetic_crisis" if synthetic else "market_history"
                ),
                "evidence_use": "signal_without_executed_reentry"
                if blocked_old
                else "historical_exploration_or_control",
                "metrics": metrics,
                "parameters": {
                    k: value[k]
                    for k in ("config", "policy_config", "execution_config")
                    if k in value
                },
                "original_cagr_field": value.get("cagr"),
                "numeric_metrics_sha256": digest(metrics),
            }
            return
        for key, item in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from extract_cases(item, study, f"{pointer}/{escaped}", period)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from extract_cases(item, study, f"{pointer}/{index}", period)


def build_registry(root: Path) -> dict:
    studies, cases = [], []
    for path in sorted((root / "docs/research").glob("*/results.json")):
        report = json.loads(path.read_bytes())
        name = path.parent.name
        records = list(extract_cases(report, name))
        evidence = str(path.relative_to(root)).replace("\\", "/")
        study = {
            "study_id": name,
            "family": route_family(name, report),
            "revision_status": revision_status(name),
            "artifact": evidence,
            "artifact_sha256": file_sha(path),
            "case_count": len(records),
            "experiment_signature": report_signature(report, name),
            "new_out_of_sample": report.get("new_out_of_sample", False),
            "profitability_demonstrated": report.get("profitability_demonstrated", False),
            "source_fingerprints": report.get("source_sha256", report.get("code_sha256", {})),
            "dataset_manifest_sha256": report.get(
                "manifest_sha256",
                report.get("frozen_dataset_manifest_sha256", report.get("dataset_manifest_sha256")),
            ),
            "specification": report.get(
                "registered_specification", report.get("pre_registered_rule")
            ),
            "decision": report.get("decision", report.get("decisions")),
        }
        studies.append(study)
        cases.extend(
            {
                "study_id": name,
                "family": study["family"],
                "revision_status": study["revision_status"],
                "artifact": evidence,
                "artifact_sha256": study["artifact_sha256"],
                **record,
            }
            for record in records
        )
    numeric_groups = {}
    for case in cases:
        numeric_groups.setdefault(case["numeric_metrics_sha256"], []).append(case["case_id"])
    return {
        "schema": SCHEMA,
        "policy": {
            "numeric_equality_is_not_rule_equivalence": True,
            "cashflow_duplicates_use_full_specification_dataset_and_engine_identity": True,
            "legacy_dedup_requires_manual_provenance_review": True,
            "calendar_cagr_preferred_over_trading_session_cagr": True,
            "archived_revisions_are_not_new_out_of_sample": True,
        },
        "study_count": len(studies),
        "case_count": len(cases),
        "studies": studies,
        "cases": cases,
        "identical_numeric_groups": [ids for ids in numeric_groups.values() if len(ids) > 1],
        "data_acceptance_artifacts": [
            {
                "artifact": str(path.relative_to(root)).replace("\\", "/"),
                "artifact_sha256": file_sha(path),
            }
            for path in sorted(
                {
                    *(root / "docs/research").glob("*acceptance*.json"),
                    *(root / "docs/research").glob("*/acceptance.json"),
                }
            )
        ],
    }


def duplicate_preflight(root: Path, signature: str, rerun_reason: str | None = None) -> list[str]:
    """Refuse identical scientific experiments before creating an output directory."""
    path = root / "docs/research/experiment-registry.json"
    if not path.exists():
        raise ValueError("research registry missing; run scripts/update_research_registry.py first")
    registry = json.loads(path.read_bytes())
    if registry.get("schema") != SCHEMA:
        raise ValueError("unsupported research registry")
    recorded = {s["artifact"]: s["artifact_sha256"] for s in registry["studies"]}
    current = {
        str(p.relative_to(root)).replace("\\", "/"): file_sha(p)
        for p in (root / "docs/research").glob("*/results.json")
    }
    if current != recorded:
        raise ValueError("research registry stale or evidence altered; audit and regenerate first")
    hits = []
    for study in registry["studies"]:
        if study["experiment_signature"] == signature:
            evidence = root / study["artifact"]
            if not evidence.is_file() or file_sha(evidence) != study["artifact_sha256"]:
                raise ValueError("registered evidence missing or altered; audit registry first")
            hits.append(study["artifact"])
    if hits and not (rerun_reason and rerun_reason.strip()):
        raise ValueError(
            "identical experiment already exists; reuse "
            + "; ".join(hits)
            + ". For a justified regression, supply --rerun-reason."
        )
    return hits
