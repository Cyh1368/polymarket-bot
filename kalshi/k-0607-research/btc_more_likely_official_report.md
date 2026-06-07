# Kalshi BTC More-Likely Side Official-Outcome Backtest

Generated: `2026-06-07T13:50:19Z`

## Summary

This report uses only Kalshi quote columns from `data_BTC/*.csv` and official Kalshi market outcomes from the public `GET /markets?tickers=...` API. Spot-price-derived outcomes are not used.

The best single-decile parameter with N >= 20 is T=420s and bin 0.60-0.70: P(success)=80.00%, avg cost c=0.6505, EV p-c=0.1495, N=20.

The unrestricted raw single-decile argmax is T=60s and bin 0.70-0.80 with EV 0.2800, N=2.

Gross EV is calculated per 1 contract before fees:

```text
EV = P(success) - average buy cost
```

where success means the official Kalshi result resolves to the same side as the more-likely Kalshi midpoint side at the selected horizon.

## Artifacts

- Entry ledger: `btc_more_likely_entries_official.csv`
- Parameter grid: `btc_more_likely_parameter_grid_official.csv`
- Machine-readable summary: `btc_more_likely_summary_official.json`
- Official market cache: `btc_official_market_results.json`

## Data And Method

- Contract CSV files: `186`
- Raw rows: `30095`
- Valid Kalshi quote rows: `27418`
- Invalid quote rows rejected: `0`
- Close-time range: `2026-06-01T23:30:00Z` to `2026-06-03T21:45:00Z`
- Markets returned by Kalshi API: `186`
- Resolved official outcomes: `186`
- Official outcome counts: YES `77`, NO `109`, missing `0`
- Official market statuses: finalized `186`
- Horizon grid: `60`s to `900`s in 60s steps
- Horizon row selection tolerance: `45` seconds
- Primary result minimum sample size: `N >= 20`

For each contract and horizon, I selected the valid row closest to T seconds before expiry. The more-likely side is YES if `kalshi_yes_mid >= 0.5`, otherwise NO. The tested decile bins are based on `max(kalshi_yes_mid, 1 - kalshi_yes_mid)`. The cost `c` is the best ask to buy that selected side: `kalshi_yes_ask` for YES and `kalshi_no_ask` for NO.

## Usable Entry Counts

| T seconds | Usable resolved entries |
|---:|---:|
| 60 | 150 |
| 120 | 178 |
| 180 | 183 |
| 240 | 185 |
| 300 | 185 |
| 360 | 184 |
| 420 | 184 |
| 480 | 184 |
| 540 | 184 |
| 600 | 184 |
| 660 | 184 |
| 720 | 185 |
| 780 | 184 |
| 840 | 184 |
| 900 | 184 |

## Best Parameter Cells

Top single-decile cells with `N >= 20`:

| T seconds | Price bin | N | P(success) | Avg cost c | EV p-c | Gross P&L | Wilson EV low |
|---:|---|---:|---:|---:|---:|---:|---:|
| 420 | 0.60-0.70 | 20 | 80.00% | 0.6505 | 0.1495 | 2.9900 | -0.0665 |
| 480 | 0.60-0.70 | 30 | 76.67% | 0.6563 | 0.1103 | 3.3100 | -0.0656 |
| 660 | 0.50-0.60 | 34 | 64.71% | 0.5538 | 0.0932 | 3.1700 | -0.0747 |
| 660 | 0.70-0.80 | 52 | 84.62% | 0.7567 | 0.0894 | 4.6500 | -0.0320 |
| 720 | 0.50-0.60 | 47 | 63.83% | 0.5545 | 0.0838 | 3.9400 | -0.0591 |
| 420 | 0.80-0.90 | 44 | 93.18% | 0.8589 | 0.0730 | 3.2100 | -0.0411 |
| 720 | 0.60-0.70 | 59 | 72.88% | 0.6566 | 0.0722 | 4.2600 | -0.0526 |
| 840 | 0.70-0.80 | 21 | 80.95% | 0.7424 | 0.0671 | 1.4100 | -0.1424 |
| 600 | 0.70-0.80 | 49 | 81.63% | 0.7553 | 0.0610 | 2.9900 | -0.0689 |
| 540 | 0.70-0.80 | 37 | 81.08% | 0.7614 | 0.0495 | 1.8300 | -0.1034 |
| 720 | 0.70-0.80 | 47 | 78.72% | 0.7462 | 0.0411 | 1.9300 | -0.0952 |
| 540 | 0.50-0.60 | 27 | 59.26% | 0.5556 | 0.0370 | 1.0000 | -0.1483 |
| 480 | 0.80-0.90 | 44 | 88.64% | 0.8537 | 0.0327 | 1.4390 | -0.0935 |
| 780 | 0.50-0.60 | 62 | 58.06% | 0.5498 | 0.0308 | 1.9100 | -0.0932 |
| 540 | 0.80-0.90 | 52 | 88.46% | 0.8542 | 0.0304 | 1.5800 | -0.0839 |

