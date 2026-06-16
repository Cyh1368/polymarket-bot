#!/usr/bin/env python3
"""
polymarket/train_lgb_v3.py

Train and save the production LightGBM v3 binary classifier.
Trains on ALL available BTC 5m contracts at T1=180s — no held-out split.
Output: polymarket/lgb_v3_t180.txt  (LightGBM native text format)

Uses identical features, hyperparameters, and extraction logic as:
  2026-06-14-research/settlement_lgb_v3.py
  2026-06-15-research/settlement_lgb_v3_200seed.py
"""
from __future__ import annotations
import math, re
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import pandas as pd
import lightgbm as lgb

APP_DIR   = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

DATA_DIR      = APP_DIR / "data_BTC_5m"
OUTCOMES_PATH = APP_DIR / "polymarket_btc_5m_official_outcomes.csv"
MODEL_OUT     = APP_DIR / "lgb_v3_t180.txt"

T1          = 180
HORIZON_TOL = 12.0
IND_WINDOW  = 60.0
COST_ADD    = 0.01
LGB_SEED    = 42

FEATURES = [
    "p_yes_mid",
    "yes_mid_z_60", "yes_mid_vol_60",
    "yes_mid_z_20", "yes_mid_vol_20",
    "mid_change_60",
    "book_qty_log",
    "OBI", "OBI_vol_60", "OBI_z_60",
    "spread_yes",
    "tod_sin", "tod_cos",
]

CFG = {
    "max_depth": 3, "num_leaves": 7, "min_child_samples": 20,
    "lambda_l2": 5.0, "subsample": 0.90, "feature_fraction": 0.90,
    "learning_rate": 0.05, "n_rounds": 300,
}


def fnum(v):
    try:
        out = float(v)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def series_stats(values, last=None):
    c = pd.to_numeric(values, errors="coerce").dropna()
    if c.empty:
        return {"z": 0.0, "vol": 0.0}
    lv   = float(c.iloc[-1] if last is None else last)
    mean = float(c.mean())
    vol  = float(c.std(ddof=0)) if len(c) > 1 else 0.0
    z    = (lv - mean) / vol if vol > 1e-12 else 0.0
    return {"z": z, "vol": vol}


@dataclass(frozen=True)
class ContractData:
    slug: str; close_time: pd.Timestamp; label: int; df: pd.DataFrame


def load_data() -> list[ContractData]:
    outcomes: dict[str, str] = {}
    for row in pd.read_csv(OUTCOMES_PATH).to_dict(orient="records"):
        slug = str(row.get("market_slug") or "").strip()
        wo   = str(row.get("winning_outcome") or "").strip()
        if slug and wo in {"Up", "Down"}:
            outcomes[slug] = wo

    required = ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask", "seconds_to_close")
    contracts: list[ContractData] = []
    for path in sorted(DATA_DIR.glob("*.csv")):
        slug = path.stem
        if "_5m_" in slug:
            slug = slug.split("_5m_", 1)[1]
        wo = outcomes.get(slug)
        if not wo:
            continue
        try:
            df = pd.read_csv(path, low_memory=False)
        except Exception:
            continue
        if df.empty or any(c not in df.columns for c in required):
            continue
        df = df.copy()
        df["_stc"] = pd.to_numeric(df["seconds_to_close"], errors="coerce")
        df["_ts"]  = pd.to_datetime(df.get("timestamp_utc"), utc=True, errors="coerce")
        df = df[df["_stc"].notna()]
        if df.empty:
            continue
        m = re.search(r"(\d{10})$", slug)
        if not m:
            continue
        close_time = pd.Timestamp(int(m.group(1)) + 300, unit="s", tz="UTC")
        contracts.append(ContractData(
            slug=slug, close_time=close_time,
            label=(1 if wo == "Up" else 0), df=df,
        ))
    contracts.sort(key=lambda c: c.close_time)
    return contracts


