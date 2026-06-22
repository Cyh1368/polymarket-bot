# Task Spec: Parsimonious Mean-Reversion Model + Honest Profit Backtest — Polymarket BTC 5m

_Builds on all prior runs. Lockbox/registry/pre-registration discipline, day-clustered inference, purged walk-forward, and "don't invent schema" all carry over — this spec adds the model, the execution model, and the profit accounting. Scope: BTC (the only coin with an HF Kraken feed: `kraken_hf/trades_BTC_backfill.csv`)._

## 0. The principles this encodes
Every prior run says the same thing: the mid is calibrated, capacity overfits, and the one signal that recurs with a stable sign is **mean-reversion of recent spot returns at T≈180** (RTDS coef ≈ −90, HF `hf_ret_60s` coef ≈ −201, both negative). It is roughly spread-sized, so **this is a cost problem, not a prediction problem.** The build follows from that:

- **Tiny model.** Regularized logistic regression, ≤4 features, anchored on the mid. No LightGBM, no 100-feature sets — capacity is the enemy on 12 days.
- **Expanding window**, not short-rolling (recency overfits) and not frozen-forever (drift decays it).
- **Cost is the lever.** Backtest as both taker (pays spread) and maker (earns spread), because the signal only becomes profitable if the cost sign flips.
- **Profit, honestly.** Report net PnL/contract with day-clustered intervals; let the number be whatever it is.

## Hard rules
1. **Lockbox sealed.** Reuse the existing split (hash `40bac7e8…`, most-recent 5 days). All model fitting, feature choice, L2 selection, and execution-rule tuning happen on the non-lockbox days only. Open the lockbox exactly once, at the Final Gate, to confirm a single frozen configuration.
2. **Trial registry.** Log every variant evaluated — feature set, L2 value, horizon, SKIP threshold, taker/maker — and deflate the final claim by the count. Fixing the horizon at 180 a priori (below) is what keeps this count small; do not re-sweep all six horizons fresh.
3. **Few-cluster inference.** You have ~12 day-clusters. Ordinary cluster-robust SEs are anti-conservative with so few clusters — use a **wild cluster bootstrap** for all CIs and p-values, and say so. Never bootstrap below the day level.
4. **Maker-fill realism (the new overfitting trap).** A maker backtest that assumes free fills is fantasy. A resting order counts as filled only if the snapshot time series for that contract shows the price was actually reached before close (opposing best crosses your level, or a print reaches it). No fill assumed otherwise. Report the realized **fill rate** — an edge that needs a 90% fill rate it never achieves is not an edge.
5. **Pre-register** hypothesis, metric, and pass/fail thresholds before running. Report negatives plainly.

## Phase 1 — The model
- **Horizon:** T=180 primary (pre-justified by the recurring signal). Report T=120 and T=240 as robustness only, logged as trials.
- **Model:** logistic regression, `P(up) = σ(β₀ + β₁·logit(mid) + β₂·hf_ret_60s + β₃·OBI)`. Strong L2 set a priori. Expect β₁ ≈ 1 (mid is calibrated), β₂ < 0 (mean-reversion). Drop β₃ if it doesn't help OOS — fewer features is the default.
- **Decision rule (with cost baked in):** entry cost is already inside `c_yes = up_ask + 0.01`, `c_no = down_ask + 0.01`. Compute `EV_yes = P(up) − c_yes`, `EV_no = (1 − P(up)) − c_no`. Trade the side whose EV exceeds a threshold `τ_skip`; otherwise **SKIP**. `τ_skip` is the selectivity dial — trade only when the modeled edge clears realized cost plus margin.
- **HP search:** essentially none. Fix strong L2; if you must tune, ≤3 values selected by the Phase-2 walk-forward, deflated by the grid size. Tuning hard on 12 days just fits validation noise.

## Phase 2 — Overfitting-safe backtest and profit numbers
- **Scheme:** expanding-window purged day walk-forward. For each day d (after ≥4 train days): fit on all days < d, embargo the boundary, deploy the frozen model on day d. Strictly point-in-time; `logit(mid)` and `hf_ret_60s` computed only from pre-entry data.
- **Two execution models, same signal:**
  - **Taker:** enter by crossing the spread at `c_yes`/`c_no`. This is the pessimistic, always-available case.
  - **Maker:** post a limit order at the mid (or one tick inside); realize the better price **only if Rule 4's fill check passes**; otherwise no trade that contract.
- **Profit metric:** net PnL per contract = settlement payoff − realized entry cost, SKIPs = 0 in the denominator (profit per available contract). Report, for taker and maker separately: mean net PnL/contract, **day-clustered wild-bootstrap CI**, trade rate, and (maker) fill rate. Aggregate over the non-lockbox days.
- Deflate the headline number by the registry trial count.

## Phase 3 — No-arb check (model-free, parallel, highest-EV)
Independently of the model: scan `up_ask_plus_down_ask` and `up_bid_plus_down_bid` across all BTC contracts. Flag every moment `up_ask + down_ask < 1 − fees` (buy both, locked profit) or `up_bid + down_bid > 1 + fees` (sell both). For each, compute capturable size from the `tau`-depth columns and net profit after fees. Report frequency, total capturable profit, and per-event size. This needs no training and no features.

## Final Gate
Open the lockbox once, only to confirm the single frozen configuration that passed Phase 2 (positive deflated net PnL/contract with a wild-bootstrap CI lower bound above zero — most plausibly the maker case). Report the lockbox net PnL/contract with its CI. If nothing cleared zero in Phase 2, do not open the lockbox; report the negative.

## Deliverables
```
results/profit_backtest/
  preregistration.md
  trial_registry.jsonl
  model_fit.md            (coefficients, L2, sign checks)
  profit_table.md         (taker vs maker: net PnL/contract, day-clustered CI, trade rate, fill rate; T=180 primary, 120/240 robustness)
  noarb_check.md          (violation frequency, capturable profit net of fees)
  FINAL_GATE_profit.md    (lockbox confirmation or sealed-negative)
  RESULTS_profit.md        (one-page synthesis + honest verdict)
```

`RESULTS_profit.md` states the profit numbers plainly and answers: does the parsimonious mean-reversion signal net positive per contract as a taker (expected: no), as a maker after a realistic fill rate (the live question), and does the no-arb scan find any free profit. A clean "taker negative, maker breakeven, no-arb empty — not deployable" is an acceptable and useful result if that's what the data says.