Best single-decile cell at each horizon with `N >= 20`:

| T seconds | Price bin | N | P(success) | Avg cost c | EV p-c | Gross P&L | Wilson EV low |
|---:|---|---:|---:|---:|---:|---:|---:|
| 60 | 0.90-1.00 | 134 | 98.51% | 0.9926 | -0.0076 | -1.0120 | -0.0454 |
| 120 | 0.90-1.00 | 146 | 99.32% | 0.9843 | 0.0088 | 1.2890 | -0.0221 |
| 180 | 0.90-1.00 | 141 | 98.58% | 0.9758 | 0.0100 | 1.4100 | -0.0261 |
| 240 | 0.90-1.00 | 119 | 97.48% | 0.9698 | 0.0050 | 0.5890 | -0.0413 |
| 300 | 0.80-0.90 | 41 | 85.37% | 0.8619 | -0.0082 | -0.3360 | -0.1463 |
| 360 | 0.80-0.90 | 43 | 88.37% | 0.8606 | 0.0232 | 0.9960 | -0.1054 |
| 420 | 0.60-0.70 | 20 | 80.00% | 0.6505 | 0.1495 | 2.9900 | -0.0665 |
| 480 | 0.60-0.70 | 30 | 76.67% | 0.6563 | 0.1103 | 3.3100 | -0.0656 |
| 540 | 0.70-0.80 | 37 | 81.08% | 0.7614 | 0.0495 | 1.8300 | -0.1034 |
| 600 | 0.70-0.80 | 49 | 81.63% | 0.7553 | 0.0610 | 2.9900 | -0.0689 |
| 660 | 0.50-0.60 | 34 | 64.71% | 0.5538 | 0.0932 | 3.1700 | -0.0747 |
| 720 | 0.50-0.60 | 47 | 63.83% | 0.5545 | 0.0838 | 3.9400 | -0.0591 |
| 780 | 0.50-0.60 | 62 | 58.06% | 0.5498 | 0.0308 | 1.9100 | -0.0932 |
| 840 | 0.70-0.80 | 21 | 80.95% | 0.7424 | 0.0671 | 1.4100 | -0.1424 |
| 900 | 0.50-0.60 | 142 | 58.45% | 0.5554 | 0.0292 | 4.1400 | -0.0531 |

## Selected Horizon Detail

All single-decile bins at T=420s:

| T seconds | Price bin | N | P(success) | Avg cost c | EV p-c | Gross P&L | Wilson EV low |
|---:|---|---:|---:|---:|---:|---:|---:|
| 420 | 0.50-0.60 | 23 | 56.52% | 0.5522 | 0.0130 | 0.3000 | -0.1841 |
| 420 | 0.60-0.70 | 20 | 80.00% | 0.6505 | 0.1495 | 2.9900 | -0.0665 |
| 420 | 0.70-0.80 | 33 | 63.64% | 0.7597 | -0.1233 | -4.0700 | -0.2935 |
| 420 | 0.80-0.90 | 44 | 93.18% | 0.8589 | 0.0730 | 3.2100 | -0.0411 |
| 420 | 0.90-1.00 | 64 | 96.88% | 0.9531 | 0.0156 | 0.9990 | -0.0601 |

## Stability In Parameter Space

Chosen parameter: T=420s, bin 0.60-0.70, EV=0.1495, N=20.

Stability assessment: `not stable`.

Positive connected component size around the chosen cell: `3` cells using 4-neighbor adjacency over T and price bin.

Immediate neighbors with `N >= 20`:

| T seconds | Price bin | N | P(success) | Avg cost c | EV p-c | Gross P&L | Wilson EV low |
|---:|---|---:|---:|---:|---:|---:|---:|
| 360 | 0.70-0.80 | 21 | 61.90% | 0.7681 | -0.1490 | -3.1300 | -0.3593 |
| 420 | 0.50-0.60 | 23 | 56.52% | 0.5522 | 0.0130 | 0.3000 | -0.1841 |
| 420 | 0.70-0.80 | 33 | 63.64% | 0.7597 | -0.1233 | -4.0700 | -0.2935 |
| 480 | 0.60-0.70 | 30 | 76.67% | 0.6563 | 0.1103 | 3.3100 | -0.0656 |
| 480 | 0.70-0.80 | 43 | 69.77% | 0.7630 | -0.0653 | -2.8100 | -0.2141 |

Neighbor EV mean is `-0.0429`, with `2/5` positive neighbors.

The optimum is fragile: nearby cells do not form a broad positive plateau under the minimum sample filter. Treat it as a hypothesis rather than a stable equilibrium.

## Time And Day Dependence

Selected parameter performance by ET date:

| ET date | N | Wins | Losses | P(success) | Avg cost c | EV p-c | Gross P&L |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-06-01 | 4 | 3 | 1 | 75.00% | 0.6675 | 0.0825 | 0.3300 |
| 2026-06-02 | 8 | 7 | 1 | 87.50% | 0.6375 | 0.2375 | 1.9000 |
| 2026-06-03 | 8 | 6 | 2 | 75.00% | 0.6550 | 0.0950 | 0.7600 |

Best single-decile parameter by ET date with `N >= 10`:

| ET date | Best T | Best price bin | N | P(success) | Avg cost c | EV p-c | Gross P&L |
|---|---:|---|---:|---:|---:|---:|---:|
| 2026-06-01 | 900 | 0.50-0.60 | 11 | 72.73% | 0.5573 | 0.1700 | 1.8700 |
| 2026-06-02 | 480 | 0.60-0.70 | 16 | 81.25% | 0.6538 | 0.1588 | 2.5400 |
| 2026-06-03 | 720 | 0.50-0.60 | 16 | 81.25% | 0.5450 | 0.2675 | 4.2800 |

Selected parameter performance by ET 4-hour close-time bucket:

| ET close-hour bucket | N | Wins | Losses | P(success) | Avg cost c | EV p-c | Gross P&L |
|---|---:|---:|---:|---:|---:|---:|---:|
| 00-03 | 5 | 5 | 0 | 100.00% | 0.6520 | 0.3480 | 1.7400 |
| 04-07 | 1 | 1 | 0 | 100.00% | 0.6400 | 0.3600 | 0.3600 |
| 08-11 | 2 | 0 | 2 | 0.00% | 0.6300 | -0.6300 | -1.2600 |
| 12-15 | 3 | 3 | 0 | 100.00% | 0.6900 | 0.3100 | 0.9300 |
| 16-19 | 4 | 3 | 1 | 75.00% | 0.6225 | 0.1275 | 0.5100 |
| 20-23 | 5 | 4 | 1 | 80.00% | 0.6580 | 0.1420 | 0.7100 |

Best single-decile parameter by ET 4-hour close-time bucket with `N >= 10`:

| ET close-hour bucket | Best T | Best price bin | N | P(success) | Avg cost c | EV p-c | Gross P&L |
|---|---:|---|---:|---:|---:|---:|---:|
| 00-03 | 840 | 0.50-0.60 | 15 | 73.33% | 0.5407 | 0.1927 | 2.8900 |
| 04-07 | 720 | 0.70-0.80 | 11 | 81.82% | 0.7464 | 0.0718 | 0.7900 |
| 08-11 | 780 | 0.50-0.60 | 10 | 80.00% | 0.5530 | 0.2470 | 2.4700 |
| 12-15 | 900 | 0.50-0.60 | 26 | 65.38% | 0.5523 | 0.1015 | 2.6400 |
| 16-19 | 480 | 0.90-1.00 | 11 | 100.00% | 0.9501 | 0.0499 | 0.5490 |
| 20-23 | 900 | 0.50-0.60 | 21 | 71.43% | 0.5529 | 0.1614 | 3.3900 |

Interpretation: if the best parameter moves materially across dates or time buckets, the apparent edge may be regime-dependent rather than a persistent market mispricing.

## Conclusion

The main mispriced opportunity in this sample is buying the Kalshi more-likely side in the `0.60-0.70` more-likely midpoint bin at about `7` minutes before expiry, subject to the stability and time/day caveats above.

Fees are not included. Applying Kalshi fees will reduce every EV estimate, especially near 0.50 where fees are largest.
