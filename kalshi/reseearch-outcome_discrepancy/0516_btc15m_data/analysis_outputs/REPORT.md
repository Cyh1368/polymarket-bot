# BTC 15m Kalshi/Polymarket Outcome Discrepancy Report

Data analyzed: 48 `combined_KXBTC15M-*.csv` files in `reseearch-outcome_discrepancy/0516_btc15m_data`.

Rows with non-empty `polymarket_error` were excluded. Arb detection used only rows where `kalshi_status == "active"`.

## Headline Results

- Known outcome discrepancies: 8 / 48 contracts = 16.67%.
- One additional contract, `KXBTC15M-26MAY162345-45`, has no finite `polymarket_btc_target` in the file, so its Polymarket outcome is marked `UNKNOWN` rather than counted as a discrepancy.
- Every known discrepant contract had arb opportunities and had adverse-direction arb ticks.
- Labeled arb ticks: 15,921.
- Dangerous arb ticks: 2,608.
- Worst single-tick normalized loss: `-0.996`.

## Discrepant Contracts

| contract_id | close_time | K outcome | P outcome | K final | P final | K target | P target | close gap | target gap | K margin | P margin | worst loss |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| KXBTC15M-26MAY170445-45 | 2026-05-17T08:45:00Z | NO | YES | 78029.32 | 78067.52 | 78046.78 | 78042.24 | -38.20 | 4.54 | 17.46 | 25.28 | -0.990 |
| KXBTC15M-26MAY170500-00 | 2026-05-17T09:00:00Z | YES | NO | 78065.17 | 78059.03 | 78045.17 | 78070.58 | 6.14 | -25.41 | 20.00 | 11.55 | -0.890 |
| KXBTC15M-26MAY170530-30 | 2026-05-17T09:30:00Z | NO | YES | 78092.44 | 78103.88 | 78101.71 | 78096.01 | -11.44 | 5.70 | 9.27 | 7.87 | -0.990 |
| KXBTC15M-26MAY170800-00 | 2026-05-17T12:00:00Z | NO | YES | 78381.35 | 78380.63 | 78389.42 | 78373.78 | 0.72 | 15.64 | 8.07 | 6.84 | -0.990 |
| KXBTC15M-26MAY170815-15 | 2026-05-17T12:15:00Z | YES | NO | 78419.45 | 78358.10 | 78386.90 | 78381.04 | 61.35 | 5.86 | 32.55 | 22.94 | -0.990 |
| KXBTC15M-26MAY170830-30 | 2026-05-17T12:30:00Z | NO | YES | 78405.50 | 78395.67 | 78411.52 | 78356.93 | 9.83 | 54.59 | 6.02 | 38.74 | -0.890 |
| KXBTC15M-26MAY170930-30 | 2026-05-17T13:30:00Z | NO | YES | 78239.53 | 78258.52 | 78246.97 | 78229.24 | -18.99 | 17.73 | 7.44 | 29.28 | -0.996 |
| KXBTC15M-26MAY171030-30 | 2026-05-17T14:30:00Z | YES | NO | 78064.59 | 78016.01 | 78053.12 | 78043.09 | 48.58 | 10.03 | 11.47 | 27.08 | -0.990 |

## Filter Findings

Univariate features were useful but not sufficient alone. The strongest plain signal was `price_direction_agreement == False`: those ticks had a 41.8% dangerous rate vs 10.9% when direction agreed.

`min_distance_from_target < 10` flagged 1,248 / 2,608 dangerous ticks with 23.3% danger rate. `source_price_gap_relative_to_target_distance > 1.0` flagged 1,150 / 2,608 dangerous ticks with 21.2% danger rate. Pure source gap thresholds were weak on their own; for example `source_price_gap > 5` caught 1,937 dangerous ticks but also flagged 9,979 safe ticks.

Danger was not purely an expiry-only problem. Danger rate was 16.9% in the last 30 seconds, 20.0% at 60-120 seconds, 17.9% at 120-300 seconds, and 15.1% beyond 600 seconds.

## Revised Recommendation

The first-pass filter is necessary but not sufficient because it is only a snapshot entry check. More importantly, the tick-level PnL aggregate is not a strategy PnL. A bot enters once per contract and holds to expiry, so the correct evaluation unit is the first allowed entry per contract.

The rule `direction_agreement and source_gap <= $100 and min_distance >= max($10, 0.05 * seconds_to_expiry) and abs_target_divergence <= $15` allowed 1,568 ticks, but only 36 contract entries. On the correct contract-level accounting it produced:

| metric | value |
|---|---:|
| entered contracts | 36 |
| safe entries | 34 |
| dangerous entries | 2 |
| win rate | 94.4% |
| total PnL | -0.771 |
| average PnL / entry | -0.0214 |
| average safe gain | +0.0250 |
| average dangerous loss | -0.8100 |
| breakeven dangerous rate | 3.0% |
| actual dangerous rate | 5.6% |

The filter improves the baseline first-arb-per-contract result, which was `-5.120` total PnL and `-0.1089` per entry, but it does not make the strategy profitable. The residual dangerous-entry rate is still too high relative to the thin safe spread.

Target divergence should not be used as a hard `$15` cutoff. It blocks safe high-value contracts and is not selective enough in this sample. The contract-level sensitivity is:

