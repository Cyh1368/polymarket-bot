# Huber Hyperparameter Sweep — What Moves the Needle

**Date:** 2026-06-17
**Scripts:** `huber_common.py` (shared), `huber_sweep.py` (sweep), `train_huber_save.py` (save)
**Results:** `huber_sweep_results/huber_sweep_summary.csv`
**Saved model:** `saved_huber_model/` (production huber trained on all 2200 contracts)

Baseline + 7 single-axis modifications, each through the **same** expanding-window CV
(5 folds, MIN_TRAIN=200, T1=180s) and the **same** robust gate. One axis changed per config
so effects are attributable. Metrics in return-on-$1-stake units.

---

## Results

| Config | changed | trades | EV/avail | EV/trig | WR | **win-capped** | trim10 | true-CI-lo | GATE |
|---|---|---|---|---|---|---|---|---|---|
| baseline | — | 940 | +0.0136 | +0.025 | 65.1% | −0.0333 | −0.042 | −0.017 | fail |
| depth_up | depth 3→5, leaves 7→31 | 1047 | +0.0030 | +0.005 | 60.6% | −0.0785 | −0.098 | −0.049 | fail |
| lr_down | lr 0.05→0.02, rounds→750 | 941 | +0.0134 | +0.025 | 65.1% | −0.0337 | −0.042 | −0.009 | fail |
| rounds_up | rounds 300→600 | 1052 | +0.0075 | +0.013 | 62.5% | −0.0619 | −0.077 | −0.028 | fail |
| huber_d_0.5 | huber_alpha 1.0→0.5 | 1242 | +0.0146 | +0.021 | **68.6%** | **−0.0015** | **+0.004** | −0.030 | fail |
| huber_d_2.0 | huber_alpha 1.0→2.0 | 1099 | +0.0029 | +0.005 | 59.0% | −0.1087 | −0.134 | −0.026 | fail |
| **l2_strong** | lambda_l2 5→20 | 942 | **+0.0284** | **+0.053** | 67.4% | −0.0014 | −0.002 | **+0.0032** | fail* |
| **min_child_50** | min_child 20→50 | 942 | **+0.0261** | +0.049 | 66.8% | −0.0072 | −0.009 | **+0.0039** | fail* |

\* l2_strong and min_child_50 are the **first configs whose true-EV day-block CI lower bound
clears zero** (significant positive EV). They miss the full gate only because win-capped /
trim10 sit a hair below zero (−0.001 to −0.009).

---

## What matters (and what doesn't)

**1. Regularization is the dominant lever — and the baseline was over-fitting.**
- `lambda_l2 5→20` **doubled** EV/avail (+0.0136 → +0.0284) and pushed true-EV into
  significance (CI lo +0.0032) while making win-capped ≈ 0 (lottery-neutral).
- `min_child_samples 20→50` did almost the same (+0.0261, CI lo +0.0039).
- Both shrink the model, which *raises* EV — a textbook sign the baseline was fitting noise.

**2. Adding capacity HURTS.**
- `depth 3→5 / leaves 7→31` is the worst EV config (+0.003) and most lottery-dependent
  (win-capped −0.079).
- `rounds 300→600` also degrades (+0.008, win-capped −0.062).
- More capacity → more overfitting → it re-learns the jackpots. Keep the model small.

**3. The Huber cap (δ) is real and tighter is better.**
- `huber_alpha 2.0` (looser cap) is the most lottery-dependent of all (win-capped −0.109):
  loosening the cap lets jackpots back into the fit.
- `huber_alpha 0.5` (tighter cap) is the **most lottery-free** config — win-capped −0.0015
  and trim10 **positive (+0.004)**, WR 68.6%, most trades (1242). It just doesn't lift true
  EV much on its own.

**4. Learning rate / round count (at fixed shrinkage) barely matters.**
- `lr 0.05→0.02 + rounds→750` is statistically identical to baseline. Not a useful knob here.

---

## Ranking of levers

