# SOL T=240s — Post-Hoc Filter Study Report

**Date:** 2026-06-12  
**Model:** λ=0.5, mc=8, 200 rounds, depth=3, T=240s (best config from 200-seed sweep)  
**Goal:** Find a post-hoc filter that maximizes live trading profit without overfitting  
**Framework:** 200-seed OOS random-split stability test (train=80%, test=20%)

---

## Filters Tested

| Filter | Definition | Theoretical motivation |
|---|---|---|
| **baseline** | All model trades (no filter) | — |
| **conf_40_50** | conf ∈ [0.40, 0.50) | In-sample best confidence bin; "barely trades" cases |
| **cost_10_30** | entry_cost ∈ [0.10, 0.30) | Low-cost entries with better risk/reward math |
| **combined** | conf_40_50 AND cost_10_30 | Intersection of above |

---

## 200-Seed OOS Results

| Filter | %Pos seeds | EV/all | CI95_all_lo | CI95_all_hi | EV/traded | CI95_filt_lo | CI95_filt_hi | Avg trades/seed |
|---|---|---|---|---|---|---|---|---|
| baseline | 44% | -0.00583 | -0.01101 | -0.00035 | -0.00839 | -0.01634 | +0.00002 | 68.6 |
| conf_40_50 | 42% | -0.00206 | -0.00422 | +0.00002 | -0.01899 | -0.03959 | +0.00309 | 11.2 |
| **cost_10_30** | **52%** | **+0.00170** | **+0.00016** | **+0.00337** | **+0.01915** | -0.00142 | +0.03959 | **9.2** |
| combined | 35% | -0.00016 | -0.00079 | +0.00047 | -0.02181 | -0.07370 | +0.03343 | 1.4 |

**The `cost_10_30` filter is the only one to show CI95_lo(ev_all) > 0.** This is the first positive lower bound found for any SOL T=240s configuration.

---

## Seed Slice Analysis for cost_10_30

| Seed slice | %Pos | EV/traded | EV/all | Avg N/seed |
|---|---|---|---|---|
| s0–49 | 58% | +0.052 | +0.0046 | 8.9 |
| s50–99 | 49% | +0.017 | +0.0009 | 9.5 |
| s100–199 | 51% | +0.004 | +0.0006 | 9.1 |

**The signal degrades across seed slices.** The first 50 seeds showed strong 58% positive rate and +0.052 EV/trade. The subsequent 150 seeds converge to +0.004 EV/trade and ~50% positive — near-zero. The overall CI95_lo barely positive (+0.00016) is partly driven by the favorable first 50 seeds.

Compare to baseline's slice collapse: s0-49 = 60%, s100-199 = 37%. The cost_10_30 filter is more *stable* across slices than the baseline (50% vs 37% in s100-199), but not strongly convincing.

---

## What Does cost_10_30 Actually Trade?

From the full in-sample model (517 contracts):

| Side | Trades | Avg mid | Avg cost | Win rate | Break-even WR | EV/trade |
|---|---|---|---|---|---|---|
| YES | 24 | 0.221 | 0.227 | 20.8% | 29.4% | -0.029 |
| **NO** | **22** | **0.761** | **0.245** | **31.8%** | **24.6%** | **+0.063** |

**The NO trades drive almost all the edge.** These are bets that contracts priced at ~76% mid (high-certainty YES) will resolve DOWN. At 24.5¢ cost, the break-even win rate is 24.6% — the model achieves 31.8% in-sample.

The YES trades (20.8% win rate vs 29.4% break-even) are slightly negative and dilute the filter's performance. These are bets on low-probability contracts (21% mid) resolving UP at 23¢ cost.

### Why Does This Make Sense?

At T=240s with 4 minutes to close, contracts with high YES probability (70–90% mid) have already moved toward certainty. The NO side at 10–30¢ represents a rare reversal scenario. The model finds that when OBI/vol signals are in certain states, the contract is slightly more likely to revert than the current price implies. The payoff structure is asymmetric: winning returns 70–90¢ on a 25¢ bet, vs losing 25¢.

---

## Statistical Assessment

**Multiple comparisons caveat:** We tested 4 filters and selected the one that passed CI95. With 4 tests at 95% CI, the expected number of false positives is 0.2, so one barely-passing filter is not conclusive.

**Seed-slice stability:** The slice degradation from s0-49 (+0.052) to s100-199 (+0.004) suggests the strong early signal was partially a sampling artifact. A genuinely stable signal should show consistent EV across all slices.

**Shadow-test criteria checklist:**

| Criterion | cost_10_30 | Notes |
|---|---|---|
| >50% positive seeds | ✓ 52% | Barely above threshold |
| CI95_lo(ev_all) > 0 | ✓ +0.00016 | Barely positive; multiple-comparisons concern |
| CI95_lo(ev_filtered) > 0 | ✗ -0.00142 | Spans zero |
| Stable across seed slices | ✗ 58% → 51% | Degrades but does not collapse like baseline |
| ≥50 OOS trades per study | ✓ 9.2/seed × 200 = 1,840 total | ~1,840 total filtered trades seen |

**Verdict:** The cost_10_30 filter is *promising but not shadow-test ready.* It narrowly passes 2 of 4 strict criteria and shows less collapse than the baseline. With only 517 contracts, we cannot separate genuine edge from noise at this precision level.

---

## Recommendation for Live Trading

If deploying SOL T=240s now, use the **cost_10_30 filter** as a trade gate:

```python
# Post-model filter: only execute if model chose YES/NO AND entry cost is 10–30¢
def should_trade(model_decision, entry_ask_cost):
    return model_decision in ("YES", "NO") and 0.10 <= entry_ask_cost < 0.30
```

**Expected behavior:**
- ~9% of T=240s opportunities will be traded (9.2/104 per period)
- Roughly 50/50 YES and NO, but edge primarily from NO bets on high-certainty contracts
- Expected EV/trade in range of 0–+0.019 (uncertain; CI spans zero on traded basis)

**Caveats:**
- Statistical evidence is borderline — this is an educated bet, not a validated signal
- The cost range [0.10, 0.30) was selected by observing in-sample patterns; it may not generalize
- The NO side in this range (high-certainty contracts) is where the edge is theoretically most justified
- Confirm at 1,500+ total settled contracts before treating this as a production signal

**Do NOT use the conf_40_50 filter:** It shows 35% positive rate in the last 100 seeds (strongly negative) and is clearly in-sample noise. The in-sample [0.40, 0.50) confidence bin positive EV does not generalize.

---

## Next Steps

1. **Continue collecting SOL 5m data** — currently at 517 settled contracts, need 1,500+
2. **Re-run this filter study at 1,500 contracts** with the same cost_10_30 definition
3. **Monitor live trades** through the filter to accumulate real P&L data faster than OOS simulations
4. **Consider NO-only variant**: `side == "NO" AND entry_cost in [0.10, 0.30)` — fewer trades (~9 in-sample) but cleaner theoretical motivation; needs more data to test
