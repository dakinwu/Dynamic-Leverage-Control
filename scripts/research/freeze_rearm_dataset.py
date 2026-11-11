"""One-time bridge from legacy preparation scripts to immutable, lossless inputs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from run_recovery_rearm_study import REARM
from run_strategy_value_study import prepared_groups
from run_valuation_reserve_study import base_config

from dynamic_leverage.services.research_artifacts import encode_exact

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data/private/rearm-dataset-20260928"


def main():
    OUT.mkdir(exist_ok=False)
    prior = json.loads(
        (ROOT / "docs/research/recovery-rearm-results-20260922/results.json").read_bytes()
    )
    # The bootstrap itself may evolve; every source/raw input used in the old
    # study must still match except operational modules unused by this replay.
    checked = {}
    for name, expected in prior["evidence_sha256"].items():
        actual = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"frozen research source changed: {name}")
        checked[name] = actual
    manifest = {
        "schema": "rearm-frozen-dataset-v1",
        "new_out_of_sample": False,
        "source_evidence": checked,
        "datasets": {},
    }
    for label, stress, rows in prepared_groups():
        payload = {
            "policy_config": asdict(REARM),
            "execution_config": asdict(base_config(stress)),
            "rows": [asdict(replace(row, target=1)) for row in rows],
        }
        path = OUT / f"{label}.json"
        path.write_text(
            json.dumps(payload, default=encode_exact, ensure_ascii=False), encoding="utf-8"
        )
        manifest["datasets"][label] = {
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Frozen {len(manifest['datasets'])} datasets")


if __name__ == "__main__":
    main()
