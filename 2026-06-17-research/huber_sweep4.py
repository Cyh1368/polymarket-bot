#!/usr/bin/env python3
"""
2026-06-17-research/huber_sweep4.py

Push min_child_samples up to 1000 on the baseline base (l2=5, δ=1.0), with MULTI-SEED
averaging (significance was seed-fragile in round 3). Tests the hypothesis: lottery-freeness
improves up to a sweet spot then HIGH mc UNDERFITS and degrades (in CV early folds mc can
exceed the ~440-row training set → no splits → degenerate).

For each mc we run N_SEEDS CV passes and report mean ± spread of EV/avail, win-capped,
trim10, true-EV CI lower bound, and how many seeds pass the full gate. Also prints, for the
final production fit on ALL data, how many trees the model can actually build (capacity).
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

OUT = Path(__file__).parent / "huber_sweep_results"
OUT.mkdir(exist_ok=True)
N_FOLDS, MIN_TRAIN, BOOT, N_SEEDS = 5, 200, 4000, 5
MC_GRID = [150, 200, 300, 500, 750, 1000]


def run_cv(df, cfg, seed_off):
    n = len(df); fs = n // N_FOLDS; recs = []
    for k in range(N_FOLDS):
        te_s = k * fs; te_e = (k + 1) * fs if k < N_FOLDS - 1 else n
        if te_s < MIN_TRAIN:
            continue
        tr, te = df.iloc[:te_s], df.iloc[te_s:te_e]
        Xtr = tr[hc.FEATURES].to_numpy().astype(float); Xte = te[hc.FEATURES].to_numpy().astype(float)
        ty = tr["y_settle"].to_numpy().astype(float) / tr["c_yes"].to_numpy() - 1.0
        tn = (1.0 - tr["y_settle"].to_numpy().astype(float)) / tr["c_no"].to_numpy() - 1.0
        my = lgb.train(hc.huber_params(cfg, seed=k + seed_off), lgb.Dataset(Xtr, label=ty, free_raw_data=False), num_boost_round=cfg["n_rounds"])
        mn = lgb.train(hc.huber_params(cfg, seed=k + seed_off), lgb.Dataset(Xtr, label=tn, free_raw_data=False), num_boost_round=cfg["n_rounds"])
        py, pn = my.predict(Xte), mn.predict(Xte)
        for i, (_, row) in enumerate(te.iterrows()):
            d = row.to_dict(); side = hc.decide_huber(float(py[i]), float(pn[i]), float(d["p_yes_mid"]))
            recs.append({"contract_id": d["contract_id"], "action": ["YES", "NO", "SKIP"][side],
                         "pnl": hc.pnl_of(side, float(d["y_settle"]), float(d["up_ask"]), float(d["down_ask"]))})
    return pd.DataFrame(recs)


def metrics(led):
    t = led[led.action != "SKIP"].dropna(subset=["pnl"]); pool = len(led)
    if len(t) == 0:
        return dict(trades=0, ev_avail=0.0, win_capped=float("nan"), trim10=float("nan"),
                    true_ci_lo=float("nan"), gate=False)
    r = t.pnl.to_numpy()
    ep = t.contract_id.astype(str).str.extract(r"(\d{10})$")[0].astype(float)
    day = pd.to_datetime(ep, unit="s", utc=True).dt.date.astype(str)
    lo, _, _ = day_block_bootstrap(pd.DataFrame({"r": r, "day": day.to_numpy()}), np.mean, BOOT)
    cap = win_capped_ev(r); tr10 = float(stats.trim_mean(r, 0.10))
    return dict(trades=len(t), ev_avail=r.sum() / pool, win_capped=cap, trim10=tr10,
                true_ci_lo=lo, gate=(lo > 0 and cap > 0 and tr10 > 0))


def main():
    print("Loading contracts…")
    df = hc.build_frame()
    print(f"  {len(df)} rows at T1={hc.T1}s   (CV fold-1 trains on ~{len(df)//N_FOLDS} rows)\n")

    rows = []
    for mc in MC_GRID:
        cfg = copy.deepcopy(hc.BASE_CFG); cfg.update(min_child_samples=mc)
        runs = [metrics(run_cv(df, cfg, so)) for so in range(N_SEEDS)]
        agg = {key: np.array([r[key] for r in runs], dtype=float) for key in
               ["trades", "ev_avail", "win_capped", "trim10", "true_ci_lo"]}
        npass = sum(r["gate"] for r in runs)
        rows.append({
            "mc": mc, "mean_trades": np.nanmean(agg["trades"]),
            "ev_avail": np.nanmean(agg["ev_avail"]),
            "win_capped": np.nanmean(agg["win_capped"]),
            "trim10": np.nanmean(agg["trim10"]),
            "true_ci_lo_mean": np.nanmean(agg["true_ci_lo"]),
            "pass_rate": f"{npass}/{N_SEEDS}",
        })
        print(f"  mc={mc:4d}  trades≈{np.nanmean(agg['trades']):5.0f}  "
              f"EV/avail={np.nanmean(agg['ev_avail']):+.4f}  "
              f"capped={np.nanmean(agg['win_capped']):+.4f}  trim10={np.nanmean(agg['trim10']):+.4f}  "
              f"trueCIlo={np.nanmean(agg['true_ci_lo']):+.4f}  pass={npass}/{N_SEEDS}")

    res = pd.DataFrame(rows)
    res.to_csv(OUT / "huber_sweep4_summary.csv", index=False)

    # Production capacity at each mc (trees that can actually split on ALL 2200 rows)
    print(f"\n{'='*60}\nProduction-fit capacity on all {len(df)} rows:")
    X = df[hc.FEATURES].to_numpy().astype(float)
    ty = df["y_settle"].to_numpy().astype(float) / df["c_yes"].to_numpy() - 1.0
    for mc in MC_GRID:
        cfg = copy.deepcopy(hc.BASE_CFG); cfg.update(min_child_samples=mc)
        m = lgb.train(hc.huber_params(cfg, seed=0), lgb.Dataset(X, label=ty, free_raw_data=False), num_boost_round=cfg["n_rounds"])
        leaves = sum(t["num_leaves"] for t in m.dump_model()["tree_info"]) / m.num_trees()
        print(f"  mc={mc:4d}: avg {leaves:.2f} leaves/tree  (1.0 = no splits = underfit)")
    print(f"\nSummary → {OUT}/huber_sweep4_summary.csv")


if __name__ == "__main__":
    main()
