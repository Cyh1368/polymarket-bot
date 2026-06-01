# 2026-05-31 Live Divergence Review

## Scope

I analyzed `kp-0531-research/concise_trader_log.txt`, the per-contract CSVs in `kp-0531-research/`, and the prior research outputs in `kp-0529-research/horizon_models/`.

The relevant live regime begins at:

`2026-05-31T18:27:04.297Z | START cli_trader_v2 contracts=3 profit_margin=0.1800 ...`

The bot was using the current latch behavior where `5m` and `3m` are observe-only and `2m` / `1m` can latch tradable. The log string still says `strategy=any_2_1_latch_hold`, but the roles in the log show the effective strategy is `latch_2m_1m`.

## Bottom Line

The core observation is valid: live outcomes were far worse than the prior backtest would imply.

One correction: in the saved settlement CSVs I find **5 filled trades** after the `0.18`, size-3 regime started. Of those, **3 settled with outcome discrepancies**, and **all 3 were adverse to the bot's selected direction**. I do not find a favorable split among those three. The other two trades were normal agreement-settlement wins.

| ticker | entry time UTC | direction | size | all-in | PM leg price | settlement | result | estimated PnL |
| --- | ---: | --- | ---: | ---: | ---: | --- | --- | ---: |
| KXBTC15M-26MAY311430-30 | 18:28:14.918 | NK+P | 3 | 67.4c | 45.0c | agree NO/NO | agree win | +$0.978 |
| KXBTC15M-26MAY311445-45 | 18:44:04.536 | K+NP | 3 | 37.9c | 36.0c | split K=NO, P=YES | split loss | -$1.137 |
| KXBTC15M-26MAY311645-45 | 20:43:01.529 | K+NP | 3 | 65.5c | 56.0c | split K=NO, P=YES | split loss | -$1.965 |
| KXBTC15M-26MAY311745-45 | 21:43:02.218 | NK+P | 3 | 50.2c | 46.0c | agree YES/YES | agree win | +$1.494 |
| KXBTC15M-26MAY311830-30 | 22:29:03.787 | NK+P | 3 | 53.0c | 50.0c | split K=YES, P=NO | split loss | -$1.590 |

Estimated net PnL from these five fills is about **-$2.22** using the logged fill costs and settlement outcomes.

## Statistical Check

The closest prior backtest setting is `latch_2m_1m`, `profit_margin=0.18`, Polymarket price floor `>33c`.

| sample | trades | divergences | divergence rate |
| --- | ---: | ---: | ---: |
| calibration | 164 | 9 | 5.49% |
| held-out test | 158 | 4 | 2.53% |
| all historical | 751 | 13 | 1.73% |

The probability of seeing at least 3 divergent trades in 5 fills is:

| assumed divergence rate | source | P(>=3 divergences in 5) |
| ---: | --- | ---: |
| 1.73% | all historical live-like trades | 0.0051% |
| 2.53% | held-out test live-like trades | 0.0156% |
| 5.49% | calibration live-like trades | 0.1520% |

So under the prior model assumptions, the live observation is very unlikely.

The broader 05/31 afternoon regime was also abnormal. From `18:30` through `23:15` UTC, the saved contract CSVs show **6 divergent settlements out of 20 contracts**. That is 30%, versus the historical base rate near 7.7%.

## Main Cause

The adverse live splits were primarily **target-spread / own-target** failures, not just ordinary BRTI-vs-RTDS feed spread.

The current model features do **not** include Polymarket's own target as a feature. The feature list includes `polymarket_distance_to_target`, but in both training and live inference that is computed as:

`polymarket_btc_price - kalshi_btc_target`

It does not compute:

`polymarket_btc_price - polymarket_btc_target`

Nor does it include:

- `target_spread = kalshi_btc_target - polymarket_btc_target`
- whether current price is between the two platform targets
- whether each feed is on the correct side of its own target
- trade-direction-specific split risk

That matters because the three adverse splits all occurred when Polymarket was close to its own target, even when the model saw the feeds as relatively safe versus the Kalshi target.

| ticker | direction | model prob | K target - P target | BRTI - K target at entry | RTDS - P target at entry | adverse settlement |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| KXBTC15M-26MAY311445-45 | K+NP | 0.1091 | +$45.62 | -$31.55 | +$0.78 | K=NO, P=YES |
| KXBTC15M-26MAY311645-45 | K+NP | 0.0448 | +$21.99 | -$6.97 | -$8.30 | K=NO, P=YES |
| KXBTC15M-26MAY311830-30 | NK+P | 0.0649 | -$15.92 | +$26.11 | +$0.14 | K=YES, P=NO |

