#!/usr/bin/env python3
"""
2026-06-17-research/huber_common.py

Shared data-loading / feature-extraction / params for the Huber edge-regression model.
Identical logic to cv_band_and_huber.py so saved models and the sweep stay consistent.

The Huber model is TWO regressors:
  f_yes(x) ≈ realized YES return  =  y/c_yes − 1
  f_no(x)  ≈ realized NO  return  =  (1−y)/c_no − 1
Decision: trade the side with the higher predicted edge if it exceeds SKIP_BONUS.
"""
from __future__ import annotations
import math, re
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import pandas as pd

REPO_ROOT   = Path(__file__).resolve().parents[1]
COIN        = "BTC"
COST_ADD    = 0.01
HORIZON_TOL = 12.0
IND_WINDOW  = 60.0
T1          = 180
SKIP_BONUS  = 0.05
YES_GATE_LO = 0.25

# Baseline hyperparameters (the config the CV huber variant used)
BASE_CFG = {
    "max_depth": 3, "num_leaves": 7, "min_child_samples": 20,
    "lambda_l2": 5.0, "subsample": 0.90, "feature_fraction": 0.90,
    "learning_rate": 0.05, "n_rounds": 300, "huber_alpha": 1.0,
}

TAU_LEVELS = [1, 2, 3, 5, 7, 10, 15, 20]
TAU_LOG_X  = np.log(np.array(TAU_LEVELS) / 100.0)
TAU_COLS   = [f"up_book_imbalance_tau_{t}c" for t in TAU_LEVELS]

FEATURES = [
    "p_yes_mid", "yes_mid_z_60", "yes_mid_vol_60", "yes_mid_z_20", "yes_mid_vol_20",
    "mid_change_60", "book_qty_log", "OBI", "OBI_vol_60", "OBI_z_60",
    "spread_yes", "tod_sin", "tod_cos", "obi_depth_slope",
]
CLASS_YES, CLASS_NO, CLASS_SKIP = 0, 1, 2


def fnum(v):
    try:
        out = float(v); return out if math.isfinite(out) else None
    except Exception:
        return None

def series_stats(values, last=None):
    c = pd.to_numeric(values, errors="coerce").dropna()
    if c.empty: return {"z": 0.0, "vol": 0.0}
    lv   = float(c.iloc[-1] if last is None else last)
    mean = float(c.mean()); vol = float(c.std(ddof=0)) if len(c) > 1 else 0.0
    z    = (lv - mean) / vol if vol > 1e-12 else 0.0
    return {"z": z, "vol": vol}

def ols_slope(y_vals: np.ndarray) -> float:
    mask = np.isfinite(y_vals)
    if mask.sum() < 2: return float("nan")
    x = TAU_LOG_X[mask]; y = y_vals[mask]
    xm, ym = x.mean(), y.mean()
    denom = ((x - xm) ** 2).sum()
    if denom < 1e-12: return float("nan")
    return float(((x - xm) * (y - ym)).sum() / denom)


@dataclass(frozen=True)
class ContractData:
    slug: str; close_time: pd.Timestamp; label: int; df: pd.DataFrame

def load_data(data_dir=None, outcomes_path=None):
    data_dir = data_dir or (REPO_ROOT / "polymarket" / f"data_{COIN}_5m")
    outcomes_path = outcomes_path or (REPO_ROOT / "polymarket" / f"polymarket_{COIN.lower()}_5m_official_outcomes.csv")
    outcomes = {}
    for row in pd.read_csv(outcomes_path).to_dict(orient="records"):
        slug = str(row.get("market_slug") or "").strip()
        wo   = str(row.get("winning_outcome") or "").strip()
        if slug and wo in {"Up", "Down"}: outcomes[slug] = wo
    required = ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask", "seconds_to_close")
    contracts = []
    for path in sorted(data_dir.glob("*.csv")):
        slug = path.stem
        if "_5m_" in slug: slug = slug.split("_5m_", 1)[1]
        wo = outcomes.get(slug)
        if not wo: continue
        try: df = pd.read_csv(path, low_memory=False)
        except Exception: continue
        if df.empty or any(c not in df.columns for c in required): continue
        df = df.copy()
        df["_stc"] = pd.to_numeric(df["seconds_to_close"], errors="coerce")
        df["_ts"]  = pd.to_datetime(df.get("timestamp_utc"), utc=True, errors="coerce")
        df = df[df["_stc"].notna()]
        if df.empty: continue
        m = re.search(r"(\d{10})$", slug)
        if not m: continue
        ct = pd.Timestamp(int(m.group(1)) + 300, unit="s", tz="UTC")
        contracts.append(ContractData(slug=slug, close_time=ct,
                                      label=(1 if wo == "Up" else 0), df=df))
    contracts.sort(key=lambda c: c.close_time)
    return contracts

