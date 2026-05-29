# Contract-Level Horizon Divergence Models

Generated: 2026-05-29T17:40:11+00:00

## Setup

- Unit of analysis: one row per contract per prediction horizon.
- Horizon rows use only the trailing 60 seconds ending at 5m, 3m, 2m, or 1m before `kalshi_close_time`.
- Base snapshot features are the same leakage-safe features from the snapshot model, then aggregated with last/mean/std/min/max/range/change.
- Model family: calibrated Logistic Regression, continuing with the best-performing snapshot model family.
- Calibration method: `sigmoid` on a held-out calibration-contract split.
- Split policy: one stratified contract split reused for every horizon: 60% core training, 20% calibration, 20% final test.

## Target Construction

- Settlement row: first snapshot at or after `kalshi_close_time`; fallback to last pre-close snapshot.
- Ambiguous labels are marked when either final feed is within $1.00 of its target.
- Settlement snapshots more than 10s from close are excluded from labelable contracts.
- Labelable contracts: 1,175 of 1,176.
- Labelable divergence base rate: 0.0826 (97/1,175).
- Training-eligible contracts after quality filters: 1,159; base rate 0.0768.

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
| 1m | 1159 | 89 | 0.0768 | 29.6402 | 1.0550 |
| 2m | 1158 | 89 | 0.0769 | 29.6658 | 1.0495 |
| 3m | 1157 | 88 | 0.0761 | 29.6690 | 1.0450 |
| 5m | 1157 | 89 | 0.0769 | 29.7208 | 1.0540 |

## Final Test Metrics

| horizon | contracts_total | contracts_test | test_divergences | test_base_rate | auc_roc | brier | log_loss | classification_threshold | precision | recall | f1 | recommended_trade_threshold |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 5m | 1157 | 232 | 18 | 0.0776 | 0.6789 | 0.0684 | 0.2576 | 0.1400 | 0.4167 | 0.2778 | 0.3333 | 0.0739 |
| 3m | 1157 | 232 | 18 | 0.0776 | 0.7591 | 0.0693 | 0.2621 | 0.0900 | 0.2090 | 0.7778 | 0.3294 | 0.0739 |
| 2m | 1158 | 232 | 18 | 0.0776 | 0.7871 | 0.0661 | 0.2454 | 0.1700 | 0.5000 | 0.2778 | 0.3571 | 0.0936 |
| 1m | 1159 | 232 | 18 | 0.0776 | 0.8712 | 0.0527 | 0.2286 | 0.2600 | 0.6000 | 0.5000 | 0.5455 | 0.1083 |

Combined horizon performance plot: `plots/horizon_auc_brier_summary.png`

## Trading Threshold Coverage

Counts below are on the final test set only. A contract is `tradable` when one buy-side
combination is cheaper than 0.98:
`min(kalshi_yes_ask + polymarket_no_ask, kalshi_no_ask + polymarket_yes_ask) < 0.98`.
`Pass` means `diverge_prob` is below that horizon's recommended trading threshold.
`Expected return` is per executed trade after the filter: `1.0 - mean_entry_cost - mean_predicted_diverge_prob`,
assuming a divergence pays 0 and non-divergence pays 1.00.
`Test return` uses the actual held-out divergence rate instead: `1.0 - mean_entry_cost - actual_diverge_rate`.

| horizon | recommended_trade_threshold | tradable_test_contracts | trade_threshold_pass_contracts | trade_threshold_fail_contracts | trade_threshold_pass_divergences | trade_threshold_fail_divergences | trade_threshold_pass_diverge_rate | trade_threshold_fail_diverge_rate | trade_threshold_pass_mean_entry_cost | trade_threshold_fail_mean_entry_cost | trade_threshold_pass_mean_predicted_diverge_prob | trade_threshold_pass_expected_return | trade_threshold_pass_test_return | trade_threshold_pass_mean_arb_return | trade_threshold_fail_mean_arb_return |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 5m | 0.0739 | 107 | 43 | 64 | 3 | 10 | 0.0698 | 0.1562 | 0.8615 | 0.9104 | 0.0503 | 0.0882 | 0.0687 | 0.1385 | 0.0896 |
| 3m | 0.0739 | 124 | 55 | 69 | 2 | 13 | 0.0364 | 0.1884 | 0.7393 | 0.8945 | 0.0460 | 0.2148 | 0.2244 | 0.2607 | 0.1055 |
| 2m | 0.0936 | 139 | 91 | 48 | 1 | 12 | 0.0110 | 0.2500 | 0.7239 | 0.8409 | 0.0454 | 0.2307 | 0.2651 | 0.2761 | 0.1591 |
| 1m | 0.1083 | 182 | 140 | 42 | 2 | 14 | 0.0143 | 0.3333 | 0.7087 | 0.7669 | 0.0322 | 0.2591 | 0.2770 | 0.2913 | 0.2331 |