In plain terms: the model often saw both feeds relative to the Kalshi target, but the actual Polymarket settlement boundary was elsewhere. This can make a large arbitrage edge look attractive exactly when the trade is exposed to a target-driven split.

## Backtest vs Live Differences

Key mismatches:

- The prior price-floor sweep used `CONTRACTS_PER_LEG = 1.0`; live was running `contracts=3`.
- The prior floor was `polymarket_price > 0.33`; live enforces Polymarket's `$1` minimum as `polymarket_price * contracts >= 1.0`, which is approximately `>= 33.33c` for size 3.
- The backtest is price/outcome based. It does not simulate fresh FOK order checks, retries, live liquidity, execution latency, or websocket/API stale-state behavior.
- Restarts during the live run caused missing/partial model histories. Example: the first size-3 trade used the `2m` model with a partial feature window of `28/30` samples.
- The backtest treats any divergence as a full loss. Directionally, a divergence can be a double win or a full loss. In the historical `0.18`, `>33c` setting, adverse splits were still more common than favorable splits: 10 adverse vs 3 favorable in the full historical sample.
- The current model predicts contract-level binary divergence, not trade-direction-specific loss probability.

I also reran the historical live-like filter with a size-3 fee gate and `$1` PM notional gate. The divergence rate remained low historically: 13 divergences in 724 trades, or 1.80%. This means size alone does not explain the live cluster.

## What The Divergent Live Contracts Had In Common

The common pattern was not simply "high profit margin." It was:

- very cheap one-sided Kalshi legs, often near 0c to 8c
- Polymarket leg still above the min-notional floor
- large target offset between Kalshi and Polymarket
- Polymarket RTDS very close to Polymarket's own target at entry
- the selected trade direction was the wrong side of the eventual split

This suggests that high apparent edge can be a warning sign. At high margins, the bot is often selecting contracts where the markets are cheap because the two venues are effectively trading around different settlement boundaries.

## Better Backtest Framework

The next backtest should be event-driven and should reproduce the bot's live decision loop:

- Use `contracts=3` exactly.
- Use the exact fee gate used by `cli_trader_v2.py`.
- Use exact Polymarket min notional: `polymarket_price * contracts >= 1.0`.
- Use one trade per contract, same entry buffer, same latch behavior, same retry assumptions.
- Simulate entry from ask-side liquidity, not just prices, where available.
- Treat direction explicitly:
  - K+NP wins both legs if `K=YES, P=NO`
  - NK+P wins both legs if `K=NO, P=YES`
  - the opposite split is a full loss
  - agreement pays one leg
- Add target-aware filters and compare against the current no-filter baseline.
- Evaluate by walk-forward date blocks, not only random contract splits, because divergence clusters by regime.

## Recommended Research Changes

Retrain the horizon models with target-aware features:

- `polymarket_distance_to_own_target = polymarket_btc_price - polymarket_btc_target`
- `target_spread = kalshi_btc_target - polymarket_btc_target`
- `abs_target_spread`
- `price_between_targets`
- `kalshi_side_of_own_target`
- `polymarket_side_of_own_target`
- `own_target_sides_agree`
- rolling min/max distance to each platform's own target
- candidate direction, candidate all-in cost, and candidate Polymarket leg price

The better modeling target is not just `diverge` as a binary event. Train either:

1. a three-class model: `agree`, `K_yes_P_no`, `K_no_P_yes`; or
2. two directional models:
   - `P(adverse split | candidate=K+NP)`
   - `P(adverse split | candidate=NK+P)`

Then compute expected value directly:

`EV(K+NP) = P(agree) * (1 - cost) + P(K_yes_P_no) * (2 - cost) + P(K_no_P_yes) * (0 - cost)`

and analogously for `NK+P`.

## Interim Trading Settings

Until the model is retrained, profit margin alone is not a sufficient safety control. Raising margin may even select more target-mismatch trades.

I would add temporary hard filters before live trading:

- Reject if `abs(kalshi_btc_target - polymarket_btc_target) > $10-$15` until this is backtested.
- Reject if `abs(polymarket_btc_price - polymarket_btc_target) < $10-$15`.
- Reject if price is inside the interval between Kalshi target and Polymarket target.
- Reject K+NP if current own-target state is already `K=NO, P=YES`.
- Reject NK+P if current own-target state is already `K=YES, P=NO`.
- Consider requiring both `2m` and `1m` to pass, or requiring `3m` not to fail, but only after a target-aware backtest. In the 05/31 sample, stricter horizon agreement would have avoided some losses, but this is too small to trust.

The most important immediate change is to make the model and backtest aware of Polymarket's own target. The 05/31 losses were exactly the class of trades the current feature set is least able to identify.
