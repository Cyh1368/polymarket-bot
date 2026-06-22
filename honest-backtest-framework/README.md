# Honest Backtest Framework

A reusable methodology and code toolkit for **leakage-proof statistical validation of
trading strategies** on short-horizon prediction-market / time-series data. It was built
and battle-tested on Polymarket 5-minute up/down markets across seven coins, but the
primitives are dataset-agnostic.

The framework exists to answer one question honestly:

> **Does a real, deployable edge exist — or are we fooling ourselves?**

It is deliberately biased toward saying *"no edge"*. On small samples with correlated
observations and a search process, the default failure mode is a false positive. Every
component below is designed to *resist* that, not to find winners.

---

## Why this exists — two real failures it caught

1. **Contract-level validation overstated edge.** Treating each market contract as an
   independent observation leaked information: overlapping 5-minute markets within the
   same time window are correlated. A model that looked profitable (ETH T=265) lost ~5%
   under honest day-level testing. → **Unit of analysis must be the trading day.**

2. **A maker-fill look-ahead leak fabricated a +0.23/contract "edge" on XRP.** The
   backtest decided whether a limit order *filled* by scanning the **post-entry price
   path** — which is correlated with the eventual settlement. Filtering trades on
   future-correlated fills kept only winners. When the leak was removed, the edge
   collapsed to ≈0. → see [`leak-detection/`](leak-detection/).

Both are subtle, both passed a naive backtest, both were false. This framework is the
codification of how to not get fooled again.

---

## The eight hard rules

These apply to every analysis. They are non-negotiable; omitting one silently
reintroduces bias.

1. **Unit of analysis is the trading day.** Never split, resample, or bootstrap below the
   day level. Within-day observations are correlated and must move together.
2. **Lockbox.** Reserve the most-recent K days (≥25%, min 5) *before any analysis*. No
   training, tuning, or peeking. Opened **exactly once**, at the Final Gate, after
   everything is frozen. Content-hashed so the split cannot drift.
3. **Trial registry.** Every config/feature/horizon/hyperparameter you *evaluate* —
   including failures — is one append-only line with a timestamp, a definition hash, and
   the metric seen. This count feeds the deflation math. An untracked evaluation is a
   silent bias.
4. **Pre-register before you run.** Write hypothesis, metric, and pass/fail thresholds to
   a `preregistration.md` *before* executing a phase. Never edit a threshold after seeing
   results.
5. **Report negatives plainly.** "No edge detected" is a successful, valuable outcome.
   Do not hunt for a framing that rescues a dead candidate.
6. **Cap model capacity.** With ~2000 contracts/coin and ~20 independent days, extra
   capacity buys overfitting, not edge. No deep learning. The model is fixed; compute is
   spent on simulation and validation, not on bigger models or harder feature mining.
7. **Confidence intervals over point estimates.** Every performance number is reported
   with a day-level (block) bootstrap CI. A bare point estimate is incomplete. With few
   day-clusters, use a wild-cluster bootstrap — ordinary cluster-robust SEs are
   anti-conservative.
8. **Don't invent the schema.** Inspect the real data, document it, and flag ambiguous
   columns rather than guessing.

---

## The pipeline (phase by phase)

| Phase | Question | Key artifact |
|---|---|---|
| **0 — Setup** | How many independent days do we have? What is the within-day correlation ρ? | `data_audit.md`, `lockbox_split.json`, `trial_registry.jsonl`, `env_manifest.json` |
| **1 — Power** | Is the roadmap even sound? Can the test detect a true edge at N days, and is Type-I calibrated? | power curves, minimum detectable effect |
| **2 — Overfitting-aware validation** | Does selecting the in-sample-best config generalize? | **CPCV → PBO → Deflated Sharpe** (`cpcv.py`) |
| **3 — Process walk-forward** | What does the frozen policy actually earn OOS, day by day? | purged daily walk-forward + day-block bootstrap (`cfes.py`) |
| **4 — Execution** | Does the edge survive realistic cost / fills? | cost sensitivity, fill rate |
| **5 — Structure** | Is there any incremental predictability beyond the mid? | OOS Δlog-loss |
| **Final Gate** | Open the lockbox **once** to confirm a single already-surviving config. | `FINAL_GATE.md` |

If nothing survives Phase 2, **the lockbox is never opened** — burning the one honest
future-holdout on an already-failed candidate is the mistake the protocol exists to
prevent. (In the reference run, nothing survived: PBO=0.67, DSR=0.022, OOS EV ≈ 0. The
correct, pre-registered outcome was a sealed negative.)

---

## What's in this folder

```
honest-backtest-framework/
├── README.md                       ← this file
├── REPLICATION.md                  ← step-by-step guide to apply it to a new dataset
├── pmcluster/                      ← the reusable Python package
│   ├── config.py                   ← single source of truth: paths, costs, horizons, gate thresholds
│   ├── model.py                    ← the frozen edge model + YES/NO/SKIP decision rules
│   ├── features.py / features_hf.py← point-in-time feature extraction (no look-ahead)
│   ├── cfes.py                     ← day-level walk-forward + day-block bootstrap + the Gate
│   ├── cpcv.py                     ← CSCV/PBO + Deflated Sharpe (overfitting math)
│   └── registry.py                 ← trial registry, lockbox, env manifest
├── leak-detection/                 ← the maker-fill leak: worked example + before/after
│   ├── maker_fill_LEAKY_example.py ← the original (future-path fill check — DO NOT COPY)
│   ├── maker_fill_FIXED_example.py ← leak-free version (unconditional fill at mid)
│   ├── results_LEAKY.csv           ← XRP maker +0.229
│   └── results_FIXED.csv           ← XRP maker −0.001  (edge was the leak)
└── specs/                          ← the original task specs / pre-registration prompts
    ├── polymarket_cluster_analysis_prompt.md
    └── profit_backtest_spec.md
```

The reference results, pre-registrations, and per-phase summaries from the validated run
live in the repo at `cluster/2026-06-21-results/` and the package source at
`cluster/bouchet/pmcluster/`.

---

## The leak-detection discipline (read this before any maker/fill backtest)

A backtest that decides **whether** a trade happened using information unavailable at
decision time will manufacture edge. The maker-fill trap is the canonical case:

- **Wrong:** "the order filled because the price later reached my limit." The post-entry
  price path is correlated with settlement → you keep only the trades that won.
- **Right:** decide fills using **only entry-time book state** (was the limit marketable
  at entry?), or assume an **unconditional** fill at the limit and pay the maker fee.
  Report the realized fill rate — an edge needing a 90% fill rate it never achieves is
  not an edge.

**The tell:** a maker edge much larger than the taker edge on the *same* trades is a leak
until proven otherwise. On XRP the gap was +0.229 vs −0.013. Confirm by checking whether
the fill flag predicts the outcome — it should not. (It did: Up-won = 0.26 when the order
"filled" vs 0.997 when it didn't — a near-perfect oracle.) See
[`leak-detection/`](leak-detection/) for the full worked example.

More generally, audit every gate in a backtest with: *"could I have known this at the
moment I had to decide?"* Purge + embargo around test days, point-in-time features, and
day-level bootstrapping all enforce the same principle at different layers.