## 5m Model Card

- Model artifact: `divergence_horizon_5m_model.pkl`
- Feature JSON: `divergence_horizon_5m_feature_list.json`
- Metadata JSON: `divergence_horizon_5m_metadata.json`
- Calibration plot: `plots/divergence_horizon_5m_calibration.png`
- Feature-importance plot: `plots/divergence_horizon_5m_feature_importance.png`
- Test AUC: `0.6789`; Brier: `0.0684`; F1: `0.3333`.
- Recommended trading filter threshold: `diverge_prob < 0.0739`.
- Tradable final-test contracts: `107`; pass threshold: `43`; fail threshold: `64`.
- Pass/fail observed divergence rates: `0.0698` / `0.1562`.
- Filtered expected return per executed trade: `0.0882`.
- Filtered test return per executed trade: `0.0687`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| price_spread_roll10_std_change | 0.0236 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_last | 0.0219 | abs_scaled_logit_coefficient |
| price_spread_std | 0.0195 | abs_scaled_logit_coefficient |
| kalshi_btc_price_lag5_change | 0.0191 | abs_scaled_logit_coefficient |
| window_rows | 0.0174 | abs_scaled_logit_coefficient |
| kalshi_last_price_change | 0.0168 | abs_scaled_logit_coefficient |
| feeds_on_same_side_x_elapsed_fraction_std | 0.0159 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_std_range | 0.0156 | abs_scaled_logit_coefficient |
| price_spread_abs_x_elapsed_fraction_std | 0.0145 | abs_scaled_logit_coefficient |
| spread_vs_distance_ratio_x_elapsed_fraction_mean | 0.0137 | abs_scaled_logit_coefficient |
| spread_vs_distance_ratio_mean | 0.0133 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_mean | 0.0132 | abs_scaled_logit_coefficient |
| feeds_on_same_side_std | 0.0123 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_10_mean | 0.0122 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_mean_range | 0.0121 | abs_scaled_logit_coefficient |

## 3m Model Card

- Model artifact: `divergence_horizon_3m_model.pkl`
- Feature JSON: `divergence_horizon_3m_feature_list.json`
- Metadata JSON: `divergence_horizon_3m_metadata.json`
- Calibration plot: `plots/divergence_horizon_3m_calibration.png`
- Feature-importance plot: `plots/divergence_horizon_3m_feature_importance.png`
- Test AUC: `0.7591`; Brier: `0.0693`; F1: `0.3294`.
- Recommended trading filter threshold: `diverge_prob < 0.0739`.
- Tradable final-test contracts: `124`; pass threshold: `55`; fail threshold: `69`.
- Pass/fail observed divergence rates: `0.0364` / `0.1884`.
- Filtered expected return per executed trade: `0.2148`.
- Filtered test return per executed trade: `0.2244`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| implied_prob_spread_roll10_std_change | 0.0281 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_mean | 0.0188 | abs_scaled_logit_coefficient |
| kalshi_yes_mid_range | 0.0185 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_mean_std | 0.0173 | abs_scaled_logit_coefficient |
| price_spread_mean | 0.0163 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_last | 0.0159 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_last | 0.0155 | abs_scaled_logit_coefficient |
| kalshi_distance_to_target_range | 0.0132 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_last | 0.0132 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_10_min | 0.0131 | abs_scaled_logit_coefficient |
| polymarket_order_book_imbalance_max | 0.0130 | abs_scaled_logit_coefficient |
| polymarket_yes_mid_std | 0.0128 | abs_scaled_logit_coefficient |
| kalshi_last_price_std | 0.0125 | abs_scaled_logit_coefficient |
| price_spread_abs_range | 0.0123 | abs_scaled_logit_coefficient |
| kalshi_yes_mid_max | 0.0122 | abs_scaled_logit_coefficient |

## 2m Model Card

