#!/usr/bin/env python3
"""
Sweep T1 entry times for ETH using the CFES purged daily walk-forward.
Evaluates on offensive sessions (08-12 & 20-24 UTC) combined.
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "2026-06-17-research"))
sys.path.insert(0, str(REPO / "2026-06-20-research"))

import consistency_framework as cf
import consistency_framework_4h as c4

GOOD_PHASES = [2, 5]   # 08-12, 20-24 — pre-registered
T1_VALUES   = [215, 225, 235, 245, 255, 265, 275, 285, 295, 305]
N_BOOT      = 10000

def day_stats(led):
    de = led.groupby("date").apply(lambda x: x.pnl.sum() / len(x), include_groups=False).to_numpy()
    D = len(de); mean = de.mean()
    std = de.std(ddof=1) if D > 1 else np.nan
    t = mean / (std / np.sqrt(D)) if (D > 1 and std > 1e-9) else np.nan
    pos = float((de > 0).mean())
    k = max(1, int(np.ceil(0.2 * D)))
    cvar = float(np.sort(de)[:k].mean())
    sharpe = mean / std if (std and std > 1e-9) else 0.0
    return {"D": D, "mean": mean, "std": std, "t": t, "pos": pos, "cvar": cvar, "sharpe": sharpe}

def dayblock_ci(led, n=N_BOOT):
    de = led.groupby("date").apply(lambda x: x.pnl.sum() / len(x), include_groups=False)
    nw = led.groupby("date").size()
    r, w = de.to_numpy(), nw.to_numpy()
    D = len(r); rng = np.random.default_rng(0)
    boot = [np.sum(r[i] * w[i]) / np.sum(w[i]) for i in (rng.integers(0, D, D) for _ in range(n))]
    return float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))

print("ETH T1 sweep — CFES purged daily walk-forward, offensive sessions (08-12 & 20-24 UTC)")
print("=" * 80)
print(f"{'T1':>5} {'days':>5} {'avail':>7} {'EV/avail':>9} {'sharpe':>7} "
      f"{'t-stat':>7} {'pos%':>6} {'CVaR20':>8} {'CI_lo':>7} {'CI_hi':>7}")
print("-" * 80)

rows = []
for t1 in T1_VALUES:
    try:
        df = cf.load_frame("ETH", t1)
        if df["date"].nunique() < 5:
            print(f"{t1:>5}  insufficient data"); continue
        led_all = c4.walkforward(df, mode="single")
        led = led_all[led_all.phase.isin(GOOD_PHASES)].copy()
        if len(led) < 50:
            print(f"{t1:>5}  too few rows ({len(led)})"); continue
        ev = led.pnl.sum() / len(led)
        s  = day_stats(led)
        lo, hi = dayblock_ci(led)
        print(f"{t1:>5} {s['D']:>5} {len(led):>7} {ev:>+9.4f} {s['sharpe']:>+7.3f} "
              f"{(s['t'] or 0):>+7.3f} {s['pos']:>5.0%} {s['cvar']:>+8.4f} "
              f"{lo:>+7.4f} {hi:>+7.4f}")
        rows.append({"t1": t1, "n_days": s["D"], "n_avail": len(led),
                     "ev": ev, "sharpe": s["sharpe"], "t": s["t"], "pos": s["pos"],
                     "cvar": s["cvar"], "ci_lo": lo, "ci_hi": hi})
    except Exception as e:
        print(f"{t1:>5}  error: {e}")

print("=" * 80)
if rows:
    best = max(rows, key=lambda r: r["sharpe"])
    print(f"\nBest by Sharpe: T1={best['t1']}  "
          f"EV={best['ev']:+.4f}  Sharpe={best['sharpe']:+.3f}  "
          f"t={best['t']:+.3f}  pos={best['pos']:.0%}  CI=[{best['ci_lo']:+.4f},{best['ci_hi']:+.4f}]")
    best_t = max(rows, key=lambda r: (r["t"] or -99))
    print(f"Best by t-stat: T1={best_t['t1']}  "
          f"EV={best_t['ev']:+.4f}  Sharpe={best_t['sharpe']:+.3f}  "
          f"t={best_t['t']:+.3f}  pos={best_t['pos']:.0%}  CI=[{best_t['ci_lo']:+.4f},{best_t['ci_hi']:+.4f}]")
    pd.DataFrame(rows).to_csv(REPO / "2026-06-20-research" / "eth_t1_sweep_cfes.csv", index=False)
    print("\nSaved: 2026-06-20-research/eth_t1_sweep_cfes.csv")
