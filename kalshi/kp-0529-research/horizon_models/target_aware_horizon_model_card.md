# Target-Aware Horizon Model Card

Generated: 2026-06-01T01:09:02+00:00

## What Changed

- The live feature path now recomputes Kalshi and Polymarket `yes_mid` from bid/ask before model aggregation, preventing stale websocket mids from entering `polymarket_yes_mid_*` and `implied_prob_spread_*`.
- Horizon models were retrained with target-aware features so Polymarket geometry is measured against the Polymarket target, not only the Kalshi target.
- New feature families include `polymarket_distance_to_own_target`, `target_spread`, `target_spread_abs`, `feeds_on_same_side_own_targets`, and `price_between_targets`.
- Existing legacy features such as `polymarket_distance_to_target` and `feeds_on_same_side` remain in the feature set for continuity, but the model can now learn the own-target geometry explicitly.

## Dataset And Label Quality

- Labelable contracts: `1175` of `1176`.
- Training-eligible contracts: `1159`; eligible base divergence rate: `0.0768`.
- Horizon rows aggregate exactly the trailing one-minute window ending at the horizon decision time, using a 2-second previous-tick sampling grid where possible.
- Split policy remains contract-level: 60% core training, 20% calibration, 20% final test.

### Label Status Counts

| label_status | contracts |
| --- | --- |
| clean | 1154 |
| ambiguous_near_target | 15 |
| target_inferred | 5 |
| feed_error_at_settlement | 1 |
| invalid_settlement_snapshot_gap | 1 |

### Aggregation Status Counts

| horizon | aggregation_status | contracts |
| --- | --- | --- |
| 10m | missing_window | 1 |
| 10m | ok | 1174 |
| 1m | ok | 1175 |
| 2m | missing_window | 1 |
| 2m | ok | 1174 |
| 3m | missing_window | 1 |
| 3m | ok | 1173 |
| 3m | stale_asof_snapshot | 1 |
| 5m | missing_window | 1 |
| 5m | ok | 1173 |
| 5m | too_few_window_rows | 1 |

### Horizon Dataset Summary

| horizon | contracts | divergences | base_rate | mean_window_rows | median_asof_gap_seconds |
| --- | --- | --- | --- | --- | --- |
| 10m | 1158 | 89 | 0.0769 | 29.6831 | 1.0110 |
| 1m | 1159 | 89 | 0.0768 | 29.6402 | 1.0550 |
| 2m | 1158 | 89 | 0.0769 | 29.6658 | 1.0495 |
| 3m | 1157 | 88 | 0.0761 | 29.6690 | 1.0450 |
| 5m | 1157 | 89 | 0.0769 | 29.7208 | 1.0540 |

## Final Test Metrics

| horizon | contracts_total | contracts_test | test_divergences | test_base_rate | auc_roc | brier | log_loss | classification_threshold | precision | recall | f1 | recommended_trade_threshold | feature_count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 10m | 1158 | 232 | 18 | 0.0776 | 0.5776 | 0.0711 | 0.2699 | 0.0900 | 0.1765 | 0.3333 | 0.2308 | 0.0690 | 382 |
| 5m | 1157 | 232 | 18 | 0.0776 | 0.7009 | 0.0690 | 0.2586 | 0.1200 | 0.2917 | 0.3889 | 0.3333 | 0.0788 | 382 |
| 3m | 1157 | 232 | 18 | 0.0776 | 0.7163 | 0.0700 | 0.2641 | 0.0900 | 0.1967 | 0.6667 | 0.3038 | 0.0788 | 382 |
| 2m | 1158 | 232 | 18 | 0.0776 | 0.7985 | 0.0648 | 0.2446 | 0.1900 | 0.7500 | 0.3333 | 0.4615 | 0.0788 | 382 |
| 1m | 1159 | 232 | 18 | 0.0776 | 0.8863 | 0.0559 | 0.2291 | 0.2300 | 0.5556 | 0.5556 | 0.5556 | 0.1329 | 382 |

## Trading Threshold Coverage

A contract is tradable when one buy-side combination has positive fee-adjusted edge: `raw_combo_cost + Kalshi_fee + Polymarket_fee < 1.0`. `Expected return` uses mean predicted divergence probability; `Test return` uses actual held-out divergence results.

