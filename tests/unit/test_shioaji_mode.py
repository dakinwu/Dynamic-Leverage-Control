"""SHIOAJI_MODE must mean what it says: disabled never logs in, and only real modes exist."""

import sys

import pytest
from pydantic import ValidationError

from dynamic_leverage import cli
from dynamic_leverage.config import Settings, get_settings


class _RecordingAdapter:
    connections: list[str] = []

    def __init__(self, *, secret_file) -> None:
        self.secret_file = secret_file

    def connect(self) -> None:
        _RecordingAdapter.connections.append("login")

    def account_candidates(self):
        return [{"binding": "9A00-1234567", "signed": True}]

    def accounts(self):
        return []

    def contract_status(self, codes):
        return {}

    def disconnect(self) -> None:
        pass


@pytest.fixture
def disabled_with_secret(monkeypatch, tmp_path):
    secret = tmp_path / "runtime.env"
    secret.write_text("SJ_API_KEY=key\nSJ_SECRET_KEY=secret\n", encoding="utf-8")
    monkeypatch.setenv("SHIOAJI_MODE", "disabled")
    monkeypatch.setenv("SHIOAJI_SECRET_FILE", str(secret))
    monkeypatch.setenv("SHIOAJI_ACCOUNT_ID", "9A00-1234567")
    get_settings.cache_clear()
    _RecordingAdapter.connections = []
    monkeypatch.setattr(cli, "ShioajiReadOnlyAdapter", _RecordingAdapter)
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("command", [["shioaji-accounts"], ["shioaji-check"]])
def test_cli_never_logs_in_when_shioaji_is_disabled(
    monkeypatch, disabled_with_secret, command
) -> None:
    monkeypatch.setattr(sys, "argv", ["dvl", *command])

    with pytest.raises((RuntimeError, SystemExit)):
        cli.main()

    assert _RecordingAdapter.connections == []


def test_simulation_is_not_an_accepted_mode() -> None:
    with pytest.raises(ValidationError, match="disabled or readonly"):
        Settings(shioaji_mode="simulation")
