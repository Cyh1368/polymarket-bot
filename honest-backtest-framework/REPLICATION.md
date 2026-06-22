# Replication Guide — applying the framework to a new dataset

This guide explains how to port the `pmcluster` package and the validation pipeline to a
new dataset (different market, asset class, or horizon). The code is written so that
swapping the data adapter and the config is usually all that's required — the validation
math stays fixed.

## Prerequisites

```bash
# the project venv has lightgbm, pandas, scipy, statsmodels, sklearn
kalshi/.venv-cli-trader/bin/python -c "import lightgbm, scipy, statsmodels, sklearn; print('ok')"
```

`pmcluster` resolves all paths from the `PM_ROOT` environment variable (default
`~/project/pm`), so the same code runs on a cluster and locally:

```bash
export PM_ROOT=/path/to/your/dataset_root
# expected layout:
#   $PM_ROOT/data/data_<COIN>_5m/*.csv        raw snapshots
#   $PM_ROOT/outcomes/<...>_official_outcomes.csv
#   $PM_ROOT/features/<COIN>.parquet          built by features.py
#   $PM_ROOT/cluster/<run>-results/           registry, lockbox, outputs
```

## Step 1 — Adapt the data layer, not the math

The only files that encode dataset specifics:

- **`config.py`** — paths, `COINS`, cost model (`COST_ADD`), `HORIZONS`, the feature
  list, the frozen `MODEL_CFG`, and the `Gate` thresholds. Edit these. **Do not** edit
  the gate thresholds after seeing results (rule 4).
- **`features.py` / `features_hf.py`** — parse your raw snapshots into one feature row
  per (contract, horizon). The contract is the critical invariant: every feature must be
  computable from **pre-entry data only**. If you add a feature, prove it is
  point-in-time.

`model.py`, `cfes.py`, `cpcv.py`, and `registry.py` are dataset-agnostic and should not
need changes. They assume only that each feature row has: `date`, `horizon`, the columns
in `config.FEATURES`, `y_settle` (0/1 outcome), and `c_yes`/`c_no` (entry costs).

## Step 2 — Phase 0: audit, lockbox, registry

```python
from pmcluster import registry, config as C
# inventory days per coin -> all_days_by_coin
split = registry.build_lockbox(all_days_by_coin, frac=0.25, min_days=5)
registry.write_env_manifest()
```

Before anything else, **characterize within-day correlation ρ**. If ρ is high (overlapping
markets), contract-level inference is invalid and the day-level framework is mandatory.
Document the schema and any ambiguity in `data_audit.md`.

## Step 3 — Phase 1: power simulation

Calibrate a generative day model to Phase 0 (true edge `e`, correlation ρ, contracts/day
distribution). Monte-Carlo the *actual* day-level test over a grid of `(e, N_days)`:
confirm Type-I at `e=0`, then map detection power. This tells you whether your day count
can even detect a plausible edge before you waste effort. (Reference run found 12 days was
wildly insufficient.)

## Step 4 — Phase 2: overfitting-aware validation

```python
from pmcluster import cpcv
# 1. point-in-time per-config daily-EV matrix (purged walk-forward, no retrain inside CSCV)
M_df, N_df = cpcv.build_config_day_matrix(frames, C.HORIZONS, allowed_days=non_lockbox_days)
# 2. does picking the IS-best config generalize?
pbo = cpcv.cscv_pbo(M_df)              # want PBO < 0.5
# 3. deflate the selected config's Sharpe by the number of trials tried
dsr = cpcv.deflated_sharpe(selected_returns, n_trials=registry.count_trials())
```

**Promotion bar:** positive deflated OOS edge **and** PBO < 0.5. Log every config to the
registry — the trial count feeds `deflated_sharpe`.

## Step 5 — Phase 3: process walk-forward + the Gate

```python
from pmcluster import cfes, config as C
ledger = cfes.daily_walkforward(coin_horizon_df, mode="ensemble")  # purged, point-in-time
result = cfes.metrics(ledger, gate=C.Gate())
print(result["pass"], result["checks"])
```

`metrics` computes everything at the **day** level: mean daily EV, day Sharpe, t-stat,
positive-day fraction, CVaR-20, **day-block bootstrap CI**, and top-day concentration.
`Gate` is the shadow-test bar (≥20 OOS days, CI lower bound > 0, etc.).

## Step 6 — Phase 4–5: execution & structure

- Re-price under realistic cost/fills. **Apply the leak-detection discipline below.**
- Check incremental predictability beyond the mid (OOS Δlog-loss). In-sample
  significance is necessary, not sufficient — it must clear cost OOS.

## Step 7 — Final Gate

Only if a single config survived Phase 2–3: open the lockbox **once** and confirm. Report
the lockbox CI. If nothing survived, **do not open the lockbox** — report the sealed
negative.

---

## Leak-detection checklist (run on every backtest)

Adapted from the maker-fill leak in [`leak-detection/`](leak-detection/):

- [ ] **Decision-time test.** For every gate (did the trade happen? did the order fill?
      which side?), ask: *could I have known this at the moment I had to decide?* If it
      uses any post-entry data, it is a leak.
- [ ] **Fills use entry-time state only** — marketable-at-entry, or unconditional fill at
      the limit with the fee paid. Never "the price later reached my limit." Report the
      realized fill rate.
- [ ] **Maker ≈ taker sanity.** A maker edge much larger than the taker edge on the same
      trades is a leak until proven otherwise.
- [ ] **Fill-flag independence.** The fill/trade flag must not predict the outcome.
      Test it: `df.groupby(fill_flag)[outcome].mean()` should be flat. (XRP: 0.26 vs
      0.997 — a near-perfect oracle = leak.)
- [ ] **Purge + embargo** train days around each test day (`PURGE_DAYS`).
- [ ] **Point-in-time features** — every feature computable from pre-entry data only.
- [ ] **Day-level bootstrap** — never resample below the day; contracts within a day are
      correlated.
- [ ] **Lockbox sealed** until the Final Gate; **every trial logged** to the registry.

## Worked example

`leak-detection/maker_fill_LEAKY_example.py` vs `maker_fill_FIXED_example.py` differ in
one place: the leaky version sets `maker_*_fills` by scanning post-entry snapshots; the
fixed version assumes an unconditional fill at the entry mid. Compare `results_LEAKY.csv`
(XRP maker **+0.229**) with `results_FIXED.csv` (XRP maker **−0.001**). The entire edge
was the leak.
