# Kalshi New Strategy Backtest On p-0604 Data

## Setup

- Source: `p-0604-research/data/*.csv`
- Contract files: `186`
- Usable `T=630` entries: `180`
- Usable `T=600` entries: `180`
- Entry row: closest row to target `T` within `45` seconds.
- Side: more-likely side from `kalshi_yes_mid >= 0.5`.
- Fee model: `0.07 * ask * (1 - ask)` per contract.
- Objective: net PnL per available contract; skipped contracts contribute zero.

## Primary Benchmark

|Rule|Traded|S/U/K|Win %|Avg ask|Net|Net/available|Net/traded|
|---|---|---|---|---|---|---|---|
|Existing T=630 mid [0.55,0.80)|110|87/23/76|79.09%|0.6925|$9.2252|$0.0496|$0.0839|
|New T=630 ask (0.50,0.78) + spot|90|70/20/96|77.78%|0.6632|$8.9459|$0.0481|$0.0994|

Delta, new minus existing: `$-0.2793` total net, `$-0.0015` per available contract.

The new rule is better per traded contract, but it is slightly worse on the requested total-opportunity objective in this dataset. It trades fewer contracts and gives up enough profitable old-rule trades to almost exactly offset its cleaner lower-price entries.

## Component Breakdown

|Rule|Traded|S/U/K|Win %|Avg ask|Net|Net/available|Net/traded|
|---|---|---|---|---|---|---|---|
|Existing T=630 mid [0.55,0.80)|110|87/23/76|79.09%|0.6925|$9.2252|$0.0496|$0.0839|
|New T=630 ask (0.50,0.78) + spot|90|70/20/96|77.78%|0.6632|$8.9459|$0.0481|$0.0994|
|Existing T=630 mid [0.55,0.80) + spot|93|76/17/93|81.72%|0.7002|$9.5492|$0.0513|$0.1027|
|Ask (0.50,0.78), no spot filter|114|83/31/72|72.81%|0.6510|$7.0342|$0.0378|$0.0617|
|Old live-like T=600 mid [0.60,0.80)|80|60/20/106|75.00%|0.7124|$1.8813|$0.0101|$0.0235|

The spot-agreement filter itself is positive on this dataset: applying it to the existing midpoint band improves the benchmark. The ask-price cutoff is the part that loses edge here, because several `ask >= 0.78` entries in this older dataset were successful.

Best direct rule in this comparison: `Existing T=630 mid [0.55,0.80) + spot`, net `$9.5492`, net/available `$0.0513`.

## Daily Split

|Date|Rule|Traded|S/U/K|Net|Net/available|
|---|---|---|---|---|---|
|2026-06-01|existing|1|1/0/0|$0.2176|$0.2176|
|2026-06-01|new|1|1/0/0|$0.2176|$0.2176|
|2026-06-02|existing|58|47/11/37|$5.8992|$0.0621|
|2026-06-02|new|46|37/9/49|$5.8689|$0.0618|
|2026-06-03|existing|51|39/12/33|$3.1084|$0.0370|
|2026-06-03|new|43|32/11/41|$2.8594|$0.0340|

## Artifacts

- Entries: `kalshi_new_strategy_backtest_entries.csv`
- Results: `kalshi_new_strategy_backtest_results.csv`
- Report: `kalshi_new_strategy_backtest_report.md`