| horizon | recommended_trade_threshold | tradable_test_contracts | trade_threshold_pass_contracts | trade_threshold_fail_contracts | trade_threshold_pass_divergences | trade_threshold_fail_divergences | trade_threshold_pass_diverge_rate | trade_threshold_fail_diverge_rate | trade_threshold_pass_mean_all_in_cost | trade_threshold_pass_mean_predicted_diverge_prob | trade_threshold_pass_expected_return | trade_threshold_pass_test_return |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 10m | 0.0690 | 122 | 26 | 96 | 1 | 11 | 0.0385 | 0.1146 | 0.9716 | 0.0609 | -0.0324 | -0.0100 |
| 5m | 0.0788 | 140 | 76 | 64 | 4 | 10 | 0.0526 | 0.1562 | 0.9147 | 0.0521 | 0.0332 | 0.0326 |
| 3m | 0.0788 | 167 | 97 | 70 | 3 | 12 | 0.0309 | 0.1714 | 0.8489 | 0.0557 | 0.0954 | 0.1202 |
| 2m | 0.0788 | 187 | 112 | 75 | 1 | 13 | 0.0089 | 0.1733 | 0.7898 | 0.0385 | 0.1716 | 0.2012 |
| 1m | 0.1329 | 211 | 174 | 37 | 3 | 14 | 0.0172 | 0.3784 | 0.7754 | 0.0416 | 0.1831 | 0.2074 |

## Per-Horizon Notes

### 10m

- Model artifact: `divergence_horizon_10m_model.pkl`.
- Feature list: `divergence_horizon_10m_feature_list.json`; feature count `382`.
- Test AUC `0.5776`, Brier `0.0711`, F1 `0.2308`.
- Trading threshold: `diverge_prob < 0.0690`.
- Calibration plot: `plots/divergence_horizon_10m_calibration.png`.
- Feature-importance plot: `plots/divergence_horizon_10m_feature_importance.png`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| implied_prob_spread_roll10_std_change | 0.0162 | abs_scaled_logit_coefficient |
| price_between_targets_mean | 0.0153 | abs_scaled_logit_coefficient |
| price_between_targets_last | 0.0147 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_10_last | 0.0126 | abs_scaled_logit_coefficient |
| k_no_p_yes_polymarket_fee_std | 0.0116 | abs_scaled_logit_coefficient |
| k_yes_p_no_polymarket_fee_std | 0.0114 | abs_scaled_logit_coefficient |
| spread_vs_distance_ratio_min | 0.0112 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_std_last | 0.0108 | abs_scaled_logit_coefficient |
| price_between_targets_min | 0.0107 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_min | 0.0104 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_last | 0.0103 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_last | 0.0101 | abs_scaled_logit_coefficient |
| price_between_targets_change | 0.0101 | abs_scaled_logit_coefficient |
| window_rows | 0.0099 | abs_scaled_logit_coefficient |
| kalshi_last_price_std | 0.0096 | abs_scaled_logit_coefficient |

Best-ranked target-aware feature families:

| target_aware_family | best_rank | best_feature | importance_normalized |
| --- | --- | --- | --- |
| polymarket_distance_to_own_target | 62 | polymarket_distance_to_own_target_last | 0.0048 |
| target_spread | 181 | target_spread_min | 0.0019 |
| target_spread_abs | 358 | target_spread_abs_last | 0.0000 |
| feeds_on_same_side_own_targets | 24 | feeds_on_same_side_own_targets_mean | 0.0077 |
| price_between_targets | 2 | price_between_targets_mean | 0.0153 |

### 5m

