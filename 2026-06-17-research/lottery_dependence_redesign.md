# Reducing Lottery Dependence: Loss Function / Objective / Dataset Redesign

**Date:** 2026-06-17
**Context:** Polymarket BTC 5m settlement model (LightGBM v3.1, `lgb_v3p1_t180.txt`, T1=180s)
**Problem:** The model's backtest profitability depends on a handful of rare, large-payoff
("lottery") trades. The 200-seed CV showed ~74–90% positive seeds, but that positivity is
driven by a few trades, not a broad, repeatable edge. Goal: redesign so the model
**accumulates many small wins** instead of relying on longshot payouts.

---

## 1. Diagnosis (quantified from the v3.1 CV ledger)

Source: `2026-06-15-research/settlement_lgb_v3p1_results/cv_v3p1_records.csv`
(999 traded contracts, +5.45% EV/available — the result that looked good).

| Statistic | Value | Reading |
|---|---|---|
| Total PnL (return-on-stake units) | +78.38 | looks profitable |
| Mean EV/triggered | +0.0785 | looks profitable |
| **Top 10 trades (1% of 999)** | **= 105% of total PnL** | remove them → strategy is negative |
| **Win-capped mean** (`min(win,+1)`) | **−0.105** | loses money without big payoffs |
| **10% trimmed mean** | **−0.121** | loses once tails are removed |
| Median PnL/trade | +0.190 | >half of trades win small… |
| Per-trade Sharpe (mean/std) | 0.058 | …but consistency is ~noise |
| Win rate | 55.5% | mediocre |

**Why the seed CV lied.** The 200 seeds only reshuffled LightGBM's *training* randomness,
not the trade outcomes. Every seed kept the same ~10 lottery winners in the OOS set, so
every seed looked positive. Two mistakes compounded:

1. Wrong **resampling unit** — seeds, not trades/days.
2. Wrong **statistic** — the *mean* of a `1/p − 1` payoff is dominated by low-price tail
   wins by construction, so even an honest bootstrap of the mean understates fragility.

---

## 2. Where the consistent edge actually lives (by fill price)

Same ledger, sliced by the price actually paid. The `capped` column is the **lottery-free
EV** (every win counted as flat +1):

| Fill band | N | WR | mean | **capped** | trim10 | Sharpe |
|---|---|---|---|---|---|---|
| [0.00, 0.20) | 81 | 17% | +0.444 | **−0.654** | −0.398 | pure lottery |
| [0.20, 0.30) | 90 | 22% | −0.127 | **−0.556** | −0.450 | negative |
| [0.30, 0.50) | 245 | 44% | +0.100 | **−0.127** | +0.001 | marginal |
| [0.50, 0.70) | 326 | 61% | +0.040 | **+0.040** | +0.055 | +0.047 |
| **[0.70, 0.85)** | 205 | **82%** | +0.085 | **+0.085** | **+0.175** | **+0.168** |
| [0.85, 1.00) | 52 | 87% | −0.020 | −0.020 | +0.075 | thin |

**Broad band [0.40, 0.80):** N=588, WR 65%, capped +0.065, trim10 +0.081, Sharpe 0.109 —
positive *without any lottery payoffs*. That is the "accumulate small wins" profile.
The lottery dependence is entirely from `fill < 0.30`.

---

## 3. Three orthogonal levers

### Lever 1 — Fix the selection metric (do first; free; it is the gate)

Stop selecting on raw mean EV. Gate on metrics insensitive to payoff *size*:

| Metric | Measures | Caught the false positive? |
|---|---|---|
| Win-capped EV | profit from win *frequency* | **Yes** (−0.105) |
| Trimmed mean (10%) | central tendency w/o tails | **Yes** (−0.121) |
| Per-trade Sharpe | consistency | Yes (0.058 fails) |
| Day-block bootstrap | honest CI under clustering | Partially |

**Deployment gate (conjunction — see §4.5 on why true EV stays in):**

- True mean EV > 0 **AND** its day-block bootstrap lower-CI > 0 → it actually makes money
  (big wins included), **AND**
- Win-capped EV > 0 **AND** 10% trimmed mean > 0 → the edge is not *only* lotteries.

