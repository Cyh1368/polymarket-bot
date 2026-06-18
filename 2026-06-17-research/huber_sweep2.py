#!/usr/bin/env python3
"""
2026-06-17-research/huber_sweep2.py

Round 2 of the Huber hyperparameter sweep. Round 1 (huber_sweep.py) identified the winning
directions:
  - regularization ↑  (lambda_l2 20, min_child 50)  → doubles true EV, makes it significant
  - Huber δ ↓ (0.5)                                  → drives win-capped positive
  - capacity ↑ (depth/rounds)                        → HURTS (overfit)

This round COMBINES and tunes those directions to try to clear the full gate
(significant true-EV AND positive win-capped AND positive trim10) for the first time.

Same CV + robust gate as round 1 (imported from huber_sweep).
"""
from __future__ import annotations
import copy
from pathlib import Path
import pandas as pd

import huber_common as hc
from huber_sweep import run_cv, score

OUT = Path(__file__).parent / "huber_sweep_results"
OUT.mkdir(exist_ok=True)


def combos():
    base = copy.deepcopy(hc.BASE_CFG)
    out = {"baseline": base}
    def mk(**kw):
        c = copy.deepcopy(base); c.update(**kw); return c
    out["core"]        = mk(lambda_l2=20, min_child_samples=50, huber_alpha=0.5)
    out["l2_huber"]    = mk(lambda_l2=20, huber_alpha=0.5)
    out["mc_huber"]    = mk(min_child_samples=50, huber_alpha=0.5)
    out["l2_mc"]       = mk(lambda_l2=20, min_child_samples=50)
    out["reg_extreme"] = mk(lambda_l2=40, min_child_samples=80)
    out["core_d0.7"]   = mk(lambda_l2=20, min_child_samples=50, huber_alpha=0.7)
    out["core_fine"]   = mk(lambda_l2=20, min_child_samples=50, huber_alpha=0.5,
                            learning_rate=0.03, n_rounds=500)
    return out


def main():
    print("Loading contracts…")
    df = hc.build_frame()
    print(f"  {len(df)} rows at T1={hc.T1}s\n")

    rows = []
    for name, cfg in combos().items():
        s = score(run_cv(df, cfg))
        s["config"] = name
        changed = {k: v for k, v in cfg.items() if hc.BASE_CFG.get(k) != v}
        s["changed"] = ",".join(f"{k}={v}" for k, v in changed.items()) or "(baseline)"
        rows.append(s)
        print(f"  {name:12s} trades={s['trades']:4d}  EV/avail={s['ev_avail']:+.4f}  "
              f"EV/trig={s['ev_trig']:+.4f}  WR={s['win_rate']:.1%}  "
              f"capped={s['win_capped']:+.4f}  trim10={s['trim10']:+.4f}  "
              f"trueCIlo={s['true_ev_ci_lo']:+.4f}  {'PASS ✅' if s['GATE'] else 'fail'}")

    res = pd.DataFrame(rows)[["config", "changed", "trades", "ev_avail", "ev_trig",
                              "win_rate", "win_capped", "trim10", "true_ev_ci_lo", "GATE"]]
    res.to_csv(OUT / "huber_sweep2_summary.csv", index=False)
    passers = res[res.GATE]
    print(f"\n{'='*60}")
    print(f"GATE PASSERS: {list(passers.config) if len(passers) else 'none (see closest below)'}")
    if len(passers) == 0:
        # closest = positive true-CI and least-negative capped
        cand = res[res.true_ev_ci_lo > 0].sort_values("win_capped", ascending=False)
        if len(cand):
            print("Closest (significant true-EV, ranked by win-capped):")
            for _, r in cand.iterrows():
                print(f"  {r['config']:12s} capped={r['win_capped']:+.4f} trim10={r['trim10']:+.4f} "
                      f"trueCIlo={r['true_ev_ci_lo']:+.4f} EV/avail={r['ev_avail']:+.4f}")
    print(f"\nSummary → {OUT}/huber_sweep2_summary.csv")


if __name__ == "__main__":
    main()
