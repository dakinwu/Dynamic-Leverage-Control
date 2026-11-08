from pathlib import Path
from types import SimpleNamespace

import pytest

from dynamic_leverage.infrastructure.brokers.shioaji import (
    ProductionOrderDisabled,
    ShioajiAccountSnapshotReader,
    ShioajiReadOnlyAdapter,
    ShioajiUnavailable,
    load_credentials,
)


def test_credentials_repr_is_redacted(tmp_path: Path) -> None:
    secret_file = tmp_path / "shioaji.env"
    secret_file.write_text(
        "SJ_API_KEY=visible-only-in-file\nSJ_SECRET_KEY=also-secret\n",
        encoding="utf-8",
    )
    credentials = load_credentials(secret_file)
    assert "visible-only-in-file" not in repr(credentials)
    assert "also-secret" not in repr(credentials)


def test_empty_credentials_are_rejected(tmp_path: Path) -> None:
    secret_file = tmp_path / "empty.env"
    secret_file.write_text("SJ_API_KEY=\nSJ_SECRET_KEY=\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SJ_API_KEY, SJ_SECRET_KEY"):
        load_credentials(secret_file)


def test_production_submission_method_is_hard_disabled(tmp_path: Path) -> None:
    adapter = ShioajiReadOnlyAdapter(secret_file=tmp_path / "missing.env")
    with pytest.raises(ProductionOrderDisabled):
        adapter.submit_production_order()


def test_account_candidates_are_restricted_to_stock_accounts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "runtime.env"
    secret_file.write_text("SJ_API_KEY=key\nSJ_SECRET_KEY=secret\n", encoding="utf-8")
    stock = SimpleNamespace(broker_id="9A00", account_id="1234567", account_type="S", signed=True)
    futures = SimpleNamespace(
        broker_id="F002000", account_id="7654321", account_type="F", signed=True
    )
    api = SimpleNamespace(
        login=lambda **_kwargs: None,
        logout=lambda: None,
        list_accounts=lambda: [stock, futures],
    )
    library = SimpleNamespace(Shioaji=lambda **_kwargs: api)
    monkeypatch.setattr(ShioajiReadOnlyAdapter, "_library", staticmethod(lambda: library))

    adapter = ShioajiReadOnlyAdapter(secret_file=secret_file)
    adapter.connect()
    assert adapter.account_candidates() == [
        {
            "binding": "9A00-1234567",
            "broker_id": "9A00",
            "account_id": "1234567",
            "signed": True,
        }
    ]
    adapter.disconnect()


class FakeShioajiApi:
    def __init__(self) -> None:
        self.logged_out = False
        self.account = SimpleNamespace(
            broker_id="9A00",
            account_id="1234567",
            account_type="S",
            signed=True,
        )

    def login(self, **_credentials: str) -> None:
        assert _credentials["subscribe_trade"] is False
        assert _credentials["receive_window"] == 120_000
        return None

    def logout(self) -> None:
        self.logged_out = True

    def list_accounts(self):
        return [self.account]

    def account_balance(self, *, account):
        assert account is self.account
        return SimpleNamespace(acc_balance=80000, errmsg="")

    def list_positions(self, *, account, unit):
        assert account is self.account
        assert unit == "Share"
        return [
            SimpleNamespace(
                code="0050",
                quantity=1000,
                price=48,
                last_price=50,
                direction=SimpleNamespace(value="Buy"),
                cond=SimpleNamespace(value="Cash"),
            )
        ]

    def settlements(self, *, account):
        assert account is self.account
        return [SimpleNamespace(date="2026-09-02", amount=-10000, T=2)]


class FakeShioajiLibrary:
    constant = SimpleNamespace(Unit=SimpleNamespace(Share="Share"))

    def __init__(self, api: FakeShioajiApi) -> None:
        self.api = api

    def Shioaji(self, *, simulation: bool):
        assert simulation is False
        return self.api


def test_account_reader_returns_normalized_snapshot_and_logs_out(
    monkeypatch,
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "runtime.env"
    secret_file.write_text("SJ_API_KEY=key\nSJ_SECRET_KEY=secret\n", encoding="utf-8")
    api = FakeShioajiApi()
    library = FakeShioajiLibrary(api)
    monkeypatch.setattr(ShioajiAccountSnapshotReader, "_library", staticmethod(lambda: library))

    snapshot = ShioajiAccountSnapshotReader(secret_file=secret_file).read_account("9A00-1234567")

    assert snapshot.cash == 80000
    assert snapshot.positions[0].instrument == "0050"
    assert snapshot.positions[0].quantity == 1000
    assert snapshot.settlements[0].t == 2
    assert api.logged_out is True


def test_account_reader_fails_closed_when_balance_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "runtime.env"
    secret_file.write_text("SJ_API_KEY=key\nSJ_SECRET_KEY=secret\n", encoding="utf-8")
    api = FakeShioajiApi()
    api.account_balance = lambda **_kwargs: SimpleNamespace(
        acc_balance=None,
        errmsg="unsupported settlement bank",
    )
    library = FakeShioajiLibrary(api)
    monkeypatch.setattr(ShioajiAccountSnapshotReader, "_library", staticmethod(lambda: library))

    with pytest.raises(ShioajiUnavailable, match="account balance is unavailable"):
        ShioajiAccountSnapshotReader(secret_file=secret_file).read_account("9A00-1234567")

    assert api.logged_out is True
