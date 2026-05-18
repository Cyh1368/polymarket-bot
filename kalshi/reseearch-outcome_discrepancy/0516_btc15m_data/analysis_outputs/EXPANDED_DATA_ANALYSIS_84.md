# Expanded 0516 BTC Data Analysis

Run date: after the dataset expanded to 84 `combined_KXBTC15M-*.csv` files.

Generated outputs refreshed:

- `contract_summary.csv`
- `discrepancies.csv`
- `arb_ticks.csv`
- `contract_filter_eval.csv`
- `contract_entries.csv`
- `hold_filter_current_eval_84.csv`

## Dataset Summary

| metric | value |
|---|---:|
| combined CSV contracts | 84 |
| known outcome discrepancies | 14 |
| discrepancy rate | 16.67% |
| contracts with unknown outcome | 1 |
| contracts with any arb opportunity | 84 |
| contracts with dangerous arb ticks | 14 |
| arb ticks after filtering bad Polymarket rows | 28,621 |
| labeled arb ticks | 28,571 |
| dangerous arb ticks | 4,607 |

The discrepancy rate stayed exactly the same as the smaller 48-contract set: `14 / 84 = 16.67%`.

One contract still has unknown Polymarket outcome because no finite `polymarket_btc_target` is present:

```text
KXBTC15M-26MAY162345-45
```

## Discrepant Contracts

The known discrepant contracts are:

```text
KXBTC15M-26MAY170445-45
KXBTC15M-26MAY170500-00
KXBTC15M-26MAY170530-30
KXBTC15M-26MAY170800-00
KXBTC15M-26MAY170815-15
KXBTC15M-26MAY170830-30
KXBTC15M-26MAY170930-30
KXBTC15M-26MAY171030-30
KXBTC15M-26MAY171430-30
KXBTC15M-26MAY171445-45
KXBTC15M-26MAY171700-00
KXBTC15M-26MAY171715-15
KXBTC15M-26MAY171745-45
KXBTC15M-26MAY172030-30
```

The additional CSVs introduced 6 new known discrepancies beyond the earlier 8-contract discrepancy set.

## Contract-Level Filter Results

These are one-entry-per-contract results. This is the correct strategy accounting unit.

| filter | entries | safe | dangerous | dangerous rate | pre-fee PnL | fee-adjusted PnL | avg fee PnL |
|---|---:|---:|---:|---:|---:|---:|---:|
| no filter, first arb | 83 | 70 | 13 | 15.66% | -9.620 | -11.020 | -0.1328 |
| risk screen only: time-scaled + gap <= 100 + target <= 35 | 77 | 70 | 7 | 9.09% | -3.733 | -5.133 | -0.0667 |
| risk screen + arb_cost <= 0.98 | 77 | 70 | 7 | 9.09% | -2.754 | -4.154 | -0.0539 |
| risk screen + arb_cost <= 0.96 | 74 | 66 | 8 | 10.81% | +0.194 | -1.126 | -0.0152 |
| risk screen + arb_cost <= 0.95 | 70 | 62 | 8 | 11.43% | +2.036 | +0.796 | +0.0114 |

The fee-adjusted result is still negative at `arb_cost <= 0.96`. The stricter `arb_cost <= 0.95` threshold is positive on this sample, but that result is fragile: it still admits 8 dangerous contracts and relies on larger safe spreads offsetting discrepancy losses.

## Fee Impact

Under the risk screen without a spread threshold:

```text
safe entries: 70
safe entries with cost >= 0.98: 38
safe entries with cost <= 0.96: 19
```

This confirms the earlier conclusion: many visually safe arbs are unusable after a 2% winning-payout fee. A spread threshold is required.

With `arb_cost <= 0.96`:

```text
entries: 74
safe: 66
dangerous: 8
fee-adjusted PnL: -1.126
```

With `arb_cost <= 0.95`:

```text
entries: 70
safe: 62
dangerous: 8
fee-adjusted PnL: +0.796
```

The spread filter improves fee-adjusted EV by increasing average safe gain, but it does not reduce residual discrepancy count in this expanded sample.

## Residual Dangerous Entries

The `arb_cost <= 0.96` and `arb_cost <= 0.95` filters both admit 8 dangerous contracts:

