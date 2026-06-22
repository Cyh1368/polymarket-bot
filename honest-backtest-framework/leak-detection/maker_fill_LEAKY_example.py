#!/usr/bin/env python3
"""
Multi-coin profit backtest: extend BTC profit_backtest.py to ETH/SOL/XRP/DOGE/HYPE/BNB.
Same pre-registered config: logit_mid + hf_ret_60s, C=1.0, T=180s, tau_skip=0.005.
Maker fill check: up_best_bid crosses up_mid for YES; mirrored for NO.
Day-clustered wild-bootstrap CI (2000 reps, Rademacher weights per day).

Usage:
  kalshi/.venv-cli-trader/bin/python 2026-06-21-research/multicoin_backtest/multicoin_profit_backtest.py
"""
from __future__ import annotations
import math, glob, json, warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import logit as scipy_logit
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
import statsmodels.api as sm

warnings.filterwarnings("ignore")

# ── constants (same as BTC run) ────────────────────────────────────────────────
COINS        = ["BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB"]
HORIZON      = 180          # seconds before close
C_REG        = 1.0          # frozen from BTC walk-forward
FEATS        = ["logit_mid", "hf_ret_60s"]
TAU_SKIP     = 0.005
TAKER_FEE    = 0.01
MAKER_FEE    = 0.005
N_BOOT       = 2000
LOCKBOX_DAYS = {"2026-06-16","2026-06-17","2026-06-18","2026-06-19","2026-06-20"}
HF_DIR       = Path("kraken_hf")
PM_DIR       = Path("polymarket")
OUT_DIR      = Path("2026-06-21-research/multicoin_backtest")
RNG          = np.random.default_rng(42)

OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── fee / pnl helpers ──────────────────────────────────────────────────────────
def taker_fee_pct(p: float) -> float:
    return math.ceil(0.07 * p * (1 - p) * 100) / 100

def net_pnl_taker(ask: float, outcome: int, side: int) -> float:
    fee = taker_fee_pct(ask) + TAKER_FEE
    if side == 1:
        gross = (1.0 - ask) * outcome + (-ask) * (1 - outcome)
    else:
        gross = (1.0 - ask) * (1 - outcome) + (-ask) * outcome
    return gross - fee

def net_pnl_maker(limit: float, outcome: int, side: int) -> float:
    fee = taker_fee_pct(limit) + MAKER_FEE
    if side == 1:
        gross = (1.0 - limit) * outcome + (-limit) * (1 - outcome)
    else:
        gross = (1.0 - limit) * (1 - outcome) + (-limit) * outcome
    return gross - fee

# ── wild-cluster bootstrap CI ──────────────────────────────────────────────────
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

# ── load Kraken HF trades for a coin ──────────────────────────────────────────
def load_hf(coin: str) -> np.ndarray | None:
    """Returns sorted array of (kraken_ts_ms, price). None if file missing."""
    parts = []
    for suffix in ["_backfill", ""]:
        p = HF_DIR / f"trades_{coin}{suffix}.csv"
        if p.exists():
            try:
                df = pd.read_csv(p, usecols=["kraken_ts_ms", "price"])
                parts.append(df)
            except Exception:
                pass
    if not parts:
        return None
    hf = pd.concat(parts).drop_duplicates(subset=["kraken_ts_ms"]).sort_values("kraken_ts_ms")
    return hf[["kraken_ts_ms", "price"]].values  # (N, 2)

def hf_price_at(hf: np.ndarray, ts_ms: int, lookback_ms: int) -> float | None:
    """Last trade price at or before ts_ms - lookback_ms."""
    target = ts_ms - lookback_ms
    idx = np.searchsorted(hf[:, 0], target, side="right") - 1
    if idx < 0:
        return None
    return float(hf[idx, 1])

