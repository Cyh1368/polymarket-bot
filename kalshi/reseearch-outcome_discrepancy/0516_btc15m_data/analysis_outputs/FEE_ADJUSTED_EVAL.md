# Fee-Adjusted Arb Evaluation

Fee model:

```text
normalized positive settlement payout: 1.00 gross -> 0.98 net
adverse settlement payout: 0.00 -> 0.00
fee_adjusted_pnl = fee_adjusted_payout - arb_cost
```

This is applied per contract entry, not per tick.

## Canonical Entry Filter

Canonical entry rule:

```text
price_direction_agreement
AND source_price_gap <= 5
AND min_distance_from_target >= max(10, seconds_to_expiry * 0.05)
AND abs_target_divergence <= 35
```

Contract-level result:

| metric | pre-fee | fee-adjusted |
|---|---:|---:|
| entries | 44 | 44 |
| safe entries | 40 | 40 |
| dangerous entries | 4 | 4 |
| total PnL | -2.141 | -2.941 |
| avg PnL / entry | -0.0487 | -0.0668 |
| avg safe gain | +0.0330 | +0.0130 |
| avg dangerous loss | -0.8650 | -0.8650 |
| breakeven dangerous rate | 3.67% | 1.48% |

Fees materially change the strategy. The average safe winner falls from `+0.0330` to only `+0.0130`, so the strategy can tolerate very little residual discrepancy risk.

## Spread Viability

For the 40 safe canonical entries:

```text
24 / 40 have arb_cost >= 0.98
16 / 40 have arb_cost < 0.98
10 / 40 have arb_cost <= 0.96
```

Interpretation:

- `arb_cost <= 0.98` is fee-adjusted breakeven for a normal one-winner arb.
- `arb_cost <= 0.96` leaves at least `+0.02` net after the 2-cent fee.
- Without a minimum spread threshold, most safe entries are too thin to matter after fees.

## Spread Threshold Variants

These rows are from `contract_filter_eval.csv`.

| filter | entries | safe | dangerous | fee-adjusted PnL | avg fee-adjusted PnL |
|---|---:|---:|---:|---:|---:|
| canonical | 44 | 40 | 4 | -2.941 | -0.0668 |
| canonical + `arb_cost <= 0.98` | 44 | 40 | 4 | -2.715 | -0.0617 |
| canonical + `arb_cost <= 0.96` | 42 | 37 | 5 | -1.025 | -0.0244 |
| canonical + `arb_cost <= 0.95` | 40 | 35 | 5 | +0.483 | +0.0121 |

Important caveat: these spread-threshold variants choose the first tick per contract that passes the full filter including spread. That can move the entry later in the contract and can change which dangerous contracts are admitted. The apparent improvement at `arb_cost <= 0.95` is sample-specific and should not be treated as robust with only 48 contracts.

## Revised Entry Implication

The entry filter now needs two layers:

```text
1. risk screen:
   direction_agreement
   AND source_gap <= 5
   AND min_distance >= max(10, seconds_to_expiry * 0.05)
   AND abs_target_divergence <= 35

2. spread screen:
   arb_cost <= 0.96   # minimum +2c net edge after 2c fee
```

For a stricter production default, use `arb_cost <= 0.95` until more data shows the residual discrepancy rate is below the fee-adjusted breakeven level.
