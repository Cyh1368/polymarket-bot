#!/usr/bin/env python3
"""
2026-06-17-research/t240_finetune.py

Fine-tune the frozen-architecture Huber edge model AT ENTRY TIME T1=240s.

Entry-time sweep (entry_time_sweep_report.md addendum) showed T1≈240 is a broad
LOTTERY-FREE band (win-capped/trim10 > 0 across 225-260) but fails the SIGNIFICANCE half
of the gate (true-EV day-block CI lower bound < 0). So the goal here is to push true-EV
significance up *without* re-introducing lottery dependence.

Baseline = production config (`baseA_mc150`). We test 7 single-axis hyperparameter changes,
each through the SAME 5-fold expanding CV + the SAME robust gate, 5-seed averaged (so the
significance read is seed-robust, not a one-seed fluke). Levers chosen from the round-1..4
findings in huber_sweep_report.md:
  L2 ↑ lifts true EV; min_child is an inverted-U (peak ~150); Huber δ trades significance
  (δ=1.0) against lottery-freeness (δ=0.5); lr/rounds ~neutral.

Outputs → t240_finetune_results/
  t240_finetune_summary.csv     one row per config (5-seed mean + worst-seed)
  cv_<config>_records.csv       seed-0 OOS ledger per config
"""
from __future__ import annotations
import copy
from pathlib import Path
import numpy as np
import pandas as pd

import huber_common as hc
from entry_time_sweep import build_frame_at, run_cv_seed, score, N_SEEDS

T1_FIX = 240
OUT = Path(__file__).parent / "t240_finetune_results"
OUT.mkdir(exist_ok=True)

# Baseline = production winner (BASE_CFG + min_child_samples=150).
BASELINE = copy.deepcopy(hc.BASE_CFG); BASELINE.update(min_child_samples=150)


def configs():
    out = {"baseline_mc150": copy.deepcopy(BASELINE)}
    c = copy.deepcopy(BASELINE); c.update(lambda_l2=20.0);                  out["l2_20"]        = c
    c = copy.deepcopy(BASELINE); c.update(lambda_l2=40.0);                  out["l2_40"]        = c
    c = copy.deepcopy(BASELINE); c.update(min_child_samples=50);            out["mc_50"]        = c
    c = copy.deepcopy(BASELINE); c.update(min_child_samples=250);           out["mc_250"]       = c
    c = copy.deepcopy(BASELINE); c.update(huber_alpha=0.5);                 out["huber_d_0.5"]  = c
    c = copy.deepcopy(BASELINE); c.update(huber_alpha=2.0);                 out["huber_d_2.0"]  = c
    c = copy.deepcopy(BASELINE); c.update(learning_rate=0.02, n_rounds=750); out["lr_down"]     = c
    return out


def main():
    print("Loading contracts once…")
    contracts = hc.load_data()
    df = build_frame_at(contracts, T1_FIX)
    print(f"  {len(contracts)} contracts → {len(df)} rows at T1={T1_FIX}s")
    print(f"  baseline = mc150 production config; {N_SEEDS}-seed averaging\n")

    hdr = (f"  {'config':16s} {'changed':24s} {'trades':>6} {'EV/avail':>9} {'EV/trig':>8} "
           f"{'WR':>6} {'capped':>8} {'trim10':>8} {'CIlo':>9} {'CIlo_min':>9} {'pass':>5}")
    print(hdr); print("  " + "-" * (len(hdr) - 2))

    rows = []
    for name, cfg in configs().items():
        ss = [score(run_cv_seed(df, cfg, seed=s * 100)) for s in range(N_SEEDS)]
        # save seed-0 ledger
        run_cv_seed(df, cfg, seed=0).to_csv(OUT / f"cv_{name}_records.csv", index=False)
        changed = {k: v for k, v in cfg.items() if BASELINE.get(k) != v}
        chg = ",".join(f"{k}={v}" for k, v in changed.items()) or "(baseline)"
        a = dict(
            config=name, changed=chg, T1=T1_FIX,
            trades=int(np.mean([x["trades"] for x in ss])),
            ev_avail=float(np.mean([x["ev_avail"] for x in ss])),
            ev_trig=float(np.mean([x["ev_trig"] for x in ss])),
            win_rate=float(np.mean([x["win_rate"] for x in ss])),
            win_capped=float(np.mean([x["win_capped"] for x in ss])),
            trim10=float(np.mean([x["trim10"] for x in ss])),
            true_ev_ci_lo=float(np.mean([x["true_ev_ci_lo"] for x in ss])),
            cilo_min=float(np.min([x["true_ev_ci_lo"] for x in ss])),
            capped_min=float(np.min([x["win_capped"] for x in ss])),
            seeds_pass=int(sum(x["GATE"] for x in ss)),
        )
        rows.append(a)
        print(f"  {name:16s} {chg:24s} {a['trades']:>6} {a['ev_avail']:>+9.4f} "
              f"{a['ev_trig']:>+8.4f} {a['win_rate']:>6.1%} {a['win_capped']:>+8.4f} "
              f"{a['trim10']:>+8.4f} {a['true_ev_ci_lo']:>+9.4f} {a['cilo_min']:>+9.4f} "
              f"{a['seeds_pass']}/{N_SEEDS}")

    res = pd.DataFrame(rows)
    res.to_csv(OUT / "t240_finetune_summary.csv", index=False)
    print(f"\nSummary → {OUT}/t240_finetune_summary.csv")

    base = res[res.config == "baseline_mc150"].iloc[0]
    print(f"\n{'='*72}\nΔ vs baseline (T1=240) — what moves the needle\n{'='*72}")
    for _, r in res[res.config != "baseline_mc150"].iterrows():
        print(f"  {r['config']:16s} {r['changed']:24s} "
              f"ΔEV/avail={r['ev_avail']-base['ev_avail']:+.4f}  "
              f"Δcapped={r['win_capped']-base['win_capped']:+.4f}  "
              f"ΔCIlo={r['true_ev_ci_lo']-base['true_ev_ci_lo']:+.4f}")


if __name__ == "__main__":
    main()
