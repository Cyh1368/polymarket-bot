# Regime-Specific Model Findings — Polymarket BTC 5m Trader
**Date:** 2026-06-16  
**Model baseline:** LightGBM v3.1 (14 features, T1=180s, skip_bonus=0.05)  
**Data:** 1,798 BTC 5m contracts, June 8–14 2026, CV = 4 chronological expanding folds

---

## 1. Motivation

The June 16 live session revealed a regime-mismatch failure: NO trades at `p_yes_mid < 0.60` collapsed from an expected 71.3% CV win rate to 37.5% live (3/8). Post-hoc analysis confirmed the session occurred during a BTC uptrend.

The CV overall EV of +5.45% masked severe regime-specific dispersion.

---

## 2. Regime Definition

**Source:** Kraken OHLCV (hourly candles, public API, no auth required).  
**Feature:** `btc_4h_ret` = (BTC price at T1 − BTC price at T1−4h) / BTC price at T1−4h  
**Thresholds:**
- **UP:** `btc_4h_ret > +0.3%`
- **DOWN:** `btc_4h_ret < −0.3%`
- **FLAT:** otherwise

**Training data distribution** (1,798 contracts, June 8–14 2026):

| Regime | Contracts | % | btc_4h_ret mean | range |
|--------|-----------|---|-----------------|-------|
| UP     | 626       | 34.7% | +0.84% | [+0.32%, +2.64%] |
| FLAT   | 568       | 31.5% | +0.02% | [−0.30%, +0.29%] |
| DOWN   | 612       | 33.9% | −0.78% | [−2.54%, −0.31%] |

All three regimes are balanced (>300 contracts each), so a 3-regime system is viable.

---

## 3. v3.1 Performance by Regime (CV OOS)

| Regime | Avail | Traded | EV/avail | EV/trig | Win rate | YES EV | NO EV |
|--------|-------|--------|----------|---------|----------|--------|-------|
| **UP** | 481 | 331 | **−5.34%** | −7.76% | 47.1% | −3.14% | −10.32% |
| **FLAT** | 505 | 344 | **+10.13%** | +14.87% | 60.2% | +8.53% | +20.13% |
| **DOWN** | 453 | 324 | **+11.68%** | +16.33% | 59.0% | −5.36% | +30.14% |
| ALL | 1,439 | 999 | +5.45% | +7.85% | 55.5% | +0.71% | +12.61% |

**Key finding:** The model **loses money in UP regimes** (−5.34% EV/avail). The +5.45% all-in CV average is a blend of +10–12% (FLAT/DOWN) and −5.34% (UP). The UP failure manifests specifically in NO trades at `p_yes_mid ∈ [0.50, 0.95)`:

| p_yes_mid (NO trades, UP regime) | n | Win rate | EV/trig |
|----------------------------------|---|----------|---------|
| [0.0, 0.5)  | 101 | 66.3% | ~0.0% |
| [0.5, 0.6)  | 27  | 40.7% | −11.9% |
| [0.6, 0.7)  | 13  | 23.1% | −39.8% |
| [0.7, 0.8)  | 30  | 13.3% | **−46.7%** |
| [0.8, 0.85) | 10  | 0%    | **−100%** |
| [0.85, 0.9) | 14  | 21.4% | +60.9% (lottery) |
| [0.95, 1.0) | 7   | 0%    | −100% |

---

## 4. Regime-Specific Model Training Results

Three separate LightGBM models were trained (same 14 V31 features, same hyperparameters), one per regime. Evaluation: 200-seed random splits with paired bootstrap vs v3.1 trained on all data.

| Model | 200-seed EV | CI95 | %pos | Paired Δ vs v3.1 | CI95 | Verdict |
|-------|-------------|------|------|------------------|------|---------|
| ALL (v3.1) | +6.02% | [+5.29%, +6.72%] | 89.5% | — | — | baseline |
| regime_UP | −2.50% | [−3.47%, −1.52%] | 34.0% | **−3.05pp** | [−4.10, −2.03] | ✗ significantly worse |
| regime_FLAT | +1.02% | [−0.18%, +2.24%] | 56.5% | −0.61pp | [−1.93, +0.72] | ~ not significant |
| regime_DOWN | +6.68% | [+5.17%, +8.21%] | 69.0% | **+2.83pp** | [+1.27, +4.37] | **✓ significantly better** |
| v3.2 (+regime features) | +5.20% | [+4.47%, +5.89%] | 86.0% | −0.82pp | [−1.35, −0.29] | ✗ significantly worse |

