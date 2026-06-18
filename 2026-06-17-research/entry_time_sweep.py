#!/usr/bin/env python3
"""
2026-06-17-research/entry_time_sweep.py

Entry-time (T1) sweep for the production Huber edge-regression model.

ARCHITECTURE IS FROZEN. We keep exactly the model selected by sweeps 1-4
(`train_huber_save.py` BEST_CFG = BASE_CFG + min_child_samples=150), the same 14
features, the same decision rule (`hc.decide_huber`), the same expanding-window CV
(5 folds, MIN_TRAIN=200), and the same robust-metric gate. The ONLY thing that varies
is T1 — how many seconds before the 5m market closes we snapshot the book and trade.

Goal:
  (1) How does the model behave near the current T1=180s? (local sensitivity)
  (2) Is there another entry time with a stronger / more robust signal?

`extract_row` reads the module-global `huber_common.T1`, so we load the contracts once
and rebuild the feature frame at each candidate T1 (cheap — no disk re-read).

Outputs → entry_time_sweep_results/
  entry_time_sweep_summary.csv     one row per T1 (metrics + gate)
  cv_t<sec>_records.csv            per-T1 OOS trade ledger (for robust_metrics.py)
"""
from __future__ import annotations
import copy
from pathlib import Path
import numpy as np
import pandas as pd
import lightgbm as lgb

import huber_common as hc
from robust_metrics import win_capped_ev, day_block_bootstrap
from scipy import stats

N_FOLDS, MIN_TRAIN, BOOT = 5, 200, 4000
N_SEEDS = 5                      # match sweep4 multi-seed robustness reporting

# Entry times to test (seconds before close). Market length is 300s, so T1 ranges
# from just-after-open (~270s) to near-expiry (~30s). Dense around the current 180s.
T1_GRID = [30, 45, 60, 90, 120, 150, 165, 180, 195, 210, 240, 270]

# Frozen production config (sweeps 1-4 winner).
BEST_CFG = copy.deepcopy(hc.BASE_CFG)
BEST_CFG.update(min_child_samples=150)

OUT = Path(__file__).parent / "entry_time_sweep_results"
OUT.mkdir(exist_ok=True)


def build_frame_at(contracts, t1: int) -> pd.DataFrame:
    """Rebuild the feature frame at a given entry time T1 (cached contracts)."""
    hc.T1 = t1                                  # extract_row reads this module global
    rows = [hc.extract_row(cd) for cd in contracts]
    return pd.DataFrame([r for r in rows if r is not None])


def run_cv_seed(df: pd.DataFrame, cfg: dict, seed: int) -> pd.DataFrame:
    """One expanding-window CV pass. Fold seed = base_seed + fold (parity with sweeps)."""
    n = len(df); fold_size = n // N_FOLDS
    recs = []
    for k in range(N_FOLDS):
        te_s = k * fold_size
        te_e = (k + 1) * fold_size if k < N_FOLDS - 1 else n
        if te_s < MIN_TRAIN:
            continue
        tr, te = df.iloc[:te_s], df.iloc[te_s:te_e]
        Xtr = tr[hc.FEATURES].to_numpy().astype(float)
        Xte = te[hc.FEATURES].to_numpy().astype(float)
        ty = tr["y_settle"].to_numpy().astype(float) / tr["c_yes"].to_numpy() - 1.0
        tn = (1.0 - tr["y_settle"].to_numpy().astype(float)) / tr["c_no"].to_numpy() - 1.0
        my = lgb.train(hc.huber_params(cfg, seed=seed + k),
                       lgb.Dataset(Xtr, label=ty, free_raw_data=False),
                       num_boost_round=cfg["n_rounds"])
        mn = lgb.train(hc.huber_params(cfg, seed=seed + k),
                       lgb.Dataset(Xtr, label=tn, free_raw_data=False),
                       num_boost_round=cfg["n_rounds"])
        py, pn = my.predict(Xte), mn.predict(Xte)
        for i, (_, row) in enumerate(te.iterrows()):
            d = row.to_dict()
            side = hc.decide_huber(float(py[i]), float(pn[i]), float(d["p_yes_mid"]))
            recs.append({
                "contract_id": d["contract_id"],
                "action": ["YES", "NO", "SKIP"][side],
                "pnl": hc.pnl_of(side, float(d["y_settle"]),
                                 float(d["up_ask"]), float(d["down_ask"])),
            })
    return pd.DataFrame(recs)


