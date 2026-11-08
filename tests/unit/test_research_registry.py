import json
from decimal import Decimal
from pathlib import Path

import pytest

from dynamic_leverage.services.research_registry import (
    build_registry,
    cashflow_signature,
    duplicate_preflight,
    extract_cases,
)

ENGINES = {
    "src/dynamic_leverage/services/account_replay.py": "replay-a",
    "src/dynamic_leverage/services/cashflow_research.py": "cashflow-a",
}


def register(tmp_path: Path, monthly=0):
    output = tmp_path / "docs/research/cashflow-signal-results-example"
    output.mkdir(parents=True)
    report = {
        "schema": "cashflow-signal-research-v1",
        "manifest_sha256": "dataset-a",
        "registered_specification": {"monthly_contribution": monthly, "costs": "base-and-stress"},
        "source_sha256": {**ENGINES, "scripts/wrapper.py": "wrapper-v1"},
        "groups": {
            "real-etf-base": {
                "results": {
                    "control": {
                        "calendar_cagr": 0.1,
                        "cagr": 0.11,
                        "final_nav": 550000,
                        "maximum_drawdown": -0.2,
                        "dates": {"start": "2020-01-01", "end": "2021-01-01"},
                    }
                }
            }
        },
    }
    artifact = output / "results.json"
    artifact.write_text(json.dumps(report), encoding="utf-8")
    index = build_registry(tmp_path)
    (tmp_path / "docs/research/experiment-registry.json").write_text(
        json.dumps(index), encoding="utf-8"
    )
    return artifact, report, index


def test_identical_experiment_is_rejected_before_any_rerun(tmp_path):
    _, report, index = register(tmp_path)
    signature = cashflow_signature("dataset-a", report["registered_specification"], ENGINES)
    assert signature == index["studies"][0]["experiment_signature"]
    with pytest.raises(ValueError, match="identical experiment"):
        duplicate_preflight(tmp_path, signature)


@pytest.mark.parametrize("changed", ["dataset", "engine", "cashflow", "costs"])
def test_changed_scientific_inputs_are_distinct_experiments(tmp_path, changed):
    _, report, _ = register(tmp_path)
    manifest = "dataset-b" if changed == "dataset" else "dataset-a"
    engines = {**ENGINES}
    specification = {**report["registered_specification"]}
    if changed == "engine":
        engines["src/dynamic_leverage/services/account_replay.py"] = "replay-b"
    elif changed == "cashflow":
        specification["monthly_contribution"] = 10000
    elif changed == "costs":
        specification["costs"] = "new-cost-assumption"
    assert duplicate_preflight(tmp_path, cashflow_signature(manifest, specification, engines)) == []


def test_wrapper_wording_change_does_not_evade_duplicate_detection():
    a = cashflow_signature(
        "dataset", {"monthly_contribution": 0}, {**ENGINES, "scripts/wrapper.py": "a"}
    )
    b = cashflow_signature(
        "dataset", {"monthly_contribution": 0}, {**ENGINES, "scripts/wrapper.py": "b"}
    )
    assert a == b


def test_runtime_decimal_and_persisted_float_have_same_signature():
    assert cashflow_signature("dataset", {"monthly_contribution": Decimal(0)}, ENGINES) == (
        cashflow_signature("dataset", {"monthly_contribution": 0.0}, ENGINES)
    )


def test_justified_regression_returns_old_evidence_for_lineage(tmp_path):
    _, _, index = register(tmp_path)
    assert duplicate_preflight(
        tmp_path,
        index["studies"][0]["experiment_signature"],
        "verify a diagnosed accounting defect",
    ) == [index["studies"][0]["artifact"]]


def test_modified_result_or_unregistered_result_blocks_preflight(tmp_path):
    artifact, _, index = register(tmp_path)
    artifact.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="stale or evidence altered"):
        duplicate_preflight(tmp_path, index["studies"][0]["experiment_signature"], "regression")


def test_unknown_registry_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="registry missing"):
        duplicate_preflight(tmp_path, "signature")


def test_calendar_cagr_and_synthetic_scope_are_preserved_without_reinterpretation():
    records = list(
        extract_cases(
            {
                "groups": {
                    "gfc-path": {
                        "start": "2007-01-02",
                        "end": "2010-12-31",
                        "results": {
                            "candidate": {
                                "calendar_cagr": 0.1,
                                "cagr": 0.11,
                                "final_nav": 1,
                                "maximum_drawdown": -0.4,
                            }
                        },
                    }
                }
            },
            "example",
        )
    )
    assert len(records) == 1
    assert records[0]["metrics"]["calendar_cagr"] == 0.1
    assert records[0]["original_cagr_field"] == 0.11
    assert records[0]["data_kind"] == "synthetic_crisis"
    assert records[0]["period"]["start"] == "2007-01-02"


def test_equal_numbers_do_not_merge_different_protocols(tmp_path):
    _, report, _ = register(tmp_path)
    second = tmp_path / "docs/research/another-protocol"
    second.mkdir()
    report["registered_specification"]["monthly_contribution"] = 10000
    (second / "results.json").write_text(json.dumps(report), encoding="utf-8")
    index = build_registry(tmp_path)
    assert index["case_count"] == 2
    assert len(index["identical_numeric_groups"]) == 1
    assert len({s["experiment_signature"] for s in index["studies"]}) == 2


def test_nav_proxy_keeps_its_evidence_type_and_scientific_identity():
    from dynamic_leverage.services.research_registry import report_signature, route_family

    report = {
        "schema": "alternative-nav-proxy-study-v1",
        "experiment_identity": {"engines": "a", "manifests": "b"},
    }
    assert report_signature(report, "folder-a") == report_signature(report, "folder-b")
    assert route_family("alternative-nav-results-example") == "alternative-markets-nav-proxy"
    records = list(
        extract_cases({"calendar_cagr": 0.1, "final_nav": 1, "data_kind": "nav_proxy"}, "example")
    )
    assert records[0]["data_kind"] == "nav_proxy"