Resample **whole days/sessions**, not seeds. A strategy passing both halves makes money
from frequent wins *and* pockets the occasional jackpot as a bonus — without surviving on
jackpots alone.

### Lever 2 — Restrict the dataset to the consistency zone (highest leverage, simplest)

Add a hard cost-band filter to the decision rule: only trade when `0.40 ≤ cost ≤ 0.80`
(tunable; [0.50, 0.85) also defensible). One config line; removes the dependence at the
source. Cost: you forgo real EV from longshots — correct trade-off *if* the objective is
consistency / low drawdown rather than raw EV.

### Lever 3 — Change what the model optimizes (deeper; after 1 & 2)

Current pipeline: `binary_logloss` (pure calibration, zero payoff awareness) + a bolted-on
EV rule. The upgrade:

- **Robust edge regression.** Predict per-side net edge with **Huber/quantile** loss
  instead of P(UP) + EV rule. Huber caps the gradient of 19× outcomes so tail wins can't
  dominate the *fit* (but the trained model still *takes* a genuine high-EV cheap bet — see
  §4.5). One-line objective swap + target change.

Avoid a naive custom-PnL objective as a first move — pure EV-maximization makes
lottery-chasing *worse* unless the variance penalty is baked in. Continuous fractional-Kelly
sizing is dropped: under the $1 min-bet, sizing collapses to trade/skip (§4.5), so the
price-band filter is the sizing lever.

---

## 4. Recommended sequence

1. **Now:** Re-score every past ledger with win-capped EV + trimmed mean + day-block
   bootstrap, **reported alongside true EV**; re-gate on the §3 conjunction.
2. **Next:** Add the `[0.40, 0.80)` cost-band filter; re-run CV under the new metrics to
   confirm a consistent edge survives.
3. **Then:** Swap training target to Huber-regressed edge; compare to the band-filtered
   baseline. (Continuous Kelly sizing is moot under min-bet — see §4.5.)

---

## 4.5 Refinement for live constant-value (fixed-dollar) staking

Live trading uses a **fixed dollar stake per trade** (~$1, set by Polymarket's $1 min-order
rule), not fixed contracts. At fill price `p` you buy `S/p` contracts; a win pays `S/p`, so
per-dollar return is exactly `r = 1/p − 1` (win) / `−1` (loss). **This is the same
return-on-stake unit the backtest already uses**, so the economics are correctly modelled —
and a cheap win really does pay big dollars. That money is real; we must not define it away.

Three consequences:

1. **Win-capped EV / trimmed mean are diagnostics, not accounting.** Keep **true EV** as the
   money metric; run the lottery-free metrics *alongside* it as the gate (§3 conjunction).
   Reject a strategy only if it fails the lottery-free half — i.e. it has no broad edge and
   survives purely on jackpots.

2. **Huber bounds the *fit's sensitivity* to jackpots, not the *trade's payoff*.** The
   trained model still takes a high-EV cheap bet when features genuinely justify it; it is
   simply not *trained to hope* for a single +19 outlier. So Lever 3 does not forfeit real
   big-win dollars — it removes the incentive to overfit toward them.

3. **Under a $1 min-bet, sizing collapses to trade/skip.** Continuous fractional-Kelly
   cannot size a longshot below the $1 floor, so the Kelly-consistent action for a longshot
   is to **skip it**. Therefore the **price-band filter (Lever 2) *is* the sizing
   mechanism** here; continuous-Kelly (old Lever 3a) is dropped as impractical. Log-growth
   remains useful only as an *evaluation* lens, not a sizing rule.

---

## 5. Metric definitions

### 5.1 Win-capped EV

Per-trade return on a $1 stake at fill price `p`:

```
r_i = (1 / p_i) - 1   if the trade wins      (e.g. p=0.05 → +19)
r_i = -1              if the trade loses
```

Raw EV = mean(r_i). **Win-capped EV** caps every positive return at +1 before averaging:

```
r_i^cap = min(r_i, +1)   for wins
r_i^cap = -1             for losses
EV_capped = mean(r_i^cap)
```