def extract_row(cd):
    df  = cd.df
    t1c = df[(df["_stc"] - T1).abs() <= HORIZON_TOL]
    if t1c.empty: return None
    t1r = t1c.loc[(t1c["_stc"] - T1).abs().idxmin()]
    ya = fnum(t1r.get("up_best_ask")); yb = fnum(t1r.get("up_best_bid"))
    na = fnum(t1r.get("down_best_ask")); nb = fnum(t1r.get("down_best_bid"))
    ubs = fnum(t1r.get("up_best_bid_size")) or 0.0
    dbs = fnum(t1r.get("down_best_bid_size")) or 0.0
    if any(v is None for v in (ya, yb, na, nb)): return None
    if not (0 < ya < 1 and 0 < na < 1 and yb <= ya and nb <= na): return None
    up_mid = (yb + ya) / 2.0
    t1_ts = t1r.get("_ts"); ts_ok = not pd.isna(t1_ts)
    if ts_ok:
        h60 = df[df["_ts"].notna() & (df["_ts"] <= t1_ts) &
                 ((t1_ts - df["_ts"]).dt.total_seconds() <= IND_WINDOW)]
        h20 = h60[(t1_ts - h60["_ts"]).dt.total_seconds() <= 20.0] if not h60.empty else h60
    else:
        h60 = h20 = pd.DataFrame()
    if h60.empty: h60 = t1r.to_frame().T
    if h20.empty: h20 = t1r.to_frame().T
    def _mids(h): return pd.to_numeric(h.get("up_mid", pd.Series(dtype=float)), errors="coerce")
    def _obis(h):
        u = pd.to_numeric(h.get("up_best_bid_size",   pd.Series(dtype=float)), errors="coerce").fillna(0)
        d = pd.to_numeric(h.get("down_best_bid_size", pd.Series(dtype=float)), errors="coerce").fillna(0)
        return (u - d) / (u + d + 1e-9)
    obi_cur = (ubs - dbs) / (ubs + dbs + 1e-9)
    ym60 = series_stats(_mids(h60), up_mid); ym20 = series_stats(_mids(h20), up_mid)
    ob60 = series_stats(_obis(h60), obi_cur)
    mids_60 = _mids(h60).dropna()
    mid_change = up_mid - float(mids_60.iloc[0]) if not mids_60.empty else 0.0
    if ts_ok:
        secs = t1_ts.hour * 3600 + t1_ts.minute * 60 + t1_ts.second
        tod_sin = math.sin(2*math.pi*secs/86400); tod_cos = math.cos(2*math.pi*secs/86400)
    else:
        tod_sin = tod_cos = 0.0
    tau_vals = np.array([fnum(t1r.get(c)) for c in TAU_COLS], dtype=float)
    rd = {
        "contract_id": cd.slug, "close_time": cd.close_time,
        "y_settle": cd.label, "c_yes": ya + COST_ADD, "c_no": na + COST_ADD,
        "up_ask": ya, "down_ask": na, "p_yes_mid": up_mid,
        "yes_mid_z_60": ym60["z"], "yes_mid_vol_60": ym60["vol"],
        "yes_mid_z_20": ym20["z"], "yes_mid_vol_20": ym20["vol"],
        "mid_change_60": mid_change, "book_qty_log": math.log1p(ubs + dbs),
        "OBI": obi_cur, "OBI_vol_60": ob60["vol"], "OBI_z_60": ob60["z"],
        "spread_yes": ya - yb, "tod_sin": tod_sin, "tod_cos": tod_cos,
        "obi_depth_slope": ols_slope(tau_vals),
    }
    feats_ok = [f for f in FEATURES if f != "obi_depth_slope"]
    if any(not math.isfinite(float(rd.get(f, float("nan")))) for f in feats_ok):
        return None
    return rd


def build_frame() -> pd.DataFrame:
    contracts = load_data()
    rows = [extract_row(cd) for cd in contracts]
    rows = [r for r in rows if r is not None]
    return pd.DataFrame(rows)


def huber_params(cfg: dict, seed: int = 0) -> dict:
    return {"objective": "huber", "alpha": cfg["huber_alpha"], "metric": "huber",
            "num_leaves": cfg["num_leaves"], "max_depth": cfg["max_depth"],
            "min_child_samples": cfg["min_child_samples"], "subsample": cfg["subsample"],
            "feature_fraction": cfg["feature_fraction"], "lambda_l2": cfg["lambda_l2"],
            "lambda_l1": 0.0, "learning_rate": cfg["learning_rate"],
            "num_threads": 4, "seed": seed, "verbose": -1}


def decide_huber(pred_yes, pred_no, p_mid):
    """Edge rule (no band): trade higher predicted edge above SKIP_BONUS, Filter B on YES."""
    allow_yes = p_mid >= YES_GATE_LO
    if pred_no > SKIP_BONUS and pred_no >= pred_yes:
        return CLASS_NO
    if allow_yes and pred_yes > SKIP_BONUS and pred_yes > pred_no:
        return CLASS_YES
    return CLASS_SKIP


def pnl_of(side, y, ya, na):
    if side == CLASS_YES: return y / max(ya, 1e-6) - 1.0
    if side == CLASS_NO:  return (1.0 - y) / max(na, 1e-6) - 1.0
    return None