# ── load contracts for a coin ──────────────────────────────────────────────────
def load_contracts(coin: str) -> pd.DataFrame | None:
    data_dir = PM_DIR / f"data_{coin}_5m"
    outcomes_path = PM_DIR / f"polymarket_{coin.lower()}_5m_official_outcomes.csv"
    if not data_dir.exists() or not outcomes_path.exists():
        print(f"  {coin}: missing data dir or outcomes file")
        return None

    outcomes = pd.read_csv(outcomes_path)
    # up_won=1 means Up won (label=1), up_won=0 means Down won (label=0)
    outcomes = outcomes.dropna(subset=["up_won"])
    outcome_map = dict(zip(outcomes["condition_id"], outcomes["up_won"].astype(int)))

    USECOLS = ["sample_epoch_ms", "seconds_to_close", "condition_id",
               "up_best_bid", "up_best_ask", "up_mid",
               "down_best_bid", "down_best_ask", "down_mid",
               "event_end_utc"]
    frames = []
    for f in glob.glob(str(data_dir / "*.csv")):
        try:
            df = pd.read_csv(f, usecols=lambda c: c in USECOLS)
            cid = df["condition_id"].iloc[0] if "condition_id" in df.columns else None
            if cid and cid in outcome_map:
                df["label"] = int(outcome_map[cid])
                frames.append(df)
        except Exception:
            pass
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)

# ── build entry rows for a coin ───────────────────────────────────────────────
def build_entry_rows(coin: str, contracts: pd.DataFrame, hf: np.ndarray) -> pd.DataFrame:
    target_col = "seconds_to_close"
    rows = []
    for cid, cdf in contracts.groupby("condition_id"):
        cdf = cdf.sort_values("sample_epoch_ms")
        label = int(cdf["label"].iloc[0])

        # find snapshot closest to T=HORIZON seconds before close
        diffs = (cdf[target_col] - HORIZON).abs()
        if diffs.min() > 30:
            continue
        snap = cdf.loc[diffs.idxmin()]
        ts_ms = int(snap["sample_epoch_ms"])

        # compute HF feature
        p_now = hf_price_at(hf, ts_ms, 0)
        p_60  = hf_price_at(hf, ts_ms, 60_000)
        if p_now is None or p_60 is None or p_60 <= 0 or p_now <= 0:
            continue
        hf_ret_60s = float(np.log(p_now / p_60))

        ua = float(snap.get("up_best_ask", snap.get("up_mid", np.nan)))
        ub = float(snap.get("up_best_bid", snap.get("up_mid", np.nan)))
        um = float(snap.get("up_mid", np.nan))
        da = float(snap.get("down_best_ask", snap.get("down_mid", np.nan)))

        if not (0.01 < um < 0.99):
            continue

        # maker fill check: did up_best_bid ever reach up_mid in post-entry rows?
        post = cdf[cdf["sample_epoch_ms"] > ts_ms]
        maker_yes_fills = bool(len(post) > 0 and "up_best_bid" in post.columns and
                               (post["up_best_bid"] >= um).any())
        maker_no_fills  = bool(len(post) > 0 and "up_best_bid" in post.columns and
                               (post["up_best_bid"] <= (1 - um)).any())

        # close_date from event_end_utc or sample_epoch_ms
        if "event_end_utc" in snap.index:
            close_date = str(snap["event_end_utc"])[:10]
        else:
            close_date = pd.Timestamp(ts_ms, unit="ms", tz="UTC").strftime("%Y-%m-%d")

        rows.append({
            "condition_id": cid,
            "close_date": close_date,
            "sample_epoch_ms": ts_ms,
            "label": label,
            "up_ask": ua, "up_bid": ub, "up_mid": um, "down_ask": da,
            "hf_ret_60s": hf_ret_60s,
            "logit_mid": float(scipy_logit(np.clip(um, 1e-6, 1-1e-6))),
            "maker_yes_fills": maker_yes_fills,
            "maker_no_fills": maker_no_fills,
        })

    return pd.DataFrame(rows)

# ── run walk-forward for one coin ─────────────────────────────────────────────
def run_coin(coin: str, df: pd.DataFrame, split: str = "nonlockbox") -> dict:
    """split='nonlockbox' for walk-forward; 'lockbox' for final gate."""
    dates = sorted(df["close_date"].unique())

    if split == "nonlockbox":
        test_dates = [d for d in dates if d not in LOCKBOX_DAYS]
    else:
        test_dates = [d for d in dates if d in LOCKBOX_DAYS]

    t_pnl, t_day, m_pnl, m_day = [], [], [], []
    nyes = nno = nfill = ntrade = 0

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
            t_pnl.append(net_pnl_taker(ask, lbl, side))
            t_day.append(d)
            if side == 1: nyes += 1
            else: nno += 1

            fills = bool(row["maker_yes_fills"]) if side == 1 else bool(row["maker_no_fills"])
            if fills:
                nfill += 1
                limit = um if side == 1 else (1 - um)
                m_pnl.append(net_pnl_maker(limit, lbl, side))
                m_day.append(d)

    n_avail = len(df[df["close_date"].isin(test_dates)])
    t_mu, t_lo, t_hi = wild_ci(np.array(t_pnl), np.array(t_day))
    m_mu, m_lo, m_hi = wild_ci(np.array(m_pnl), np.array(m_day))

    return {
        "coin": coin, "split": split,
        "n_avail": n_avail, "n_traded": ntrade,
        "trade_rate": ntrade / max(n_avail, 1),
        "n_yes": nyes, "n_no": nno,
        "taker_mean": t_mu, "taker_lo": t_lo, "taker_hi": t_hi, "n_taker": len(t_pnl),
        "fill_rate": nfill / max(ntrade, 1), "n_fills": nfill,
        "maker_mean": m_mu, "maker_lo": m_lo, "maker_hi": m_hi, "n_maker": len(m_pnl),
    }