This makes every win worth the same (+1) regardless of price, so the metric reflects
**how often you win**, not **how big the rare wins are**. If `EV_capped < 0`, the strategy
only makes money from payoff *size* on rare events → lottery-dependent.

### 5.2 Trimmed mean

Sort the per-trade PnL, drop the top and bottom α fraction, average the rest:

```
trim_mean(x, α) = mean( x_(k+1), ..., x_(n-k) )   where k = floor(α·n)
```

At α=0.10, the largest 10% and smallest 10% of trades are discarded. If the trimmed mean
is negative while the raw mean is positive, the profit lives entirely in the discarded
tails (the lottery wins).

### 5.3 Day-block bootstrap

Standard bootstrap resamples individual trades with replacement — but trades on the same
day are correlated (a trending day makes many outcomes go the same direction), so per-trade
resampling understates the true variance. **Day-block bootstrap** resamples whole days:

```
1. Group trades by calendar day (or session): D_1, D_2, ..., D_m
2. Repeat B times:
     - sample m days with replacement: D*_1, ..., D*_m
     - concatenate their trades; compute the metric (e.g. mean PnL)
3. The 2.5th / 97.5th percentiles of the B values = 95% CI
```

This preserves within-day correlation, so the CI is honest about clustered risk. A
strategy whose edge is a few good days will show a lower-CI well below zero here even if
the naive per-trade CI looks fine.

---

## 6. Training objective: before → after (equations)

Notation: features `x_i`, settlement label `y_i ∈ {0,1}` (1 = UP). Model output `f(x_i)`.

### 6.1 BEFORE — binary log-loss (calibration only)

The model predicts a probability `p̂_i = σ(f(x_i))`, `σ(z) = 1/(1+e^{-z})`, and is trained
to minimize binary cross-entropy:

```
L_before = - (1/N) Σ_i [ y_i · log(p̂_i) + (1 - y_i) · log(1 - p̂_i) ]
```

LightGBM gradient/hessian per sample:

```
g_i = p̂_i - y_i
h_i = p̂_i · (1 - p̂_i)
```

**Key property:** this loss is a function of `(p̂_i, y_i)` only. It contains **no price and
no payoff term**. Every contract contributes equally regardless of whether a win pays 1.05×
or 20×. The trading EV rule (`p̂/cost − 1 > skip_bonus`) is bolted on *after* training, so
the model is never told that a calibrated probability on a 0.05-priced longshot leads to a
huge-variance bet. Nothing in `L_before` discourages lottery exposure.

### 6.2 AFTER — robust edge regression (Huber on per-side payoff)

Instead of predicting P(UP), predict the **net edge of taking a side** and fit it with a
loss whose gradient is *bounded*, so a handful of 19× outcomes cannot dominate.

Define the realized per-trade return target (return on $1 stake), e.g. for the NO side at
price `c_i^{no}`:

```
t_i = (1 - y_i) / c_i^{no} - 1     # +big if DOWN wins cheap, -1 if it loses
```

(Symmetrically `t_i = y_i / c_i^{yes} - 1` for the YES side; train one model per side or
stack both as samples.) The model regresses `f(x_i) ≈ t_i` under **Huber loss** with
threshold `δ`:

```
residual:  e_i = f(x_i) - t_i

L_after = (1/N) Σ_i  ℓ_δ(e_i)

           ⎧  ½ · e_i²                     if |e_i| ≤ δ        (quadratic core)
ℓ_δ(e_i) = ⎨
           ⎩  δ · (|e_i| - ½·δ)            if |e_i| >  δ        (linear tail)
```

Huber gradient/hessian (what LightGBM actually uses):

```
g_i = clip(e_i, -δ, +δ)              # gradient is CAPPED at ±δ
h_i = 1   if |e_i| ≤ δ   else  ~0    # tail samples stop pulling the fit
```

**Key property:** because `|g_i| ≤ δ`, a single contract whose true return is +19 cannot
exert 19× the pull of a normal contract — its influence saturates at `δ`. The trees
therefore split to explain the **many mid-price trades** (where residuals are small and the
quadratic core dominates) instead of contorting to fit a few longshot jackpots. Contrast
with `L_before`, where a mispredicted longshot is invisible to the loss anyway, and with a
naive squared-error/EV objective, where it would dominate (gradient `∝ e_i`, unbounded).

