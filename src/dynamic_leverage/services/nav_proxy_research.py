"""Research-only single-product ledger using explicitly hypothetical NAV fills.

No volume is invented and no production execution type is reused. Investor costs,
integer units, payment dates and settlement are modeled; liquidity, executable
market price and NAV publication clocks remain unverified.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR
from decimal import Decimal as D


@dataclass(frozen=True)
class NavPoint:
    day: date
    price: D
    price_as_of: date
    fill_available: bool = True
    market_volatility: D | None = None
    split: D = D(1)
    dividend: tuple[D, date] | None = None


@dataclass(frozen=True)
class ProxyConfig:
    initial: D = D(500000)
    delay: int = 1
    cost_multiplier: D = D(1)
    commission: D = D("0.001425")
    minimum_fee: D = D(20)
    sell_tax: D = D("0.001")
    slippage: D = D("0.0005")
    settlement: int = 2
    no_trade_band: D = D("0.15")

    def __post_init__(self):
        if self.initial <= 0 or self.delay < 1 or self.settlement < 1:
            raise ValueError("positive capital and conservative timing required")
        if (
            any(
                not value.is_finite() or value < 0
                for value in (
                    self.initial,
                    self.cost_multiplier,
                    self.commission,
                    self.minimum_fee,
                    self.sell_tax,
                    self.slippage,
                    self.no_trade_band,
                )
            )
            or self.cost_multiplier == 0
        ):
            raise ValueError("finite nonnegative proxy costs required")
        if self.slippage * self.cost_multiplier >= 1:
            raise ValueError("proxy slippage cannot exhaust price")


def simulate(points: list[NavPoint], mode: str, cfg: ProxyConfig, *, exposure_multiple=D(2)):
    if mode not in {"buyhold", "static-1.2", "vol20"} or exposure_multiple not in {D(1), D(2)}:
        raise ValueError("declared proxy mode and instrument exposure required")
    if not points or any(b.day <= a.day for a, b in zip(points, points[1:], strict=False)):
        raise ValueError("nonempty unique chronological valuation calendar required")
    for point in points:
        if not point.price.is_finite() or point.price <= 0 or point.price_as_of > point.day:
            raise ValueError("positive nonfuture price evidence required")
        if not point.split.is_finite() or point.split <= 0:
            raise ValueError("positive split factor required")
        if point.market_volatility is not None and (
            not point.market_volatility.is_finite() or point.market_volatility < 0
        ):
            raise ValueError("finite observed volatility required")
    cash, units, peak = cfg.initial, 0, cfg.initial
    sales, dividends, daily, fills = [], [], [], []
    pending = None
    stop_day = None
    max_dd = max_principal = D(0)
    total_cost, previous_nav = D(0), cfg.initial
    commission = cfg.commission * cfg.cost_multiplier
    min_fee = cfg.minimum_fee * cfg.cost_multiplier
    tax = cfg.sell_tax * cfg.cost_multiplier
    slip = cfg.slippage * cfg.cost_multiplier

    for index, point in enumerate(points):
        # Split and ex-dividend entitlement apply to units already owned.
        new_units = D(units) * point.split
        if new_units != new_units.to_integral_value():
            raise ValueError("fractional corporate-action entitlement unsupported")
        units = int(new_units)
        if point.dividend:
            amount, payment = point.dividend
            if not amount.is_finite() or amount < 0 or payment < point.day:
                raise ValueError("nonnegative dividend and actual future payment required")
            dividends.append((payment, D(units) * amount))
        cash += sum((value for due, value in sales if due <= index), D(0))
        sales = [(due, value) for due, value in sales if due > index]
        cash += sum((value for due, value in dividends if due <= point.day), D(0))
        dividends = [(due, value) for due, value in dividends if due > point.day]

        nav = cash + D(units) * point.price + sum((v for _, v in sales + dividends), D(0))
        if (
            mode != "buyhold"
            and stop_day is None
            and (1 - nav / peak >= D("0.4") or 1 - nav / cfg.initial >= D("0.4"))
        ):
            stop_day = point.day
            pending = (index + cfg.delay, D(0), point.day)
        if pending and pending[0] <= index and point.fill_available:
            _, weight, signal_day = pending
            target_units = int((weight * nav / point.price).to_integral_value(rounding=ROUND_FLOOR))
            change = target_units - units
            quantity, side = 0, None
            if change < 0:
                quantity, side = -change, "SELL"
                notional = D(quantity) * point.price * (1 - slip)
                fee = max(min_fee, notional * commission)
                sell_tax = notional * tax
                proceeds = notional - fee - sell_tax
                if proceeds > 0:
                    units -= quantity
                    sales.append((index + cfg.settlement, proceeds))
                    trade_cost = fee + sell_tax + D(quantity) * point.price * slip
                else:
                    quantity = 0
            elif change > 0:
                side = "BUY"
                fill_price = point.price * (1 + slip)
                # Bound by settled cash. Receivables never finance purchases.
                quantity = min(
                    change,
                    max(
                        0,
                        int(
                            (cash / (fill_price * (1 + commission))).to_integral_value(
                                rounding=ROUND_FLOOR
                            )
                        ),
                    ),
                )
                while (
                    quantity
                    and D(quantity) * fill_price
                    + max(min_fee, D(quantity) * fill_price * commission)
                    > cash
                ):
                    quantity -= 1
                if quantity:
                    notional = D(quantity) * fill_price
                    fee = max(min_fee, notional * commission)
                    cash -= notional + fee
                    units += quantity
                    trade_cost = fee + D(quantity) * point.price * slip
            if quantity:
                total_cost += trade_cost
                fills.append(
                    {
                        "date": point.day,
                        "signal_date": signal_day,
                        "side": side,
                        "units": quantity,
                        "cost": trade_cost,
                        "fill_model": "hypothetical-nav-or-benchmark-close",
                    }
                )
            pending = None
        receivable_value = sum((v for _, v in sales + dividends), D(0))
        nav = cash + D(units) * point.price + receivable_value
        if cash < 0 or units < 0 or nav <= 0:
            raise ValueError("cash-only proxy solvency invariant failed")
        peak = max(peak, nav)
        max_dd = max(max_dd, 1 - nav / peak)
        max_principal = max(max_principal, 1 - nav / cfg.initial)
        if (
            mode != "buyhold"
            and stop_day is None
            and (max_dd >= D("0.4") or max_principal >= D("0.4"))
        ):
            stop_day = point.day
            pending = (index + cfg.delay, D(0), point.day)
        requested = (
            D(0)
            if stop_day
            else (
                D(1)
                if mode == "buyhold"
                else D("0.6")
                if mode == "static-1.2"
                else None
                if point.market_volatility is None
                else D("0.6")
                if point.market_volatility < D("0.2")
                else D("0.4")
            )
        )
        actual_weight = D(units) * point.price / nav
        if pending is None and requested is not None and point.fill_available:
            # A buy-and-hold benchmark reinvests paid distributions without the
            # strategy's allocation buffer. Unpaid entitlements cannot be used.
            can_reinvest = mode == "buyhold" and cash >= (
                point.price * (1 + slip) + max(min_fee, point.price * (1 + slip) * commission)
            )
            if (
                can_reinvest
                or (units == 0 and requested > 0)
                or (stop_day and units > 0)
                or abs((requested - actual_weight) * exposure_multiple) >= cfg.no_trade_band
            ):
                pending = (index + cfg.delay, requested, point.day)
        daily.append(
            {
                "date": point.day,
                "nav": nav,
                "cash": cash,
                "receivables": receivable_value,
                "units": units,
                "price": point.price,
                "price_as_of": point.price_as_of,
                "proxy_fill_available": point.fill_available,
                "daily_return": nav / previous_nav - 1,
                "maximum_drawdown": max_dd,
                "principal_loss": max(D(0), 1 - nav / cfg.initial),
                "exposure": actual_weight * exposure_multiple,
                "stop_signal_date": stop_day,
            }
        )
        previous_nav = nav
    years = (points[-1].day - points[0].day).days / 365.2425
    returns = [float(row["daily_return"]) for row in daily]
    metrics = {
        "start": points[0].day,
        "end": points[-1].day,
        "final_nav": daily[-1]["nav"],
        "calendar_cagr": float(daily[-1]["nav"] / cfg.initial) ** (1 / years) - 1
        if years
        else None,
        "annualized_volatility": statistics.stdev(returns) * math.sqrt(252)
        if len(returns) > 1
        else 0,
        "maximum_drawdown": max_dd,
        "maximum_principal_loss": max_principal,
        "fill_count": len(fills),
        "total_cost": total_cost,
        "stop_signal_date": stop_day,
        "mean_actual_exposure": statistics.mean(float(row["exposure"]) for row in daily),
        "execution_evidence": "proxy-not-executable-price-backtest",
        "profitability_demonstrated": False,
    }
    return metrics, daily, fills