| Lever | Effect on true EV | Effect on lottery-freeness |
|---|---|---|
| **L2 regularization ↑** | **large +** (doubles) | + (capped → 0) |
| **min_child_samples ↑** | **large +** | + |
| Huber δ ↓ (0.5) | small + | **large +** (trim10 positive) |
| learning rate ↓ | ~0 | ~0 |
| n_rounds ↑ | − | − |
| model depth/leaves ↑ | **large −** | **large −** |

---

## Next experiment (obvious from the above)

The two winning directions are **complementary**: L2/min_child lift *true EV into
significance*, and huber_δ=0.5 drives *win-capped positive*. Combining them —
`lambda_l2=20, min_child_samples=50, huber_alpha=0.5` (and keep depth=3) — is the natural
candidate to clear the **full** gate (significant true EV **and** positive win-capped +
trim10) for the first time. Recommend running that combined config next.

---

## Saved model

`train_huber_save.py` saved the **baseline** huber (trained on all 2200 contracts) to
`saved_huber_model/` (`huber_yes_t180.txt`, `huber_no_t180.txt`, `metadata.json`). NB: this
is the baseline config; the sweep shows `lambda_l2=20` / `min_child=50` are strictly better,
so re-save once the combined config is validated. metadata.json carries a do-not-deploy
warning until the gate passes.

---

## Round 2 — combining the winning directions (`huber_sweep2.py`)

Stacking/tuning the round-1 winners (regularization ↑, Huber δ ↓). Same CV + gate.

| Config | changed | trades | EV/avail | EV/trig | WR | win-capped | trim10 | true-CI-lo | GATE |
|---|---|---|---|---|---|---|---|---|---|
| baseline | — | 940 | +0.0136 | +0.025 | 65% | −0.033 | −0.042 | −0.017 | fail |
| core | l2=20, mc=50, d=0.5 | 1251 | +0.0241 | +0.034 | 70% | +0.021 | +0.032 | −0.013 | fail(sig) |
| l2_huber | l2=20, d=0.5 | 1245 | +0.0255 | +0.036 | 70% | +0.014 | +0.022 | −0.004 | fail(sig) |
| mc_huber | mc=50, d=0.5 | 1258 | +0.0170 | +0.024 | 70% | +0.010 | +0.019 | −0.032 | fail |
| l2_mc | l2=20, mc=50 | 924 | +0.0181 | +0.035 | 67% | −0.010 | −0.012 | −0.025 | fail |
| reg_extreme | l2=40, mc=80 | 890 | +0.0129 | +0.026 | 68% | +0.005 | +0.008 | −0.034 | fail |
| core_d0.7 | l2=20, mc=50, d=0.7 | 1045 | +0.0201 | +0.034 | 69% | +0.014 | +0.021 | −0.010 | fail |
| core_fine | core + lr=0.03, rounds=500 | 1249 | +0.0227 | +0.032 | 70% | +0.019 | +0.029 | −0.014 | fail |

`core` and `l2_huber` pass the **lottery-free** half (win-capped & trim10 clearly positive)
but miss the **significance** half (true-EV CI lower bound just below 0).

### Key finding: a δ-mediated trade-off between the two gate halves

```
Round-1 l2_strong (d=1.0):  942 trades, EV/trig +0.053 -> SIGNIFICANT (CI lo +0.003) but capped -0.001 (mild lottery)
Round-2 core      (d=0.5): 1251 trades, EV/trig +0.034 -> LOTTERY-FREE (capped +0.021) but CI lo -0.013 (not significant)
```

Tightening δ to 0.5 makes the model take ~300 more marginal trades: lottery-free but
lower-edge, which dilutes EV/trig and widens the day-block CI back below zero. δ=1.0 + strong
reg = fewer/higher-edge/significant/mildly-lottery; δ=0.5 + strong reg = more/lower-edge/
lottery-free/not-significant. `core_d0.7` sits between and passes neither cleanly.

### Honest conclusion: the remaining blocker is data, not hyperparameters

