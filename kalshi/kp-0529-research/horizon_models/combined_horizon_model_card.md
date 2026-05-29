# Contract-Level Horizon Divergence Models

Generated: 2026-05-29T20:34:44+00:00

## Setup

- Unit of analysis: one row per contract per prediction horizon.
- Horizon rows use only the trailing 60 seconds ending at 10m, 5m, 3m, 2m, or 1m before `kalshi_close_time`.
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

| horizon | contracts_total | contracts_test | test_divergences | test_base_rate | auc_roc | brier | log_loss | classification_threshold | precision | recall | f1 | recommended_trade_threshold |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 10m | 1158 | 232 | 18 | 0.0776 | 0.5922 | 0.0713 | 0.2711 | 0.0800 | 0.0989 | 0.5000 | 0.1651 | 0.0739 |
| 5m | 1157 | 232 | 18 | 0.0776 | 0.7072 | 0.0677 | 0.2527 | 0.1400 | 0.5000 | 0.3333 | 0.4000 | 0.0493 |
| 3m | 1157 | 232 | 18 | 0.0776 | 0.7417 | 0.0696 | 0.2619 | 0.0900 | 0.2131 | 0.7222 | 0.3291 | 0.0788 |
| 2m | 1158 | 232 | 18 | 0.0776 | 0.8126 | 0.0641 | 0.2418 | 0.2000 | 0.6667 | 0.3333 | 0.4444 | 0.0788 |
| 1m | 1159 | 232 | 18 | 0.0776 | 0.8959 | 0.0538 | 0.2247 | 0.1500 | 0.4828 | 0.7778 | 0.5957 | 0.1378 |

Combined horizon performance plot: `plots/horizon_auc_brier_summary.png`

## Trading Threshold Coverage

Counts below are on the final test set only. A contract is `tradable` when one buy-side
combination has positive fee-adjusted edge:
`min(raw_combo_cost + Kalshi_fee + Polymarket_fee) < 1.0`.
Fees are computed per contract with `N=1`: `Kalshi_fee = 0.07 * N * p * (1-p)`
and `Polymarket_fee = 0.05 * N * p * (1-p)`.
`Pass` means `diverge_prob` is below that horizon's recommended trading threshold.
`Expected return` is per executed trade after the filter: `1.0 - mean_all_in_cost - mean_predicted_diverge_prob`,
assuming a divergence pays 0 and non-divergence pays 1.00.
`Test return` uses the actual held-out divergence rate instead: `1.0 - mean_all_in_cost - actual_diverge_rate`.

| horizon | recommended_trade_threshold | tradable_test_contracts | trade_threshold_pass_contracts | trade_threshold_fail_contracts | trade_threshold_pass_divergences | trade_threshold_fail_divergences | trade_threshold_pass_diverge_rate | trade_threshold_fail_diverge_rate | trade_threshold_pass_mean_raw_entry_cost | trade_threshold_pass_mean_total_fee | trade_threshold_pass_mean_all_in_cost | trade_threshold_fail_mean_all_in_cost | trade_threshold_pass_mean_predicted_diverge_prob | trade_threshold_pass_expected_return | trade_threshold_pass_test_return | trade_threshold_pass_mean_fee_adjusted_edge | trade_threshold_fail_mean_fee_adjusted_edge |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 10m | 0.0739 | 122 | 26 | 96 | 1 | 11 | 0.0385 | 0.1146 | 0.9513 | 0.0201 | 0.9714 | 0.9649 | 0.0688 | -0.0402 | -0.0099 | 0.0286 | 0.0351 |
| 5m | 0.0493 | 140 | 27 | 113 | 1 | 13 | 0.0370 | 0.1150 | 0.8767 | 0.0102 | 0.8869 | 0.9399 | 0.0308 | 0.0823 | 0.0761 | 0.1131 | 0.0601 |
| 3m | 0.0788 | 167 | 98 | 69 | 3 | 12 | 0.0306 | 0.1739 | 0.8409 | 0.0085 | 0.8494 | 0.9252 | 0.0571 | 0.0935 | 0.1200 | 0.1506 | 0.0748 |
| 2m | 0.0788 | 187 | 117 | 70 | 1 | 13 | 0.0085 | 0.1857 | 0.7910 | 0.0076 | 0.7985 | 0.8913 | 0.0372 | 0.1643 | 0.1929 | 0.2015 | 0.1087 |
| 1m | 0.1378 | 211 | 178 | 33 | 3 | 14 | 0.0169 | 0.4242 | 0.7727 | 0.0082 | 0.7808 | 0.7326 | 0.0423 | 0.1769 | 0.2023 | 0.2192 | 0.2674 |

## 10m Model Card

