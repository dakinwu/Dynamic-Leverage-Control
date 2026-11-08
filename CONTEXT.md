# Dynamic Leverage Control

This context defines the language used to control market exposure, compare strategy candidates, and safely progress from monitoring to autonomous execution.

## Portfolio boundaries

**Strategy Capital Budget**:
The amount the owner has committed to make available to this strategy; it is a funding decision, not the observed value of its assets after gains or losses.
_Avoid_: Strategy NAV, observed balance

**Strategy Principal Loss**:
The shortfall of Strategy NAV below the strategy's net contributed capital, including unrealized losses; with no subsequent contributions or withdrawals, its reference is the initial contributed capital.
_Avoid_: Peak drawdown, realized trading loss

**Strategy Peak Drawdown**:
The percentage decline of the strategy's investment value from its previous high, measured independently of loss relative to contributed capital; contributions and withdrawals are not investment gains or losses.
_Avoid_: Strategy Principal Loss, index drawdown

**Strategy Account**:
The dedicated brokerage-account boundary whose positions, cash, settlements, and orders are controlled by the platform.
_Avoid_: Total wealth, household portfolio

**Strategy NAV**:
The marked value of all assets and liabilities inside the Strategy Account.
_Avoid_: Net worth, available cash

**Strategy Cash**:
Settlement-ready TWD held inside the Strategy Account and available to satisfy orders and pending settlements; borrowed money and cash-like securities are not Strategy Cash.
_Avoid_: Household reserve, money-market position

**Observed Account State**:
The latest reconciled positions, Strategy Cash, settlements, and NAV obtained from Read-only Broker Sync for the explicitly selected Strategy Account.
_Avoid_: Planning NAV, configured balance, assumed holdings

**Strategy Account Binding**:
The explicit allowlist relationship between this platform and exactly one broker account; a missing, ambiguous, or mismatched account is Broker Unsafe.
_Avoid_: First available account, currently selected account

**Household Reserve**:
Funds reserved for living expenses and emergencies outside the Strategy Capital Budget and excluded from every strategy exposure and performance calculation; their amount is a separate household decision.
_Avoid_: Strategy cash, dry powder

## Exposure language

**Target Exposure**:
The desired market exposure expressed as a multiple of Strategy NAV, bounded between 0.0x (cash) and the Exposure Ceiling for the accepted research scope; a frozen Strategy Revision may impose a narrower range.
_Avoid_: Beta, leverage ratio

**Actual Exposure**:
The point-in-time market exposure of current positions relative to Strategy NAV, using the instrument's observed or officially reported exposure when available.
_Avoid_: Target beta, allocation percentage

**Realized Beta**:
A backward-looking regression estimate of the Strategy Account's sensitivity to its benchmark; it is a monitoring statistic, not the exposure control variable.
_Avoid_: Target Exposure, allocation

**Exposure Ceiling**:
The non-bypassable 2.0x maximum on Target Exposure and permitted Actual Exposure; the initial live canary may impose a lower ceiling.
_Avoid_: Suggested maximum, strategy parameter

## Decision and safety

**Operator Console**:
The private, exception-driven control surface that tells the operator whether the platform is safe, whether action is required, and what evidence supports that conclusion.
_Avoid_: Monitoring dashboard, analytics dashboard, trading terminal

**Operator Status**:
The single highest-severity operational conclusion shown by the Operator Console: Normal, Attention, Action Required, or Blocked, with every contributing reason preserved.
_Avoid_: Collection of status lights, health score

**Planning Mode**:
A clearly labelled fallback state that may display planning assumptions when Observed Account State is unavailable; its values cannot be presented as broker truth or authorize an exposure increase.
_Avoid_: Live account, estimated account

**Operator Queue**:
The ordered set of unresolved safety changes, reconciliation failures, and decisions that require human acknowledgement or action; items move through Open, Acknowledged, Resolved, and auditable Reopened transitions. Telegram only announces changes to this queue.
_Avoid_: Notification feed, activity log, all alerts

**Freshness Contract**:
The source-specific validity window for an authoritative fact; there is no platform-wide timestamp that can make differently timed data valid.
_Avoid_: Last updated, updated today

**Reconciliation Break**:
A mismatch between Observed Account State and the platform ledger, including an unknown position or external transaction; it makes the broker state unsafe until explained and reconciled.
_Avoid_: Harmless difference, ignored holding

**Acknowledgement**:
An auditable statement that the operator has seen an Operator Queue item; it never changes the underlying safety condition.
_Avoid_: Resolve, dismiss, override

**Resolution**:
The evidence-backed transition that closes an Operator Queue item after its underlying condition has actually cleared.
_Avoid_: Acknowledge, hide alert

**Strategy Run**:
An immutable execution of one Strategy Revision against identified input snapshots, triggered by the schedule or explicitly marked as manual.
_Avoid_: Strategy Revision, overwritten result, current calculation

**Paper Evidence Chain**:
The replayable lineage from Strategy Run through Trade Intent, Risk Decision, Paper Order, Fill, settlement, and reconciliation.
_Avoid_: Paper balance, simulated return, trade history

