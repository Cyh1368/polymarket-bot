#!/usr/bin/env python3
"""
2026-06-17-research/train_huber_save.py

Train the production Huber edge-regression model on ALL available BTC 5m contracts at
T1=180s (no held-out split — production model), and save both regressors + metadata.

Outputs → 2026-06-17-research/saved_huber_model/
  huber_yes_t180.txt   f_yes  ≈ E[YES return]  (objective=huber, alpha=1.0)
  huber_no_t180.txt    f_no   ≈ E[NO  return]
  metadata.json        features, target defs, hyperparams, training-data range

Decision at inference: trade the side with higher predicted edge if > SKIP_BONUS (0.05),
Filter B (block YES when p_yes_mid < 0.25). See huber_common.decide_huber.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import lightgbm as lgb

import copy
import huber_common as hc

OUT = Path(__file__).parent / "saved_huber_model"
OUT.mkdir(exist_ok=True)

# Best config for live (sweeps 1–4): baseline base + min_child_samples=150.
# Highest true EV among the robustly lottery-free configs (CV EV/avail≈+0.029,
# win-capped≈+0.040 positive on every seed, 3/5 seeds clear the full gate). mc>200 underfits
# and lottery dependence returns; stacking L2/δ on top of high mc over-regularizes.
BEST_CFG = copy.deepcopy(hc.BASE_CFG)
BEST_CFG.update(min_child_samples=150)


def main():
    print("Loading contracts…")
    df = hc.build_frame()
    n = len(df)
    print(f"  {n} rows at T1={hc.T1}s  UP={int(df['y_settle'].sum())}  DOWN={int((df['y_settle']==0).sum())}")

    X = df[hc.FEATURES].to_numpy().astype(float)
    t_yes = df["y_settle"].to_numpy().astype(float) / df["c_yes"].to_numpy() - 1.0
    t_no  = (1.0 - df["y_settle"].to_numpy().astype(float)) / df["c_no"].to_numpy() - 1.0

    cfg = BEST_CFG
    print(f"Training f_yes / f_no  (huber alpha={cfg['huber_alpha']}, "
          f"depth={cfg['max_depth']}, leaves={cfg['num_leaves']}, lr={cfg['learning_rate']}, "
          f"rounds={cfg['n_rounds']})…")

    m_yes = lgb.train(hc.huber_params(cfg, seed=0),
                      lgb.Dataset(X, label=t_yes, free_raw_data=False),
                      num_boost_round=cfg["n_rounds"])
    m_no  = lgb.train(hc.huber_params(cfg, seed=0),
                      lgb.Dataset(X, label=t_no, free_raw_data=False),
                      num_boost_round=cfg["n_rounds"])

    m_yes.save_model(str(OUT / "huber_yes_t180.txt"))
    m_no.save_model(str(OUT / "huber_no_t180.txt"))

    py, pn = m_yes.predict(X), m_no.predict(X)
    meta = {
        "model": "huber_edge_v2_mc150",
        "trained": "2026-06-18",
        "selected_as": "best-for-live (sweeps 1-4): highest true EV among robustly "
                       "lottery-free configs; win-capped positive on every seed; 3/5 seeds "
                       "clear the full gate. min_child_samples=150 is the inverted-U peak.",
        "venue": "polymarket", "coin": hc.COIN, "horizon_seconds": hc.T1,
        "features": hc.FEATURES,
        "target_yes": "y/c_yes - 1   (realized YES return, c_yes = up_ask + 0.01)",
        "target_no":  "(1-y)/c_no - 1 (realized NO  return, c_no = down_ask + 0.01)",
        "objective": "huber", "huber_alpha": cfg["huber_alpha"],
        "hyperparams": cfg,
        "decision_rule": "trade max(pred_yes,pred_no) if > 0.05; Filter B: block YES if p_yes_mid<0.25",
        "skip_bonus": hc.SKIP_BONUS, "yes_gate_lo": hc.YES_GATE_LO,
        "n_train_contracts": n,
        "train_close_time_min": str(df["close_time"].min()),
        "train_close_time_max": str(df["close_time"].max()),
        "insample_pred_yes": {"min": float(py.min()), "max": float(py.max()), "mean": float(py.mean())},
        "insample_pred_no":  {"min": float(pn.min()), "max": float(pn.max()), "mean": float(pn.mean())},
        "cv_performance": "EV/avail≈+0.029, win-capped≈+0.040, trim10≈+0.052 (5-fold "
                          "expanding CV, ~874 trades); lottery-free robustly across seeds.",
        "WARNING": "Gate pass is SEED-FRAGILE: true-EV day-block CI lower bound sits on the "
                   "zero boundary (~3/5 seeds pass) because there are only ~9 day-blocks. "
                   "Lottery-freeness is robust; statistical significance is sample-size "
                   "limited. Research artifact — do NOT deploy capital until the gate passes "
                   "robustly on more trading days.",
    }
    (OUT / "metadata.json").write_text(json.dumps(meta, indent=2))

    print(f"\nSaved → {OUT}/")
    print(f"  huber_yes_t180.txt   ({m_yes.num_trees()} trees)")
    print(f"  huber_no_t180.txt    ({m_no.num_trees()} trees)")
    print(f"  metadata.json")
    print(f"  in-sample pred_yes range [{py.min():+.3f},{py.max():+.3f}] mean {py.mean():+.3f}")
    print(f"  in-sample pred_no  range [{pn.min():+.3f},{pn.max():+.3f}] mean {pn.mean():+.3f}")


if __name__ == "__main__":
    main()