- Model artifact: `divergence_horizon_10m_model.pkl`
- Feature JSON: `divergence_horizon_10m_feature_list.json`
- Metadata JSON: `divergence_horizon_10m_metadata.json`
- Calibration plot: `plots/divergence_horizon_10m_calibration.png`
- Feature-importance plot: `plots/divergence_horizon_10m_feature_importance.png`
- Test AUC: `0.5922`; Brier: `0.0713`; F1: `0.1651`.
- Recommended trading filter threshold: `diverge_prob < 0.0739`.
- Tradable final-test contracts: `122`; pass threshold: `26`; fail threshold: `96`.
- Pass/fail observed divergence rates: `0.0385` / `0.1146`.
- Filtered fee-adjusted expected return per executed trade: `-0.0402`.
- Filtered fee-adjusted test return per executed trade: `-0.0099`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| k_yes_p_no_polymarket_fee_std | 0.0150 | abs_scaled_logit_coefficient |
| k_no_p_yes_polymarket_fee_std | 0.0146 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_change | 0.0142 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_std_last | 0.0126 | abs_scaled_logit_coefficient |
| window_rows | 0.0124 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_range | 0.0123 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_last | 0.0122 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_min | 0.0119 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_10_last | 0.0118 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_last | 0.0107 | abs_scaled_logit_coefficient |
| best_raw_entry_cost_std | 0.0104 | abs_scaled_logit_coefficient |
| kalshi_last_price_std | 0.0100 | abs_scaled_logit_coefficient |
| spread_vs_distance_ratio_min | 0.0098 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_mean | 0.0097 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_std_std | 0.0095 | abs_scaled_logit_coefficient |

## 5m Model Card

- Model artifact: `divergence_horizon_5m_model.pkl`
- Feature JSON: `divergence_horizon_5m_feature_list.json`
- Metadata JSON: `divergence_horizon_5m_metadata.json`
- Calibration plot: `plots/divergence_horizon_5m_calibration.png`
- Feature-importance plot: `plots/divergence_horizon_5m_feature_importance.png`
- Test AUC: `0.7072`; Brier: `0.0677`; F1: `0.4000`.
- Recommended trading filter threshold: `diverge_prob < 0.0493`.
- Tradable final-test contracts: `140`; pass threshold: `27`; fail threshold: `113`.
- Pass/fail observed divergence rates: `0.0370` / `0.1150`.
- Filtered fee-adjusted expected return per executed trade: `0.0823`.
- Filtered fee-adjusted test return per executed trade: `0.0761`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| implied_prob_spread_roll10_std_last | 0.0199 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_std_range | 0.0169 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_change | 0.0165 | abs_scaled_logit_coefficient |
| price_spread_std | 0.0147 | abs_scaled_logit_coefficient |
| kalshi_btc_price_lag5_change | 0.0145 | abs_scaled_logit_coefficient |
| window_rows | 0.0138 | abs_scaled_logit_coefficient |
| kalshi_last_price_change | 0.0131 | abs_scaled_logit_coefficient |
| feeds_on_same_side_x_elapsed_fraction_std | 0.0129 | abs_scaled_logit_coefficient |
| spread_vs_distance_ratio_x_elapsed_fraction_mean | 0.0123 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_10_min | 0.0121 | abs_scaled_logit_coefficient |
| spread_vs_distance_ratio_mean | 0.0120 | abs_scaled_logit_coefficient |
| kalshi_distance_to_target_change | 0.0116 | abs_scaled_logit_coefficient |
| price_spread_abs_x_elapsed_fraction_std | 0.0112 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_change | 0.0111 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_min | 0.0107 | abs_scaled_logit_coefficient |

## 3m Model Card

- Model artifact: `divergence_horizon_3m_model.pkl`
- Feature JSON: `divergence_horizon_3m_feature_list.json`
- Metadata JSON: `divergence_horizon_3m_metadata.json`
- Calibration plot: `plots/divergence_horizon_3m_calibration.png`
- Feature-importance plot: `plots/divergence_horizon_3m_feature_importance.png`
- Test AUC: `0.7417`; Brier: `0.0696`; F1: `0.3291`.
- Recommended trading filter threshold: `diverge_prob < 0.0788`.
- Tradable final-test contracts: `167`; pass threshold: `98`; fail threshold: `69`.
- Pass/fail observed divergence rates: `0.0306` / `0.1739`.
- Filtered fee-adjusted expected return per executed trade: `0.0935`.
- Filtered fee-adjusted test return per executed trade: `0.1200`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| implied_prob_spread_roll10_std_change | 0.0206 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_mean_std | 0.0143 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_mean | 0.0135 | abs_scaled_logit_coefficient |
| price_spread_mean | 0.0122 | abs_scaled_logit_coefficient |
| price_spread_abs_range | 0.0118 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_last | 0.0117 | abs_scaled_logit_coefficient |
| price_spread_abs_max | 0.0115 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_max | 0.0115 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_last | 0.0113 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_last | 0.0113 | abs_scaled_logit_coefficient |
| polymarket_order_book_imbalance_max | 0.0109 | abs_scaled_logit_coefficient |
| k_no_p_yes_polymarket_fee_change | 0.0105 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_std | 0.0105 | abs_scaled_logit_coefficient |
| k_yes_p_no_polymarket_fee_change | 0.0105 | abs_scaled_logit_coefficient |
| price_spread_abs_x_elapsed_fraction_range | 0.0097 | abs_scaled_logit_coefficient |

