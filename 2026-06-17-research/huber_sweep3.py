#!/usr/bin/env python3
"""
2026-06-17-research/huber_sweep3.py

Does more min_child_samples keep helping? Round 1 showed mc 20→50 doubled EV; reg_extreme
(l2=40, mc=80) then *reduced* EV, hinting at a sweet spot. Here we scan mc = 20/50/100/150/200
on two bases:
  A) baseline    (l2=5, huber_alpha=1.0)  — isolates min_child's effect cleanly
  B) l2_huber    (l2=20, huber_alpha=0.5) — our leading config; does mc help it too?

Same CV + robust gate as the other sweeps.
"""
from __future__ import annotations
import copy
from pathlib import Path
import pandas as pd

import huber_common as hc
from huber_sweep import run_cv, score

OUT = Path(__file__).parent / "huber_sweep_results"
OUT.mkdir(exist_ok=True)
MC_GRID = [20, 50, 100, 150, 200]


def configs():
    base = copy.deepcopy(hc.BASE_CFG)
    l2h  = copy.deepcopy(hc.BASE_CFG); l2h.update(lambda_l2=20, huber_alpha=0.5)
    out = {}
    for mc in MC_GRID:
        a = copy.deepcopy(base); a.update(min_child_samples=mc); out[f"baseA_mc{mc}"] = a
    for mc in MC_GRID:
        b = copy.deepcopy(l2h);  b.update(min_child_samples=mc); out[f"l2huber_mc{mc}"] = b
    return out


def main():
    print("Loading contracts…")
    df = hc.build_frame()
    print(f"  {len(df)} rows at T1={hc.T1}s\n")

    rows = []
    for name, cfg in configs().items():
        s = score(run_cv(df, cfg))
        s["config"] = name
        s["base"] = "baseline(l2=5,d=1.0)" if name.startswith("baseA") else "l2_huber(l2=20,d=0.5)"
        s["min_child"] = cfg["min_child_samples"]
        rows.append(s)
        print(f"  {name:16s} mc={cfg['min_child_samples']:3d}  trades={s['trades']:4d}  "
              f"EV/avail={s['ev_avail']:+.4f}  EV/trig={s['ev_trig']:+.4f}  WR={s['win_rate']:.1%}  "
              f"capped={s['win_capped']:+.4f}  trim10={s['trim10']:+.4f}  "
              f"trueCIlo={s['true_ev_ci_lo']:+.4f}  {'PASS ✅' if s['GATE'] else 'fail'}")

    res = pd.DataFrame(rows)[["config", "base", "min_child", "trades", "ev_avail", "ev_trig",
                              "win_rate", "win_capped", "trim10", "true_ev_ci_lo", "GATE"]]
    res.to_csv(OUT / "huber_sweep3_summary.csv", index=False)

    print(f"\n{'='*64}\nmin_child trend (EV/avail) — is more increasingly better?\n{'='*64}")
    for base_label in ["baseA", "l2huber"]:
        sub = res[res.config.str.startswith(base_label)].sort_values("min_child")
        tag = sub.iloc[0]["base"]
        trend = "  ".join(f"mc{r.min_child}:{r.ev_avail:+.4f}" for _, r in sub.iterrows())
        print(f"  {tag:24s}  {trend}")
    print(f"\nSummary → {OUT}/huber_sweep3_summary.csv")


if __name__ == "__main__":
    main()
