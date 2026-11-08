import pytest

from dynamic_leverage.config import Settings


def test_default_strategy_capital_budget_is_five_hundred_thousand() -> None:
    assert Settings().initial_strategy_nav == 500_000


def test_mvp_cannot_enable_production_order_submission() -> None:
    with pytest.raises(ValueError, match="not available"):
        Settings(production_order_submission_enabled=True)


def test_account_binding_falls_back_to_mounted_runtime_file(tmp_path) -> None:
    secret_file = tmp_path / "runtime.env"
    secret_file.write_text(
        "SJ_API_KEY=<redacted>\nSJ_SECRET_KEY=<redacted>\nSHIOAJI_ACCOUNT_ID=9A9B-0239014\n",
        encoding="utf-8",
    )

    settings = Settings(
        shioaji_secret_file=secret_file,
        shioaji_account_id=None,
    )

    assert settings.shioaji_account_id == "9A9B-0239014"
