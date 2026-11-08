"""Daily historical account simulation. No broker connection or production state mutation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Protocol

from dynamic_leverage.domain.execution import OneXTwoXRecipe, TwoXCashRecipe
from dynamic_leverage.domain.ledger import _loss_metrics
from dynamic_leverage.domain.models import SafetyState, StrategyInputs
from dynamic_leverage.domain.reserve_policy import ReserveAccount, ReservePolicy
from dynamic_leverage.domain.risk import RiskContext, RiskGate

D = Decimal
SYMBOLS = ("0050", "00631L")


@dataclass(frozen=True)
class Quote:
    close: Decimal
    volume: int
    buy_allowed: bool = True
    sell_allowed: bool = True


@dataclass(frozen=True)
class ReplayDay:
    day: date
    quotes: dict[str, Quote]
    target: Decimal
    strategy_state: SafetyState = SafetyState.NORMAL
    rv20: Decimal = D("0.2")
    above_trend: bool = True
    dividends: dict[str, tuple[Decimal, date]] = field(default_factory=dict)
    splits: dict[str, Decimal] = field(default_factory=dict)
    data_safe: bool = True
    broker_safe: bool = True
    manual_halt: bool = False
    account_eligible: bool = True
    etf_abnormal: bool = False
    policy_inputs: StrategyInputs | None = None
    market_recovery_ready: bool | None = None
    contribution: Decimal = D(0)
    reserved_cash: Decimal = D(0)
    scheduled_investment: bool = False


@dataclass(frozen=True)
class ReplayConfig:
    mode: str = "existing"
    recipe: str = "1x-2x-v1"
    ceiling: Decimal = D(2)
    initial_nav: Decimal = D(500000)
    commission: Decimal = D("0.001425")
    sell_tax: Decimal = D("0.001")
    slippage: Decimal = D("0.0005")
    minimum_commission: Decimal = D(20)
    execution_delay: int = 1
    settlement_delay: int = 2
    volume_fraction: Decimal = D("0.01")
    no_trade_band: Decimal = D("0.15")
    cash_interest_rate: Decimal = D(0)
    loss_gate_basis: str = "principal-and-drawdown"

    def __post_init__(self):
        if self.recipe not in {"1x-2x-v1", "2x-cash-v1"}:
            raise ValueError("unknown allocation recipe")
        if self.mode not in {"ungated", "existing", "defensive", "buyhold"}:
            raise ValueError("unknown research mode")
        if self.loss_gate_basis not in {"principal-and-drawdown", "principal-only-research"}:
            raise ValueError("unknown loss gate basis")
        if not self.ceiling.is_finite() or not 0 <= self.ceiling <= 2:
            raise ValueError("cash-account ceiling must be between zero and two")
        if self.initial_nav <= 0 or self.execution_delay < 1 or self.settlement_delay < 0:
            raise ValueError("invalid NAV or timing")
        rates = (
            self.commission,
            self.sell_tax,
            self.slippage,
            self.minimum_commission,
            self.volume_fraction,
            self.no_trade_band,
            self.cash_interest_rate,
        )
        if any(not value.is_finite() or value < 0 for value in rates):
            raise ValueError("invalid replay cost or liquidity parameter")
        if self.slippage >= 1 or self.commission + self.sell_tax >= 1:
            raise ValueError("cost rate cannot exhaust trade proceeds")
        if not 0 < self.volume_fraction <= 1:
            raise ValueError("volume participation must be positive and at most one")
        if self.cash_interest_rate > 1:
            raise ValueError("cash interest assumption must be at most one")


@dataclass
class Pending:
    signal_date: date
    due: int
    target: Decimal
    orders: list[tuple[str, str, int]]
    cash_floor: Decimal = D(0)
    buy_budget: Decimal | None = None


@dataclass
class ReplayResult:
    daily: list[dict]
    events: list[dict]
    fills: list[dict]


class ReplaySignalPolicy(Protocol):
    def target(self, row: ReplayDay, principal_loss: Decimal) -> Decimal: ...


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


class AccountReplay:
    def __init__(self, config: ReplayConfig):
        self.config = config

    def run(
        self,
        rows: list[ReplayDay],
        *,
        reserve_policy: ReservePolicy | None = None,
        signal_policy: ReplaySignalPolicy | None = None,
    ) -> ReplayResult:
        cfg = self.config
        if reserve_policy and signal_policy:
            raise ValueError("choose exactly one research signal policy")
        if reserve_policy and (cfg.recipe != "2x-cash-v1" or cfg.mode != "existing"):
            raise ValueError("reserve policy requires gated 2x-cash research configuration")
        if reserve_policy and (
            cfg.loss_gate_basis != "principal-and-drawdown"
            or any(r.contribution or r.reserved_cash for r in rows)
        ):
            raise ValueError("reserve policy requires original risk and capital semantics")
        recipe = OneXTwoXRecipe() if cfg.recipe == "1x-2x-v1" else TwoXCashRecipe()
        if not rows or any(a.day >= b.day for a, b in zip(rows, rows[1:], strict=False)):
            raise ValueError("replay requires strictly increasing dates")
        for row in rows:
            if any(
                not value.is_finite() or value < 0
                for value in (row.contribution, row.reserved_cash)
            ):
                raise ValueError("nonnegative finite cash flow and reserve required")
            if reserve_policy and (row.policy_inputs is None or row.policy_inputs.as_of != row.day):
                raise ValueError("reserve policy inputs must match the decision date")
            if set(row.quotes) != set(SYMBOLS) or not row.target.is_finite():
                raise ValueError("complete quotes and finite signal required")
            if not row.rv20.is_finite() or row.rv20 < 0:
                raise ValueError("finite nonnegative realized volatility required")
            if not 0 <= row.target <= 2:
                raise ValueError("signal requires an explicit cash-account variant")
            if any(
                not q.close.is_finite() or q.close <= 0 or q.volume < 0 for q in row.quotes.values()
            ):
                raise ValueError("invalid historical quote")
            if any(not s.is_finite() or s <= 0 for s in row.splits.values()):
                raise ValueError("invalid split")
            if any(
                not amount.is_finite() or amount < 0 or payment < row.day
                for amount, payment in row.dividends.values()
            ):
                raise ValueError("invalid dividend")
        cash = cfg.initial_nav
        holdings = {symbol: 0 for symbol in SYMBOLS}
        receivables: list[tuple[int | date, Decimal, str]] = []
        peak, maximum = cash, D(0)
        previous_nav = cash
        net_contributions = cash
        principal_limit_latched = False
        pending = None
        retry = False
        last_normal_week = None
        defensive_target = D(0)
        recovery_streak = 0
        recovery_week = None
        result = ReplayResult([], [], [])
        gate = RiskGate()
        first_breach = None
        reserve_state = reserve_policy.initial_state() if reserve_policy else None

        def fee(notional: Decimal) -> Decimal:
            return max(cfg.minimum_commission, notional * cfg.commission)

        for index, row in enumerate(rows):
            daily_cost = D(0)
            paid = D(0)
            interest = D(0)
            if index and cfg.cash_interest_rate:
                elapsed = (row.day - rows[index - 1].day).days
                factor = (1 + cfg.cash_interest_rate) ** (D(elapsed) / D("365.2425"))
                interest = cash * (factor - 1)
                cash += interest
            cash += row.contribution
            net_contributions += row.contribution
            if row.contribution:
                # Contributions arrive at the start of the session. Rescale the NAV
                # high-water mark so depositing money cannot erase performance drawdown.
                peak *= (previous_nav + row.contribution) / previous_nav
                result.events.append(
                    {"date": row.day, "kind": "contribution", "amount": row.contribution}
                )
            for symbol, factor in row.splits.items():
                quantity = D(holdings[symbol]) * factor
                if quantity != quantity.to_integral_value():
                    raise ValueError("fractional split entitlement needs an explicit cash rule")
                holdings[symbol] = int(quantity)
                if pending:
                    pending.orders = [
                        (s, side, int(D(q) * factor) if s == symbol else q)
                        for s, side, q in pending.orders
                    ]
            for symbol, (amount, payment_day) in row.dividends.items():
                entitlement = D(holdings[symbol]) * amount
                if entitlement:
                    receivables.append((payment_day, entitlement, "dividend"))
                    result.events.append(
                        {
                            "date": row.day,
                            "kind": "dividend_entitlement",
                            "amount": entitlement,
                            "payment_date": payment_day,
                        }
                    )
            remaining = []
            for due, amount, kind in receivables:
                available = due <= index if isinstance(due, int) else due <= row.day
                if available:
                    cash += amount
                    paid += amount
                    result.events.append(
                        {"date": row.day, "kind": kind + "_paid", "amount": amount}
                    )
                else:
                    remaining.append((due, amount, kind))
            receivables = remaining

            if pending and pending.due <= index:
                # Operational faults can prevent a queued fill. Market/account decisions
                # were made after the signal close; no today's-close loss look-ahead here.
                blocked = row.manual_halt or not row.broker_safe or not row.account_eligible
                retry = False
                for symbol, side, requested in pending.orders:
                    quote = row.quotes[symbol]
                    reason = None
                    if blocked:
                        reason = "OPERATIONAL_GATE_CLOSED"
                    elif (not row.data_safe or row.etf_abnormal) and side == "BUY":
                        # Conservative execution assumption: no buys while data is unsafe.
                        reason = "UNSAFE_BUY_BLOCKED"
                    if quote.volume <= 0 or not (
                        quote.buy_allowed if side == "BUY" else quote.sell_allowed
                    ):
                        reason = "MARKET_UNFILLABLE"
                    quantity = min(requested, _floor(D(quote.volume) * cfg.volume_fraction))
                    price = quote.close * (1 + cfg.slippage if side == "BUY" else 1 - cfg.slippage)
                    if side == "BUY":
                        spending_cash = max(cash - pending.cash_floor, D(0))
                        if pending.buy_budget is not None:
                            spending_cash = min(spending_cash, pending.buy_budget)
                        affordable = min(
                            _floor(max(spending_cash - cfg.minimum_commission, D(0)) / price),
                            _floor(spending_cash / (price * (1 + cfg.commission))),
                        )
                        quantity = min(quantity, affordable)
                    else:
                        quantity = min(quantity, holdings[symbol])
                    if reason:
                        quantity = 0
                    if quantity <= 0:
                        retry = True
                        result.events.append(
                            {
                                "date": row.day,
                                "signal_date": pending.signal_date,
                                "kind": "unfilled",
                                "symbol": symbol,
                                "side": side,
                                "reason": reason or "CASH_OR_VOLUME_LIMIT",
                            }
                        )
                        continue
                    gross = D(quantity) * price
                    commission = fee(gross)
                    tax = gross * cfg.sell_tax if side == "SELL" else D(0)
                    slippage_cost = D(quantity) * abs(price - quote.close)
                    if side == "BUY":
                        cash -= gross + commission
                        if pending.buy_budget is not None:
                            pending.buy_budget -= gross + commission
                        holdings[symbol] += quantity
                    else:
                        proceeds = gross - commission - tax
                        if proceeds < 0:
                            result.events.append(
                                {
                                    "date": row.day,
                                    "kind": "unfilled",
                                    "symbol": symbol,
                                    "side": side,
                                    "reason": "PROCEEDS_BELOW_MINIMUM_FEE",
                                }
                            )
                            continue
                        holdings[symbol] -= quantity
                        if cfg.settlement_delay:
                            receivables.append((index + cfg.settlement_delay, proceeds, "sale"))
                        else:
                            cash += proceeds
                    daily_cost += commission + tax + slippage_cost
                    retry |= quantity < requested
                    result.fills.append(
                        {
                            "date": row.day,
                            "signal_date": pending.signal_date,
                            "symbol": symbol,
                            "side": side,
                            "quantity": quantity,
                            "requested_quantity": requested,
                            "price": price,
                            "commission": commission,
                            "tax": tax,
                            "slippage_cost": slippage_cost,
                        }
                    )
                    if reserve_policy and side == "BUY":
                        result.events.append(
                            {
                                "date": row.day,
                                "signal_date": pending.signal_date,
                                "kind": "reserve_buy_fill",
                                "spent": gross + commission,
                                "cash_floor": pending.cash_floor,
                                "cash_after": cash,
                                "budget_remaining": pending.buy_budget,
                            }
                        )
                pending = None
            if cash < 0 or any(q < 0 for q in holdings.values()):
                raise AssertionError("cash-account simulation borrowed or shorted")
            unsettled = sum((amount for _, amount, _ in receivables), D(0))
            nav = (
                cash
                + unsettled
                + sum((D(holdings[s]) * row.quotes[s].close for s in SYMBOLS), D(0))
            )
            peak, _, loss, drawdown, maximum = _loss_metrics(
                nav=nav,
                net_contributions=net_contributions,
                prior_peak=peak,
                prior_maximum_drawdown=maximum,
            )
            exposure = (
                sum(
                    (
                        D(holdings[s]) * row.quotes[s].close * (1 if s == "0050" else 2)
                        for s in SYMBOLS
                    ),
                    D(0),
                )
                / nav
            )
            principal_limit_latched |= loss >= D("0.4")
            breached = principal_limit_latched or (
                cfg.loss_gate_basis == "principal-and-drawdown" and maximum >= D("0.4")
            )
            if breached and first_breach is None:
                first_breach = row.day
                result.events.append(
                    {
                        "date": row.day,
                        "kind": "capital_limit_first_breach",
                        "drawdown": drawdown,
                        "principal_loss": loss,
                    }
                )
            signal = signal_policy.target(row, loss) if signal_policy else row.target
            if not signal.is_finite() or not 0 <= signal <= 2:
                raise ValueError("research policy produced an invalid target")
            target = min(signal, cfg.ceiling)
            reserve_decision = None
            if reserve_policy:
                reserve_decision = reserve_policy.evaluate(
                    row.policy_inputs,
                    ReserveAccount(
                        nav,
                        cash,
                        exposure,
                        drawdown,
                        breached,
                        cfg.initial_nav,
                        row.market_recovery_ready,
                    ),
                    reserve_state,
                )
                reserve_state = reserve_decision.state
                target = min(reserve_decision.target, cfg.ceiling)
                result.events.append(
                    {
                        "date": row.day,
                        "kind": "reserve_decision",
                        "desired": reserve_decision.desired,
                        "target": target,
                        "can_trade": reserve_decision.can_trade,
                        "cash_floor": reserve_decision.cash_floor,
                        "buy_budget": reserve_decision.buy_budget,
                        "paused": reserve_state.paused,
                        "clear_days": reserve_state.clear_days,
                        "reasons": reserve_decision.reasons,
                        "pe_ttm": row.policy_inputs.pe_ttm,
                        "pe_percentile": row.policy_inputs.pe_percentile,
                        "eps_yoy_proxy": row.policy_inputs.eps_yoy,
                        "rv20": row.policy_inputs.rv20,
                        "index_daily_return": row.policy_inputs.taiex_daily_return,
                    }
                )
                if hasattr(reserve_decision, "risk_floor"):
                    result.events[-1].update(
                        {
                            "mode": reserve_state.mode,
                            "risk_floor": reserve_decision.risk_floor,
                            "risk_exposure_cap": reserve_decision.risk_exposure_cap,
                            "drawdown_exposure_cap": reserve_decision.drawdown_exposure_cap,
                            "market_recovery_ready": row.market_recovery_ready,
                        }
                    )
            if cfg.mode == "defensive":
                healthy = row.above_trend and row.rv20 < D("0.30")
                recovery_streak = recovery_streak + 1 if healthy else 0
                cap = min(cfg.ceiling, D("0.20") / row.rv20) if row.rv20 > 0 else cfg.ceiling
                if not row.above_trend:
                    cap = min(cap, D("0.5"))
                if row.rv20 >= D("0.35"):
                    cap = D(0)
                proposed = min(target, cap)
                week = row.day.isocalendar()[:2]
                if proposed <= defensive_target:
                    defensive_target = proposed
                elif recovery_streak >= 5 and recovery_week != week:
                    defensive_target = min(proposed, defensive_target + D("0.2"))
                    recovery_week = week
                target = defensive_target
            context = RiskContext(
                requested_exposure=target,
                actual_exposure=exposure,
                strategy_state=SafetyState.NORMAL if reserve_policy else row.strategy_state,
                data_safe=row.data_safe,
                broker_safe=row.broker_safe,
                etf_abnormal=row.etf_abnormal,
                manual_halt=row.manual_halt,
                account_eligible=row.account_eligible,
                capital_loss_limit_breached=breached,
            )
            decision = gate.evaluate(context)
            gated = cfg.mode in {"existing", "defensive"}
            allowed = decision.allowed_exposure if gated else target
            approved = decision.approved if gated else True
            reasons = decision.reason_codes if gated else ("RESEARCH_GATE_BYPASS",)
            if gated and not approved:
                result.events.append(
                    {
                        "date": row.day,
                        "kind": "risk_rejected",
                        "reasons": reasons,
                        "requested": target,
                        "actual": exposure,
                    }
                )
            week = row.day.isocalendar()[:2]
            reduction = allowed < exposure - D("0.001")
            gap = abs(allowed - exposure)
            urgent = reduction and (
                (gated and decision.risk_reducing_only)
                or cfg.mode == "defensive"
                or exposure > cfg.ceiling + D("0.001")
            )
            eligible = retry or (
                gap >= cfg.no_trade_band and (reduction or week != last_normal_week)
            )
            if cfg.mode == "buyhold":
                eligible = (
                    not any(holdings.values())
                    or paid > 0
                    or retry
                    or row.scheduled_investment
                    or row.contribution > 0
                )
            elif row.contribution:
                eligible = True
            if reserve_decision:
                eligible = reserve_decision.can_trade
            if (
                pending
                and allowed < pending.target - D("0.001")
                and pending.target > exposure + D("0.001")
            ):
                # Cancel a queued increase when risk worsens. Do not keep resetting
                # the due date of an already queued reduction as the market falls.
                result.events.append(
                    {
                        "date": row.day,
                        "kind": "superseded_intent",
                        "old_signal_date": pending.signal_date,
                    }
                )
                pending = None
                urgent = True
            policy_permits = reserve_decision is None or reserve_decision.can_trade
            if approved and policy_permits and not pending and (eligible or urgent):
                weights = {s: D(0) for s in SYMBOLS}
                weights.update({a.instrument: a.weight for a in recipe.allocate(allowed)})
                allocation_nav = max(nav - row.reserved_cash, D(0))
                estimate = D(0)
                for symbol in SYMBOLS:
                    delta = (
                        allocation_nav * weights[symbol]
                        - D(holdings[symbol]) * row.quotes[symbol].close
                    )
                    if abs(delta) >= row.quotes[symbol].close:
                        estimate += fee(abs(delta)) + abs(delta) * cfg.slippage
                        if delta < 0:
                            estimate += abs(delta) * cfg.sell_tax
                budget = max(allocation_nav - estimate, D(0))
                orders = []
                for symbol in SYMBOLS:
                    desired = _floor(budget * weights[symbol] / row.quotes[symbol].close)
                    delta = desired - holdings[symbol]
                    if delta:
                        orders.append((symbol, "BUY" if delta > 0 else "SELL", abs(delta)))
                # Deterministic order: reductions before buys; sale proceeds remain unavailable.
                orders.sort(key=lambda order: (order[1] != "SELL", order[0]))
                if orders:
                    pending = Pending(
                        row.day,
                        index + cfg.execution_delay,
                        allowed,
                        orders,
                        reserve_decision.cash_floor if reserve_decision else row.reserved_cash,
                        reserve_decision.buy_budget if reserve_decision else None,
                    )
                    last_normal_week = week
                    result.events.append(
                        {
                            "date": row.day,
                            "kind": "intent",
                            "target": allowed,
                            "due_index": pending.due,
                            "orders": orders,
                            "risk_reasons": reasons,
                        }
                    )
                    if reserve_decision:
                        result.events[-1].update(
                            {
                                "reserve_cash_floor": pending.cash_floor,
                                "reserve_buy_budget": pending.buy_budget,
                                "reserve_reasons": reserve_decision.reasons,
                            }
                        )
                retry = False
            result.daily.append(
                {
                    "date": row.day,
                    "nav": nav,
                    "cash": cash,
                    "receivables": unsettled,
                    "quantity_0050": holdings["0050"],
                    "quantity_00631L": holdings["00631L"],
                    "target": target,
                    "allowed_target": allowed,
                    "actual_exposure": exposure,
                    "daily_return": nav / (previous_nav + row.contribution) - 1,
                    "cost": daily_cost,
                    "drawdown": drawdown,
                    "maximum_drawdown": maximum,
                    "principal_loss": loss,
                    "capital_limit_latched": breached,
                    "principal_limit_latched": principal_limit_latched,
                    "loss_gate_basis": cfg.loss_gate_basis,
                    "net_contributions": net_contributions,
                    "contribution": row.contribution,
                    "cash_interest": interest,
                    "reserved_cash": row.reserved_cash,
                    "risk_reasons": "|".join(reasons),
                }
            )
            previous_nav = nav
        return result