### 6.3 (Evaluation lens only — not a sizing rule under min-bet; see §4.5) Log-utility

> Under the $1 min-bet, you cannot continuously size positions, so the formulas below are
> used to *evaluate* a ledger (log-growth rate), not to set live stake. Sizing is trade/skip
> and the price-band filter is the lever.

Even with a robust model, *how much you bet* controls lottery exposure. Current implicit
objective maximizes linear expected return:

```
BEFORE:  maximize  E[r]            # indifferent to variance; longshots look great
```

Log-utility (Kelly) instead maximizes expected log-growth, sizing bet fraction `f`:

```
AFTER:   maximize  E[ log(1 + f · r) ]

         optimal fraction (binary payoff, win prob p̂, net win odds b = 1/c - 1):
         f* = (p̂ · b - (1 - p̂)) / b           # fraction of bankroll
         deploy  f_used = κ · f*               # κ = ¼ … ½  (fractional Kelly)
```

Because `log(1 + f·19) ≈ 3` while `log(1 + f·1) ≈ 0.7`, the marginal value of a 19× win is
heavily discounted, and any `f` that risks ruin is rejected (`log → -∞` as `1 + f·r → 0`).
Longshots (`p̂` small) get `f* ≈ 0`; steady-edge favorites get sized up. Evaluate the
strategy on realized **log-growth rate** `(1/N) Σ log(1 + f_used·r_i)` rather than mean EV.

---

