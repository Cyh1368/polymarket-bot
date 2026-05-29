# BTC 15m Kalshi/Polymarket Divergence Model Card

Generated: 2026-05-29T16:33:05+00:00

## Target Construction

- Settlement row: first snapshot at or after `kalshi_close_time`; fallback to last pre-close snapshot.
- Ambiguous contracts are marked when either final feed is within $1.00 of its target.
- Polymarket target fallback order: settlement row, prior observed target, any observed target, first RTDS price.
- Labelable contracts: 1,175 of 1,176.
- Contract-level divergence base rate: 0.0826 (97/1,175).
- Training-eligible contracts after quality filters: 1,159; base rate 0.0768.
- Training rows: 506,014; row-level base rate 0.0769.

### Label Status Counts

| label_status | contracts |
| --- | --- |
| clean | 1154 |
| ambiguous_near_target | 15 |
| target_inferred | 5 |
| feed_error_at_settlement | 1 |
| invalid_settlement_snapshot_gap | 1 |

### Polymarket Target Sources

| polymarket_target_source | contracts |
| --- | --- |
| observed_at_settlement | 1170 |
| inferred_from_opening_rtds | 5 |
| observed_before_settlement | 1 |

## Validation

- Split policy: contract-level split only; 60% core training, 20% calibration, 20% final test.
- Calibration: isotonic calibration fit on the held-out calibration contracts.
- Best model by Brier score: **Logistic Regression**.

| model | auc_roc | brier | log_loss | classification_threshold | precision | recall | f1 | mean_predicted_prob | empirical_diverge_rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Logistic Regression | 0.7352 | 0.0684 | 0.2635 | 0.1100 | 0.2144 | 0.4799 | 0.2964 | 0.0701 | 0.0775 |
| Random Forest | 0.6644 | 0.0699 | 0.2697 | 0.1000 | 0.1655 | 0.2799 | 0.2080 | 0.0712 | 0.0775 |
| LightGBM | 0.6247 | 0.0709 | 0.3443 | 0.1000 | 0.1168 | 0.4886 | 0.1885 | 0.0699 | 0.0775 |

Recommended `diverge_prob` threshold for trading filter: `0.0886`.
Best F1 classification threshold: `0.1100`.
Best-model AUC: `0.7352`; Brier: `0.0684`.

Calibration plot: `divergence_plots/divergence_calibration_curve.png`
Elapsed-fraction performance plot: `divergence_plots/divergence_time_performance.png`
Feature-importance plot: `divergence_plots/divergence_feature_importance.png`

## Time-in-Contract Performance

| elapsed_bin | rows | contracts | diverge_rate | auc_roc | brier |
| --- | --- | --- | --- | --- | --- |
| 0.0-0.1 | 8343 | 232 | 0.0767 | 0.6257 | 0.0705 |
| 0.1-0.2 | 10356 | 232 | 0.0773 | 0.6287 | 0.0709 |
| 0.2-0.3 | 10379 | 232 | 0.0775 | 0.6607 | 0.0704 |
| 0.3-0.4 | 10363 | 232 | 0.0781 | 0.6556 | 0.0707 |
| 0.4-0.5 | 10327 | 232 | 0.0779 | 0.6771 | 0.0701 |
| 0.5-0.6 | 10369 | 232 | 0.0773 | 0.7008 | 0.0692 |
| 0.6-0.7 | 10348 | 232 | 0.0775 | 0.7397 | 0.0688 |
| 0.7-0.8 | 10357 | 232 | 0.0777 | 0.7614 | 0.0684 |
| 0.8-0.9 | 10353 | 232 | 0.0778 | 0.8522 | 0.0656 |
| 0.9-1.0 | 10359 | 232 | 0.0774 | 0.8807 | 0.0602 |

## Feature Importance

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| polymarket_bid_ask_spread_yes | 0.2222 | abs_coefficient |
| k_plus_np | 0.1479 | abs_coefficient |
| nk_plus_p | 0.1422 | abs_coefficient |
| arb_available | 0.0929 | abs_coefficient |
| kalshi_btc_price_roll10_mean | 0.0536 | abs_coefficient |
| kalshi_bid_ask_spread_yes | 0.0503 | abs_coefficient |
| feeds_on_same_side_x_elapsed_fraction | 0.0464 | abs_coefficient |
| implied_prob_spread_roll10_std | 0.0444 | abs_coefficient |
| price_spread_roll10_std | 0.0423 | abs_coefficient |
| time_to_close_seconds | 0.0266 | abs_coefficient |
| elapsed_fraction | 0.0266 | abs_coefficient |
| kalshi_btc_price_roll10_std | 0.0174 | abs_coefficient |
| polymarket_distance_to_target | 0.0135 | abs_coefficient |
| price_spread_abs_x_elapsed_fraction | 0.0090 | abs_coefficient |
| kalshi_btc_price_lag10 | 0.0088 | abs_coefficient |

`feeds_on_same_side` ranked #16 with normalized importance 0.0088;
it was useful but not dominant in the best model. In this split, microstructure and arb-spread
features added substantial signal beyond raw feed-side geometry. The explicit elapsed-time/feed
interaction features contributed 0.0582 normalized importance, while the
time-bin analysis shows performance improves materially late in the contract.

## Limitations

- Labels use sampled close snapshots, not exchange adjudication records.
- The trading threshold uses a conservative proxy payoff: non-divergence earns observed arb edge, divergence loses 1 unit.
- Very early-contract calls have less history for rolling and lag features; inference imputes missing history.
- Regime changes in RTDS lag, exchange APIs, liquidity, or BTC volatility can break calibration.
- Contract rows are highly correlated; reported metrics are row-level live-call metrics under contract-level splits.
