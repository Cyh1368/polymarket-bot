# SOL Polymarket T=240s — Full Backtest Bin Analysis Report

**Date:** 2026-06-12  
**Model config:** λ_l2=0.5, min_child_samples=8, n_rounds=200, max_depth=3, lr=0.06  
**Horizon:** T=240s (entry 4 minutes before contract close)  
**Dataset:** 517 settled SOL 5m contracts (all available as of 2026-06-12)  
**Evaluation:** In-sample — full model trained on all 517 contracts, predicted on the same 517

> **Note:** These are in-sample predictions. OOS estimates (200-seed random-split study) gave mean EV = -0.00583, 44% positive-seed rate. In-sample EV is informative about what patterns the model found, not what it would earn on new contracts.

---

## Overall Summary

| Metric | Value |
|---|---|
| Contracts total | 517 |
| Traded | 259 (50%) — YES=120, NO=139 |
| Skipped | 258 (50%) |
| Win rate (traded) | 117/259 = 45.2% |
| Total PnL | -5.55 |
| EV/available (in-sample) | **-0.01074** |
| EV/traded (in-sample) | **-0.02143** |

Even in-sample, EV is negative — the model cannot memorize a profitable rule on 517 rows with λ=0.5 regularization. This confirms the signal is genuinely weak rather than an OOS generalization problem.

---

## Confidence Bins

Bins by argmax probability of the predicted class (YES/NO/SKIP). Confidence < 0.33 is impossible (random baseline = 1/3).

| Conf range | n_avail | n_traded | Skip% | Win% | EV/avail | EV/traded | CI95_lo | CI95_hi |
|---|---|---|---|---|---|---|---|---|
| [0.33, 0.40) | 41 | 22 | 46% | 27.3% | -0.0861 | -0.1605 | -0.3626 | +0.0417 |
| **[0.40, 0.50)** | **143** | **72** | **50%** | **52.8%** | **+0.0402** | **+0.0799** | -0.0338 | +0.1935 |
| [0.50, 0.60) | 135 | 44 | 67% | 45.5% | -0.0147 | -0.0452 | -0.1741 | +0.0837 |
| [0.60, 0.70) | 94 | 45 | 52% | 40.0% | -0.0422 | -0.0882 | -0.2234 | +0.0469 |
| [0.70, 0.80) | 34 | 26 | 24% | 50.0% | +0.0112 | +0.0146 | -0.1662 | +0.1954 |
| [0.80, 0.90) | 21 | 17 | 19% | 41.2% | -0.0476 | -0.0588 | -0.2802 | +0.1626 |
| [0.90, 1.01) | 49 | 33 | 33% | 45.5% | -0.0243 | -0.0361 | -0.1806 | +0.1085 |

**Key observations:**
- The **[0.40, 0.50) confidence bin is the only clearly positive-EV cluster** in-sample: 72 trades, 52.8% win rate, EV/traded = +0.080. This is counter-intuitive — the model's most profitable predictions are its *least confident* ones.
- The **[0.33, 0.40) bin is the worst**: 27.3% win rate, EV/traded = -0.161. These near-random predictions are strongly negative EV.
- High-confidence bins (≥0.60) are uniformly negative EV. The model is confidently wrong in those cases.
- All CI95 intervals span zero. With 22–72 trades per bin, individual bin EV estimates have ±0.05–0.16 uncertainty.

---

## Price Bins (Entry Ask Cost)

Bins by the ask cost of the side traded (YES ask for YES trades, NO ask for NO trades). For skips, binned by min(yes_ask, no_ask) but only traded contracts count toward EV.

| Price bin | n_avail | n_traded | Skip% | Win% | EV/avail | EV/traded | CI95_lo | CI95_hi |
|---|---|---|---|---|---|---|---|---|
| [0.00, 0.10) | 3 | 1 | 67% | 0.0% | -0.027 | -0.080 | n/a | n/a |
| [0.10, 0.20) | 27 | 12 | 56% | 25.0% | **+0.032** | **+0.072** | -0.182 | +0.325 |
| [0.20, 0.30) | 80 | 34 | 57% | 26.5% | -0.002 | -0.005 | -0.153 | +0.144 |
| [0.30, 0.40) | 136 | 63 | 54% | 33.3% | -0.011 | -0.024 | -0.141 | +0.092 |
| [0.40, 0.50) | 155 | 54 | 65% | 42.6% | -0.006 | -0.017 | -0.151 | +0.116 |
| [0.50, 0.60) | 50 | 29 | 42% | 51.7% | -0.025 | -0.043 | -0.223 | +0.138 |
| [0.60, 0.70) | 25 | 25 | 0% | 64.0% | -0.010 | -0.010 | -0.197 | +0.177 |
| [0.70, 0.80) | 28 | 28 | 0% | 67.9% | -0.073 | -0.073 | -0.248 | +0.103 |
| [0.80, 0.90) | 11 | 11 | 0% | 81.8% | -0.028 | -0.028 | -0.266 | +0.210 |
| [0.90, 1.01) | 2 | 2 | 0% | 100.0% | +0.065 | +0.065 | +0.016 | +0.114 |

