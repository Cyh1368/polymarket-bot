# Expanding BTC Trading To Other Coins

## Objective

The current BTC trading script should not be copied directly to ETH, SOL, XRP, HYPE, DOGE, or BNB because each token needs its own divergence labels, horizon models, thresholds, liquidity behavior, and entry strategy. The correct sequence is data collection, label validation, horizon modeling, threshold selection, strategy backtesting, then live deployment.

## 1. Collect Sufficient Data

Run the new data-only collectors continuously for each token:

```bash
.venv-cli-trader/bin/python cli_data_ETH.py
.venv-cli-trader/bin/python cli_data_SOL.py
.venv-cli-trader/bin/python cli_data_XRP.py
.venv-cli-trader/bin/python cli_data_HYPE.py
.venv-cli-trader/bin/python cli_data_DOGE.py
.venv-cli-trader/bin/python cli_data_BNB.py
```

Collect enough full contracts to estimate rare disagreement events. A practical first target is at least 1,000 contracts per token, but lower-liquidity tokens may need more because usable tradable opportunities are sparser.

Track these collection-quality checks per token:

- Percent of contracts with complete Kalshi close rows.
- Percent with usable Polymarket RTDS close and target data.
- Polymarket feed error rate.
- Missing top-of-book rates by leg.
- Contracts with ambiguous close values near the target.
- Distribution of raw and fee-adjusted arbitrage opportunities.

## 2. Build Token-Specific Labels

For each contract, compute the final settlement side independently on Kalshi and Polymarket:

- Kalshi result: source price at close greater than Kalshi target.
- Polymarket result: Polymarket RTDS close price greater than Polymarket target.
- `diverge = 1` if the two results differ.

Before training, verify each token's Kalshi settlement source. BTC uses BRTI. Non-BTC markets may use different reference indexes or exchange aggregates; the label must match actual settlement, not just a convenient spot feed.

Exclude or tag edge cases:

- Missing close snapshots.
- Missing or stale RTDS snapshots.
- Polymarket errors.
- Contracts where either source closes within a small epsilon of the target.
- Contracts where the inferred Polymarket target differs from official market metadata.

Report the divergence base rate per token.

## 3. Feature Engineering

Use the same horizon framing as BTC: one training row per contract per horizon, built from the past minute of data ending at the horizon timestamp.

Core horizons:

- 5 minutes to expiry.
- 3 minutes to expiry.
- 2 minutes to expiry.
- 1 minute to expiry.

Candidate 10-minute horizon:

- Train and evaluate separately; only include if it improves net strategy return after exit-cost simulation.

Use the existing BTC feature family:

- Feed spread and absolute feed spread.
- Distance to target.
- Spread-vs-distance ratios.
- Whether feeds are on the same side of the target.
- Kalshi and Polymarket bid/ask spreads.
- Orderbook imbalance.
- Implied probability spread.
- Fee-adjusted entry cost and edge.
- Rolling/lagged feed features.
- Window summary stats: last, mean, std, min, max, range, change.

Add token-normalized versions because price scales differ:

- Percent feed spread: `(kalshi_price - polymarket_price) / target`.
- Percent distance to target.
- Spread in basis points.
- Volatility-normalized spread from the past-minute window.

Keep all aggregation causal: only rows at or before the horizon timestamp can be used.

## 4. Train Horizon Models

Train separate models per token and horizon. Do not pool tokens at first because liquidity, feed behavior, and settlement noise differ.

Use contract-level splits:

- Train/calibration/test split by contract, never by row.
- Prefer chronological test splits after enough data is available, because live performance depends on recent regime behavior.

Models to compare:

- Logistic regression as an interpretable baseline.
- Gradient boosting as the likely strongest model.
- Random forest as a robustness check.

Evaluation:

- AUC-ROC.
- Brier score.
- Calibration curve.
- Precision, recall, F1.
- Reliability by predicted-probability bucket.
- Feature importance.
- Performance by time-to-close and by liquidity bucket.

The output for each token/horizon should be:

- `divergence_<TOKEN>_<HORIZON>_model.pkl`
- `divergence_<TOKEN>_<HORIZON>_metadata.json`
- ordered feature list
- model card with calibration and limitations

## 5. Determine Thresholds

Do not use `0.5` as the trade threshold. `diverge_prob` is an estimated probability, and the trade threshold should be selected from expected value.

For each token and horizon, simulate trades on calibration data using:

```text
fee_adjusted_entry_cost = raw_entry_cost + Kalshi fee + Polymarket fee
profit_if_no_divergence = 1.00 - fee_adjusted_entry_cost
loss_if_divergence = fee_adjusted_entry_cost
expected_return = (1 - p_diverge) * (1 - fee_adjusted_entry_cost) - p_diverge * fee_adjusted_entry_cost
```

Equivalently, with total loss on divergence:

```text
expected_return = 1 - fee_adjusted_entry_cost - p_diverge
```

Only consider rows where the trade is executable with actual liquidity on both legs.

Select thresholds that maximize expected return on calibration data, then report final results only on test data. Include bootstrap confidence intervals when possible.

## 6. Backtest Entry Logic

The BTC work showed that thresholding each horizon independently can create costly emergency exits. For each token, backtest strategies that include the operational cost of exits and partial fills.

Strategies to test:

- Single-horizon only: trade only on 5m, 3m, 2m, or 1m.
- No emergency exits: once any accepted model says tradable, hold that state until expiry.
- Latch strategies: only allow selected late horizons to latch `tradable=True`.
- Consecutive-pass strategy: require two adjacent horizons to pass before trading.
- Late-only strategy: ignore 5m and 3m, use 2m/1m only.
- Liquidity-gated strategy: require both legs to have sufficient size at the quoted price before considering the signal valid.
- Edge-gated strategy: require fee-adjusted edge to exceed token-specific slippage and exit-risk estimates.

For each strategy, report:

- Number of model-pass contracts.
- Number of tradable contracts with actual liquidity.
- Number of entries.
- Number of good trades missed.
- Number of bad trades avoided.
- Number and cost of emergency exits.
- Partial fill count and cleanup cost.
- Gross return, fees, exit losses, and net return.
- Return per contract and per trade.
- Maximum drawdown.

Define a bad trade explicitly as a trade whose realized outcome is negative after fees and divergence result, not merely a trade with high predicted divergence.

## 7. Choose Token-Specific Live Logic

Only promote a token to live trading after:

- The best strategy beats simpler baselines out of sample.
- Calibration is acceptable in the probability range used for entry.
- Live liquidity is sufficient for the intended order size.
- Emergency exits are rare or the strategy is designed to avoid them.
- Paper/live shadow trading confirms that model features match training distributions.

The live script should load token-specific artifacts:

- model files
- metadata and thresholds
- feature list
- entry strategy name
- liquidity minimums
- profit margin
- token source settings

BTC's current `any_2_1_latch_hold` strategy should be treated as a candidate, not as the default for other tokens.

## 8. Monitoring And Retraining

For each deployed token, monitor:

- Predicted divergence distribution.
- Actual divergence rate.
- Calibration drift.
- Feature distribution drift.
- Liquidity failure rate.
- Partial fill rate.
- Realized return by horizon.
- Difference between expected and realized return.

Retrain when there is material drift, when the exchanges change resolution rules, or after enough new contracts are collected to materially improve sample size.