# ── main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"Multi-coin profit backtest  config: {FEATS}  C={C_REG}  T={HORIZON}s  tau={TAU_SKIP}\n")
    results = []

    for coin in COINS:
        print(f"── {coin} ──")
        hf = load_hf(coin)
        if hf is None:
            print(f"  no HF data — skipping\n")
            continue
        print(f"  HF trades: {len(hf):,}")

        contracts = load_contracts(coin)
        if contracts is None or len(contracts) == 0:
            print(f"  no contract data — skipping\n")
            continue
        print(f"  Contracts: {contracts['condition_id'].nunique():,}")

        entry = build_entry_rows(coin, contracts, hf)
        if len(entry) == 0:
            print(f"  no valid entry rows — skipping\n")
            continue
        print(f"  Entry rows: {len(entry):,}  dates: {entry['close_date'].nunique()}")

        # save entry rows
        entry.to_csv(OUT_DIR / f"entry_rows_{coin}.csv", index=False)

        # walk-forward (non-lockbox)
        wf = run_coin(coin, entry, split="nonlockbox")
        results.append(wf)
        print(f"  [WF]  taker {wf['taker_mean']:+.4f} [{wf['taker_lo']:+.4f},{wf['taker_hi']:+.4f}]  n={wf['n_taker']}")
        print(f"  [WF]  maker {wf['maker_mean']:+.4f} [{wf['maker_lo']:+.4f},{wf['maker_hi']:+.4f}]  fill={wf['fill_rate']*100:.1f}%  n={wf['n_maker']}")

        # lockbox
        lb = run_coin(coin, entry, split="lockbox")
        results.append(lb)
        print(f"  [LB]  taker {lb['taker_mean']:+.4f} [{lb['taker_lo']:+.4f},{lb['taker_hi']:+.4f}]  n={lb['n_taker']}")
        print(f"  [LB]  maker {lb['maker_mean']:+.4f} [{lb['maker_lo']:+.4f},{lb['maker_hi']:+.4f}]  fill={lb['fill_rate']*100:.1f}%  n={lb['n_maker']}")
        print()

    # summary table
    df_res = pd.DataFrame(results)
    df_res.to_csv(OUT_DIR / "results_summary.csv", index=False)

    print("\n=== SUMMARY (walk-forward, maker) ===")
    wf_only = df_res[df_res["split"] == "nonlockbox"].copy()
    wf_only["maker_ci_lo_pos"] = wf_only["maker_lo"] > 0
    for _, r in wf_only.sort_values("maker_mean", ascending=False).iterrows():
        flag = " ← CI_lo>0" if r["maker_lo"] > 0 else ""
        print(f"  {r['coin']:5s}  maker {r['maker_mean']:+.4f} [{r['maker_lo']:+.4f},{r['maker_hi']:+.4f}]  "
              f"fill={r['fill_rate']*100:.1f}%  n={int(r['n_maker'])}{flag}")

    print("\n=== LOCKBOX (maker) ===")
    lb_only = df_res[df_res["split"] == "lockbox"].copy()
    for _, r in lb_only.sort_values("maker_mean", ascending=False).iterrows():
        flag = " ← CI_lo>0" if r["maker_lo"] > 0 else ""
        print(f"  {r['coin']:5s}  maker {r['maker_mean']:+.4f} [{r['maker_lo']:+.4f},{r['maker_hi']:+.4f}]  "
              f"fill={r['fill_rate']*100:.1f}%  n={int(r['n_maker'])}{flag}")

    print(f"\nResults saved: {OUT_DIR}/results_summary.csv")


if __name__ == "__main__":
    main()