*Generated 2026-06-17. Empirical figures from
`2026-06-15-research/settlement_lgb_v3p1_results/cv_v3p1_records.csv`.*

 ---
  The three metrics

  Win-capped EV

  Per-trade return on a $1 stake at fill price p:

  r_i = (1/p_i) − 1   if win    (p=0.05 → +19 ;  p=0.75 → +0.33)
  r_i = −1            if loss

  Raw EV averages these — so one win at p=0.05 (+19) outweighs ~19 losses. Win-capped EV caps every win at +1 first:

  r_i^cap = min(r_i, +1)   for wins
          = −1             for losses
  EV_capped = mean(r_i^cap)

  Now every win counts the same regardless of price, so the metric measures how often you win, not how big the rare wins are. Your ledger: raw mean
  +0.078 but EV_capped = −0.105 → it only profits from payoff size on rare events. That one sign flip is the entire diagnosis.

  Trimmed mean

  Sort all per-trade PnL, drop the top and bottom α, average the middle:

  trim_mean(x, α) = mean( x_(k+1), …, x_(n−k) ),   k = ⌊α·n⌋

  At α=0.10 the largest and smallest 10% of trades are discarded. If trimmed mean is negative while raw mean is positive, the profit lives in the
  discarded tails. Your ledger: trim10 = −0.121.

  Day-block bootstrap

  A plain bootstrap resamples individual trades — but trades on the same day are correlated (a trending day pushes many outcomes the same way), so
  per-trade resampling understates variance. Day-block resamples whole days:

  1. Group trades by day:  D_1 … D_m
  2. Repeat B times:
       sample m days WITH replacement
       concatenate their trades → compute the metric
  3. 2.5 / 97.5 percentiles = 95% CI

  This preserves within-day correlation, so the CI honestly reflects "my edge is really just a few good days." It's the right unit because, as we
  saw, June 16 vs June 17 were opposite regimes — the risk is clustered by day, not spread evenly across trades.

  ---
  Training objective: before → after

  BEFORE — binary log-loss (calibration only)

  Predict p̂_i = σ(f(x_i)), minimize cross-entropy:

  $$
  L_{\text{before}} = -\frac{1}{N}\sum_i \big[, y_i \log \hat p_i + (1-y_i)\log(1-\hat p_i),\big]
  $$

  LightGBM gradient/hessian:

  $$
  g_i = \hat p_i - y_i, \qquad h_i = \hat p_i(1-\hat p_i)
  $$

  The problem: this depends only on (p̂_i, y_i). There is no price and no payoff term anywhere. A contract whose win pays 1.05× and one that pays 20×
  contribute identically. The EV rule (p̂/cost − 1 > skip_bonus) is applied after training, so the model is never told that a calibrated probability
  on a 0.05-priced longshot becomes a giant-variance bet. Nothing in the loss discourages lottery exposure.

  AFTER — robust edge regression (Huber)

  Regress the realized per-side return target directly (NO side shown):

  $$
  t_i = \frac{1-y_i}{c_i^{\text{no}}} - 1
  $$

  Fit f(x_i) ≈ t_i with Huber loss, residual e_i = f(x_i) − t_i:

  $$
  \ell_\delta(e_i) =
  \begin{cases}
  \tfrac12 e_i^2 & |e_i| \le \delta \quad\text{(quadratic core)}\[4pt]
  \delta\big(|e_i| - \tfrac12\delta\big) & |e_i| > \delta \quad\text{(linear tail)}
  \end{cases}
  $$

  Gradient/hessian LightGBM uses:

  $$
  g_i = \operatorname{clip}(e_i,,-\delta,,+\delta), \qquad
  h_i = \begin{cases}1 & |e_i|\le\delta\ \approx 0 & |e_i|>\delta\end{cases}
  $$

  The fix: because |g_i| ≤ δ, a contract whose true return is +19 cannot pull the fit 19× harder than a normal one — its influence saturates at δ.
  The trees split to explain the many mid-price trades (small residuals, quadratic core) instead of contorting to fit rare jackpots. Compare the
  three regimes:

  ┌───────────────────┬───────────────────────────┬────────────────────────────────────┐
  │     Objective     │ Gradient on a +19 outcome │               Effect               │
  ├───────────────────┼───────────────────────────┼────────────────────────────────────┤
  │ log-loss (before) │ p̂ − y, payoff-blind       │ longshot invisible to loss         │
  ├───────────────────┼───────────────────────────┼────────────────────────────────────┤
  │ naive squared/EV  │ ∝ e_i, unbounded          │ longshot dominates the fit (worse) │
  ├───────────────────┼───────────────────────────┼────────────────────────────────────┤
  │ Huber (after)     │ capped at ±δ              │ longshot bounded → fits the bulk   │
  └───────────────────┴───────────────────────────┴────────────────────────────────────┘

  Optional — sizing: linear → log-utility (Kelly)

  BEFORE:  maximize E[r]                  # variance-blind; longshots look great
  AFTER:   maximize E[log(1 + f·r)]       # log-growth; discounts jackpots, rejects ruin

  $$
  f^* = \frac{\hat p, b - (1-\hat p)}{b}, \quad b = \tfrac{1}{c}-1, \qquad f_{\text{used}} = \kappa f^*\ (\kappa=\tfrac14!-!\tfrac12)
  $$

  Since log(1+19f) ≈ 3 vs log(1+f) ≈ 0.7, a 19× win is heavily discounted, and any f risking ruin is rejected (log → −∞). Longshots get f* ≈ 0;
  steady favorites get sized up.
---

## 7. Step 1 results — robust re-scoring of existing ledgers

Script: `2026-06-17-research/robust_metrics.py`
(`--boot 5000`; summary → `robust_metrics_summary.csv`). Day-block bootstrap; metrics in
return-on-$1-stake units (matches live constant-value staking, §4.5).

| Ledger | N | days | true mean EV | true 95% CI (day-block) | win-capped | trim10 | Sharpe | GATE |
|---|---|---|---|---|---|---|---|---|
| v3.1 CV | 999 | 6 | +0.078 | [+0.005, +0.148] | **−0.105** | **−0.121** | 0.058 | ❌ |
| v3 CV | 1013 | 6 | +0.040 | [−0.013, +0.119] | **−0.107** | **−0.131** | 0.032 | ❌ |
| regime CV | 811 | 6 | +0.044 | [−0.048, +0.131] | **−0.112** | **−0.133** | 0.033 | ❌ |
| live dry-run | 66 | 2 | −0.201 | [−0.331, −0.180] | −0.269 | −0.317 | −0.209 | ❌ |

**All four fail the gate.** Reading:

- **v3.1 CV is the textbook false positive:** it *passes* the true-money half (CI lower
  bound +0.005) but the win-capped EV is −0.105 → the entire edge is lottery payoffs.
