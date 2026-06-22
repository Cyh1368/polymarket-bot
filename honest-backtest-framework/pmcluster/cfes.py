#!/usr/bin/env python3
"""
pmcluster.cfes — Consistency-First Evaluation & Selection at the DAY level.

The calendar day is the unit of analysis. Every statistic (mean, std, Sharpe,
t-stat, CI, CVaR) is computed over days, never over contracts. The CI is a
day-block bootstrap. This is the honest framework from 2026-06-20.
"""
from __future__ import annotations
import datetime
import numpy as np
import pandas as pd

from . import config as C
from . import model as M


# ── purged daily walk-forward ─────────────────────────────────────────────────
def _to_date(d):
    if isinstance(d, datetime.date):
        return d
    return pd.Timestamp(d).date()


def daily_walkforward(df: pd.DataFrame, mode: str = "single",
                      purge_days: int = C.PURGE_DAYS,
                      min_train_days: int = C.MIN_TRAIN_DAYS,
                      min_train_rows: int = C.MIN_TRAIN_ROWS,
                      ensemble_seeds: int = C.ENSEMBLE_SEEDS,
                      num_threads: int = 1) -> pd.DataFrame:
    """One honest OOS row per contract, tagged by day. df is one coin+horizon slice."""
    df = df.copy()
    df["_d"] = df["date"].map(_to_date)
    days = sorted(df["_d"].unique())
    ledger = []
    for di, test_day in enumerate(days):
        if di < min_train_days:
            continue
        cutoff = test_day - datetime.timedelta(days=purge_days)
        tr = df[df["_d"] < cutoff]
        te = df[df["_d"] == test_day]
        if len(tr) < min_train_rows or te.empty:
            continue
        if mode == "single":
            m_yes, m_no = M.train_pair(tr, seed=0, num_threads=num_threads)
            sides, pnls = M.decide_single(te, m_yes, m_no)
        else:
            models = [M.train_pair(tr, seed=s, num_threads=num_threads)
                      for s in range(ensemble_seeds)]
            sides, pnls = M.decide_ensemble(te, models)
        ledger.append(pd.DataFrame({"date": [test_day] * len(te), "side": sides, "pnl": pnls}))
    if not ledger:
        return pd.DataFrame(columns=["date", "side", "pnl"])
    return pd.concat(ledger, ignore_index=True)


# ── day-level series + metrics ────────────────────────────────────────────────
def daily_series(ledger: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for date, grp in ledger.groupby("date"):
        n = len(grp)
        traded = grp[grp.side != C.CLASS_SKIP]
        rows.append({
            "date": date, "n_avail": n, "n_traded": len(traded),
            "trade_rate": len(traded) / n if n else 0.0,
            "ev_avail": grp.pnl.sum() / n if n else 0.0,
            "total_pnl": grp.pnl.sum(),
        })
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def dayblock_bootstrap(r: np.ndarray, w: np.ndarray, n_boot: int = C.N_BOOT, seed: int = 0):
    """Day-block bootstrap of the contract-weighted pooled EV. Returns (lo, hi, p_neg, draws)."""
    D = len(r)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, D, size=(n_boot, D))
    rw = (r * w)[idx].sum(axis=1)
    ww = w[idx].sum(axis=1)
    boot = rw / ww
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return float(lo), float(hi), float((boot <= 0).mean()), boot


def metrics(ledger: pd.DataFrame, gate: C.Gate = None) -> dict:
    gate = gate or C.Gate()
    ds = daily_series(ledger)
    r = ds["ev_avail"].to_numpy()
    w = ds["n_avail"].to_numpy().astype(float)
    pnl = ds["total_pnl"].to_numpy()
    D = len(r)
    if D < 2:
        return {"n_days": D, "pass": False, "reason": "insufficient days", "daily": ds}

    mean = float(r.mean())
    std = float(r.std(ddof=1))
    sharpe = mean / std if std > 1e-9 else 0.0
    tstat = mean / (std / np.sqrt(D)) if std > 1e-9 else 0.0
    pos_frac = float((r > 0).mean())
    k = max(1, int(np.ceil(0.20 * D)))
    cvar20 = float(np.sort(r)[:k].mean())
    ci_lo, ci_hi, p_neg, _ = dayblock_bootstrap(r, w)
    total = pnl.sum()
    topday_share = float(np.max(pnl) / total) if total > 1e-9 else 1.0
    pooled_ev = float(np.sum(r * w) / np.sum(w))

    checks = {
        "min_oos_days":   (D >= gate.min_oos_days, f"{D} >= {gate.min_oos_days}"),
        "positive_frac":  (pos_frac >= gate.min_positive_frac, f"{pos_frac:.2f} >= {gate.min_positive_frac}"),
        "daily_sharpe":   (sharpe >= gate.min_daily_sharpe, f"{sharpe:.2f} >= {gate.min_daily_sharpe}"),
        "tstat":          (tstat >= gate.min_tstat, f"{tstat:.2f} >= {gate.min_tstat}"),
        "dayblock_ci_lo": (ci_lo > gate.dayblock_ci_lo_gt, f"{ci_lo:+.4f} > {gate.dayblock_ci_lo_gt}"),
        "cvar20_floor":   (cvar20 >= gate.max_cvar20, f"{cvar20:+.4f} >= {gate.max_cvar20}"),
        "topday_share":   (topday_share <= gate.max_topday_share, f"{topday_share:.2f} <= {gate.max_topday_share}"),
    }
    return {
        "n_days": D, "n_avail": int(w.sum()), "n_traded": int(ds.n_traded.sum()),
        "pooled_ev": pooled_ev, "mean_daily_ev": mean, "std_daily_ev": std,
        "daily_sharpe": sharpe, "tstat": tstat, "positive_frac": pos_frac,
        "cvar20": cvar20, "ci_lo": ci_lo, "ci_hi": ci_hi, "p_neg": p_neg,
        "topday_share": topday_share, "checks": checks,
        "pass": all(ok for ok, _ in checks.values()), "daily": ds,
    }