- Model artifact: `divergence_horizon_5m_model.pkl`.
- Feature list: `divergence_horizon_5m_feature_list.json`; feature count `382`.
- Test AUC `0.7009`, Brier `0.0690`, F1 `0.3333`.
- Trading threshold: `diverge_prob < 0.0788`.
- Calibration plot: `plots/divergence_horizon_5m_calibration.png`.
- Feature-importance plot: `plots/divergence_horizon_5m_feature_importance.png`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| implied_prob_spread_roll10_std_last | 0.0186 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_change | 0.0159 | abs_scaled_logit_coefficient |
| kalshi_btc_price_lag5_change | 0.0155 | abs_scaled_logit_coefficient |
| window_rows | 0.0142 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_std_range | 0.0138 | abs_scaled_logit_coefficient |
| kalshi_last_price_change | 0.0131 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_change | 0.0128 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_10_min | 0.0119 | abs_scaled_logit_coefficient |
| feeds_on_same_side_x_elapsed_fraction_std | 0.0116 | abs_scaled_logit_coefficient |
| spread_vs_distance_ratio_x_elapsed_fraction_mean | 0.0112 | abs_scaled_logit_coefficient |
| spread_vs_distance_ratio_mean | 0.0111 | abs_scaled_logit_coefficient |
| price_spread_std | 0.0110 | abs_scaled_logit_coefficient |
| kalshi_distance_to_target_change | 0.0101 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_change | 0.0099 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_10_mean | 0.0096 | abs_scaled_logit_coefficient |

Best-ranked target-aware feature families:

| target_aware_family | best_rank | best_feature | importance_normalized |
| --- | --- | --- | --- |
| polymarket_distance_to_own_target | 63 | polymarket_distance_to_own_target_change | 0.0048 |
| target_spread | 98 | target_spread_last | 0.0032 |
| target_spread_abs | 265 | target_spread_abs_max | 0.0008 |
| feeds_on_same_side_own_targets | 30 | feeds_on_same_side_own_targets_max | 0.0076 |
| price_between_targets | 38 | price_between_targets_mean | 0.0072 |

### 3m

- Model artifact: `divergence_horizon_3m_model.pkl`.
- Feature list: `divergence_horizon_3m_feature_list.json`; feature count `382`.
- Test AUC `0.7163`, Brier `0.0700`, F1 `0.3038`.
- Trading threshold: `diverge_prob < 0.0788`.
- Calibration plot: `plots/divergence_horizon_3m_calibration.png`.
- Feature-importance plot: `plots/divergence_horizon_3m_feature_importance.png`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| implied_prob_spread_roll10_std_change | 0.0173 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_mean_std | 0.0150 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_mean | 0.0132 | abs_scaled_logit_coefficient |
| price_spread_mean | 0.0121 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_last | 0.0112 | abs_scaled_logit_coefficient |
| polymarket_order_book_imbalance_max | 0.0105 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_last | 0.0102 | abs_scaled_logit_coefficient |
| price_spread_abs_range | 0.0100 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_max | 0.0098 | abs_scaled_logit_coefficient |
| price_spread_abs_max | 0.0098 | abs_scaled_logit_coefficient |
| k_no_p_yes_polymarket_fee_change | 0.0098 | abs_scaled_logit_coefficient |
| k_yes_p_no_polymarket_fee_change | 0.0097 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_std | 0.0096 | abs_scaled_logit_coefficient |
| kalshi_distance_to_target_change | 0.0096 | abs_scaled_logit_coefficient |
| price_between_targets_mean | 0.0094 | abs_scaled_logit_coefficient |

Best-ranked target-aware feature families:

| target_aware_family | best_rank | best_feature | importance_normalized |
| --- | --- | --- | --- |
| polymarket_distance_to_own_target | 92 | polymarket_distance_to_own_target_std | 0.0038 |
| target_spread | 290 | target_spread_mean | 0.0007 |
| target_spread_abs | 214 | target_spread_abs_mean | 0.0014 |
| feeds_on_same_side_own_targets | 24 | feeds_on_same_side_own_targets_range | 0.0078 |
| price_between_targets | 15 | price_between_targets_mean | 0.0094 |

### 2m

