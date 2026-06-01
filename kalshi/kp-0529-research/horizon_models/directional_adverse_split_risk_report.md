# Directional Adverse Split Risk

## Scope

- Data: `kp-0529-research/horizon_models/profit_margin_latch_2m_1m_poly_price_floor_trades.csv` joined to `kp-0529-research/horizon_models/horizon_contract_labels.csv`.
- Strategy slice: `latch_2m_1m` first entry after latch.
- Profit margin: `0.18`.
- Polymarket leg price floor: `33c`.
- `K+NP` means buy Kalshi YES and Polymarket NO.
- `NK+P` means buy Kalshi NO and Polymarket YES.
- Adverse split means both legs lose:
  - `K+NP` adverse: Kalshi settles NO and Polymarket settles YES.
  - `NK+P` adverse: Kalshi settles YES and Polymarket settles NO.

## Directional Rates

| sample | direction | trades | divergences | favorable_splits | adverse_splits | adverse_split_rate | adverse_rate_ci_low | adverse_rate_ci_high | mean_all_in_cost | mean_polymarket_price |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| calibration | K+NP | 84 | 4 | 0 | 4 | 0.0476 | 0.0187 | 0.1161 | 0.6134 | 0.5868 |
| calibration | NK+P | 80 | 5 | 1 | 4 | 0.0500 | 0.0196 | 0.1216 | 0.6130 | 0.5796 |
| test | K+NP | 76 | 0 | 0 | 0 | 0.0000 | 0.0000 | 0.0481 | 0.5969 | 0.5680 |
| test | NK+P | 82 | 4 | 2 | 2 | 0.0244 | 0.0067 | 0.0846 | 0.6156 | 0.5873 |
| all | K+NP | 380 | 4 | 0 | 4 | 0.0105 | 0.0041 | 0.0267 | 0.6098 | 0.5845 |
| all | NK+P | 371 | 9 | 3 | 6 | 0.0162 | 0.0074 | 0.0348 | 0.6129 | 0.5849 |

## Difference Test

| sample | k_plus_np_adverse_rate | nk_plus_p_adverse_rate | rate_difference_nk_minus_k | fisher_exact_p_value |
| --- | --- | --- | --- | --- |
| calibration | 0.0476 | 0.0500 | 0.0024 | 1.0000 |
| test | 0.0000 | 0.0244 | 0.0244 | 0.4975 |
| all | 0.0105 | 0.0162 | 0.0056 | 0.5416 |

## Conclusion

The two directional adverse-split rates are not statistically distinguishable in the full historical sample.

In the `all` sample, `K+NP` adverse-split risk is 1.05%, while `NK+P` adverse-split risk is 1.62%. The point estimates differ by 0.56%, but the Fisher exact p-value is 0.5416.

This supports training direction-aware models because the economic outcome differs by direction, but this specific historical slice does not prove that one direction has a reliably higher adverse-split base rate than the other.

## Output

- CSV summary: `kp-0529-research/horizon_models/directional_adverse_split_risk.csv`
