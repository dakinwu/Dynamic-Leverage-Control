from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _account_binding_from_secret_file(path: Path | None) -> str | None:
    """Read only the non-secret account binding from the mounted runtime file."""
    if path is None:
        return None
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("SHIOAJI_ACCOUNT_ID="):
                value = line.split("=", 1)[1].strip()
                return value or None
    except OSError:
        return None
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    database_url: str = "sqlite+pysqlite:///./dynamic_leverage.db"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    web_origin: str = "http://localhost:5173"
    auto_create_schema: bool = True
    shioaji_mode: str = "readonly"
    shioaji_secret_file: Path | None = None
    shioaji_ca_path: Path | None = None
    shioaji_account_id: str | None = None
    telegram_secret_file: Path | None = None
    finmind_token_file: Path | None = None
    # Official valuation inputs.  TWSE may change the monthly report's
    # download route, so keep both URLs configurable without code changes.
    twse_market_valuation_url: str = "https://www.twse.com.tw/zh/trading/statistics/index04.html"
    mops_market_eps_url: str = "https://openapi.twse.com.tw/v1/opendata/t187ap14_L"
    # Initial investment-only capital budget. Once a strategy ledger exists,
    # exposure must use its current NAV rather than this starting amount.
    initial_strategy_nav: int = Field(default=500_000, ge=1)
    production_order_submission_enabled: bool = False

    def model_post_init(self, __context: object) -> None:
        if not self.shioaji_account_id:
            binding = _account_binding_from_secret_file(self.shioaji_secret_file)
            if binding:
                self.shioaji_account_id = binding
        if self.production_order_submission_enabled:
            raise ValueError("production order submission is not available in the MVP")
        if self.shioaji_mode not in {"disabled", "readonly", "simulation"}:
            raise ValueError("SHIOAJI_MODE must be disabled, readonly, or simulation")


@lru_cache
def get_settings() -> Settings:
    return Settings()
