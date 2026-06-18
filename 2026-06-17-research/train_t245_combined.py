#!/usr/bin/env python3
"""
2026-06-17-research/train_t245_combined.py

Train + save the production-candidate Huber edge model at ENTRY TIME T1=245s with the
COMBINED config that produced the study's most seed-robust gate pass:

    mc150 baseline + huber_alpha=0.5 + learning_rate=0.02 + n_rounds=750

(see entry_time_sweep_report.md Addendum 3: combined @ T1=245 → EV/avail +0.039,
win-capped +0.040, trim10 +0.057, gate PASS on 5/5 seeds with positive worst-seed CI).

Trains both regressors (f_yes, f_no) on ALL available BTC 5m contracts at T1=245 (no
held-out split — production model) and saves to saved_huber_model_t245_combined/.

DRY TESTING ONLY. This is a research artifact: the gate pass is on ~9 day-blocks. Do NOT
deploy capital. metadata.json carries the warning.
"""
from __future__ import annotations
import copy, json
from pathlib import Path
import numpy as np
import lightgbm as lgb

import huber_common as hc

T1_FIX = 245
OUT = Path(__file__).parent / "saved_huber_model_t245_combined"
OUT.mkdir(exist_ok=True)

CFG = copy.deepcopy(hc.BASE_CFG)
CFG.update(min_child_samples=150, huber_alpha=0.5, learning_rate=0.02, n_rounds=750)


def main():
    print("Loading contracts…")
    hc.T1 = T1_FIX                      # extract_row reads this module global
    df = hc.build_frame()
    n = len(df)
    print(f"  {n} rows at T1={T1_FIX}s  UP={int(df['y_settle'].sum())}  "
          f"DOWN={int((df['y_settle']==0).sum())}")

    X = df[hc.FEATURES].to_numpy().astype(float)
    t_yes = df["y_settle"].to_numpy().astype(float) / df["c_yes"].to_numpy() - 1.0
    t_no  = (1.0 - df["y_settle"].to_numpy().astype(float)) / df["c_no"].to_numpy() - 1.0

    print(f"Training f_yes / f_no  (huber alpha={CFG['huber_alpha']}, depth={CFG['max_depth']}, "
          f"leaves={CFG['num_leaves']}, mc={CFG['min_child_samples']}, lr={CFG['learning_rate']}, "
          f"rounds={CFG['n_rounds']})…")
    m_yes = lgb.train(hc.huber_params(CFG, seed=0),
                      lgb.Dataset(X, label=t_yes, free_raw_data=False),
                      num_boost_round=CFG["n_rounds"])
    m_no  = lgb.train(hc.huber_params(CFG, seed=0),
                      lgb.Dataset(X, label=t_no, free_raw_data=False),
                      num_boost_round=CFG["n_rounds"])

    m_yes.save_model(str(OUT / "huber_yes_t245.txt"))
    m_no.save_model(str(OUT / "huber_no_t245.txt"))

    py, pn = m_yes.predict(X), m_no.predict(X)
    meta = {
        "model": "huber_edge_t245_combined",
        "trained": "2026-06-18",
        "selected_as": "Most seed-robust gate pass in the entry-time study: combined config "
                       "(mc150 + huber_alpha=0.5 + lr=0.02/rounds=750) at T1=245 passes 5/5 "
                       "seeds with positive worst-seed true-EV CI (+0.006). 245 & 250 form a "
                       "contiguous pass region (more credible than lone spikes at 180/235).",
        "venue": "polymarket", "coin": hc.COIN, "horizon_seconds": T1_FIX,
        "features": hc.FEATURES,
        "target_yes": "y/c_yes - 1   (realized YES return, c_yes = up_ask + 0.01)",
        "target_no":  "(1-y)/c_no - 1 (realized NO  return, c_no = down_ask + 0.01)",
        "objective": "huber", "huber_alpha": CFG["huber_alpha"], "hyperparams": CFG,
        "decision_rule": "trade max(pred_yes,pred_no) if > 0.05; Filter B: block YES if p_yes_mid<0.25",
        "skip_bonus": hc.SKIP_BONUS, "yes_gate_lo": hc.YES_GATE_LO,
        "n_train_contracts": n,
        "train_close_time_min": str(df["close_time"].min()),
        "train_close_time_max": str(df["close_time"].max()),
        "cv_performance": "EV/avail +0.039, win-capped +0.040, trim10 +0.057, true-CIlo "
                          "+0.008 (worst seed +0.006), 5/5 seeds pass (5-fold expanding CV).",
        "WARNING": "DRY TESTING ONLY. Gate pass is on ~9 day-blocks; significance is "
                   "sample-size limited. Research artifact — do NOT deploy capital until the "
                   "gate holds on substantially more trading days.",
    }
    (OUT / "metadata.json").write_text(json.dumps(meta, indent=2))

    print(f"\nSaved → {OUT}/")
    print(f"  huber_yes_t245.txt   ({m_yes.num_trees()} trees)")
    print(f"  huber_no_t245.txt    ({m_no.num_trees()} trees)")
    print(f"  metadata.json")
    print(f"  in-sample pred_yes [{py.min():+.3f},{py.max():+.3f}] mean {py.mean():+.3f}")
    print(f"  in-sample pred_no  [{pn.min():+.3f},{pn.max():+.3f}] mean {pn.mean():+.3f}")


if __name__ == "__main__":
    main()
