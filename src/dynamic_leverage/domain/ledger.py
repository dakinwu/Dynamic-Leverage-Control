from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from dynamic_leverage.domain.corporate_actions import split_units


@dataclass(frozen=True, slots=True)
class StrategyLedgerState:
    capital_budget: Decimal
    net_contributions: Decimal
    cash: Decimal
    positions: dict[str, Decimal]
    last_prices: dict[str, Decimal]
    current_nav: Decimal
    peak_nav: Decimal
    principal_loss_amount: Decimal
    principal_loss_ratio: Decimal
    current_drawdown: Decimal
    maximum_drawdown: Decimal
    position_revision: int
    reconciled: bool
    reconciliation_reason: str | None
    last_account_snapshot_id: str | None
    initialized_at: datetime
    updated_at: datetime
    actual_exposure: Decimal | None = None
    account_exposure: Decimal | None = None
    exposure_quality: str = "unknown"
    # Dividends owed since their ex-date (action_id -> amount); part of NAV.
    receivables: dict[str, Decimal] = field(default_factory=dict)
    # Dividends credited at payment date but not yet confirmed by the operator.
    unconfirmed_dividends: dict[str, Decimal] = field(default_factory=dict)


def strategy_nav(
    cash: Decimal,
    positions: dict[str, Decimal],
    prices: dict[str, Decimal],
    receivables: dict[str, Decimal],
) -> Decimal:
    """Cash, marked positions, and dividends already owed but not yet paid."""
    marked = sum((positions[code] * prices[code] for code in positions), Decimal("0"))
    return cash + marked + sum(receivables.values(), Decimal("0"))


def _revalued(state: StrategyLedgerState, **changes) -> StrategyLedgerState:
    """Apply changes, then recompute NAV and the loss metrics from the result."""
    updated = replace(state, **changes)
    nav = strategy_nav(updated.cash, updated.positions, updated.last_prices, updated.receivables)
    peak, loss, loss_ratio, drawdown, maximum_drawdown = _loss_metrics(
        nav=nav,
        net_contributions=updated.net_contributions,
        prior_peak=state.peak_nav,
        prior_maximum_drawdown=state.maximum_drawdown,
    )
    return replace(
        updated,
        current_nav=nav,
        peak_nav=peak,
        principal_loss_amount=loss,
        principal_loss_ratio=loss_ratio,
        current_drawdown=drawdown,
        maximum_drawdown=maximum_drawdown,
    )


