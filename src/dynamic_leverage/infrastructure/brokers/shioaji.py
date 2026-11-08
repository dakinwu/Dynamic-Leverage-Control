from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dynamic_leverage.domain.account import (
    BrokerAccountSnapshot,
    BrokerPosition,
    BrokerSettlement,
)


class ShioajiUnavailable(RuntimeError):
    pass


class ProductionOrderDisabled(PermissionError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class ShioajiCredentials:
    api_key: str
    secret_key: str

    def __repr__(self) -> str:
        return "ShioajiCredentials(api_key=<redacted>, secret_key=<redacted>)"


def load_credentials(path: Path) -> ShioajiCredentials:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    required = {"SJ_API_KEY", "SJ_SECRET_KEY"}
    missing = {name for name in required if not values.get(name)}
    if missing:
        raise ValueError(f"missing required Shioaji secret names: {', '.join(sorted(missing))}")
    return ShioajiCredentials(values["SJ_API_KEY"], values["SJ_SECRET_KEY"])


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value))


def _settlement_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


class ShioajiAccountSnapshotReader:
    def __init__(self, *, secret_file: Path) -> None:
        self.secret_file = secret_file

    @staticmethod
    def _library() -> Any:
        return ShioajiReadOnlyAdapter._library()

    def read_account(self, binding: str) -> BrokerAccountSnapshot:
        broker_id, separator, account_id = binding.partition("-")
        if not separator or not broker_id or not account_id:
            raise ShioajiUnavailable("account binding must use BROKER_ID-ACCOUNT_ID format")

        shioaji = self._library()
        credentials = load_credentials(self.secret_file)
        api = shioaji.Shioaji(simulation=False)
        try:
            api.login(
                api_key=credentials.api_key,
                secret_key=credentials.secret_key,
                subscribe_trade=False,
                receive_window=120_000,
            )
            matches = [
                account
                for account in api.list_accounts()
                if str(getattr(account, "broker_id", "")) == broker_id
                and str(getattr(account, "account_id", "")) == account_id
                and _enum_text(getattr(account, "account_type", "")).upper() == "S"
            ]
            if len(matches) != 1:
                raise ShioajiUnavailable(
                    "configured stock account binding did not match exactly once"
                )
            account = matches[0]
            if getattr(account, "signed", True) is False:
                raise ShioajiUnavailable("configured stock account is not signed for API access")

            balance = api.account_balance(account=account)
            balance_error = str(getattr(balance, "errmsg", "") or "").strip()
            raw_cash = getattr(balance, "acc_balance", None)
            if balance_error or raw_cash is None:
                raise ShioajiUnavailable(
                    "account balance is unavailable for the configured account"
                )

            positions = tuple(
                BrokerPosition(
                    instrument=str(getattr(position, "code", "")),
                    quantity=Decimal(str(getattr(position, "quantity", "0"))),
                    average_price=Decimal(str(getattr(position, "price", "0"))),
                    last_price=Decimal(str(getattr(position, "last_price", "0"))),
                    direction=_enum_text(getattr(position, "direction", "")),
                    condition=_enum_text(getattr(position, "cond", "")),
                )
                for position in api.list_positions(
                    account=account,
                    unit=shioaji.constant.Unit.Share,
                )
            )
            settlements = tuple(
                BrokerSettlement(
                    settlement_date=_settlement_date(item.date),
                    amount=Decimal(str(getattr(item, "amount", "0"))),
                    t=int(getattr(item, "T", 0)),
                )
                for item in api.settlements(account=account)
            )
            return BrokerAccountSnapshot(
                observed_at=datetime.now(ZoneInfo("Asia/Taipei")),
                cash=Decimal(str(raw_cash)),
                positions=positions,
                settlements=settlements,
            )
        except ShioajiUnavailable:
            raise
        except Exception as exc:
            raise ShioajiUnavailable("Shioaji read-only account sync failed") from exc
        finally:
            with suppress(Exception):
                api.logout()


class ShioajiReadOnlyAdapter:
    def __init__(self, *, secret_file: Path) -> None:
        self.secret_file = secret_file
        self._api: Any | None = None

    @staticmethod
    def _library() -> Any:
        try:
            import shioaji
        except ImportError as exc:
            raise ShioajiUnavailable("install the broker extra to use Shioaji") from exc
        return shioaji

    def connect(self) -> None:
        shioaji = self._library()
        credentials = load_credentials(self.secret_file)
        # This adapter is intentionally production read-only.  Paper
        # execution is local and must never call a broker order API.
        api = shioaji.Shioaji(simulation=False)
        api.login(
            api_key=credentials.api_key,
            secret_key=credentials.secret_key,
            subscribe_trade=False,
            receive_window=120_000,
        )
        self._api = api

    def disconnect(self) -> None:
        if self._api is not None:
            self._api.logout()
            self._api = None

    def contract_status(self, codes: tuple[str, ...]) -> dict[str, dict[str, object]]:
        if self._api is None:
            raise ShioajiUnavailable("Shioaji is not connected")
        result = {}
        for code in codes:
            contract = self._api.Contracts.Stocks.get(code)
            result[code] = {
                "exists": contract is not None,
                "exchange": str(getattr(contract, "exchange", "")) if contract else None,
                "update_date": str(getattr(contract, "update_date", "")) if contract else None,
                "limit_up": str(getattr(contract, "limit_up", "")) if contract else None,
                "limit_down": str(getattr(contract, "limit_down", "")) if contract else None,
            }
        return result

    def accounts(self) -> list[str]:
        if self._api is None:
            raise ShioajiUnavailable("Shioaji is not connected")
        return [str(account) for account in self._api.list_accounts()]

    def account_candidates(self) -> list[dict[str, str | bool]]:
        if self._api is None:
            raise ShioajiUnavailable("Shioaji is not connected")
        candidates = []
        for account in self._api.list_accounts():
            account_type = _enum_text(getattr(account, "account_type", "")).upper()
            broker_id = str(getattr(account, "broker_id", ""))
            account_id = str(getattr(account, "account_id", ""))
            if account_type != "S" or not broker_id or not account_id:
                continue
            candidates.append(
                {
                    "binding": f"{broker_id}-{account_id}",
                    "broker_id": broker_id,
                    "account_id": account_id,
                    "signed": bool(getattr(account, "signed", False)),
                }
            )
        return candidates

    def submit_production_order(self, *_args: object, **_kwargs: object) -> None:
        raise ProductionOrderDisabled("production order submission is excluded from the MVP")