def extract_row(cd: ContractData) -> dict | None:
    df  = cd.df
    t1c = df[(df["_stc"] - T1).abs() <= HORIZON_TOL]
    if t1c.empty:
        return None
    t1r = t1c.loc[(t1c["_stc"] - T1).abs().idxmin()]

    ya  = fnum(t1r.get("up_best_ask"));   yb  = fnum(t1r.get("up_best_bid"))
    na  = fnum(t1r.get("down_best_ask")); nb  = fnum(t1r.get("down_best_bid"))
    ubs = fnum(t1r.get("up_best_bid_size"))   or 0.0
    dbs = fnum(t1r.get("down_best_bid_size")) or 0.0

    if any(v is None for v in (ya, yb, na, nb)):
        return None
    if not (0 < ya < 1 and 0 < na < 1 and yb <= ya and nb <= na):
        return None

    up_mid = (yb + ya) / 2.0
    t1_ts  = t1r.get("_ts")
    ts_ok  = not pd.isna(t1_ts)

    if ts_ok:
        h60 = df[df["_ts"].notna() & (df["_ts"] <= t1_ts) &
                 ((t1_ts - df["_ts"]).dt.total_seconds() <= IND_WINDOW)]
        h20 = h60[(t1_ts - h60["_ts"]).dt.total_seconds() <= 20.0] if not h60.empty else h60
    else:
        h60 = h20 = pd.DataFrame()

    if h60.empty: h60 = t1r.to_frame().T
    if h20.empty: h20 = t1r.to_frame().T

    def _mids(h):
        return pd.to_numeric(h.get("up_mid", pd.Series(dtype=float)), errors="coerce")

    def _obis(h):
        u = pd.to_numeric(h.get("up_best_bid_size",   pd.Series(dtype=float)), errors="coerce").fillna(0)
        d = pd.to_numeric(h.get("down_best_bid_size", pd.Series(dtype=float)), errors="coerce").fillna(0)
        return (u - d) / (u + d + 1e-9)

    obi_cur = (ubs - dbs) / (ubs + dbs + 1e-9)
    ym60    = series_stats(_mids(h60), up_mid)
    ym20    = series_stats(_mids(h20), up_mid)
    ob60    = series_stats(_obis(h60), obi_cur)

    mids_60    = _mids(h60).dropna()
    mid_change = up_mid - float(mids_60.iloc[0]) if not mids_60.empty else 0.0

    if ts_ok:
        secs    = t1_ts.hour * 3600 + t1_ts.minute * 60 + t1_ts.second
        tod_sin = math.sin(2 * math.pi * secs / 86400)
        tod_cos = math.cos(2 * math.pi * secs / 86400)
    else:
        tod_sin = tod_cos = 0.0

    rd = {
        "y_settle":       cd.label,
        "p_yes_mid":      up_mid,
        "yes_mid_z_60":   ym60["z"],
        "yes_mid_vol_60": ym60["vol"],
        "yes_mid_z_20":   ym20["z"],
        "yes_mid_vol_20": ym20["vol"],
        "mid_change_60":  mid_change,
        "book_qty_log":   math.log1p(ubs + dbs),
        "OBI":            obi_cur,
        "OBI_vol_60":     ob60["vol"],
        "OBI_z_60":       ob60["z"],
        "spread_yes":     ya - yb,
        "tod_sin":        tod_sin,
        "tod_cos":        tod_cos,
    }
    if any(not math.isfinite(float(rd.get(f, float("nan")))) for f in FEATURES):
        return None
    return rd


def main():
    print("Loading BTC 5m contracts…")
    contracts = load_data()
    print(f"  {len(contracts)} contracts with official outcomes")

    rows = [extract_row(cd) for cd in contracts]
    rows = [r for r in rows if r is not None]
    df   = pd.DataFrame(rows)
    print(f"  {len(df)} rows extracted at T1={T1}s  "
          f"UP={int(df['y_settle'].sum())}  DOWN={int((df['y_settle']==0).sum())}")

    X = df[FEATURES].to_numpy().astype(float)
    y = df["y_settle"].astype(float).to_numpy()

    params = {
        "objective":         "binary",
        "metric":            "binary_logloss",
        "num_leaves":        CFG["num_leaves"],
        "max_depth":         CFG["max_depth"],
        "min_child_samples": CFG["min_child_samples"],
        "subsample":         CFG["subsample"],
        "feature_fraction":  CFG["feature_fraction"],
        "lambda_l2":         CFG["lambda_l2"],
        "lambda_l1":         0.0,
        "learning_rate":     CFG["learning_rate"],
        "num_threads":       4,
        "seed":              LGB_SEED,
        "verbose":           -1,
        "is_unbalance":      False,
    }

    print(f"\nTraining on all {len(df)} contracts (no split — production model)…")
    ds = lgb.Dataset(X, label=y, feature_name=FEATURES, free_raw_data=False)
    model = lgb.train(
        params,
        train_set=ds,
        num_boost_round=CFG["n_rounds"],
        valid_sets=[ds],
        callbacks=[lgb.log_evaluation(period=50)],
    )

    model.save_model(str(MODEL_OUT))
    print(f"\nModel saved → {MODEL_OUT}")

    # Quick sanity check
    p_up = model.predict(X)
    print(f"P(UP) stats: min={p_up.min():.3f}  max={p_up.max():.3f}  mean={p_up.mean():.3f}")
    fi = sorted(zip(FEATURES, model.feature_importance(importance_type="gain")), key=lambda x: -x[1])
    print("Feature importance (gain, top 6):")
    for feat, gain in fi[:6]:
        print(f"  {feat:20s}  {gain:.0f}")


if __name__ == "__main__":
    main()
