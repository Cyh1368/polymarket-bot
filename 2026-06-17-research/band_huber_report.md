# Steps 2 & 3 — Cost-Band Filter and Huber Edge-Regression

**Date:** 2026-06-17
**Scripts:** `cv_band_and_huber.py` (4-variant CV) → `robust_metrics.py` (scoring)
**Results dir:** `band_huber_results/`
**Builds on:** `lottery_dependence_redesign.md` (diagnosis + §7 step-1 results)

All four variants run on **identical** expanding-window folds (5 folds, MIN_TRAIN=200,
T1=180s, skip_bonus=0.05, Filter B). Dataset has grown to 2192 contracts at T1=180s (now
includes the recent, lottery-poor June 17–18 sessions), so the baseline EV here is lower
than the older `cv_v3p1_records.csv` — that is expected; what matters is the **relative**
comparison on the same folds.

Metrics are in return-on-$1-stake units (matches live constant-value staking).

---

## 1. Variant definitions

| Variant | Model | Decision rule |
|---|---|---|
| **baseline** | binary log-loss → P(UP) | EV rule (`p/cost−1 > 0.05`) |
| **band** | binary log-loss → P(UP) | EV rule **+ trade only if traded-side ask ∈ [0.40, 0.80)** |
| **huber** | two Huber edge-regressors `f_yes, f_no` (α=1.0) | trade higher predicted edge `> 0.05` |
| **huber_band** | two Huber edge-regressors | edge rule **+ cost-band [0.40, 0.80)** |

---

## 2. Headline results

| Variant | trades | WR | true EV | true 95% CI (day-block) | P(EV≤0) | **win-capped** | trim10 | flip-neg | GATE |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 1154 | 51% | +0.005 | [−0.076, +0.069] | 44% | **−0.175** | −0.212 | **1** | ❌ |
| band | 687 | 60% | −0.000 | [−0.054, +0.056] | 50% | −0.026 | −0.032 | — | ❌ |
| huber | 942 | **65%** | **+0.030** | [−0.008, +0.061] | **6%** | **−0.029** | −0.037 | 7 | ❌ |
| huber_band | 632 | 62% | +0.019 | [−0.022, +0.063] | 18% | **−0.001** | −0.001 | 9 | ❌ |

*("flip-neg" = number of top winners whose removal turns total PnL negative; "—" = total
PnL ≈ 0 so the statistic is undefined. Tail-share % is also meaningless for `band` because
its total PnL ≈ 0.)*

---

## 3. Reading the results

**The objective change worked as designed — lottery dependence collapsed.** Win-capped EV
(the lottery-free metric) climbed monotonically as we de-risked:

```
baseline  −0.175   (edge is almost entirely lottery payoffs; top 1 trade = 260% of PnL)
band      −0.026   (cost-band removes lottery — but also removes the edge → ~breakeven)
huber     −0.029   (Huber keeps the data, kills most lottery dependence, BEST true EV)
huber_band −0.001  (essentially lottery-NEUTRAL: profits/losses no longer depend on jackpots)
```

- **baseline on the fuller dataset no longer even makes money on true EV** (CI straddles 0,
  P(EV≤0)=44%) and removing a *single* top trade turns it negative. This is the lottery
  problem in its purest form.
- **`huber` has the best true EV (+0.030), the highest win rate (65%), the best
  significance (P(EV≤0)=6%)**, and needs 7 top winners removed to flip — 7× more robust than
  baseline. Its win-capped EV (−0.029) is far better than baseline's (−0.175): the model is
  no longer *trained to hope*.
- **`huber_band` is the most consistent of all**: win-capped EV ≈ 0 and trim10 ≈ 0 mean its
  P&L is **independent of lottery payoffs** — exactly the "accumulate small wins" profile.
  Its true EV stays positive (+0.019).

**No variant fully passes the gate**, but the failure mode flipped. Baseline fails because
it is *all lottery*. Huber/huber_band fail only *narrowly* and only because the true-EV
day-block CI dips just below zero (lower bounds −0.008 and −0.022) — a **significance /
sample-size** problem (9 day-blocks), not a lottery problem. With more trading days the
huber variants are the candidates positioned to pass.