def score(ledger: pd.DataFrame) -> dict:
    traded = ledger[ledger["action"] != "SKIP"].dropna(subset=["pnl"]).copy()
    pool = len(ledger)
    if traded.empty:
        return {"trades": 0, "pool": pool, "ev_avail": float("nan"),
                "ev_trig": float("nan"), "win_rate": float("nan"),
                "true_ev_ci_lo": float("nan"), "win_capped": float("nan"),
                "trim10": float("nan"), "GATE": False}
    r = traded["pnl"].to_numpy()
    epoch = traded["contract_id"].astype(str).str.extract(r"(\d{10})$")[0].astype(float)
    day = pd.to_datetime(epoch, unit="s", utc=True).dt.date.astype(str)
    bdf = pd.DataFrame({"r": r, "day": day.to_numpy()})
    true_lo, _, _ = day_block_bootstrap(bdf, np.mean, BOOT)
    capped = win_capped_ev(r)
    trim = float(stats.trim_mean(r, 0.10))
    gate = (true_lo > 0) and (capped > 0) and (trim > 0)
    return {
        "trades": len(traded), "pool": pool, "ev_avail": r.sum() / pool,
        "ev_trig": r.mean(), "win_rate": float((r > 0).mean()),
        "n_days": int(day.nunique()), "true_ev_ci_lo": true_lo,
        "win_capped": capped, "trim10": trim, "GATE": gate,
    }


def main():
    print("Loading contracts once…")
    contracts = hc.load_data()
    print(f"  {len(contracts)} contracts loaded\n")

    rows = []
    print(f"Entry-time sweep — frozen config (mc=150), {N_SEEDS}-seed averaging, "
          f"{N_FOLDS}-fold expanding CV\n")
    header = (f"  {'T1':>4}  {'rows':>5}  {'trades':>6}  {'EV/avail':>9}  {'EV/trig':>8}  "
              f"{'WR':>6}  {'capped':>8}  {'trim10':>8}  {'trueCIlo':>9}  {'passes':>7}")
    print(header)
    print("  " + "-" * (len(header) - 2))

    for t1 in T1_GRID:
        df = build_frame_at(contracts, t1)
        nrows = len(df)
        if nrows < MIN_TRAIN + 50:
            print(f"  {t1:>4}  {nrows:>5}  (too few rows — skip)")
            continue

        seed_scores = []
        ledger0 = None
        for s in range(N_SEEDS):
            led = run_cv_seed(df, BEST_CFG, seed=s * 100)
            if s == 0:
                ledger0 = led
            seed_scores.append(score(led))
        ledger0.to_csv(OUT / f"cv_t{t1}_records.csv", index=False)

        agg = {
            "T1": t1, "rows": nrows,
            "trades": int(np.mean([x["trades"] for x in seed_scores])),
            "ev_avail": float(np.mean([x["ev_avail"] for x in seed_scores])),
            "ev_trig": float(np.mean([x["ev_trig"] for x in seed_scores])),
            "win_rate": float(np.mean([x["win_rate"] for x in seed_scores])),
            "win_capped": float(np.mean([x["win_capped"] for x in seed_scores])),
            "trim10": float(np.mean([x["trim10"] for x in seed_scores])),
            "true_ev_ci_lo": float(np.mean([x["true_ev_ci_lo"] for x in seed_scores])),
            "capped_min": float(np.min([x["win_capped"] for x in seed_scores])),
            "ci_lo_min": float(np.min([x["true_ev_ci_lo"] for x in seed_scores])),
            "seeds_pass": int(sum(x["GATE"] for x in seed_scores)),
        }
        rows.append(agg)
        print(f"  {t1:>4}  {nrows:>5}  {agg['trades']:>6}  {agg['ev_avail']:>+9.4f}  "
              f"{agg['ev_trig']:>+8.4f}  {agg['win_rate']:>6.1%}  "
              f"{agg['win_capped']:>+8.4f}  {agg['trim10']:>+8.4f}  "
              f"{agg['true_ev_ci_lo']:>+9.4f}  {agg['seeds_pass']}/{N_SEEDS}")

    res = pd.DataFrame(rows)
    res.to_csv(OUT / "entry_time_sweep_summary.csv", index=False)
    print(f"\nSummary → {OUT}/entry_time_sweep_summary.csv")

    if not res.empty:
        print(f"\n{'='*70}\nBest by metric\n{'='*70}")
        for metric, label in [("ev_avail", "true EV/avail"),
                              ("win_capped", "win-capped (lottery-free)"),
                              ("trim10", "trim10"),
                              ("true_ev_ci_lo", "true-EV CI lower bound")]:
            best = res.loc[res[metric].idxmax()]
            print(f"  {label:28s}: T1={int(best['T1']):>4}s  ({metric}={best[metric]:+.4f})")
        cur = res[res["T1"] == 180]
        if not cur.empty:
            c = cur.iloc[0]
            print(f"\n  current T1=180: EV/avail={c['ev_avail']:+.4f}  "
                  f"capped={c['win_capped']:+.4f}  trim10={c['trim10']:+.4f}  "
                  f"CIlo={c['true_ev_ci_lo']:+.4f}  seeds_pass={c['seeds_pass']}/{N_SEEDS}")


if __name__ == "__main__":
    main()
