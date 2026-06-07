# All-Coin Kalshi Profit-Stability Summary

Generated: `2026-06-07T14:35:39Z`

## Table Of Contents

- [Method](#method)
- [Leaderboard](#leaderboard)
- [Statistical Flags](#statistical-flags)
- [Per-Coin Reports](#per-coin-reports)

## Method

Each coin uses Kalshi quote columns for entry reconstruction and official Kalshi API market outcomes for settlement. Outcomes are not inferred from spot prices. The optimized objective is gross profit per available contract:

```text
(p - c) * N_traded_in_range / N_total_backtested_at_T
```

Stability requires positive profit per available contract, `N >= 20`, at least four valid neighboring cells, at least 70% positive neighbors, positive average neighbor profit, and membership in a positive connected component of at least eight cells.

## Leaderboard

| Rank | Coin | Selected T | Price range | Stable | N traded | N total | Coverage | P(success) | Avg cost | EV p-c | Profit/available | Gross P&L | Break-even p-value | Date dep p | Time-bucket dep p |
|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | BTC | 720 | 0.50-0.80 | 1 | 153 | 185 | 82.70% | 71.90% | 0.6527 | 0.0662 | 0.0548 | 10.1300 | 0.04689 | 0.5556 | 0.9621 |
| 2 | ETH | 480 | 0.80-1.00 | 1 | 282 | 576 | 48.96% | 93.26% | 0.9014 | 0.0313 | 0.0153 | 8.8190 | 0.04082 | 0.04886 | 0.4554 |
| 3 | BNB | 60 | 0.50-0.80 | 0 | 52 | 534 | 9.74% | 80.77% | 0.6992 | 0.1085 | 0.0106 | 5.6400 | 0.05288 | 0.8301 | 0.773 |
| 4 | DOGE | 780 | 0.80-1.00 | 1 | 63 | 574 | 10.98% | 95.24% | 0.8576 | 0.0948 | 0.0104 | 5.9730 | 0.01511 | 0.6148 | 0.3306 |
| 5 | HYPE | 240 | 0.90-1.00 | 0 | 276 | 576 | 47.92% | 97.83% | 0.9582 | 0.0201 | 0.0096 | 5.5380 | 0.05413 | 0.5144 | 0.4381 |
| 6 | SOL | 360 | 0.50-0.60 | 0 | 53 | 577 | 9.19% | 66.04% | 0.5650 | 0.0954 | 0.0088 | 5.0570 | 0.1018 | 0.605 | 0.8072 |
| 7 | XRP | 60 | 0.70-0.90 | 1 | 38 | 514 | 7.39% | 89.47% | 0.8418 | 0.0530 | 0.0039 | 2.0130 | 0.2576 | 0.763 | 0.3048 |

## Statistical Flags

- Stable selected parameters: `4/7` coins.
- Break-even p-value < 0.05: `BTC, ETH, DOGE`.
- Date-dependence p-value < 0.05: `ETH`.
- Time-bucket-dependence p-value < 0.05: `none`.

Interpretation: a high profit-per-available value with a non-significant break-even p-value should be treated as an exploratory signal, not confirmed edge. Date/time dependency flags indicate that the selected rule may be regime-sensitive.

## Per-Coin Reports

- [BNB](bnb_profit_stability_report.md)
- [BTC](btc_profit_stability_report.md)
- [DOGE](doge_profit_stability_report.md)
- [ETH](eth_profit_stability_report.md)
- [HYPE](hype_profit_stability_report.md)
- [SOL](sol_profit_stability_report.md)
- [XRP](xrp_profit_stability_report.md)