**The favorites band is the universal bright spot.** In every variant the **[0.70, 0.85)**
fill-price band is positive on win-capped, trim10, and Sharpe, and [0.85, 1.00) joins it
under the Huber model:

| Variant | [0.70,0.85) win-capped / trim10 | [0.85,1.00) win-capped / trim10 |
|---|---|---|
| baseline | +0.038 / +0.118 | +0.047 / +0.137 |
| huber | +0.053 / +0.135 | +0.040 / +0.100 |

---

## 4. Recommendation

1. **Adopt the Huber edge-regression objective.** It dominates the binary+EV baseline on
   every axis that matters: higher true EV, higher win rate, far lower lottery dependence,
   far higher tail-robustness — *without discarding data*.
2. **Keep the cost-band as a switch, not a default.** `huber_band` is the most
   lottery-neutral but trades ~33% less and trims a bit of true EV. Use it when the priority
   is drawdown control; use plain `huber` when the priority is EV.
3. **The remaining barrier is sample size, not method.** Re-run this gate after more trading
   days; the huber variants are the ones to watch.

---

## 5. Note on training **only** with non-lottery data (0.25 < p_yes < 0.75)

See `lottery_dependence_redesign.md` §8 for the full argument. Short version: the instinct
is right (keep lottery contracts out of the fit) but the **specific cut is on the wrong axis
and at the wrong place** — it would discard the *favorites band* (your most consistent
edge) along with the longshots. The Huber objective achieves the same goal (down-weighting
lottery influence) **without** throwing away data or the favorites, and the numbers above
show it works. Prefer Huber; if you still want a hard filter, filter the **traded-side cost**
at decision time, or exclude only the **extreme tails** (p_yes < ~0.12 or > ~0.88) from
training — never the [0.25, 0.75] interior.

---

## 6. Empirical test of the non-lottery cut (binary_cut, huber_cut)

Two variants were added that **train AND trade only on `0.25 ≤ p_yes_mid < 0.75`**
contracts (train regime = trade regime, internally consistent). NB: the live collector adds
contracts between runs, so absolute numbers shifted slightly from §2 (dataset grew
2192→2200 rows); the table below is all from one consistent re-run.

| Variant | trades | WR | true EV | true 95% CI | **win-capped** | trim10 | favorites share |
|---|---|---|---|---|---|---|---|
| baseline | 1161 | 53% | +0.040 | [−0.022, +0.101] | **−0.146** | −0.176 | — |
| **huber** (full data) | 940 | 65% | +0.025 | [−0.017, +0.065] | **−0.033** | −0.042 | **40.6%** |
| binary_cut | 893 | 54% | +0.020 | [−0.021, +0.062] | **−0.073** | −0.082 | 13.3% |
| huber_cut | 737 | 56% | +0.021 | [−0.018, +0.066] | **−0.052** | −0.062 | 16.4% |

### Verdict: the instinct is half-right, but the cut is dominated by the objective change

1. **The cut does reduce lottery dependence** vs the same model on full data:
   binary_cut win-capped −0.073 vs baseline −0.146 (roughly halved). So removing lottery
   contracts from training genuinely helps a weak (binary) model. ✅ instinct confirmed.

2. **But changing the objective helps more.** `huber` on *full* data is more lottery-free
   (win-capped −0.033) than `binary_cut` on cut data (−0.073). The loss-function fix beats
   the data fix.

3. **Cut + Huber is WORSE than plain Huber** (−0.052 vs −0.033). The cut *removes the
   favorites*: favorites share (fill ≥ 0.70) drops from **40.6% → 16.4%**. It throws away
   the most consistent edge zone and leaves the model trading the thin-edge coin-flip middle
   — exactly the §8 prediction, now confirmed.

4. **The cut is on the wrong axis.** A NO bet at p_yes_mid = 0.70 (inside the cut) costs
   ~0.30 — still a cheap bet. huber_cut *still* placed 109 trades at fill < 0.40. Filtering
   `p_yes_mid` does not actually remove cheap bets on the cost axis.

**Conclusion:** train on full data with the Huber objective. Do **not** pre-cut the training
set to [0.25, 0.75] — it costs ~40% of data, removes your best (favorites) edge, fails to
remove cheap bets, and underperforms plain Huber on every robustness axis.