- Model artifact: `divergence_horizon_2m_model.pkl`
- Feature JSON: `divergence_horizon_2m_feature_list.json`
- Metadata JSON: `divergence_horizon_2m_metadata.json`
- Calibration plot: `plots/divergence_horizon_2m_calibration.png`
- Feature-importance plot: `plots/divergence_horizon_2m_feature_importance.png`
- Test AUC: `0.7871`; Brier: `0.0661`; F1: `0.3571`.
- Recommended trading filter threshold: `diverge_prob < 0.0936`.
- Tradable final-test contracts: `139`; pass threshold: `91`; fail threshold: `48`.
- Pass/fail observed divergence rates: `0.0110` / `0.2500`.
- Filtered expected return per executed trade: `0.2307`.
- Filtered test return per executed trade: `0.2651`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| kalshi_distance_to_target_change | 0.0226 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_10_std | 0.0159 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_max | 0.0155 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_min | 0.0147 | abs_scaled_logit_coefficient |
| kalshi_btc_price_lag5_change | 0.0144 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_mean | 0.0135 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_mean | 0.0134 | abs_scaled_logit_coefficient |
| entry_edge_last | 0.0134 | abs_scaled_logit_coefficient |
| best_entry_cost_last | 0.0134 | abs_scaled_logit_coefficient |
| polymarket_order_book_imbalance_last | 0.0126 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_mean | 0.0123 | abs_scaled_logit_coefficient |
| feeds_on_same_side_x_elapsed_fraction_last | 0.0111 | abs_scaled_logit_coefficient |
| feeds_on_same_side_last | 0.0110 | abs_scaled_logit_coefficient |
| polymarket_distance_to_target_change | 0.0110 | abs_scaled_logit_coefficient |
| kalshi_btc_price_lag5_range | 0.0110 | abs_scaled_logit_coefficient |

## 1m Model Card

- Model artifact: `divergence_horizon_1m_model.pkl`
- Feature JSON: `divergence_horizon_1m_feature_list.json`
- Metadata JSON: `divergence_horizon_1m_metadata.json`
- Calibration plot: `plots/divergence_horizon_1m_calibration.png`
- Feature-importance plot: `plots/divergence_horizon_1m_feature_importance.png`
- Test AUC: `0.8712`; Brier: `0.0527`; F1: `0.5455`.
- Recommended trading filter threshold: `diverge_prob < 0.1083`.
- Tradable final-test contracts: `182`; pass threshold: `140`; fail threshold: `42`.
- Pass/fail observed divergence rates: `0.0143` / `0.3333`.
- Filtered expected return per executed trade: `0.2591`.
- Filtered test return per executed trade: `0.2770`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| polymarket_order_book_imbalance_std | 0.0302 | abs_scaled_logit_coefficient |
| kalshi_yes_mid_std | 0.0174 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_std | 0.0153 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_min | 0.0139 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_10_last | 0.0131 | abs_scaled_logit_coefficient |
| entry_edge_change | 0.0117 | abs_scaled_logit_coefficient |
| best_entry_cost_change | 0.0117 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_range | 0.0116 | abs_scaled_logit_coefficient |
| window_rows | 0.0116 | abs_scaled_logit_coefficient |
| feeds_on_same_side_x_elapsed_fraction_last | 0.0114 | abs_scaled_logit_coefficient |
| feeds_on_same_side_last | 0.0114 | abs_scaled_logit_coefficient |
| kalshi_last_price_change | 0.0113 | abs_scaled_logit_coefficient |
| price_spread_mean | 0.0112 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_mean | 0.0108 | abs_scaled_logit_coefficient |
| best_entry_cost_last | 0.0108 | abs_scaled_logit_coefficient |

## Interpretation

This contract-level framing removes the repeated-row autocorrelation from the prior live-snapshot model.
The tradeoff is sample size: each horizon has roughly one thousand contracts and fewer than one hundred
positive examples after quality filters, so calibration and feature rankings should be monitored closely
as new contracts arrive.

## Limitations

- Labels still come from sampled settlement rows, not official settlement adjudication records.
- The model sees only the trailing one-minute aggregate at the requested horizon; it deliberately ignores earlier contract path information.
- Calibration uses a small held-out calibration set, so probability estimates can move materially with more data.
- The proxy trading threshold treats divergence as a 1-unit loss and non-divergence as earning the observed arb edge.