- **v3 and regime don't even pass the true half** once you bootstrap by **day** instead of
  by seed: true CI includes 0 (P(EV≤0) = 9.7% and 17.2%). Tail concentration is extreme —
  the regime model's single best trade is 44% of all PnL; removing the top 4 trades makes it
  negative.
- **Caveat:** only 6 day-blocks (2 for live), so the CI is coarse. The gate will be far more
  trustworthy with more trading days. This is a reason to keep collecting, not to trust the
  current ✅/❌ as final.

**The one consistent signal** — reproduced across v3.1, v3, *and* the 66-trade live set —
is the **[0.70, 0.85) favorites band**: positive win-capped EV, positive trim10, and the
best Sharpe in every ledger (live: win-capped +0.065, trim10 +0.112, Sharpe +0.132 on
N=18). This is direct empirical support for Lever 2 (trade favorites, skip longshots) and
confirms that the redesign target — a broad, lottery-free edge — does exist in the data,
just not where the raw-EV optimizer was pointing.

**Conclusion for next step:** apply the `[0.40, 0.80)`–`[0.50, 0.85)` cost-band filter and
re-run the CV through `robust_metrics.py`; the band-restricted strategy is the first
candidate expected to pass the gate.

---

## 8. Should you train only on non-lottery data (0.25 < p_yes < 0.75)?

**Verdict: the instinct is right, the specific cut is wrong.** It is on the wrong axis and
removes your single best edge. Prefer the Huber objective (§7 step 2/3 report: it neutralizes
lottery dependence without discarding data). If you still want a hard filter, filter the
*traded-side cost* at decision time, or exclude only the *extreme tails*.

### 8.1 The cut would delete the favorites — your most consistent edge

The lottery lives in the **extreme tails** (p_yes < ~0.12 or > ~0.88), where a win pays
10–19×. Your **most consistent, lottery-free edge** lives in the **favorites band**: fill
price [0.70, 0.85), which corresponds to:

```
YES favorites:  p_yes_mid ∈ [0.70, 0.85]
NO  favorites:  p_yes_mid ∈ [0.15, 0.30]   (down_ask = 1 − p_yes ∈ [0.70, 0.85])
```

A `0.25 < p_yes < 0.75` training cut **removes p_yes ∈ [0.75, 0.85] and [0.15, 0.25]** —
i.e. it deletes the YES favorites and most of the NO favorites. Across every CV variant the
[0.70, 0.85) band is the *only* universally positive zone on win-capped EV / trim10 /
Sharpe. Cutting at 0.25/0.75 throws out the good favorites to remove the longshots — wrong
trade-off.

### 8.2 Right axis: cost, not mid

`p_yes_mid` is symmetric around 0.5; the lottery problem is about **cheap bets on the side
you take**, not about mid-market level. A NO bet at p_yes_mid = 0.70 costs only ~0.30 — a
cheapish bet that a `p_yes` filter centered on 0.5 treats as "safe." Filter the **traded-side
ask** (`up_ask` for YES, `down_ask` for NO), which is what Lever 2 / the band variant does.

### 8.3 Training-filter vs decision-filter

- **Decision-only filter** (train on all, trade in-band): keeps full calibration signal,
  just refuses lottery *bets*. Lower risk; this is the `band` / `huber_band` variant.
- **Training-data filter** (the proposed idea): also removes those rows from the gradient.
  Costs ~38% of data (62.4% retained), and makes the model's probabilities valid *only* in
  the trained regime — fine if you never trade outside it, but it forfeits the option to
  take a genuinely +EV favorite at 0.78, which the data says is your best zone.

### 8.4 If you insist on a training filter

Exclude only the extreme tails, not the interior:

```
keep if  0.12 ≤ p_yes_mid ≤ 0.88        # drops lottery longshots, KEEPS favorites
```

But the Huber objective already does this softly and adaptively (it down-weights, rather
than deletes, the high-variance tail) and empirically beats the baseline on true EV, win
rate, and lottery-freeness simultaneously. Reach for the hard training filter only if a
Huber model still shows tail dependence after more data.
