"""Run inside the local API container; emit sanitized P0 acceptance evidence only."""

import hashlib
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

import dynamic_leverage
from dynamic_leverage.config import get_settings
from dynamic_leverage.infrastructure.database import make_engine, make_session_factory
from dynamic_leverage.services.operations_projection import reconcile_operations


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/" + path, timeout=15) as response:
        return json.load(response)


settings = get_settings()
engine = make_engine(settings.database_url)
factory = make_session_factory(engine)


def counts():
    with factory.begin() as session:
        return {
            table: session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in [
                "operator_queue_events",
                "notification_outbox",
                "strategy_evaluations",
                "strategy_selection",
                "manual_halt_state",
            ]
        }


with factory.begin() as session:
    version = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
assert version == "0006"
system = get("system")
assert system["production_order_submission"] == "disabled"
assert system["shioaji_mode"] == "readonly"
selected = get("strategies/selection")
assert selected["strategy_version"] == "dvl-tw-v2-shadow"
# This is an explicit state-update command, not a browser request. No sender is called.
with factory.begin() as session:
    reconcile_operations(session, settings)
before = counts()
summary = get("operations/summary")
get("operations/summary")
get("data/freshness")
acceptance = get("data/acceptance")
after = counts()
assert before == after, "read requests changed evidence counts"
assert summary["operations_updated_at"]
assert summary["research"]["evidence_status"] == "verified-archive"
assert summary["research"]["selected_status"] == "unverified"
assert acceptance["profitability_demonstrated"] is False
assert "out_of_sample_and_cost_stress_comparison_not_run" not in acceptance["research_blockers"]
quote = next(r for r in summary["freshness"] if r["contract_key"] == "twse_quotes")
assert quote["source_trade_date"] == quote["expected_trade_date"]
assert quote["status"] == "FRESH"
report = {
    "checked_at": datetime.now(UTC).isoformat(),
    "runtime_source_sha256": {
        name: hashlib.sha256(
            (Path(dynamic_leverage.__file__).parent / name).read_bytes()
        ).hexdigest()
        for name in [
            "api/app.py",
            "services/calendar.py",
            "services/operations_projection.py",
            "services/research_evidence.py",
            "services/scheduler.py",
            "services/freshness.py",
            "infrastructure/providers/vix.py",
            "services/benchmark_comparison.py",
        ]
    },
    "migration": version,
    "system": {
        k: system[k] for k in ["status", "database", "production_order_submission", "shioaji_mode"]
    },
    "selected_strategy": selected["strategy_version"],
    "operator_status": summary["operator_status"],
    "operations_updated_at": summary["operations_updated_at"],
    "get_requests_changed_counts": False,
    "get_counts": before,
    "freshness": [
        {
            k: row[k]
            for k in [
                "contract_key",
                "status",
                "source_trade_date",
                "expected_trade_date",
                "reason",
            ]
        }
        for row in summary["freshness"]
    ],
    "research_archive": summary["research"]["evidence_status"],
    "selected_research_status": summary["research"]["selected_status"],
    "research_records": len(summary["research"]["records"]),
    "research_blockers": acceptance["research_blockers"],
    "profitability_demonstrated": acceptance["profitability_demonstrated"],
}
print(json.dumps(report, ensure_ascii=True, indent=2))
engine.dispose()
