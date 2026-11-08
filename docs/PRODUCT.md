# Dynamic Leverage Control MVP

> 文件性質：目標產品規格。這裡描述應達成的產品邊界與安全行為，不代表每一項能力目前都已完成；目前完成度請以 [PRODUCT-GAP-AUDIT.md](./PRODUCT-GAP-AUDIT.md) 為準。

> 2026-09-08 已確認需求：50 萬元專用投資資金、不借款、無強制提領期限、0–2 倍研究配置；每日風險檢查、每週正常調整建議，由使用者本人決定並在券商下單。本文已同步最新需求；[研究紀錄與重構驗收清單](./RESEARCH.md) 保存查證結果與取捨。程式設定及舊策略版本尚未遷移，新需求不代表功能已完成。

The accepted Operator Console information architecture, safety semantics, page responsibilities, and delivery phases are defined in [OPERATOR-CONSOLE.md](./OPERATOR-CONSOLE.md).

## Outcome

Build a private, auditable research and decision-support platform for a TWD 500,000 Strategy Capital Budget. Evaluate whether dynamic allocation can outperform holding 0050 after costs without greater measured volatility or maximum drawdown. Monitor daily risk and propose normal adjustments weekly; the owner decides and submits any real orders through the broker. The platform does not submit production orders.

## Goals

- Preserve point-in-time market, valuation, fund-exposure, account, and decision evidence.
- Run immutable strategy versions side by side without combining their intermediate logic.
- Keep Strategy Policy, Risk Gate, Execution Recipe, and broker integration as separate authorities.
- Backtest with actual costs, walk-forward evaluation, parameter sensitivity, and explicit synthetic-versus-actual labels.
- Reconcile a deterministic Paper Execution ledger and read-only Shioaji account state.
- Provide a Traditional Chinese responsive dashboard and Telegram alerts.

## Non-goals

- Production order submission.
- Intraday prediction or discretionary LLM trading.
- Public or multi-user market-data distribution.
- Borrowing, margin, securities-backed loans, or use of the Household Reserve.
- Treating a broker sandbox as a performance simulator.

## Portfolio and authority

- Strategy Capital Budget is TWD 500,000 of investment-only, unborrowed funds. It is not an assertion that the broker currently holds that amount or an instruction to invest it all immediately. There is no mandatory withdrawal deadline after two years.
- Strategy NAV follows attributable holdings, cash, liabilities, gains, losses, and contributions or withdrawals. The bound Shioaji account may contain external positions; those remain visible for audit but do not enter the strategy's assets or exposure denominator. A configured budget cannot substitute for observed Strategy NAV.
- The owner accepts at most TWD 200,000 of principal loss relative to the initial TWD 500,000, assuming no subsequent cash flows. Track this separately from Strategy Peak Drawdown. This is risk tolerance, not a guaranteed account floor or an instruction to wait for that loss before reducing risk.
- Household Reserve is outside the account and outside every calculation.
- Accepted research Target Exposure ranges from 0.0x (cash) to the 2.0x Exposure Ceiling. Automated real-order execution and Live Canary are outside the accepted delivery scope.
- Only one Strategy Policy may ever be Trade-authoritative at a time.
- Production Shioaji access is read-only; Shioaji simulation may test submit, update, cancel, callback, and reconciliation behavior.

## Frozen strategy candidates

| Candidate | Definition | MVP authority |
|---|---|---|
| DVL-TW V1.0 | The supplied absolute P/E, percentile, EPS modifier, RV20/VIX cap, Panic Lock, confirmation, and no-trade rules | Shadow and Paper only |
| DVL-TW V1.1 raw | The supplied rolling 120-month raw P/E percentile, RV20 cap, and Panic Lock rules | Shadow and Paper only |
| DVL-TW V1.1 winsorized | A separate immutable experimental version using the documented 5%/95% treatment | Shadow and Paper only |
| DVL-TW V2 Shadow | Article-inspired valuation/drawdown candidate with an explicit 3.2x research ceiling and 2.0x cash-account ceiling | Shadow and Paper only |
| Constant Exposure 1.2 | Target Exposure 1.2x with a 1.08x/1.32x rebalance band | Shadow and Paper; future Live Canary candidate |
| Benchmarks | 1.0x, fixed 1.2x, 2x buy-and-hold | Evaluation only |

Any change to a formula, parameter, timing rule, or data meaning creates a Strategy Revision rather than rewriting history.

The frozen candidates above retain their historical definitions. In particular, the 3.2x candidate is a legacy research artifact, not an accepted recommendation or an implementable 0–2x result; adapting it requires a new revision.

## Calculation conventions