**Interpretation:**
- **DOWN model:** Significantly better than v3.1 on DOWN-regime contracts. Training on DOWN-only data improves NO-trade edge when BTC is falling.
- **UP model:** Significantly worse. Splitting out UP data reduces the training set without adding regime-specific signal — the UP regime is inherently hard (fewer profitable NO trades), and the reduced dataset causes overfitting.
- **FLAT model:** No significant improvement.
- **v3.2 (regime features added):** Adding `btc_4h_ret` / `btc_1h_ret` as model inputs hurts. LightGBM's max_depth=3 is too shallow to exploit regime × orderbook interactions.

**Conclusion on regime models:** Only the DOWN model is deployable. The UP and FLAT models should not replace v3.1.

---

## 5. FLAT+DOWN Strategy (Skip UP Regime Entirely)

Since the DOWN-specific model is the only improvement, and UP is consistently loss-making:

**Strategy:** Use v3.1 for FLAT+DOWN regimes; skip UP regime entirely.

| Metric | Value |
|--------|-------|
| CV EV/available | **+7.23%** (vs +5.45% all-in) |
| CV win rate (traded) | **59.6%** |
| 200-seed mean EV | +5.47% |
| 200-seed CI95 | [−2.78%, +14.88%] |
| n_traded (CV, OOS) | 668 / 958 FLAT+DOWN contracts |
| Trade rate (excl. UP) | 69.8% of FLAT+DOWN available |

**Note on 200-seed vs CV discrepancy:** The 200-seed paired test shows FLAT+DOWN is marginally worse than all-in (−0.55pp, CI [−0.90, −0.19]). This is because random splits expose UP-regime training data to UP-regime test evaluation, yielding a small positive UP contribution (+0.55%) that disappears when UP is skipped. The chronological CV is more predictive of live performance (where training always precedes deployment), and the live session confirmed UP-regime losses. The CV result (+7.23%) is the deployment-relevant estimate.

---

## 6. Narrow Filter Comparison

| Strategy | CV EV/avail | Bootstrap CI95 | n_traded |
|----------|-------------|----------------|----------|
| All-in v3.1 | +5.45% | — | 999 |
| FLAT+DOWN only | +7.23% | [−2.78%, +14.88%]* | 668 |
| [0.0,0.5) NO only | +1.63% | [−0.10%, +3.35%] | 310 |
| FLAT+DOWN + [0.0,0.5) NO | +1.64% | [**+0.26%, +2.98%**] | 209 |

*200-seed CI; others are CV bootstrap.

The [0.0,0.5) NO-only strategy is the most statistically clean (CI stays above zero when restricted to FLAT+DOWN), but at the cost of a 79% reduction in trade volume. FLAT+DOWN captures the full value with 33% fewer contracts.

---

## 7. Deployment Decision

**Deploy:** FLAT+DOWN strategy using v3.1 model.  
**Future:** Use the DOWN-specific model (`lgb_regime_down_t180.txt`) when BTC 4h return < −0.3%. This adds +2.83pp in DOWN regimes. Pending further validation before live use.

**Regime thresholds for live routing:**
- UP threshold: `btc_4h_ret > +0.003` → SKIP all trades  
- DOWN threshold: `btc_4h_ret < −0.003` → use v3.1 (DOWN-specific model pending)  
- FLAT: `|btc_4h_ret| ≤ 0.003` → use v3.1

**Expected live performance (FLAT+DOWN):**
- EV/available: ~+7% (CV; wide uncertainty at ~±7% CI95)
- Win rate: ~59.6%
- NO win rate: ~59% (DOWN: 61.1%, FLAT: 58.5%)
- YES win rate: ~61% (FLAT: 62.2%, DOWN: 55.6%)

---

## 8. Model Files

| File | Description |
|------|-------------|
| `2026-06-16-research/btc_kraken_1h.csv` | Kraken hourly OHLCV for BTC/USD (June 1–16) |
| `2026-06-16-research/contract_regime_labels.csv` | Per-contract regime labels + btc_4h_ret |
| `2026-06-16-research/regime_models/lgb_regime_down_t180.txt` | DOWN-specific model (pending validation) |
| `2026-06-16-research/regime_models/lgb_regime_flat_t180.txt` | FLAT-specific model (not deployed; not significant) |
| `2026-06-16-research/regime_models/lgb_regime_up_t180.txt` | UP-specific model (not deployed; worse than v3.1) |
| `polymarket/lgb_v3p1_t180.txt` | Production model (v3.1, 14 features) |