## 2m Model Card

- Model artifact: `divergence_horizon_2m_model.pkl`
- Feature JSON: `divergence_horizon_2m_feature_list.json`
- Metadata JSON: `divergence_horizon_2m_metadata.json`
- Calibration plot: `plots/divergence_horizon_2m_calibration.png`
- Feature-importance plot: `plots/divergence_horizon_2m_feature_importance.png`
- Test AUC: `0.8126`; Brier: `0.0641`; F1: `0.4444`.
- Recommended trading filter threshold: `diverge_prob < 0.0788`.
- Tradable final-test contracts: `187`; pass threshold: `117`; fail threshold: `70`.
- Pass/fail observed divergence rates: `0.0085` / `0.1857`.
- Filtered fee-adjusted expected return per executed trade: `0.1643`.
- Filtered fee-adjusted test return per executed trade: `0.1929`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| kalshi_distance_to_target_change | 0.0193 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_10_std | 0.0134 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_max | 0.0122 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_mean | 0.0116 | abs_scaled_logit_coefficient |
| kalshi_btc_price_lag5_change | 0.0116 | abs_scaled_logit_coefficient |
| polymarket_order_book_imbalance_last | 0.0113 | abs_scaled_logit_coefficient |
| polymarket_order_book_imbalance_std | 0.0111 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_min | 0.0108 | abs_scaled_logit_coefficient |
| feeds_on_same_side_x_elapsed_fraction_last | 0.0100 | abs_scaled_logit_coefficient |
| feeds_on_same_side_last | 0.0099 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_std | 0.0097 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_mean | 0.0091 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_change | 0.0087 | abs_scaled_logit_coefficient |
| kalshi_btc_price_roll10_std_mean | 0.0085 | abs_scaled_logit_coefficient |
| kalshi_btc_price_lag10_change | 0.0078 | abs_scaled_logit_coefficient |

## 1m Model Card

- Model artifact: `divergence_horizon_1m_model.pkl`
- Feature JSON: `divergence_horizon_1m_feature_list.json`
- Metadata JSON: `divergence_horizon_1m_metadata.json`
- Calibration plot: `plots/divergence_horizon_1m_calibration.png`
- Feature-importance plot: `plots/divergence_horizon_1m_feature_importance.png`
- Test AUC: `0.8959`; Brier: `0.0538`; F1: `0.5957`.
- Recommended trading filter threshold: `diverge_prob < 0.1378`.
- Tradable final-test contracts: `211`; pass threshold: `178`; fail threshold: `33`.
- Pass/fail observed divergence rates: `0.0169` / `0.4242`.
- Filtered fee-adjusted expected return per executed trade: `0.1769`.
- Filtered fee-adjusted test return per executed trade: `0.2023`.

Top features:

| feature | importance_normalized | importance_type |
| --- | --- | --- |
| polymarket_order_book_imbalance_std | 0.0275 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_mean | 0.0162 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_last | 0.0153 | abs_scaled_logit_coefficient |
| kalshi_yes_mid_std | 0.0127 | abs_scaled_logit_coefficient |
| polymarket_order_book_imbalance_change | 0.0127 | abs_scaled_logit_coefficient |
| kalshi_bid_ask_spread_yes_std | 0.0113 | abs_scaled_logit_coefficient |
| price_spread_roll10_std_std | 0.0112 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_range | 0.0105 | abs_scaled_logit_coefficient |
| k_yes_p_no_kalshi_fee_last | 0.0101 | abs_scaled_logit_coefficient |
| kalshi_order_book_imbalance_mean | 0.0100 | abs_scaled_logit_coefficient |
| k_no_p_yes_kalshi_fee_last | 0.0096 | abs_scaled_logit_coefficient |
| feeds_on_same_side_x_elapsed_fraction_last | 0.0095 | abs_scaled_logit_coefficient |
| feeds_on_same_side_last | 0.0095 | abs_scaled_logit_coefficient |
| kalshi_btc_price_momentum_5_max | 0.0094 | abs_scaled_logit_coefficient |
| implied_prob_spread_roll10_std_max | 0.0094 | abs_scaled_logit_coefficient |

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