- All timestamps use `Asia/Taipei`; a close-derived signal is effective no earlier than the next trading session.
- RV20 uses 20 simple close-to-close TAIEX price-index returns, sample standard deviation, and annualization by `sqrt(252)`.
- P/E percentile uses the previous 120 valid month-end observations, excluding the current month; ties use mid-rank and fewer than 120 values produce no signal.
- Exposure values round to 0.1x with decimal `ROUND_HALF_UP`.
- Synthetic histories, index histories, and actual ETF NAV histories are reported separately.

## Execution recipes

For Target Exposure `L` between 0.0x and 2.0x, the primary recipe is
piecewise:

- `0.0 ≤ L ≤ 1.0`: 0050 weight `L`; Strategy Cash weight `1 - L`; 00631L weight `0`.
- `1.0 < L ≤ 2.0`: 0050 weight `2 - L`; 00631L weight `L - 1`; Strategy Cash weight `0`.

The 2x-plus-cash recipe remains available as an explicit research comparison,
not as the primary target recipe.

The first primary instruments are 0050 and 00631L because their common Taiwan 50 basis reduces benchmark mismatch and their liquidity supports safer rebalancing. 00685L remains a separately evaluated lower-fee alternative and is never assumed to have exactly 2.0x Actual Exposure.

## Risk states

| State | Increase exposure | Reduce exposure | New broker orders |
|---|---:|---:|---:|
| Normal | Subject to policy and limits | Yes | According to execution maturity |
| Panic Lock | No | Yes | According to execution maturity |
| Data Unsafe | No | Yes when price and broker truth are safe | Risk-reducing only |
| ETF Abnormal | No purchase of affected ETF | Yes or substitute safe instrument | Risk-reducing only |
| Broker Unsafe | No | No until reconciliation | No |
| Manual Halt | No | No automatic liquidation | No; cancel open orders |

Actual Exposure above 2.0x creates a mandatory risk-reduction intent for the next legal session. Unknown order state is reconciled before any retry.

## Data authority

- TWSE, TAIFEX, issuer PCF/portfolio disclosures, and broker account events are primary authoritative sources for their owned facts.
- Monthly P/E/EPS imports are versioned with source, observed date, publication date, and effective date; while manually sourced they cannot authorize production trades.
- Taiwan VIX begins as monitoring-only until point-in-time history is validated.
- Supplemental aggregators such as FinMind may fill history or cross-check values but cannot independently authorize exposure increases.

## Operating schedule

- Normal adjustment recommendation: calculate weekly after Friday close. Show current and target weights, estimated costs, reasons, input timestamps, missing evidence, and research qualification. The owner decides whether to act and submits real orders personally.
- Daily risk: calculate after close and report principal loss, peak drawdown, exposure and data/account safety; any close-derived signal is effective no earlier than the next trading session.
- Risk-reduction recommendations may be issued outside the normal weekly review when daily checks identify a problem; they do not trigger automatic real orders.
- Paper execution retains next-session timing: normal simulation after 09:15, emergency simulation after 09:05, subject to data/account safety and price-bounded execution rules.
- No intraday forecasting and no unbounded market orders.

## Research acceptance and operating evidence

Fix data coverage, time splits, strategy revisions, costs and benchmark conventions before comparing results. On historical evaluation periods not used to tune the candidate, after-cost return must exceed holding 0050; annualized return volatility and maximum drawdown magnitude must each be no greater than 0050 over the same periods. A peak drawdown magnitude above 40% rejects the candidate. Report principal-loss boundary breaches separately; passing the historical screen is not a guarantee of future returns or loss limits.

Compare holding 0050, fixed 1.2x allocation, holding 00631L, and the candidate using consistent cash-flow and cost treatment. Include walk-forward evaluation, parameter-neighborhood robustness, doubled-cost and delayed-execution scenarios, recovery duration, and rolling 24-month outcomes with sample counts and uncertainty. Report unrecovered drawdowns explicitly. No mandatory two-year recovery or liquidation rule is implied. If the evidence fails or is insufficient, report that no qualifying dynamic advantage was found.

The existing proposed operating checks—60 consecutive Shadow/Paper trading days, at least 20 verifiable rebalance or risk intents, failure drills, and 20 consecutive days of paper/accounting reconciliation—remain engineering evidence to build and review. They do not independently prove investment advantage or authorize live execution; do not manufacture trades to reach a count.

Any strategy or authoritative-data change returns the candidate to Shadow. Recommendations and simulated fills remain distinct from owner-submitted real orders. Autonomous execution would require a separate future scope decision.

## Private deployment and secrets

- Local Docker Compose modular monolith: Python, FastAPI, PostgreSQL, React/TypeScript, and Ruff; Black is not used.
- Localhost-only initially; later remote access uses a private network such as Tailscale.
- Secrets and CA material live outside the repository under `%LOCALAPPDATA%\\dynamic-leverage-2x\\secrets`, are mounted read-only, and are redacted from logs, errors, database rows, and backups.
- Daily encrypted database backups are retained, exclude secrets, and receive monthly restore tests.