| filter | entries | dangerous | total PnL | avg PnL |
|---|---:|---:|---:|---:|
| time-scaled + gap <= $100 + target <= $15 | 36 | 2 | -0.771 | -0.021 |
| time-scaled + gap <= $100 + no target cap | 45 | 4 | -2.011 | -0.045 |
| time-scaled + gap <= $100 + target <= $30 | 43 | 4 | -2.271 | -0.053 |
| time-scaled + gap <= $100 + target <= $35 | 44 | 4 | -2.141 | -0.049 |

Raising or dropping the target cap recovers several safe opportunities, but it also admits additional dangerous contracts in this sample. That means target divergence is not a reliable standalone predictor; at best it is a weak risk input that needs to be weighed against spread size and the other live features.

The safer implementation is to keep the source-gap, direction-agreement, and time-scaled-distance checks as the core entry screen, and treat target divergence as a sizing / caution feature rather than an absolute reject unless it is extreme. The bot should also monitor open positions because entry-time safety does not remove path risk.

```python
def should_place_arb(row) -> bool:
    kalshi_price = float(row["kalshi_btc_price"])
    poly_price = float(row["polymarket_btc_price"])
    kalshi_target = float(row["kalshi_btc_target"])
    poly_target = float(row["polymarket_btc_target"])

    if row.get("polymarket_error"):
        return False
    if row.get("kalshi_status") != "active":
        return False

    kalshi_distance = abs(kalshi_price - kalshi_target)
    poly_distance = abs(poly_price - poly_target)
    min_distance = min(kalshi_distance, poly_distance)
    source_gap = abs(kalshi_price - poly_price)
    direction_agreement = (kalshi_price > kalshi_target) == (poly_price > poly_target)
    target_divergence = abs(kalshi_target - poly_target)

    # Current price closer to target than the lagging SMA means recent drift is toward the wire.
    kalshi_sma = float(row.get("kalshi_btc_60_sma") or "nan")
    kalshi_current_closer_than_sma = (
        abs(kalshi_price - kalshi_target) < abs(kalshi_sma - kalshi_target)
        if kalshi_sma == kalshi_sma
        else False
    )

    seconds_to_expiry = float(row["seconds_to_expiry"])
    required_distance = max(10.0, seconds_to_expiry * 0.05)

    if not direction_agreement:
        return False
    if min_distance < required_distance:
        return False
    if source_gap > 100.0:
        return False

    # Target divergence is a weak risk signal in this sample. Do not use a tight
    # $15 hard cutoff; it rejected multiple profitable safe contracts. Treat
    # very large divergence as a no-trade or size-down condition.
    if target_divergence > 35.0:
        return False
    if kalshi_current_closer_than_sma and seconds_to_expiry < 300:
        return False
    return True
```

For held positions, use a stricter stay-in monitor. Entry and stay-in thresholds should not be the same:

```python
def should_hold_arb(row) -> bool:
    kalshi_price = float(row["kalshi_btc_price"])
    poly_price = float(row["polymarket_btc_price"])
    kalshi_target = float(row["kalshi_btc_target"])
    poly_target = float(row["polymarket_btc_target"])

    if row.get("polymarket_error"):
        return False

    min_distance = min(abs(kalshi_price - kalshi_target), abs(poly_price - poly_target))
    source_gap = abs(kalshi_price - poly_price)
    direction_agreement = (kalshi_price > kalshi_target) == (poly_price > poly_target)
    target_divergence = abs(kalshi_target - poly_target)

    if not direction_agreement:
        return False
    if min_distance < 25.0:
        return False
    if source_gap > 100.0:
        return False
    if target_divergence > 35.0:
        return False
    return True
```

## Generated Files

- `contract_summary.csv`: all 48 contracts with derived outcomes and arb flags.
- `discrepancies.csv`: all known outcome discrepancies.
- `arb_ticks.csv`: per-tick arb rows with features, payout, PnL, and dangerous labels.
- `worst_danger_by_contract.csv`: worst adverse tick per discrepant contract.
- `threshold_scan_all.csv`: univariate threshold scan details.
- `threshold_scan_best.csv`: best univariate threshold per feature.
- `filter_eval.csv`: tick-level composite filter diagnostics. Do not use this as strategy PnL.
- `contract_filter_eval.csv`: one-entry-per-contract filter backtests. This is the strategy accounting table.
- `contract_entries.csv`: first allowed entry per contract for each filter.
- `discrepant_timeline.csv`: source gap and min-distance timeline buckets for discrepant contracts.

## Interpretation

Disagreements happen when the settlement comparison is close enough that feed differences, target differences, and late path movement dominate the final direction. The most actionable warnings are source-gap compression/expansion, direction disagreement, and insufficient time-scaled distance from the thresholds. The `$15` target-divergence cutoff was overfit and too blunt: safe contracts also had large target divergence, including some of the highest available spreads. Under contract-level accounting, the improved filter reduces losses but remains negative EV because safe wins are roughly cents while adverse discrepancies are close to full-cost losses. With average safe gain near `+0.025` and average dangerous loss near `-0.81`, the dangerous-entry rate must be below roughly `3%`; the current time-scaled `$15` rule is at `5.6%`. The default behavior should be conservative sizing or no trade when the filter is marginal, plus continuous stay-in monitoring for open positions rather than passive hold-to-expiry.