Every strong config has true-EV CI lower bound in [−0.013, +0.004] while the point estimate
is +0.020 to +0.028 — the point estimate ~doubled vs baseline (tuning worked), but the CI is
wide because there are only **9 day-blocks**. No hyperparameter reliably clears a 9-day CI;
that needs **more trading days**.

### Recommendation

- **Best single config: `l2_huber`** (`lambda_l2=20, huber_alpha=0.5`): highest round-2
  EV/avail (+0.0255), lottery-free (capped +0.014, trim10 +0.022), closest to significance
  (CI lo −0.004). Leading candidate to re-save and watch.
- **More regularization is not free**: `reg_extreme` (l2=40, mc=80) *reduced* EV — l2≈20 is
  the sweet spot.
- **Stop tuning for significance** — it's now sample-size-bound. Collect more days, re-run.

---

## Round 3 — min_child_samples scan: 20 / 50 / 100 / 150 / 200 (`huber_sweep3.py`)

Tested on two bases. **Does more min_child keep helping? No — not monotonically, and it
depends on the base.**

**Base A — baseline (l2=5, δ=1.0):** isolates min_child's effect.

| mc | trades | EV/avail | WR | win-capped | trim10 | true-CI-lo | GATE |
|---|---|---|---|---|---|---|---|
| 20 | 940 | +0.0136 | 65.1% | −0.033 | −0.042 | −0.017 | fail |
| 50 | 942 | +0.0261 | 66.8% | −0.007 | −0.009 | +0.004 | fail |
| 100 | 905 | +0.0171 | 68.8% | +0.010 | +0.014 | −0.009 | fail |
| 150 | 878 | +0.0264 | 70.7% | +0.034 | +0.045 | −0.003 | fail |
| **200** | 809 | +0.0195 | **71.1%** | **+0.031** | **+0.043** | **+0.007** | **PASS ✅** |

**Base B — l2_huber (l2=20, δ=0.5):** our leading config.

| mc | trades | EV/avail | WR | win-capped | true-CI-lo |
|---|---|---|---|---|---|
| 20 | 1245 | **+0.0255** | 69.5% | +0.014 | −0.004 |
| 50 | 1251 | +0.0241 | 70.2% | +0.021 | −0.013 |
| 100 | 1305 | +0.0112 | 70.0% | +0.008 | −0.019 |
| 150 | 1421 | +0.0083 | 70.2% | +0.003 | −0.025 |
| 200 | 1466 | +0.0080 | 70.4% | +0.005 | −0.022 |

### What the scan shows

1. **Lottery-freeness and win-rate climb steadily with min_child** (Base A: win-capped
   −0.033 → +0.031, WR 65% → 71%). That part *does* get monotonically better — more leaf
   regularization = fewer marginal/jackpot-ish trades.
2. **EV/avail does NOT keep improving** — it's non-monotonic and bounces around +0.02–0.026
   (mc100 dips to +0.017). More min_child is not "increasingly better" on EV.
3. **It depends on the base.** On the already-regularized `l2_huber`, more min_child strictly
   **hurts** (EV falls +0.0255 → +0.0080 as mc 20→200). min_child and (L2 + tight δ) are
   **substitute regularizers** — you want ~one dose of total regularization; stacking them
   over-shrinks into underfit.

### The first gate pass — but seed-fragile (multi-seed robustness check)

`baseA_mc200` PASSED the full gate (and mc150 nearly so). Re-running across 6 model seeds:

```
baseA_mc150:  3/6 seeds pass   (win-capped +0.034..+0.043 ALWAYS positive; true-CI-lo −0.008..+0.007)
baseA_mc200:  4/6 seeds pass   (win-capped +0.026..+0.038 ALWAYS positive; true-CI-lo −0.013..+0.008)
```

- **The lottery-free half is robust**: win-capped & trim10 are positive on *every* seed.
  Heavy min_child reliably makes the strategy lottery-free. ✅
- **The significance half is a coin-flip**: true-EV CI lower bound straddles zero across
  seeds (passes ~half). The point estimate sits right on the significance boundary.

