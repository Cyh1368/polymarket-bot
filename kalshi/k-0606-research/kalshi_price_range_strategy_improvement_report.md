# Kalshi T=630 Price Range Strategy Improvement

## Inputs

- Trade CSV: `kalshi_trader_trades.csv`
- Log: `kalshi_trader.log`
- Fee model: `0.07 * p * (1 - p)` per contract.
- Successful trade net per contract: `1 - p - fee`.
- Unsuccessful trade net per contract: `-p - fee`.
- The replay keeps entry timing controlled at `T=630`; only contracts with paired T=630 decision and outcome rows are used for rule search.
- Objective: maximize `net profit / total potential contracts`, where total potential contracts includes skipped T=630 opportunities as zero-P&L. This replay has 157 paired T=630 opportunities and 314 total potential contracts.

## Current Net Profit

|Rule|Traded|S/U/K|Win %|Avg p|Net|Net/total contract|Net/traded contract|
|---|---|---|---|---|---|---|---|
|All executed outcomes in CSV|97|71/26/0|73.20%|0.692|$4.99|$0.0257|$0.0257|
|Executed T=630 outcomes|96|71/25/0|73.96%|0.692|$6.41|$0.0334|$0.0334|
|Replayed current band, 0.55 < mid p < 0.80|102|75/27/55|73.53%|0.685|$7.30|$0.0232|$0.0358|

The executed T=630 result is the cleanest baseline because it uses actual fill prices and excludes rows without outcomes.
For the executed-only rows, skipped opportunities are not inferred, so `Net/total contract` is the same as `Net/traded contract`. Use the replay rows for objective comparisons because they include skipped T=630 opportunities in the denominator.
The replay baseline uses the T=630 decision book and can evaluate skipped opportunities under alternate rules. In replay tables, K is the number of T=630 opportunities the rule would skip.

## Best In-Sample Candidate Rules

Rules below require at least 40 trades to avoid tiny-sample one-offs and are ranked by `net / total potential contract`, not by traded-contract ROI.

|Rule|Traded|S/U/K|Win %|Avg p|Net|Net/total contract|Net/traded contract|
|---|---|---|---|---|---|---|---|
|0.50 < ask p < 0.78; spot agrees with selected side|63|49/14/94|77.78%|0.647|$14.52|$0.0462|$0.1152|
|0.50 < mid p < 0.78; spot agrees with selected side|62|48/14/95|77.42%|0.646|$14.01|$0.0446|$0.1130|
|0.50 < ask p < 0.85; spot agrees with selected side|83|65/18/74|78.31%|0.685|$13.94|$0.0444|$0.0840|
|0.50 < mid p < 0.90; spot agrees with selected side|91|72/19/66|79.12%|0.701|$13.91|$0.0443|$0.0764|
|0.50 < ask p < 0.90; spot agrees with selected side|90|71/19/67|78.89%|0.699|$13.72|$0.0437|$0.0762|
|0.50 < mid p < 0.90; spot agrees with selected side; favored spot distance >= $25|78|63/15/79|80.77%|0.706|$13.67|$0.0435|$0.0876|
|0.50 < ask p < 0.90; spot agrees with selected side; favored spot distance >= $25|78|63/15/79|80.77%|0.706|$13.67|$0.0435|$0.0876|
|0.50 < mid p < 0.88; spot agrees with selected side|89|70/19/68|78.65%|0.697|$13.50|$0.0430|$0.0758|

## Recommended Rule

Recommended in-sample rule: **0.50 < ask p < 0.78; spot agrees with selected side**.

|Rule|Traded|S/U/K|Win %|Avg p|Net|Net/total contract|Net/traded contract|
|---|---|---|---|---|---|---|---|
|Current replay|102|75/27/55|73.53%|0.685|$7.30|$0.0232|$0.0358|
|Recommended replay|63|49/14/94|77.78%|0.647|$14.52|$0.0462|$0.1152|

Why this helps:

- The old range admits too many high-cost contracts near the top of the band.
- Net profit is fee-adjusted, so the hurdle is higher than raw `success_rate > p`.
- Because the denominator is fixed across T=630 rules, maximizing `net / total potential contract` gives the same ordering as maximizing total net profit. It does not give extra credit to rules that merely trade less often.
- The best candidate shifts selection toward lower entry prices and uses spot/target agreement to avoid cases where the book's more-likely side is not supported by the underlying price at entry.

## Chronological Sanity Check

The main ranking above is in-sample. The checks below keep the denominator fixed inside each split, so skipped opportunities reduce `net / total contract` instead of disappearing from the metric.

First, compare the current rule and the recommended full-sample candidate across the first 70% and final 30% of paired T=630 contracts. This is not a pure holdout proof for the recommended candidate, because that candidate was selected using the full sample, but it shows whether the candidate is simply concentrating profit in one period.

|Rule|Traded|S/U/K|Win %|Avg p|Net|Net/total contract|Net/traded contract|
|---|---|---|---|---|---|---|---|
|Current train|73|53/20/36|72.60%|0.680|$4.59|$0.0211|$0.0315|
|Recommended candidate train|52|39/13/57|75.00%|0.646|$9.20|$0.0422|$0.0884|
|Current final 30%|29|22/7/19|75.86%|0.698|$2.71|$0.0282|$0.0466|
|Recommended candidate final 30%|11|10/1/37|90.91%|0.652|$5.32|$0.0554|$0.2418|

Second, as a stricter overfit check, optimize only on the first 70% and evaluate that selected rule on the final 30%.

Train-selected rule: **0.62 < mid p < 0.90; spot agrees with selected side; favored spot distance >= $25**.

|Rule|Traded|S/U/K|Win %|Avg p|Net|Net/total contract|Net/traded contract|
|---|---|---|---|---|---|---|---|
|Current train|73|53/20/36|72.60%|0.680|$4.59|$0.0211|$0.0315|
|Train-selected rule on train|42|38/4/67|90.48%|0.760|$11.13|$0.0511|$0.1325|
|Current final 30%|29|22/7/19|75.86%|0.698|$2.71|$0.0282|$0.0466|
|Train-selected rule on final 30%|10|9/1/38|90.00%|0.780|$2.16|$0.0225|$0.1082|

The train-selected rule is profitable, but it gives back too much coverage on the final 30% under the total-contract objective. That makes the broader full-sample recommendation a better live candidate than the narrower train-only rule until more data is collected.

## Implementation Notes

- Keep the trigger time at `T=630`.
- At the decision snapshot, infer the more-likely side from `yes_mid >= 0.5`.
- Use the tradable ask for that side as `p`: YES uses `yes_ask`, NO uses `no_ask`.
- Compute entry spot agreement from the latest nearby log line: YES agrees when `spot - target > 0`; NO agrees when `target - spot > 0`.
- If adopting the recommended rule live, keep logging the same fields and rerun this report after materially more contracts; the current sample is useful but still small.

## Artifacts

- Paired T=630 dataset: `kalshi_t630_strategy_dataset.csv`
- Rule sweep grid: `kalshi_t630_strategy_grid.csv`
