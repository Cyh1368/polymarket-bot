#!/usr/bin/env python3
"""
pmcluster.cpcv — overfitting-aware validation primitives.

* build_config_day_matrix : per-config (horizon) point-in-time daily-EV series via a
  purged, embargoed walk-forward over calendar days (optionally pooling coins). The
  series is fixed (no retraining inside CSCV) so it is a legitimate backtest path.
* cscv_pbo : Probability of Backtest Overfitting (Bailey/Lopez de Prado CSCV) — does
  selecting the in-sample-best config generalize out-of-sample?
* deflated_sharpe : Deflated Sharpe Ratio, debiting the Sharpe for the number of trials
  (from the registry) and the skew/kurtosis of the candidate's daily series.

Day is the block unit everywhere; purge+embargo drop training contracts whose info
window overlaps (or sits within embargo of) a test day.
"""
from __future__ import annotations
import datetime
import itertools
import math

import numpy as np
import pandas as pd
from scipy import stats

from . import config as C
from . import model as M


def _to_date(d):
    return d if isinstance(d, datetime.date) else pd.Timestamp(d).date()


# ── point-in-time per-config daily-EV matrix ──────────────────────────────────
def build_config_day_matrix(frames: dict, horizons, allowed_days,
                            min_train_days=2, purge_days=C.PURGE_DAYS,
                            min_train_rows=C.MIN_TRAIN_ROWS, num_threads=1):
    """
    frames: {coin: feature_df}. Pools all coins by calendar day.
    Returns (M, days, info): M is a DataFrame indexed by day, columns = horizons,
    values = pooled EV-per-available-contract that day from a model trained on
    strictly-earlier days (purged). allowed_days excludes the lockbox.
    """
    allowed = sorted(_to_date(d) for d in allowed_days)
    perday = {}   # horizon -> {day -> ev_avail}
    counts = {}   # horizon -> {day -> n_avail}
    for h in horizons:
        # pooled frame at this horizon across coins, restricted to allowed days
        parts = []
        for coin, df in frames.items():
            sub = df[df["horizon"] == h].copy()
            sub["_d"] = sub["date"].map(_to_date)
            sub = sub[sub["_d"].isin(allowed)]
            if not sub.empty:
                parts.append(sub)
        if not parts:
            continue
        pooled = pd.concat(parts, ignore_index=True)
        days = sorted(pooled["_d"].unique())
        ev_by_day, n_by_day = {}, {}
        for di, test_day in enumerate(days):
            if di < min_train_days:
                continue
            cutoff = test_day - datetime.timedelta(days=purge_days)
            tr = pooled[pooled["_d"] < cutoff]
            te = pooled[pooled["_d"] == test_day]
            if len(tr) < min_train_rows or te.empty:
                continue
            m_yes, m_no = M.train_pair(tr, seed=0, num_threads=num_threads)
            sides, pnls = M.decide_single(te, m_yes, m_no)
            ev_by_day[test_day] = float(np.sum(pnls) / len(te))
            n_by_day[test_day] = int(len(te))
        perday[h] = ev_by_day
        counts[h] = n_by_day

    all_days = sorted(set().union(*[set(d.keys()) for d in perday.values()])) if perday else []
    M_df = pd.DataFrame(index=all_days)
    N_df = pd.DataFrame(index=all_days)
    for h in perday:
        M_df[h] = pd.Series(perday[h])
        N_df[h] = pd.Series(counts[h])
    return M_df, N_df


# ── CSCV Probability of Backtest Overfitting ──────────────────────────────────
def cscv_pbo(M: pd.DataFrame, n_groups=None, metric="mean"):
    """
    M: rows = observations (days), columns = configs. Returns dict with pbo and the
    logit distribution. Uses combinatorially-symmetric CV: split rows into S groups,
    all C(S, S/2) ways to choose IS, complement = OOS.
    """
    M = M.dropna(axis=0, how="any")
    T, Ncfg = M.shape
    if T < 4 or Ncfg < 2:
        return {"pbo": float("nan"), "n_obs": int(T), "n_configs": int(Ncfg),
                "n_combos": 0, "reason": "insufficient observations/configs"}
    S = n_groups or (T if T % 2 == 0 else T - 1)
    S = max(2, min(S, T))
    if S % 2 == 1:
        S -= 1
    # split rows into S contiguous groups
    groups = np.array_split(np.arange(T), S)
    half = S // 2

    def perf(block):
        x = M.iloc[block].to_numpy()
        if metric == "sharpe":
            mu = x.mean(axis=0); sd = x.std(axis=0, ddof=1)
            return np.where(sd > 1e-12, mu / sd, 0.0)
        return x.mean(axis=0)

    logits = []
    ranks = []
    for is_combo in itertools.combinations(range(S), half):
        is_rows = np.concatenate([groups[g] for g in is_combo])
        oos_rows = np.concatenate([groups[g] for g in range(S) if g not in is_combo])
        is_perf = perf(is_rows)
        oos_perf = perf(oos_rows)
        nstar = int(np.argmax(is_perf))
        # relative rank of the IS-best config among OOS configs (1=best)
        order = oos_perf.argsort()  # ascending
        rank = int(np.where(order == nstar)[0][0]) + 1  # 1..Ncfg, higher=better OOS
        omega = rank / (Ncfg + 1)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        logits.append(math.log(omega / (1 - omega)))
        ranks.append(rank / Ncfg)
    logits = np.array(logits)
    return {"pbo": float((logits < 0).mean()), "n_obs": int(T), "n_configs": int(Ncfg),
            "n_combos": int(len(logits)), "n_groups": int(S),
            "mean_oos_rank_of_ISbest": float(np.mean(ranks)),
            "median_logit": float(np.median(logits))}


# ── Deflated Sharpe Ratio ─────────────────────────────────────────────────────
def deflated_sharpe(returns: np.ndarray, n_trials: int, var_trials_sr: float = None):
    """
    returns: per-day return series of the SELECTED config. n_trials: number of configs
    tried (from the registry). Returns DSR = P(true SR > 0) after deflation.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    T = len(r)
    if T < 3:
        return {"dsr": float("nan"), "sr": float("nan"), "T": int(T), "reason": "too few days"}
    mu = r.mean(); sd = r.std(ddof=1)
    sr = mu / sd if sd > 1e-12 else 0.0
    g3 = float(stats.skew(r)); g4 = float(stats.kurtosis(r, fisher=False))
    # expected max Sharpe under the null across n_trials independent strategies
    if var_trials_sr is None:
        var_trials_sr = 1.0 / (T - 1)  # variance of SR estimate as a proxy spread
    gamma = 0.5772156649
    e = math.e
    N = max(int(n_trials), 2)
    z1 = stats.norm.ppf(1 - 1.0 / N)
    z2 = stats.norm.ppf(1 - 1.0 / (N * e))
    sr0 = math.sqrt(var_trials_sr) * ((1 - gamma) * z1 + gamma * z2)
    denom = math.sqrt(max(1 - g3 * sr + (g4 - 1) / 4.0 * sr * sr, 1e-9))
    dsr_stat = (sr - sr0) * math.sqrt(T - 1) / denom
    return {"dsr": float(stats.norm.cdf(dsr_stat)), "sr": float(sr), "sr0_haircut": float(sr0),
            "T": int(T), "skew": g3, "kurt": g4, "n_trials": int(N)}
