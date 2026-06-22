# Task Spec: Polymarket Validation & Research on the Computing Cluster

Before reading all this, connect to the computing cluster with `ssh -i ~/.ssh/bouchet_ch2499 ch2499@bouchet.ycrc.yale.edu`. Click "2" when prompted with Duo push, wait for me to authenticate, then proceed. Use existing local polymarket data. Read through the research folders in project root: it documents the research progress, showed what worked as well as what didn't, and also lessons learned along the way.

**Audience:** the coding agent executing this work on the cluster.
**Scope:** Polymarket only (5-minute up/down markets), seven coins (BTC, ETH, SOL, XRP, HYPE, DOGE, BNB), using collected market snapshots plus Kraken spot. 

---

## 0. Read this first — the one thing that matters

The purpose of this work is **not** to find a winning strategy. It is to find out *whether* an edge exists without fooling ourselves, and to do it faster than we can by hand. We recently discovered that our old contract-level validation overstated edge because correlated contracts inside the same time window leak information; a model that looked profitable (ETH T=265) actually lost ~5% under honest day-level testing. Two leading candidates were subsequently rejected.

A compute cluster is a **multiple-comparisons amplifier**. Used carelessly it produces false winners faster than we can audit them. Therefore every phase below is built to *resist* that. The cluster's job is statistical truth-telling — power, leakage-proof validation, realistic execution, and structural research — **not** bigger models or harder feature mining.

**Hard rules (apply to every phase, no exceptions):**

1. **Unit of analysis is the trading day.** Never split, resample, or bootstrap below the day level. Within-day contracts are correlated and must move together.
2. **Lockbox.** Before any analysis, set aside a held-out set of trading days (see Phase 0). No phase 1–5 may read, train on, tune against, or even peek at lockbox days. It is opened exactly once, at the Final Gate, and only after everything else is frozen.
3. **Trial registry.** Every model, config, feature set, horizon, and hyperparameter combination you *evaluate* — including ones that fail — gets one line in `results/trial_registry.jsonl` with a timestamp, a hash of its definition, and the metric observed. This count feeds the deflation math. An untracked evaluation is a silent bias; treat omission as a bug.
4. **Pre-register before you run.** For each phase, write the hypothesis, the metric, and the pass/fail threshold to `results/<phase>/preregistration.md` *before* executing. Do not edit a threshold after seeing results.
5. **Report negatives plainly.** "No edge detected" is a successful, valuable outcome. Do not hunt for a framing that rescues a dead candidate.
6. **No deep learning.** With ~2000 contracts/coin and ~20 independent days, extra model capacity buys overfitting, not edge. LightGBM with the existing profit-optimizing YES/NO/SKIP objective stays the model. The cluster is spent on simulation and validation, never on replacing it.
7. **Confidence intervals over point estimates.** Any performance number is reported with a day-level (block) bootstrap CI. A bare point estimate is incomplete.
8. **Don't invent the schema.** Inspect the real data and document what you find. If a column's meaning is ambiguous, record the ambiguity in `results/data_audit.md` and flag it — do not guess and proceed.

---

## Phase 0 — Setup, data audit, lockbox, registry

**Do:**
- Inventory the Polymarket data: locate the snapshot CSVs, confirm per-coin contract counts (~2000 expected), and document the schema in `results/data_audit.md` — market id, coin, snapshot timestamp, YES/NO prices (or implied probability), volume/liquidity, official settlement outcome, and the aligned Kraken spot price. Note timezone and how a contract maps to a calendar trading day.
- Empirically characterize the **within-day correlation structure**: how correlated are per-contract outcomes and per-contract PnL inside a single day? With overlapping 5-minute markets there will be many heavily-overlapping contracts per day — quantify this, because Phase 1 depends on it.
- Build the **lockbox**: deterministically reserve the most recent *K* trading days (start with K = 25% of available days, minimum 5) plus optionally a fixed random set. Persist the split as `results/lockbox_split.json` with the seed and a content hash so it cannot drift.
- Initialize `results/trial_registry.jsonl` and a run manifest (`results/env_manifest.json`: package versions, seeds, git commit).

**Why:** Everything downstream is worthless if the lockbox leaks or trials go uncounted. This phase is the foundation that makes the rest honest.

**Insights to surface:** How many independent trading days do we actually have per coin? What is the realistic contracts-per-day distribution and the within-day correlation ρ? These numbers are inputs to Phase 1.

---

## Phase 1 — Statistical power simulation *(run first; it tells us if the roadmap is even sound)*

**Do:**
- Build a generative model of a trading day calibrated to Phase 0: a *true* edge per contract `e` (in profit-per-contract units), the within-day correlation ρ, and the empirical contracts-per-day distribution.
- Monte Carlo over a grid of `(e, N_days)`: for each cell, simulate many independent N-day histories and run the **actual CFES day-level test** (or a faithful reimplementation) on each. Record the detection rate.
- Calibrate Type I: with `e = 0`, confirm the false-positive rate matches the nominal level. Then map Type II: detection power as a function of `e` and `N`.

**Why:** "~20+ days" is currently a guess. We need the real power curve, and we need to know whether our day-level test is so strict that it would reject a *true* edge (a Type II failure). We have protected hard against false positives; we have not yet checked we aren't also killing real winners.

**Insights to surface:** For plausible edges (e.g. 1–3% profit/contract), how many days are actually required? Is 20 enough, optimistic, or wildly optimistic? What is the minimum detectable effect at N = 20? Does CFES have adequate power, or does it need a less brittle estimator?