```text
KXBTC15M-26MAY170445-45
KXBTC15M-26MAY170530-30
KXBTC15M-26MAY170800-00
KXBTC15M-26MAY170815-15
KXBTC15M-26MAY170930-30
KXBTC15M-26MAY171430-30
KXBTC15M-26MAY171715-15
KXBTC15M-26MAY171745-45
```

These are not all near-threshold at entry. Several have large apparent cushions and large apparent spreads. That reinforces the core risk: the entry screen can reduce discrepancy exposure but cannot eliminate it.

## Spread Threshold Scan

Risk screen plus varying `arb_cost` cap:

| max arb cost | entries | safe | dangerous | danger rate | fee-adjusted PnL | avg fee PnL |
|---:|---:|---:|---:|---:|---:|---:|
| 0.99 | 77 | 70 | 7 | 9.1% | -5.099 | -0.0662 |
| 0.98 | 77 | 70 | 7 | 9.1% | -4.154 | -0.0539 |
| 0.97 | 76 | 69 | 7 | 9.2% | -2.356 | -0.0310 |
| 0.96 | 74 | 66 | 8 | 10.8% | -1.126 | -0.0152 |
| 0.95 | 70 | 62 | 8 | 11.4% | +0.796 | +0.0114 |
| 0.94 | 68 | 60 | 8 | 11.8% | +2.568 | +0.0378 |
| 0.93 | 64 | 56 | 8 | 12.5% | +3.248 | +0.0507 |
| 0.90 | 55 | 47 | 8 | 14.5% | +4.236 | +0.0770 |
| 0.85 | 43 | 36 | 7 | 16.3% | +6.143 | +0.1429 |
| 0.80 | 35 | 31 | 4 | 11.4% | +8.515 | +0.2433 |

This does not mean lower cost thresholds are automatically better. Lower thresholds enter later/different ticks and shrink sample size. The positive results are likely selection-sensitive and should be validated on more days.

## Hold / Exit Monitor

Current production-style entry with `arb_cost <= 0.96`:

```text
entries: 74
dangerous entries: 8
hold-to-expiry fee PnL: -1.126
```

Distance-only hold behavior:

| hold distance rule | immediate exits | any exit before expiry | held to expiry | dangerous held | fee PnL if exits flatten |
|---|---:|---:|---:|---:|---:|
| 1.0x entry distance | 0 | 46 | 28 | 0 | +4.264 |
| 1.25x entry distance | 22 | 52 | 22 | 0 | +4.097 |
| 1.5x entry distance | 32 | 54 | 20 | 0 | +4.036 |
| fixed `$25` | 15 | 50 | 24 | 0 | +4.137 |

Full hold logic including `source_gap <= 100`, direction agreement, and target cap exits almost everything:

```text
73 / 74 positions exit before expiry
1 / 74 held to expiry
```

This confirms the prior caveat: full hold logic is too reactive unless the bot has a real unwind model and can exit cheaply. Distance-only hold monitoring is more useful as a risk stop.

For `arb_cost <= 0.95`, distance-only hold still stops all 8 dangerous entries before expiry and leaves 30 contracts held under the 1.0x rule, but full hold logic again exits 69 / 70 positions.

## Interpretation

The expanded dataset confirms the earlier discrepancy rate and the central risk: outcome disagreement is common enough that thin arbs are not viable after fees. The risk screen improves the baseline, but on its own it remains negative EV. Adding a spread threshold is mandatory; `arb_cost <= 0.96` is still negative after fees, while `<= 0.95` is positive in this sample but still admits 8 dangerous contracts. That positive result should be treated as provisional because it depends on larger spreads absorbing discrepancy losses rather than on eliminating the discrepancy risk. The hold monitor is promising as a stop mechanism: distance-only hold exits would have stopped all dangerous entries in this dataset, but full hold logic exits nearly everything and needs careful unwind pricing/hysteresis before live deployment.

Current practical recommendation:

```text
Entry:
  direction_agreement
  source_gap <= 100
  min_distance >= max(10, seconds_to_expiry * 0.05)
  abs_target_divergence <= 35
  arb_cost <= 0.95

Hold:
  distance-only monitor as primary stop:
  min_distance >= 1.0x to 1.25x * max(10, seconds_to_expiry * 0.05)

Exit:
  only execute if bid-side unwind value after fees is better than expected hold risk.
```
