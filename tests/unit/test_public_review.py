import importlib.util
from pathlib import Path


def checker():
    path = Path(__file__).resolve().parents[2] / "scripts/check_public_files.py"
    spec = importlib.util.spec_from_file_location("public_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_check_includes_explicitly_tracked_private_files(tmp_path):
    # Passing the indexed file directly must fail even if .gitignore would hide it.
    path = tmp_path / "data/private/account.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a public fixture")
    assert checker().inspect(tmp_path, ["data/private/account.db"])[0]["kind"] == (
        "private_or_generated_file"
    )


def test_public_check_redacts_token_and_reports_only_location(tmp_path):
    value = "ghp_" + "x" * 36
    (tmp_path / "bad.txt").write_text(value, encoding="utf-8")
    findings = checker().inspect(tmp_path, ["bad.txt"])
    assert findings == [{"file": "bad.txt", "line": 1, "kind": "github_token"}]
    assert value not in str(findings)


def test_example_environment_matches_settings_and_disables_external_accounts():
    from dynamic_leverage.config import Settings

    path = Path(__file__).resolve().parents[2] / ".env.example"
    entries = dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )
    assert set(entries) - {"DVL_SECRET_FILE"} <= {k.upper() for k in Settings.model_fields}
    settings = Settings(**{k.lower(): v for k, v in entries.items() if k != "DVL_SECRET_FILE"})
    assert settings.shioaji_mode == "disabled"
    assert settings.shioaji_secret_file is None
    assert settings.production_order_submission_enabled is False
    assert settings.database_url.startswith("sqlite+")
