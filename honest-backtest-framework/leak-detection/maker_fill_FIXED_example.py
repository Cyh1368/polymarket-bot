#!/usr/bin/env python3
"""
Leak-fixed rerun of multicoin_profit_backtest.py.

The original maker P&L conditioned each trade's "fill" on POST-ENTRY price path
(up_best_bid crossing up_mid after entry). That fill flag is correlated with the
eventual settlement outcome (XRP: Up-won = 0.26 when NO order "fills", 0.997 when
it doesn't), so the maker edge was a look-ahead/selection artifact.

Fix: drop the future-path fill check entirely. A maker limit order posted at the
entry mid is assumed filled for EVERY selected trade (optimistic on fill rate, but
unbiased w.r.t. outcome). Everything else — model, split, fees, bootstrap — is
identical to the original. Entry rows are reused from entry_rows_<coin>.csv.

Usage:
  kalshi/.venv-cli-trader/bin/python 2026-06-21-research/multicoin_backtest/multicoin_profit_backtest_nofuture.py
"""
from __future__ import annotations
import math, warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

COINS        = ["BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB"]
C_REG        = 1.0
FEATS        = ["logit_mid", "hf_ret_60s"]
TAU_SKIP     = 0.005
TAKER_FEE    = 0.01
MAKER_FEE    = 0.005
N_BOOT       = 2000
LOCKBOX_DAYS = {"2026-06-16","2026-06-17","2026-06-18","2026-06-19","2026-06-20"}
OUT_DIR      = Path("2026-06-21-research/multicoin_backtest")
RNG          = np.random.default_rng(42)


def taker_fee_pct(p: float) -> float:
    return math.ceil(0.07 * p * (1 - p) * 100) / 100

def net_pnl(price: float, outcome: int, side: int, fee_flat: float) -> float:
    fee = taker_fee_pct(price) + fee_flat
    if side == 1:
        gross = (1.0 - price) * outcome + (-price) * (1 - outcome)
    else:
        gross = (1.0 - price) * (1 - outcome) + (-price) * outcome
    return gross - fee

def wild_ci(vals: np.ndarray, days: np.ndarray, n: int = N_BOOT):
    if len(vals) == 0:
        return float("nan"), float("nan"), float("nan")
    mu = float(np.mean(vals))
    resid = vals - mu
    ud = np.unique(days)
    boots = []
    for _ in range(n):
        ws = {d: w for d, w in zip(ud, RNG.choice([-1., 1.], len(ud)))}
        boots.append(mu + np.mean(resid * np.array([ws[d] for d in days])))
    boots = np.array(boots)
    return mu, float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def run_coin(coin: str, df: pd.DataFrame, split: str) -> dict:
    dates = sorted(df["close_date"].unique())
    if split == "nonlockbox":
        test_dates = [d for d in dates if d not in LOCKBOX_DAYS]
    else:
        test_dates = [d for d in dates if d in LOCKBOX_DAYS]

    t_pnl, t_day, m_pnl, m_day = [], [], [], []
    nyes = nno = ntrade = 0

    for d in test_dates:
        if split == "nonlockbox":
            train = df[df["close_date"] < d].dropna(subset=FEATS + ["label"])
        else:
            train = df[~df["close_date"].isin(LOCKBOX_DAYS)].dropna(subset=FEATS + ["label"])
        test = df[df["close_date"] == d].dropna(subset=FEATS + ["label"])
        if len(train) < 20 or len(test) == 0:
            continue

        scaler = StandardScaler()
        Xtr = scaler.fit_transform(train[FEATS].values)
        Xte = scaler.transform(test[FEATS].values)
        model = LogisticRegression(C=C_REG, max_iter=500, solver="lbfgs")
        model.fit(Xtr, train["label"].astype(int).values)
        p_up = model.predict_proba(Xte)[:, 1]

        for j, (_, row) in enumerate(test.iterrows()):
            p = float(p_up[j]); lbl = int(row["label"])
            ua = float(row["up_ask"]); da = float(row["down_ask"]); um = float(row["up_mid"])
            if not (0 < ua < 1 and 0 < da < 1):
                continue
            ev_yes = p - (ua + TAKER_FEE)
            ev_no  = (1 - p) - (da + TAKER_FEE)
            if ev_yes >= ev_no and ev_yes > TAU_SKIP:
                side = 1
            elif ev_no > ev_yes and ev_no > TAU_SKIP:
                side = -1
            else:
                continue

            ntrade += 1
            ask = ua if side == 1 else da
            t_pnl.append(net_pnl(ask, lbl, side, TAKER_FEE)); t_day.append(d)
            if side == 1: nyes += 1
            else: nno += 1

            # LEAK FIX: assume maker order at entry mid always fills; no future peek
            limit = um if side == 1 else (1 - um)
            m_pnl.append(net_pnl(limit, lbl, side, MAKER_FEE)); m_day.append(d)

    n_avail = len(df[df["close_date"].isin(test_dates)])
    t_mu, t_lo, t_hi = wild_ci(np.array(t_pnl), np.array(t_day))
    m_mu, m_lo, m_hi = wild_ci(np.array(m_pnl), np.array(m_day))
    return {
        "coin": coin, "split": split, "n_avail": n_avail, "n_traded": ntrade,
        "trade_rate": ntrade / max(n_avail, 1), "n_yes": nyes, "n_no": nno,
        "taker_mean": t_mu, "taker_lo": t_lo, "taker_hi": t_hi, "n_taker": len(t_pnl),
        "maker_mean": m_mu, "maker_lo": m_lo, "maker_hi": m_hi, "n_maker": len(m_pnl),
    }


def main():
    print(f"Leak-fixed multi-coin backtest  config: {FEATS}  C={C_REG}  tau={TAU_SKIP}")
    print("Maker = fill-all-at-mid (no future-path selection)\n")
    results = []
    for coin in COINS:
        path = OUT_DIR / f"entry_rows_{coin}.csv"
        if not path.exists():
            print(f"  {coin}: no entry rows — skipping"); continue
        entry = pd.read_csv(path)
        for split in ("nonlockbox", "lockbox"):
            results.append(run_coin(coin, entry, split))

    df_res = pd.DataFrame(results)
    df_res.to_csv(OUT_DIR / "results_summary_nofuture.csv", index=False)

    for split, title in (("nonlockbox", "WALK-FORWARD"), ("lockbox", "LOCKBOX")):
        sub = df_res[df_res["split"] == split].sort_values("maker_mean", ascending=False)
        print(f"=== {title} ===")
        print(f"  {'coin':5s} {'taker':>26s}   {'maker':>26s}   {'n':>5s} {'yes/no':>9s}")
        for _, r in sub.iterrows():
            flag = " <-CI_lo>0" if r["maker_lo"] > 0 else ""
            tk = f"{r['taker_mean']:+.4f}[{r['taker_lo']:+.3f},{r['taker_hi']:+.3f}]"
            mk = f"{r['maker_mean']:+.4f}[{r['maker_lo']:+.3f},{r['maker_hi']:+.3f}]"
            print(f"  {r['coin']:5s} {tk:>26s}   {mk:>26s}   {int(r['n_maker']):5d} {int(r['n_yes'])}/{int(r['n_no'])}{flag}")
        print()
    print(f"Saved: {OUT_DIR}/results_summary_nofuture.csv")


if __name__ == "__main__":
    main()