**Output:** power curves (P(detect | e, N)), the calibrated Type-I check, and a one-paragraph verdict on the day-count target in `results/phase1_power/SUMMARY.md`.

---

## Phase 2 — Overfitting-aware validation: CPCV + PBO + deflated performance

**Do:**
- Implement **Combinatorial Purged Cross-Validation** with day-boundary **purging and embargo**. Because the markets are 5-minute and overlapping, the purge must drop every contract whose information window overlaps a test day, and the embargo must extend a buffer around test days. Generate the many train/test path combinations.
- For each path, train LightGBM with the existing profit/YES/NO/SKIP objective and evaluate on the purged test fold.
- Compute the **Probability of Backtest Overfitting (PBO)**: rank configs in-sample vs out-of-sample across paths; PBO is the frequency with which the in-sample-best config lands below the out-of-sample median.
- Compute a **deflated performance metric** (Deflated Sharpe Ratio or a profit-per-contract analog) using the trial count from the registry and the variance/skew/kurtosis of the candidate distribution.

**Why:** CPCV is built precisely to defeat the correlated-window leakage we already got burned by. PBO measures whether our *selection process* generalizes rather than whether one lucky model did. Deflation debits performance for how many things we tried — the direct antidote to the cluster's amplifier effect.

**Insights to surface:** What is the PBO for the Polymarket pipeline (a high PBO means our selection is overfitting and any single "winner" is suspect)? After deflation, does any coin or horizon retain a real edge, or does it collapse to zero?

---

## Phase 3 — Process-level walk-forward

**Do:**
- Replay history strictly point-in-time, day by day. On each day, run the *entire* selection procedure on data available up to that day, "deploy" the chosen config on the next day, and record realized PnL. No peeking forward, ever.
- Aggregate at the day level; report day-level block-bootstrap CIs, drawdown, and the SKIP rate of the deployed configs.

**Why:** What we will actually run live is the *process* — collect, select, deploy, repeat. Judging a single hand-picked model overstates edge because it ignores the cost of the selection step itself. This measures the thing we will deploy.

**Insights to surface:** Does the process net positive across days *after fees*, with a CI that excludes zero? What does the daily PnL distribution look like, and how deep are drawdowns? Does the process lean SKIP-heavy (declining trades) or actively trade?

---

## Phase 4 — Execution & microstructure simulation (Polymarket-specific)

**Do:**
- Replay snapshots with a realistic fill model for Polymarket: the actual fee/structure, latency from signal to fill, available liquidity at the quoted price, and partial fills. Model entry at each candidate horizon (seconds before expiry).
- Sweep the horizon grid in parallel across coins. **Log every horizon to the trial registry** — horizon choice is part of the search space and must be deflated.

**Why:** Optimistic fills and fee assumptions can manufacture an edge that evaporates in production. The 5-minute expiry and thin late-window liquidity make execution realism especially important here.

**Insights to surface:** Which horizons are robust vs fragile? How much of any apparent edge survives realistic execution and fees? Where does liquidity bind near expiry, and does that quietly cap position size?

---

## Phase 5 — Structure *(additive research — durable edges, not mined features)*

**Do:**
- Test the **lead-lag hypothesis**: does Kraken spot return lead Polymarket's 5-minute implied probability? Use lagged cross-correlation, a Granger-style test, and an information-share decomposition.
- Test whether spot momentum predicts Polymarket settlement *beyond* what is already in the Polymarket price.
- Everything here is subject to the same lockbox and trial-registry discipline as the rest.

**Why:** Structural, economically-motivated edges (one venue genuinely leading another) tend to be more durable than statistically-mined features, which decay under scrutiny. This is the one research direction worth real compute.

**Insights to surface:** Is there exploitable lead-lag at the 5-minute horizon, and from which source? Does it survive fees and execution from Phase 4? Is the signal independent of what we already extract?

---

## Phase 6 — Parallel pipeline infrastructure *(enabling; may be built early as scaffolding)*

**Do:**
- Orchestrate the 7-coin × horizon grid to run in parallel on the cluster, each job emitting a self-contained result artifact and appending to the central trial registry with deterministic seeds.
- This is plumbing for Phases 1–5; build it first if it accelerates them, but it is not itself an analysis.

**Why:** Iterating on the framework instead of babysitting serial jobs is the legitimate raw-throughput win — provided the full search space is logged on every run.

---

## Final Gate (open the lockbox exactly once)

After Phases 1–5 are complete and **frozen** — no further tuning, all thresholds pre-registered — run the single best surviving configuration (if any) against the lockbox days one time. Report the result with its day-level CI in `results/FINAL_GATE.md`. If nothing survived deflation in Phase 2, say so and do not open the lockbox; there is nothing to confirm.

---

## Deliverables

```
cluster/2026-06-21-results/
  data_audit.md
  lockbox_split.json
  env_manifest.json
  trial_registry.jsonl
  phase1_power/      (preregistration.md, power curves, SUMMARY.md)
  phase2_cpcv/       (preregistration.md, PBO, deflated metrics, SUMMARY.md)
  phase3_walkforward/
  phase4_execution/
  phase5_structure/
  phase6_infra/
  FINAL_GATE.md
  RESULTS.md         (one-page synthesis across all phases)
```

`RESULTS.md` should answer, in plain language: how many days we really need, whether any Polymarket edge survives leakage-proof validation and realistic execution, and whether the cross-venue structure is worth pursuing. A clean "no edge yet, keep collecting" is an acceptable and useful conclusion — state it without hedging if that's what the data says.