**Key observations:**
- **[0.10, 0.20) bin shows positive in-sample EV** (+0.072/trade, 12 trades). At ~15¢ cost, the break-even win rate is ~15/(100-15) = 17.6%. With 25% observed wins, this yields a small edge mathematically. These are likely NO-side entries on contracts priced 80–90% mid.
- **[0.20, 0.30) bin near-zero EV**: 34 trades, essentially flat. Break-even at ~25% is exactly met with 26.5% wins.
- **[0.30, 0.50) is slightly negative** but CI95 spans zero — noise-level.
- **[0.50–0.90) bins have high skip rate = 0%** — the model trades everything in this range. High win rates (52–82%) but EV is negative because the compressed payoff ratio (win 40–50¢, lose 50–60¢) requires >55% win rate to break even, which the model doesn't achieve.
- **[0.90, 1.01) tiny sample (2 contracts)** — not interpretable.

---

## Mid-Price Bins (p_yes_mid)

Bins by the market mid-probability at entry time. This is distinct from entry cost since the model can trade either YES or NO.

| Mid-price | n_avail | n_traded | Skip% | Win% | EV/avail | EV/traded | CI95_lo | CI95_hi |
|---|---|---|---|---|---|---|---|---|
| [0.00, 0.10) | 1 | 0 | 100% | — | +0.000 | — | — | — |
| [0.10, 0.20) | 26 | 14 | 46% | 50.0% | +0.013 | +0.024 | -0.200 | +0.249 |
| [0.20, 0.30) | 67 | 28 | 58% | 39.3% | -0.025 | -0.059 | -0.205 | +0.088 |
| [0.30, 0.40) | 84 | 22 | 74% | 45.5% | -0.004 | -0.016 | -0.213 | +0.182 |
| [0.40, 0.50) | 99 | 24 | 76% | 41.7% | -0.020 | -0.082 | -0.289 | +0.126 |
| **[0.50, 0.60)** | **92** | **54** | **41%** | **50.0%** | **+0.005** | **+0.009** | -0.123 | +0.140 |
| [0.60, 0.70) | 83 | 70 | 16% | 42.9% | -0.017 | -0.020 | -0.134 | +0.093 |
| [0.70, 0.80) | 50 | 38 | 24% | 44.7% | -0.034 | -0.045 | -0.195 | +0.106 |
| [0.80, 0.90) | 11 | 6 | 45% | 66.7% | +0.077 | +0.142 | -0.147 | +0.431 |
| [0.90, 1.01) | 4 | 3 | 25% | 33.3% | -0.040 | -0.053 | -0.148 | +0.041 |

**Key observations:**
- The model trades most actively (low skip%) in the **0.50–0.70 mid-price range**. These are contracts near 50/50 where spread is tightest.
- The **[0.50, 0.60) bin is near-zero EV** (+0.009/trade), the only marginally positive bucket at mid-price.
- **[0.80, 0.90) bin: 6 trades, 66.7% win rate, EV/trade = +0.142** — interesting but far too small (CI95 spans zero).
- Skip rates are highest in the **0.30–0.50 mid-price range** (74–76%) — the model correctly avoids these more uncertain contracts.

---

## Direction Analysis

| Side | Trades | Win rate | EV/trade | CI95 |
|---|---|---|---|---|
| YES | 120 | 49% | -0.0187 | [-0.101, +0.064] |
| NO | 139 | 42% | -0.0238 | [-0.102, +0.055] |

Neither YES nor NO trades are positive EV in-sample. NO trades (42% win rate) are weaker — the model's NO bets (that a currently-50%-mid contract resolves Down) are more often wrong.

---

## Feature Importance (Full Model, Gain)

| Rank | Feature | Gain |
|---|---|---|
| 1 | OBI_vol_60 | 199 |
| 2 | yes_mid_vol_60 | 198 |
| 3 | p_yes_mid | 148 |
| 4 | yes_mid_vol_20 | 139 |
| 5 | yes_mid_z_20 | 120 |
| 6 | OBI_z_60 | 120 |
| 7 | yes_book_imbalance_tau_5c | 86 |
| 8 | yes_book_imbalance_tau_1c | 83 |
| 9 | yes_mid_z_60 | 75 |
| 10 | mid_change_from_open | 73 |

Volatility features (OBI_vol_60, yes_mid_vol_60) dominate at T=240s. With 4 minutes remaining, the *rate of change* is more informative than the current depth snapshot.

---

## Summary Verdict

| Criterion | Status |
|---|---|
| In-sample EV > 0 | ✗ -0.01074 |
| Any price bin positive EV (with CI95_lo > 0) | ✗ All CI95 span zero |
| Any confidence bin positive EV (with CI95_lo > 0) | ✗ All CI95 span zero |
| OOS EV > 0 (200-seed study) | ✗ -0.00583, CI95 = [-0.011, -0.0004] |
| >50% positive OOS seeds | ✗ 44% |

**No sub-bin of this model meets shadow-test criteria.** The [0.40, 0.50) confidence bin and [0.10, 0.20) price bin show in-sample positive EV, but both have wide confidence intervals spanning zero. These patterns would need to hold OOS across 200 seeds to be tradeable — which the overall model fails.

**Next step:** Continue collecting SOL 5m data. Re-evaluate when ≥1,500 total contracts are available (currently 517 settled). Best confirmed hyperparameters: λ=0.5, mc=8, 200 rounds, T=240s.
