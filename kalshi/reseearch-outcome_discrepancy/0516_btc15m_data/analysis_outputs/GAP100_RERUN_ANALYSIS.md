# Gap 100 Rerun Analysis

This rerun uses the updated source-gap threshold:

```text
source_gap <= 100
```

Dataset:

```text
contracts: 84
known discrepancies: 14
discrepancy rate: 16.67%
arb ticks: 28,621
dangerous arb ticks: 4,607
```

## Contract-Level Results

Correct accounting is one first allowed entry per contract.

| filter | entries | safe | dangerous | dangerous rate | fee PnL | avg fee PnL |
|---|---:|---:|---:|---:|---:|---:|
| no filter, first arb | 83 | 70 | 13 | 15.66% | -11.020 | -0.1328 |
| risk screen, gap <= 100, target <= 35 | 78 | 70 | 8 | 10.26% | -6.086 | -0.0780 |
| gap100 + arb_cost <= 0.98 | 78 | 70 | 8 | 10.26% | -5.564 | -0.0713 |
| gap100 + arb_cost <= 0.96 | 77 | 68 | 9 | 11.69% | -4.032 | -0.0524 |
| gap100 + arb_cost <= 0.95 | 76 | 68 | 8 | 10.53% | -2.037 | -0.0268 |

Raising the source-gap threshold from 5 to 100 materially worsens the entry filter. The previous positive fee-adjusted result at `arb_cost <= 0.95` disappears; with gap 100 it is `-2.037`.

## Spread Threshold Scan

Risk screen with `source_gap <= 100`, varying max arb cost:

| max arb cost | entries | safe | dangerous | dangerous rate | fee PnL | avg fee PnL |
|---:|---:|---:|---:|---:|---:|---:|
| 0.99 | 78 | 70 | 8 | 10.3% | -5.996 | -0.0769 |
| 0.98 | 78 | 70 | 8 | 10.3% | -5.564 | -0.0713 |
| 0.97 | 77 | 69 | 8 | 10.4% | -4.256 | -0.0553 |
| 0.96 | 77 | 68 | 9 | 11.7% | -4.032 | -0.0524 |
| 0.95 | 76 | 68 | 8 | 10.5% | -2.037 | -0.0268 |
| 0.94 | 75 | 67 | 8 | 10.7% | -0.147 | -0.0020 |
| 0.93 | 73 | 65 | 8 | 11.0% | +1.571 | +0.0215 |
| 0.92 | 71 | 63 | 8 | 11.3% | +3.189 | +0.0449 |
| 0.90 | 69 | 61 | 8 | 11.6% | +4.685 | +0.0679 |
| 0.88 | 67 | 59 | 8 | 11.9% | +6.828 | +0.1019 |
| 0.85 | 58 | 51 | 7 | 12.1% | +9.109 | +0.1571 |
| 0.80 | 50 | 42 | 8 | 16.0% | +8.964 | +0.1793 |
| 0.75 | 44 | 39 | 5 | 11.4% | +12.439 | +0.2827 |
| 0.70 | 37 | 32 | 5 | 13.5% | +11.292 | +0.3052 |

The strategy only turns positive under this looser gap threshold when the spread requirement is much stricter, around `arb_cost <= 0.93` in this sample. This is not necessarily a robust production threshold; it is a sample result.

## Hold Monitor Rerun

For entry `arb_cost <= 0.96`:

```text
entries: 77
dangerous entries: 9
hold-to-expiry fee PnL: -4.032
```

| hold mode | hold rule | immediate exits | any exit | held to expiry | dangerous held | fee PnL if exits flatten |
|---|---|---:|---:|---:|---:|---:|
| distance only | 1.0x | 0 | 62 | 15 | 0 | +1.837 |
| full gap100 | 1.0x | 0 | 66 | 11 | 0 | +1.717 |
| distance only | 1.25x | 42 | 65 | 12 | 0 | +1.769 |
| full gap100 | 1.25x | 42 | 68 | 9 | 0 | +1.669 |
| distance only | 1.5x | 54 | 67 | 10 | 0 | +1.618 |
| full gap100 | 1.5x | 54 | 70 | 7 | 0 | +1.518 |

For entry `arb_cost <= 0.95`:

```text
entries: 76
dangerous entries: 8
hold-to-expiry fee PnL: -2.037
```

| hold mode | hold rule | immediate exits | any exit | held to expiry | dangerous held | fee PnL if exits flatten |
|---|---|---:|---:|---:|---:|---:|
| distance only | 1.0x | 0 | 56 | 20 | 0 | +2.526 |
| full gap100 | 1.0x | 0 | 60 | 16 | 0 | +2.396 |
| distance only | 1.25x | 39 | 59 | 17 | 0 | +2.417 |
| full gap100 | 1.25x | 39 | 62 | 14 | 0 | +2.317 |
| distance only | 1.5x | 49 | 63 | 13 | 0 | +2.186 |
| full gap100 | 1.5x | 49 | 66 | 10 | 0 | +2.086 |

The hold monitor remains the most promising part of the stack: in this historical sample it stops every dangerous entry before expiry. But the `fee PnL if exits flatten` column assumes exited trades are flattened at zero incremental settlement PnL, not actual bid-side unwind after fees. Live usefulness depends on realistic unwind execution.

## Interpretation

Increasing `source_gap` from 5 to 100 makes the entry filter much less selective. It admits more dangerous entries and turns the current `arb_cost <= 0.95` entry rule negative after fees. To compensate under gap 100, the spread threshold would need to be materially stricter, roughly `arb_cost <= 0.93` on this dataset. That said, lowering arb-cost caps changes timing and sample composition, so this should not be treated as a stable optimized threshold without more days.

Practical conclusion:

```text
If source_gap_threshold = 100:
  arb_cost <= 0.96 is too loose.
  arb_cost <= 0.95 is still negative in this sample.
  arb_cost <= 0.93 is the first tested threshold that turns fee PnL positive.
```

The hold filter should be rerun with real bid-side unwind accounting before deployment. Its historical stop behavior is strong, but flat-exit accounting is optimistic.
