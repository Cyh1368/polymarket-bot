#!/usr/bin/env python3
"""
2026-06-17-research/huber_sweep.py

Hyperparameter sweep for the Huber edge-regression model. Evaluates the BASELINE plus 7
single-axis modifications through the SAME expanding-window CV (5 folds, MIN_TRAIN=200) and
the SAME robust metrics used everywhere else, so we can attribute effects to each parameter.

Each config changes ONE axis from baseline (so deltas are interpretable):
  1. depth_up        max_depth 3→5, num_leaves 7→31     (model capacity)
  2. lr_down         learning_rate 0.05→0.02, rounds 300→750  (slower/finer learning)
  3. rounds_up       n_rounds 300→600                   (more boosting)
  4. huber_d_0.5     huber_alpha 1.0→0.5                (tighter gradient cap)
  5. huber_d_2.0     huber_alpha 1.0→2.0                (looser gradient cap)
  6. l2_strong       lambda_l2 5→20                     (more L2 regularization)
  7. min_child_50    min_child_samples 20→50            (bigger leaves / less overfit)

Reports true EV (+ day-block CI), win-capped EV, trim10, win rate, trades, GATE.
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
OUT = Path(__file__).parent / "huber_sweep_results"
OUT.mkdir(exist_ok=True)

def cfg_variants():
    base = copy.deepcopy(hc.BASE_CFG)
    out = {"baseline": base}
    c = copy.deepcopy(base); c.update(max_depth=5, num_leaves=31);          out["depth_up"]     = c
    c = copy.deepcopy(base); c.update(learning_rate=0.02, n_rounds=750);    out["lr_down"]      = c
    c = copy.deepcopy(base); c.update(n_rounds=600);                        out["rounds_up"]    = c
    c = copy.deepcopy(base); c.update(huber_alpha=0.5);                     out["huber_d_0.5"]  = c
    c = copy.deepcopy(base); c.update(huber_alpha=2.0);                     out["huber_d_2.0"]  = c
    c = copy.deepcopy(base); c.update(lambda_l2=20.0);                      out["l2_strong"]    = c
    c = copy.deepcopy(base); c.update(min_child_samples=50);                out["min_child_50"] = c
    return out


def run_cv(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
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
        my = lgb.train(hc.huber_params(cfg, seed=k), lgb.Dataset(Xtr, label=ty, free_raw_data=False),
                       num_boost_round=cfg["n_rounds"])
        mn = lgb.train(hc.huber_params(cfg, seed=k), lgb.Dataset(Xtr, label=tn, free_raw_data=False),
                       num_boost_round=cfg["n_rounds"])
        py, pn = my.predict(Xte), mn.predict(Xte)
        for i, (_, row) in enumerate(te.iterrows()):
            d = row.to_dict()
            side = hc.decide_huber(float(py[i]), float(pn[i]), float(d["p_yes_mid"]))
            recs.append({
                "contract_id": d["contract_id"],
                "action": ["YES", "NO", "SKIP"][side],
                "pnl": hc.pnl_of(side, float(d["y_settle"]), float(d["up_ask"]), float(d["down_ask"])),
            })
    return pd.DataFrame(recs)


def score(ledger: pd.DataFrame) -> dict:
    traded = ledger[ledger["action"] != "SKIP"].dropna(subset=["pnl"]).copy()
    pool = len(ledger)
    r = traded["pnl"].to_numpy()
    # day from contract_id epoch
    epoch = traded["contract_id"].astype(str).str.extract(r"(\d{10})$")[0].astype(float)
    day = pd.to_datetime(epoch, unit="s", utc=True).dt.date.astype(str)
    bdf = pd.DataFrame({"r": r, "day": day.to_numpy()})
    true_lo, _, _ = day_block_bootstrap(bdf, np.mean, BOOT)
    capped = win_capped_ev(r)
    trim = float(stats.trim_mean(r, 0.10))
    gate = (true_lo > 0) and (capped > 0) and (trim > 0)
    return {
        "trades": len(traded), "ev_avail": r.sum() / pool, "ev_trig": r.mean(),
        "win_rate": float((r > 0).mean()), "true_ev_ci_lo": true_lo,
        "win_capped": capped, "trim10": trim, "GATE": gate,
    }


def main():
    print("Loading contracts…")
    df = hc.build_frame()
    print(f"  {len(df)} rows at T1={hc.T1}s\n")

    rows = []
    for name, cfg in cfg_variants().items():
        led = run_cv(df, cfg)
        s = score(led)
        s["config"] = name
        changed = {k: v for k, v in cfg.items() if hc.BASE_CFG.get(k) != v}
        s["changed"] = ",".join(f"{k}={v}" for k, v in changed.items()) or "(baseline)"
        rows.append(s)
        print(f"  {name:13s} trades={s['trades']:4d}  EV/avail={s['ev_avail']:+.4f}  "
              f"EV/trig={s['ev_trig']:+.4f}  WR={s['win_rate']:.1%}  "
              f"capped={s['win_capped']:+.4f}  trim10={s['trim10']:+.4f}  "
              f"trueCIlo={s['true_ev_ci_lo']:+.4f}  {'PASS' if s['GATE'] else 'fail'}")

    res = pd.DataFrame(rows)[["config", "changed", "trades", "ev_avail", "ev_trig",
                              "win_rate", "win_capped", "trim10", "true_ev_ci_lo", "GATE"]]
    res.to_csv(OUT / "huber_sweep_summary.csv", index=False)

    base = res[res.config == "baseline"].iloc[0]
    print(f"\n{'='*70}\nΔ vs baseline (EV/avail, win-capped) — what moves the needle\n{'='*70}")
    for _, r in res[res.config != "baseline"].iterrows():
        print(f"  {r['config']:13s} {r['changed']:34s}  "
              f"ΔEV/avail={r['ev_avail']-base['ev_avail']:+.4f}  "
              f"Δcapped={r['win_capped']-base['win_capped']:+.4f}")
    print(f"\nSummary → {OUT}/huber_sweep_summary.csv")


if __name__ == "__main__":
    main()