- Model artifact: `divergence_horizon_2m_model.pkl`.
- Feature list: `divergence_horizon_2m_feature_list.json`; feature count `382`.
- Test AUC `0.7985`, Brier `0.0648`, F1 `0.4615`.
- Trading threshold: `diverge_prob < 0.0788`.
- Calibration plot: `plots/divergence_horizon_2m_calibration.png`.
- Feature-importance plot: `plots/divergence_horizon_2m_feature_importance.png`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| kalshi_distance_to_target_change | 0.0194 | abs_scaled_logit_coefficient |
| polymarket_order_book_imbalance_last | 0.0117 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_max | 0.0114 | abs_scaled_logit_coefficient |
| kalshi_btc_price_lag5_change | 0.0112 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_10_std | 0.0112 | abs_scaled_logit_coefficient |
| polymarket_order_book_imbalance_std | 0.0104 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_mean | 0.0101 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_min | 0.0100 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_change | 0.0092 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_mean | 0.0091 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_std | 0.0084 | abs_scaled_logit_coefficient |
| feeds_on_same_side_x_elapsed_fraction_last | 0.0083 | abs_scaled_logit_coefficient |
| feeds_on_same_side_last | 0.0082 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_last | 0.0080 | abs_scaled_logit_coefficient |
| kalshi_btc_price_lag10_change | 0.0078 | abs_scaled_logit_coefficient |

Best-ranked target-aware feature families:

| target_aware_family | best_rank | best_feature | importance_normalized |
| --- | --- | --- | --- |
| polymarket_distance_to_own_target | 33 | polymarket_distance_to_own_target_change | 0.0057 |
| target_spread | 365 | target_spread_min | 0.0001 |
| target_spread_abs | 369 | target_spread_abs_last | 0.0000 |
| feeds_on_same_side_own_targets | 18 | feeds_on_same_side_own_targets_mean | 0.0073 |
| price_between_targets | 53 | price_between_targets_std | 0.0050 |

### 1m

- Model artifact: `divergence_horizon_1m_model.pkl`.
- Feature list: `divergence_horizon_1m_feature_list.json`; feature count `382`.
- Test AUC `0.8863`, Brier `0.0559`, F1 `0.5556`.
- Trading threshold: `diverge_prob < 0.1329`.
- Calibration plot: `plots/divergence_horizon_1m_calibration.png`.
- Feature-importance plot: `plots/divergence_horizon_1m_feature_importance.png`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| polymarket_order_book_imbalance_std | 0.0229 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_mean | 0.0168 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_last | 0.0151 | abs_scaled_logit_coefficient |
| kalshi_yes_mid_std | 0.0124 | abs_scaled_logit_coefficient |
| price_between_targets_mean | 0.0115 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_range | 0.0111 | abs_scaled_logit_coefficient |
| polymarket_order_book_imbalance_change | 0.0104 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_max | 0.0100 | abs_scaled_logit_coefficient |
| feeds_on_same_side_own_targets_change | 0.0098 | abs_scaled_logit_coefficient |
| k_yes_p_no_kalshi_fee_last | 0.0098 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_std | 0.0095 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_std | 0.0091 | abs_scaled_logit_coefficient |
| k_no_p_yes_kalshi_fee_last | 0.0089 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_mean_range | 0.0087 | abs_scaled_logit_coefficient |
| kalshi_btc_price_lag10_range | 0.0085 | abs_scaled_logit_coefficient |

Best-ranked target-aware feature families:

| target_aware_family | best_rank | best_feature | importance_normalized |
| --- | --- | --- | --- |
| polymarket_distance_to_own_target | 222 | polymarket_distance_to_own_target_range | 0.0015 |
| target_spread | 159 | target_spread_mean | 0.0021 |
| target_spread_abs | 188 | target_spread_abs_min | 0.0017 |
| feeds_on_same_side_own_targets | 9 | feeds_on_same_side_own_targets_change | 0.0098 |
| price_between_targets | 5 | price_between_targets_mean | 0.0115 |

## Operational Interpretation

- The later horizons are materially stronger. The 2m and 1m models have the highest held-out AUC and the best trade-filter divergence separation.
- The target-aware features now appear in the feature lists and in the ranked coefficient table, so the model can explicitly react to target spread and whether each feed is on the same side of its own target.
- The new 2m threshold remains `0.0788`; the new 1m threshold is `0.1329`, slightly lower than the previous `0.1378` after target-aware retraining.
- These reports still assume historical quoted prices are executable. They do not model live order failures, minimum notional constraints, or stale ask-side liquidity beyond the CSV quotes.

## Artifacts

- Combined model card: `combined_horizon_model_card.md`.
- Metrics table: `horizon_model_metrics.csv`.
- Aggregated dataset: `horizon_aggregated_dataset.csv`.
- Per-horizon model artifacts: `divergence_horizon_{horizon}_model.pkl`.