**Strategy Policy**:
A deterministic, versioned, and replayable rule set that transforms point-in-time data into a Target Exposure.
_Avoid_: AI opinion, discretionary signal

**Strategy Candidate**:
An independently evaluated Strategy Policy that never shares intermediate decisions with another candidate.
_Avoid_: Combined strategy, ensemble

**Execution Recipe**:
A versioned mapping from Target Exposure to instrument weights; different recipes may express the same initial exposure while producing different costs and return paths.
_Avoid_: Strategy Policy, portfolio beta

The current primary recipe is piecewise and bounded by the cash-account ceiling: for `0.0x–1.0x`, hold `L` in 0050 and `1-L` in cash; for `1.0x–2.0x`, hold `2-L` in 0050 and `L-1` in 00631L. This is a Paper/Shadow allocation model, not a broker order quantity.

**Investable Benchmark**:
The after-cost total return of a real 1x ETF that the Strategy Account could have held instead.
_Avoid_: Economic reference, price index

**Economic Reference**:
The TAIEX total-return series used to understand market economics without claiming that it was directly investable at the reported return.
_Avoid_: Investable Benchmark, TAIEX price index

**Authoritative Data**:
A point-in-time input approved to influence the Trade-authoritative Strategy Policy, with provenance, observation time, publication time, and ingestion time preserved.
_Avoid_: Latest value, scraped value

**Supplemental Data**:
An input permitted for historical completion, comparison, or alerting but forbidden from independently authorizing a real-money exposure increase.
_Avoid_: Authoritative Data, fallback truth

**Trade Intent**:
A proposed portfolio adjustment derived from a Strategy Policy before safety checks or broker submission.
_Avoid_: Order, fill

The overview's “紙上調整預覽” is an operator confirmation step that creates a Trade Intent and Risk Decision evidence record. It is not a fill, settlement, or production order.

**Risk Gate**:
The authority independent of every Strategy Policy that may approve, reduce, defer, or reject a Trade Intent.
_Avoid_: Strategy filter, recommendation

**Panic Lock**:
A safety state that permits maintaining or reducing exposure but forbids increasing it until its release conditions are satisfied.
_Avoid_: Stop loss, market prediction

**Manual Halt**:
A persistent operator-controlled safety state that stops new automated intents while data collection and reconciliation continue; it never causes an automatic liquidation.
_Avoid_: Stop loss, shut down, sell everything

**Data Unsafe**:
A safety state caused by missing, stale, contradictory, or unverifiable critical data; it forbids increasing exposure.
_Avoid_: No signal, neutral market

**ETF Abnormal**:
A safety state in which a leveraged ETF's official exposure, tradability, tracking, or rebalancing status is outside the permitted envelope.
_Avoid_: Volatile market, discount opportunity

**Broker Unsafe**:
A safety state in which connection, order, fill, position, or settlement truth cannot be reconciled with the broker; it forbids every new order until reconciliation succeeds and the state is acknowledged.
_Avoid_: Temporary timeout, retryable order

**Eligibility Gate**:
The account-level prerequisite proving that required agreements, qualifications, and certificates permit a particular instrument and execution maturity.
_Avoid_: Strategy signal, broker login

**Promotion Gate**:
The evidence threshold a Strategy Candidate must pass before it may advance to a more authoritative execution maturity.
_Avoid_: Deployment approval, best backtest

**Strategy Revision**:
A new immutable version created whenever rules, parameters, or authoritative data semantics change; historical versions remain reproducible.
_Avoid_: Configuration edit, corrected backtest

**Trade-authoritative Strategy Policy**:
The single promoted Strategy Policy whose approved outputs may create real-money Trade Intents; every other candidate remains observational.
_Avoid_: Best current backtest, strategy ensemble

## Execution maturity

**Shadow Signal**:
A recorded Strategy Policy output that is monitored and scored without creating broker orders.
_Avoid_: Paper trade, recommendation

**Paper Execution**:
A planned deterministic local simulation of orders, fills, odd lots, costs, slippage, and settlement without broker-side trades. The current MVP records the strategy/risk/intent portion; the Paper Order, Fill, cost, and settlement ledger are the next implementation stage.
_Avoid_: Broker sandbox

**Read-only Broker Sync**:
Production-broker access limited to contracts, quotes, positions, balances, settlements, and order history, with no production order-submission capability.
_Avoid_: Live Canary, sandbox execution

**Sandbox Execution**:
Order-protocol testing against a broker's simulation environment; it validates integration behavior but never supplies performance evidence.
_Avoid_: Paper Execution, Live Canary

**Live Canary**:
Small, capped real-money execution that still requires explicit human approval for each Trade Intent.
_Avoid_: Paper trade, autonomous trading

**Autonomous Trading**:
Unattended execution of a previously approved Strategy Policy within the Risk Gate and account limits; it never means discretionary trade selection by a language model.
_Avoid_: AI trading, discretionary automation

**Execution Adapter**:
A broker-specific boundary that translates approved Trade Intents into broker commands and reconciles broker events back into platform state.
_Avoid_: Strategy engine, broker truth
