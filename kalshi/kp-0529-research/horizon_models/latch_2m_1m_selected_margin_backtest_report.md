# Backtest: `latch_2m_1m` At Selected Profit Margins

## Scope

- Requested data directory: `kp-0529-data`. This checkout has the matching dataset and current artifacts under `kp-0529-research`, so this report uses `kp-0529-research`.
- Strategy: `latch_2m_1m`. The 2m model is evaluated first; if it passes, the contract is latched tradable through expiry. If 2m fails, 1m can latch the contract.
- Current thresholds: `2m < 0.0788`, `1m < 0.1329`.
- Entry rule: after the first passing latch model, enter at the first historical row where `best_all_in_cost < 1 - profit_margin`.
- Fees are included in `best_all_in_cost` using the current odds-dependent equations: Kalshi `0.07*p*(1-p)`, Polymarket `0.05*p*(1-p)`, with `N=1`.
- Return assumption for the incomplete prompt sentence: if a trade diverges, the full all-in entry cost is lost, so profit is `-entry_price`; if it does not diverge, profit is `1 - entry_price`.
- Historical CSVs do not guarantee displayed ask-side liquidity, so this remains a price/outcome backtest rather than a fill simulator.
- Uncertainty intervals are contract-level bootstrap 95% intervals with no-trade contracts contributing zero to per-15m-contract profit.

## Direct Answers On All Eligible 0529 Contracts

| profit_margin | contracts | model_signal_contracts | model_signal_rate | approved_trades | approved_trade_rate | minutes_between_trades | divergent_trades | divergence_rate | entry_price | profit_per_traded_contract | profit_per_15m_contract | total_profit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.0500 | 1158 | 991 | 0.8558 | 785 | 0.6779 | 22.1 [21.3, 23.1] | 13 | 0.0166 [0.0087, 0.0264] | 0.7044 [0.6904, 0.7180] | 0.2791 [0.2616, 0.2960] | 0.1892 [0.1753, 0.2032] | 219.0738 [203.0451, 235.2738] |
| 0.1800 | 1158 | 991 | 0.8558 | 609 | 0.5259 | 28.5 [27.1, 30.2] | 11 | 0.0181 [0.0083, 0.0294] | 0.5978 [0.5863, 0.6090] | 0.3841 [0.3674, 0.4007] | 0.2020 [0.1875, 0.2162] | 233.9313 [217.0839, 250.3998] |
| 0.4000 | 1158 | 991 | 0.8558 | 373 | 0.3221 | 46.6 [42.9, 50.8] | 10 | 0.0268 [0.0114, 0.0446] | 0.4924 [0.4828, 0.5013] | 0.4808 [0.4612, 0.4988] | 0.1549 [0.1405, 0.1694] | 179.3267 [162.6809, 196.1278] |

Interpretation of the time column: because each market interval is 15 minutes, `minutes_between_trades = 15 / approved_trade_rate`. For example, a 50% approval rate means one expected approved trade every 30 minutes.

## Held-Out Test Split Check

The table below uses the same historical test split used in prior horizon-model reports. It is included to show sensitivity outside the full in-sample view.

| profit_margin | contracts | model_signal_contracts | model_signal_rate | approved_trades | approved_trade_rate | minutes_between_trades | divergent_trades | divergence_rate | entry_price | profit_per_traded_contract | profit_per_15m_contract | total_profit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.0500 | 232 | 199 | 0.8578 | 159 | 0.6853 | 21.9 [20.2, 24.0] | 4 | 0.0252 [0.0061, 0.0513] | 0.6889 [0.6586, 0.7206] | 0.2859 [0.2409, 0.3269] | 0.1960 [0.1609, 0.2285] | 45.4609 [37.3274, 53.0098] |
| 0.1800 | 232 | 199 | 0.8578 | 130 | 0.5603 | 26.8 [24.0, 30.3] | 4 | 0.0308 [0.0073, 0.0635] | 0.5911 [0.5650, 0.6166] | 0.3781 [0.3336, 0.4183] | 0.2119 [0.1775, 0.2454] | 49.1528 [41.1849, 56.9306] |
| 0.4000 | 232 | 199 | 0.8578 | 91 | 0.3922 | 38.2 [32.8, 45.8] | 3 | 0.0330 [0.0000, 0.0750] | 0.4923 [0.4704, 0.5113] | 0.4748 [0.4324, 0.5120] | 0.1862 [0.1519, 0.2197] | 43.2024 [35.2352, 50.9800] |

## Key Definitions

- `model_signal_contracts`: contracts where either the 2m model or, if needed, the 1m model passed its divergence threshold.
- `approved_trades`: contracts where the model signal existed and the post-latch price also met the requested profit margin.
- `entry_price`: mean all-in cost of the first approved trade, including both platform fees.
- `profit_per_traded_contract`: conditional profit per executed 1-lot paired trade.
- `profit_per_15m_contract`: total profit divided by every evaluated 15-minute market interval, including intervals with no trade.

## Output Files

- Summary CSV: `kp-0529-research/horizon_models/latch_2m_1m_selected_margin_backtest.csv`
- Trade rows CSV: `kp-0529-research/horizon_models/latch_2m_1m_selected_margin_backtest_trades.csv`
- Report: `kp-0529-research/horizon_models/latch_2m_1m_selected_margin_backtest_report.md`
