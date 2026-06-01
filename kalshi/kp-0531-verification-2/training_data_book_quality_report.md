# Training Data Book Quality Check

Date: 2026-05-31

This note checks whether the current horizon models were trained on data that resembles the imperfect live feature data observed for `KXBTC15M-26MAY312215-15`.

The live issue was:

```text
Polymarket YES bid/ask = 0.99 / 1.00
Polymarket NO  bid/ask = 0.49 / 0.01
Polymarket NO bid quantity = missing
polymarket_order_book_imbalance_* = null
k_plus_np_last = 1.489
```

That is an incoherent binary order book: the NO side is crossed, and the book simultaneously implies a near-certain YES and a high NO bid.

## Data Sources

I checked two levels of data:

- Raw training CSVs: `kp-0529-research/combined_KXBTC15M-*.csv`
- Aggregated model input dataset: `kp-0529-research/horizon_models/horizon_aggregated_dataset.csv`

The raw scan covered:

- `1,176` contract CSVs
- `523,084` raw rows

The aggregated scan used rows with:

- `aggregation_status == "ok"`
- `training_eligible_label == True`

## Raw Training CSV Findings

Bid quantity was not always present in the raw Polymarket training data.

| Raw issue | Rows | Share |
| --- | ---: | ---: |
| Polymarket YES bid exists but YES bid quantity missing | 29,460 / 523,084 | 5.63% |
| Polymarket NO bid exists but NO bid quantity missing | 28,734 / 523,084 | 5.49% |
| Either Polymarket bid quantity missing while bid exists | 58,194 / 523,084 | 11.13% |
| Kalshi bid quantity missing while bid exists | 0 / 523,084 | 0.00% |

Polymarket crossed books were also present in the raw training data.

| Raw issue | Rows | Share |
| --- | ---: | ---: |
| Polymarket YES bid > YES ask | 57,814 / 523,084 | 11.05% |
| Polymarket NO bid > NO ask | 57,810 / 523,084 | 11.05% |
| Kalshi YES bid > YES ask | 0 / 523,084 | 0.00% |
| Kalshi NO bid > NO ask | 0 / 523,084 | 0.00% |

Complement-incoherent Polymarket books existed, but were rarer.

| Raw issue | Rows | Share |
| --- | ---: | ---: |
| Polymarket YES bid + NO ask > 1 | 607 / 523,084 | 0.116% |
| Polymarket NO bid + YES ask > 1 | 633 / 523,084 | 0.121% |
| Kalshi YES bid + NO ask > 1 | 0 / 523,084 | 0.00% |
| Kalshi NO bid + YES ask > 1 | 0 / 523,084 | 0.00% |

So the raw training data did include missing Polymarket bid quantities and crossed Polymarket bid/ask prices.

## Live Pattern Search

The exact live pattern was not present in the raw training CSVs.

| Pattern | Raw rows | Contracts |
| --- | ---: | ---: |
| Exact `YES 0.99/1.00`, `NO 0.49/0.01` | 0 | 0 |
| Similar: `YES bid >= 0.98`, `YES ask >= 0.99`, `NO bid >= 0.45`, `NO ask <= 0.05`, and NO crossed | 0 | 0 |
| Same similar pattern with missing NO bid quantity | 0 | 0 |

This means the model saw some bad Polymarket book data during training, but not the specific live state where YES was quoted as nearly certain while NO still had a large stale bid.

## Aggregated Model Input Findings

The actual horizon models were trained on aggregated feature rows, not raw rows. In those aggregated rows, all-null Polymarket imbalance was common near short horizons.

| Horizon | Eligible OK rows | Polymarket imbalance all-null |
| --- | ---: | ---: |
| 10m | 1,158 | 0 / 1,158 = 0.00% |
| 5m | 1,157 | 13 / 1,157 = 1.12% |
| 3m | 1,157 | 85 / 1,157 = 7.35% |
| 2m | 1,158 | 142 / 1,158 = 12.26% |
| 1m | 1,159 | 236 / 1,159 = 20.36% |

Negative Polymarket YES bid-ask spread was also common in model inputs, especially near expiry.

| Horizon | Negative `polymarket_bid_ask_spread_yes_last` |
| --- | ---: |
| 10m | 0 / 1,158 = 0.00% |
| 5m | 51 / 1,157 = 4.41% |
| 3m | 186 / 1,157 = 16.08% |
| 2m | 294 / 1,158 = 25.39% |
| 1m | 492 / 1,159 = 42.45% |

Therefore, the models were trained with some invalid Polymarket quote features. However, the model training rows did not contain the live combination of all-null Polymarket imbalance plus an extreme `k_plus_np`.

| Horizon | Max `k_plus_np_last` when Polymarket imbalance all-null |
| --- | ---: |
| 5m | 1.009 |
| 3m | 1.009 |
| 2m | 1.009 |
| 1m | 1.029 |

The live `312215-15` 1m row had:

```text
polymarket_order_book_imbalance_* = null
k_plus_np_last = 1.489
```

That combined state did not appear in the aggregated training data.

## Interpretation

The answer to "is there always bid quantity?" is no for Polymarket. Missing Polymarket bid quantities existed in about `11.13%` of raw training rows when considering either YES or NO bid quantity. Kalshi bid quantities were complete in the scanned training rows.

The answer to "do ask/bid prices ever exhibit this pattern?" is partly yes, but not in the important exact sense:

- Crossed Polymarket books did appear in raw and aggregated training data.
- Missing Polymarket imbalance features were present in aggregated model inputs.
- The specific live pattern of `YES 0.99/1.00`, `NO 0.49/0.01`, missing NO bid quantity, and `k_plus_np_last = 1.489` did not appear.
- In training, all-null Polymarket imbalance rows had `k_plus_np_last <= 1.029` at 1m, far below the live `1.489`.

## Practical Conclusion

The live `312215-15` feature row is out-of-distribution in the combined feature state. It is not enough to say the model was trained on nulls or crossed books; the model was not trained on null Polymarket imbalance paired with an extreme impossible arbitrage quote.

The bot should reject model inference and trading when:

- either Polymarket side is crossed,
- binary complement checks fail materially,
- a bid exists but its size is missing for an imbalance feature,
- `polymarket_order_book_imbalance_*` is all-null at the decision horizon,
- or an extreme arbitrage feature is driven by an incoherent book.

Without those guards, the model can receive finite but economically invalid features and produce a confident `tradable` decision.