### Verdict

More min_child is **not** "increasingly better." It robustly improves *lottery-freeness and
win-rate* up to mc≈150–200 (on a lightly-regularized base), and it produced the project's
first gate pass — but that pass is **marginal and seed-fragile** because the binding
constraint is still **significance with only 9 day-blocks**, not the model. On a
well-regularized base it actively hurts. Net: mc≈150 on a light base is the consistency
sweet spot; don't stack it on top of strong L2 + tight δ; and treat the gate pass as
"on the threshold," not "solved." Confirm with more trading days.

---

## Round 4 — min_child to 1000, multi-seed (`huber_sweep4.py`); FINAL model selection

Hypothesis (from round 3): more min_child is an inverted-U, not "increasingly better" —
lottery-freeness improves to a sweet spot, then very high mc UNDERFITS and degrades. Tested
mc 150→1000 with 5-seed averaging.

| mc | EV/avail | win-capped | trim10 | true-CI-lo | pass | leaves/tree (prod) |
|---|---|---|---|---|---|---|
| **150** | **+0.029** | **+0.040** | **+0.052** | +0.0001 | 3/5 | 5.5 |
| 200 | +0.020 | +0.032 | +0.044 | +0.0005 | 3/5 | 5.3 |
| 300 | +0.006 | −0.083 | −0.103 | −0.054 | 0/5 | 4.7 |
| 500 | +0.006 | −0.103 | −0.129 | −0.080 | 0/5 | 3.4 |
| 750 | −0.006 | −0.172 | −0.214 | −0.138 | 0/5 | 2.0 |
| 1000 | −0.002 | −0.212 | −0.263 | −0.223 | 0/5 | 2.0 |

**Hypothesis CONFIRMED — and sharper than expected.** Peak at **mc≈150**; beyond ~200 it
collapses. Two mechanisms:
- **Underfit:** leaves/tree falls toward 2.0 (barely-splitting stumps) as mc rises.
- **Lottery dependence RETURNS:** win-capped plunges to −0.21 at mc=1000. A near-constant
  model can't tilt toward favorites, so the raw lottery payoff structure dominates the trade
  set again. (This is why high mc is *worse* than baseline, not just weaker.)

### Final model selected for live: `baseA_mc150`

`max_depth=3, num_leaves=7, min_child_samples=150, lambda_l2=5, learning_rate=0.05,
n_rounds=300, huber_alpha=1.0` (two regressors f_yes/f_no on full data).

Chosen because it has the **highest true EV among the robustly lottery-free configs**
(EV/avail +0.029), the **strongest win-capped (+0.040, positive on every seed)**, and the
most gate passes (3/5). It beats `l2_huber` on both EV and lottery-freeness, and stacking
L2/δ on top of mc=150 only over-regularizes.

**Saved →** `saved_huber_model/` (`huber_yes_t180.txt`, `huber_no_t180.txt`, `metadata.json`).

### Honest status

The model is **robustly lottery-free** (the project's original goal — achieved) but its
gate pass is **seed-fragile**: the true-EV day-block CI lower bound sits on the zero boundary
(~3/5 seeds) because there are only ~9 day-blocks. Significance is now **sample-size limited,
not model-limited**. metadata.json carries an explicit do-not-deploy-capital warning until
the gate passes robustly on more trading days.

---

## Deployment (2026-06-18)

`polymarket/polymarket_5m_trader.py` updated to use the saved Huber edge model as its
strategy:
- Loads `polymarket/huber_yes_t180.txt` + `polymarket/huber_no_t180.txt` (two regressors).
- Decision: trade the side with the higher predicted edge if it clears `skip_bonus=0.05`.
  **No post-hoc filters** (removed Filter B / B+ and any cost-band).
- New defaults: `--contract-value 1.05`, `--stop-loss 30`, mode = **dry testing** (`--live`
  remains opt-in).

Still a research artifact: the gate pass is seed-fragile (significance is sample-size
limited). Running in dry mode to accrue more live days before any capital is deployed.