def _loss_metrics(
    *,
    nav: Decimal,
    net_contributions: Decimal,
    prior_peak: Decimal,
    prior_maximum_drawdown: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    peak = max(prior_peak, nav)
    principal_loss = max(net_contributions - nav, Decimal("0"))
    principal_loss_ratio = (
        principal_loss / net_contributions if net_contributions > 0 else Decimal("0")
    )
    current_drawdown = max(Decimal("1") - nav / peak, Decimal("0")) if peak > 0 else Decimal("0")
    maximum_drawdown = max(prior_maximum_drawdown, current_drawdown)
    return peak, principal_loss, principal_loss_ratio, current_drawdown, maximum_drawdown


def bootstrap_strategy_ledger(
    *,
    capital_budget: Decimal,
    positions: dict[str, Decimal],
    prices: dict[str, Decimal],
    observed_at: datetime,
    account_snapshot_id: str,
) -> StrategyLedgerState:
    if capital_budget <= 0:
        raise ValueError("capital budget must be positive")
    if set(positions) != set(prices):
        raise ValueError("every strategy position requires a price")
    if any(quantity < 0 for quantity in positions.values()):
        raise ValueError("strategy position quantity cannot be negative")
    if any(price <= 0 for price in prices.values()):
        raise ValueError("strategy position price must be positive")

    invested = sum(
        (positions[instrument] * prices[instrument] for instrument in positions),
        Decimal("0"),
    )
    if invested > capital_budget:
        raise ValueError("strategy positions exceed the configured capital budget")
    cash = capital_budget - invested
    return StrategyLedgerState(
        capital_budget=capital_budget,
        net_contributions=capital_budget,
        cash=cash,
        positions=dict(positions),
        last_prices=dict(prices),
        current_nav=capital_budget,
        peak_nav=capital_budget,
        principal_loss_amount=Decimal("0"),
        principal_loss_ratio=Decimal("0"),
        current_drawdown=Decimal("0"),
        maximum_drawdown=Decimal("0"),
        position_revision=0,
        reconciled=True,
        reconciliation_reason=None,
        last_account_snapshot_id=account_snapshot_id,
        initialized_at=observed_at,
        updated_at=observed_at,
    )


def mark_strategy_ledger(
    state: StrategyLedgerState,
    *,
    positions: dict[str, Decimal],
    prices: dict[str, Decimal],
    observed_at: datetime,
    account_snapshot_id: str,
) -> StrategyLedgerState:
    expected = {key: value for key, value in state.positions.items() if value != 0}
    observed = {key: value for key, value in positions.items() if value != 0}
    if observed != expected:
        differences = sorted(set(expected) | set(observed))
        detail = ", ".join(
            f"{instrument}:ledger={expected.get(instrument, Decimal('0'))},"
            f"broker={observed.get(instrument, Decimal('0'))}"
            for instrument in differences
            if expected.get(instrument, Decimal("0")) != observed.get(instrument, Decimal("0"))
        )
        return replace(
            state,
            reconciled=False,
            reconciliation_reason=f"unrecorded-position-change ({detail})",
            last_account_snapshot_id=account_snapshot_id,
            updated_at=observed_at,
            actual_exposure=None,
            account_exposure=None,
            exposure_quality="reconciliation-break",
        )
    if set(expected) != set(prices):
        return replace(
            state,
            reconciled=False,
            reconciliation_reason="missing-price-for-ledger-position",
            last_account_snapshot_id=account_snapshot_id,
            updated_at=observed_at,
            actual_exposure=None,
            account_exposure=None,
            exposure_quality="reconciliation-break",
        )

    nav = strategy_nav(state.cash, expected, prices, state.receivables)
    peak, loss, loss_ratio, drawdown, maximum_drawdown = _loss_metrics(
        nav=nav,
        net_contributions=state.net_contributions,
        prior_peak=state.peak_nav,
        prior_maximum_drawdown=state.maximum_drawdown,
    )
    return replace(
        state,
        positions=expected,
        last_prices=dict(prices),
        current_nav=nav,
        peak_nav=peak,
        principal_loss_amount=loss,
        principal_loss_ratio=loss_ratio,
        current_drawdown=drawdown,
        maximum_drawdown=maximum_drawdown,
        reconciled=True,
        reconciliation_reason=None,
        last_account_snapshot_id=account_snapshot_id,
        updated_at=observed_at,
    )


def apply_strategy_fill(
    state: StrategyLedgerState,
    *,
    instrument: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    commission: Decimal,
    tax: Decimal,
    occurred_at: datetime,
) -> StrategyLedgerState:
    normalized_side = side.upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if quantity <= 0 or price <= 0:
        raise ValueError("fill quantity and price must be positive")
    if commission < 0 or tax < 0:
        raise ValueError("commission and tax cannot be negative")

    positions = dict(state.positions)
    existing = positions.get(instrument, Decimal("0"))
    gross = quantity * price
    if normalized_side == "BUY":
        cash_change = -(gross + commission + tax)
        if state.cash + cash_change < 0:
            raise ValueError("fill would make strategy cash negative")
        positions[instrument] = existing + quantity
    else:
        if quantity > existing:
            raise ValueError("sell quantity exceeds the strategy ledger position")
        cash_change = gross - commission - tax
        remaining = existing - quantity
        if remaining:
            positions[instrument] = remaining
        else:
            positions.pop(instrument, None)

    prices = dict(state.last_prices)
    if instrument in positions:
        prices[instrument] = price
    else:
        prices.pop(instrument, None)
    cash = state.cash + cash_change
    nav = strategy_nav(cash, positions, prices, state.receivables)
    peak, loss, loss_ratio, drawdown, maximum_drawdown = _loss_metrics(
        nav=nav,
        net_contributions=state.net_contributions,
        prior_peak=state.peak_nav,
        prior_maximum_drawdown=state.maximum_drawdown,
    )
    return replace(
        state,
        cash=cash,
        positions=positions,
        last_prices=prices,
        current_nav=nav,
        peak_nav=peak,
        principal_loss_amount=loss,
        principal_loss_ratio=loss_ratio,
        current_drawdown=drawdown,
        maximum_drawdown=maximum_drawdown,
        position_revision=state.position_revision + 1,
        reconciled=False,
        reconciliation_reason="awaiting-broker-confirmation",
        updated_at=occurred_at,
        actual_exposure=None,
        account_exposure=None,
        exposure_quality="awaiting-broker-confirmation",
    )


def apply_split(
    state: StrategyLedgerState, *, symbol: str, ratio: int, at: datetime
) -> StrategyLedgerState:
    """Units times ratio, price divided by ratio: wealth and loss metrics unchanged."""
    if symbol not in state.positions:
        raise ValueError("split applies only to a held strategy position")
    positions = dict(state.positions)
    prices = dict(state.last_prices)
    positions[symbol] = split_units(positions[symbol], ratio)
    prices[symbol] = prices[symbol] / Decimal(ratio)
    return _revalued(
        state,
        positions=positions,
        last_prices=prices,
        position_revision=state.position_revision + 1,
        updated_at=at,
    )


def accrue_dividend(
    state: StrategyLedgerState, *, action_id: str, amount: Decimal, at: datetime
) -> StrategyLedgerState:
    """At the ex-date the dividend becomes owed; the price drop is offset in NAV."""
    if action_id in state.receivables or action_id in state.unconfirmed_dividends:
        raise ValueError(f"dividend {action_id} is already booked")
    if not amount.is_finite() or amount < 0:
        raise ValueError("dividend entitlement cannot be negative")
    return _revalued(state, receivables={**state.receivables, action_id: amount}, updated_at=at)


def pay_dividend(
    state: StrategyLedgerState, *, action_id: str, at: datetime
) -> StrategyLedgerState:
    """At the payment date the receivable becomes cash, pending confirmation."""
    if action_id not in state.receivables:
        raise ValueError(f"no receivable for dividend {action_id}")
    receivables = dict(state.receivables)
    amount = receivables.pop(action_id)
    return _revalued(
        state,
        cash=state.cash + amount,
        receivables=receivables,
        unconfirmed_dividends={**state.unconfirmed_dividends, action_id: amount},
        updated_at=at,
    )


def confirm_dividend(
    state: StrategyLedgerState, *, action_id: str, received: Decimal, at: datetime
) -> StrategyLedgerState:
    """Book what actually arrived; withholdings and remittance fees reduce cash."""
    if action_id not in state.unconfirmed_dividends:
        raise ValueError(f"no unconfirmed payment for dividend {action_id}")
    if not received.is_finite() or received < 0:
        raise ValueError("received dividend cannot be negative")
    unconfirmed = dict(state.unconfirmed_dividends)
    credited = unconfirmed.pop(action_id)
    return _revalued(
        state,
        cash=state.cash + received - credited,
        unconfirmed_dividends=unconfirmed,
        updated_at=at,
    )
