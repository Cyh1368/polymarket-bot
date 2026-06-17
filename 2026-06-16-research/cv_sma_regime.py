#!/usr/bin/env python3
"""
2026-06-16-research/cv_sma_regime.py

5-fold chronological CV for v3.1 with SMA-based regime labels.

Regime definition (replaces point-to-point 4h return):
  signal = (spot_now - mean(spot over last 4h)) / mean(spot over last 4h)
  UP   if signal >  REGIME_THRESH
  DOWN if signal < -REGIME_THRESH
  FLAT otherwise

No UP/DOWN filters applied — v3.1 trades all regimes freely.
Goal: find which regime(s) have reliable EV to inform when to trade.
"""
from __future__ import annotations
import math, re
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd
import lightgbm as lgb

REPO_ROOT     = Path(__file__).resolve().parents[1]
OUT_DIR       = Path(__file__).parent / "regime_model_results"
OUT_DIR.mkdir(exist_ok=True)

COIN          = "BTC"
COST_ADD      = 0.01
HORIZON_TOL   = 12.0
IND_WINDOW    = 60.0
MIN_TRAIN     = 50
T1_FOCUS      = 180
SKIP_BONUS    = 0.05
YES_GATE_LO   = 0.25
REGIME_THRESH = 0.003
CLASS_YES, CLASS_NO, CLASS_SKIP = 0, 1, 2
N_BOOT        = 5_000

TAU_LEVELS = [1, 2, 3, 5, 7, 10, 15, 20]
TAU_LOG_X  = np.log(np.array(TAU_LEVELS) / 100.0)
TAU_COLS   = [f"up_book_imbalance_tau_{t}c" for t in TAU_LEVELS]

V31_FEATURES = [
    "p_yes_mid",
    "yes_mid_z_60", "yes_mid_vol_60",
    "yes_mid_z_20", "yes_mid_vol_20",
    "mid_change_60",
    "book_qty_log",
    "OBI", "OBI_vol_60", "OBI_z_60",
    "spread_yes",
    "tod_sin", "tod_cos",
    "obi_depth_slope",
]

CFG = {
    "max_depth": 3, "num_leaves": 7, "min_child_samples": 20,
    "lambda_l2": 5.0, "subsample": 0.90, "feature_fraction": 0.90,
    "learning_rate": 0.05, "n_rounds": 300,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def fnum(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
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


def ols_slope(y_vals: np.ndarray) -> float:
    mask = np.isfinite(y_vals)
    if mask.sum() < 2:
        return float("nan")
    x = TAU_LOG_X[mask]; y = y_vals[mask]
    xm = x.mean(); ym = y.mean()
    denom = ((x - xm) ** 2).sum()
    if denom < 1e-12:
        return float("nan")
    return float(((x - xm) * (y - ym)).sum() / denom)


def assign_regime(signal: float) -> str:
    if signal > REGIME_THRESH:  return "UP"
    if signal < -REGIME_THRESH: return "DOWN"
    return "FLAT"


# ── Spot timeline ─────────────────────────────────────────────────────────────

def build_spot_timeline(data_dir: Path) -> pd.DataFrame:
    parts = []
    for path in data_dir.glob("*.csv"):
        try:
            df = pd.read_csv(path, usecols=["timestamp_utc", "spot_price"], low_memory=False)
            df["ts"]   = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
            df["spot"] = pd.to_numeric(df["spot_price"], errors="coerce")
            parts.append(df[["ts", "spot"]].dropna())
        except Exception:
            pass
    if not parts:
        return pd.DataFrame(columns=["ts", "spot"])
    return pd.concat(parts).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def lookup_spot_at(spot_df: pd.DataFrame, ts: pd.Timestamp) -> float | None:
    idx = spot_df["ts"].searchsorted(ts)
    if idx >= len(spot_df):
        return None
    v = spot_df.iloc[idx]["spot"]
    return float(v) if pd.notna(v) and v > 0 else None


def compute_sma_regime(spot_df: pd.DataFrame, ts_now: pd.Timestamp,
                        window_hours: float = 4.0) -> str:
    """
    Compare current spot to the mean of the last `window_hours` of spot prices.
    signal = (spot_now - sma) / sma
    This is smoother than a point-to-point return because the SMA absorbs
    short-term noise across the full lookback window.
    """
    spot_now = lookup_spot_at(spot_df, ts_now)
    if spot_now is None:
        return "FLAT"

    t_start = ts_now - pd.Timedelta(hours=window_hours)
    mask    = (spot_df["ts"] >= t_start) & (spot_df["ts"] <= ts_now)
    window  = spot_df[mask]["spot"].dropna()
    if window.empty:
        return "FLAT"

    sma = float(window.mean())
    if sma <= 0:
        return "FLAT"

    signal = (spot_now - sma) / sma
    return assign_regime(signal)


# ── Data loading ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContractData:
    slug: str
    close_time: pd.Timestamp
    label: int
    df: pd.DataFrame


def load_data(data_dir: Path, outcomes_path: Path) -> list[ContractData]:
    outcomes: dict[str, str] = {}
    for row in pd.read_csv(outcomes_path).to_dict(orient="records"):
        slug = str(row.get("market_slug") or "").strip()
        wo   = str(row.get("winning_outcome") or "").strip()
        if slug and wo in {"Up", "Down"}:
            outcomes[slug] = wo

    required = ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask", "seconds_to_close")
    contracts: list[ContractData] = []
    for path in sorted(data_dir.glob("*.csv")):
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
            label=(1 if wo == "Up" else 0), df=df))
    contracts.sort(key=lambda c: c.close_time)
    return contracts


def extract_row(cd: ContractData, T1: int, spot_df: pd.DataFrame) -> dict | None:
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

    def _mids(h): return pd.to_numeric(h.get("up_mid", pd.Series(dtype=float)), errors="coerce")
    def _obis(h):
        u = pd.to_numeric(h.get("up_best_bid_size",   pd.Series(dtype=float)), errors="coerce").fillna(0)
        d = pd.to_numeric(h.get("down_best_bid_size", pd.Series(dtype=float)), errors="coerce").fillna(0)
        return (u - d) / (u + d + 1e-9)

    obi_cur    = (ubs - dbs) / (ubs + dbs + 1e-9)
    ym60       = series_stats(_mids(h60), up_mid)
    ym20       = series_stats(_mids(h20), up_mid)
    ob60       = series_stats(_obis(h60), obi_cur)
    mids_60    = _mids(h60).dropna()
    mid_change = up_mid - float(mids_60.iloc[0]) if not mids_60.empty else 0.0

    if ts_ok:
        secs    = t1_ts.hour * 3600 + t1_ts.minute * 60 + t1_ts.second
        tod_sin = math.sin(2 * math.pi * secs / 86400)
        tod_cos = math.cos(2 * math.pi * secs / 86400)
    else:
        tod_sin = tod_cos = 0.0

    tau_vals = np.array([fnum(t1r.get(c)) for c in TAU_COLS], dtype=float)
    slope    = ols_slope(tau_vals)

    regime_sma = "FLAT"
    if ts_ok and not spot_df.empty:
        regime_sma = compute_sma_regime(spot_df, t1_ts, window_hours=4.0)

    base_feats = [f for f in V31_FEATURES if f != "obi_depth_slope"]
    rd = {
        "contract_id":    cd.slug,
        "close_time":     cd.close_time,
        "y_settle":       cd.label,
        "c_yes":          ya + COST_ADD,
        "c_no":           na + COST_ADD,
        "up_ask":         ya,
        "down_ask":       na,
        "p_yes_mid":      up_mid,
        "yes_mid_z_60":   ym60["z"],   "yes_mid_vol_60": ym60["vol"],
        "yes_mid_z_20":   ym20["z"],   "yes_mid_vol_20": ym20["vol"],
        "mid_change_60":  mid_change,
        "book_qty_log":   math.log1p(ubs + dbs),
        "OBI":            obi_cur,
        "OBI_vol_60":     ob60["vol"], "OBI_z_60":       ob60["z"],
        "spread_yes":     ya - yb,
        "tod_sin":        tod_sin,     "tod_cos":        tod_cos,
        "obi_depth_slope": slope,
        "regime_sma":     regime_sma,
    }
    if any(not math.isfinite(float(rd.get(f, float("nan")))) for f in base_feats):
        return None
    return rd


def build_df(contracts: list[ContractData], T1: int,
             spot_df: pd.DataFrame) -> tuple[list[str], pd.DataFrame]:
    rows, seen, cids = [], set(), []
    for cd in contracts:
        r = extract_row(cd, T1, spot_df)
        if r is not None:
            rows.append(r)
            if cd.slug not in seen:
                seen.add(cd.slug); cids.append(cd.slug)
    return (cids, pd.DataFrame(rows)) if rows else ([], pd.DataFrame())


# ── Model ─────────────────────────────────────────────────────────────────────

def _lgb_params(seed: int) -> dict:
    return {
        "objective": "binary", "metric": "binary_logloss",
        "num_leaves": CFG["num_leaves"], "max_depth": CFG["max_depth"],
        "min_child_samples": CFG["min_child_samples"],
        "subsample": CFG["subsample"], "feature_fraction": CFG["feature_fraction"],
        "lambda_l2": CFG["lambda_l2"], "lambda_l1": 0.0,
        "learning_rate": CFG["learning_rate"], "num_threads": 4,
        "seed": seed, "verbose": -1, "is_unbalance": False,
    }


def train_model(tr: pd.DataFrame, seed: int):
    y_tr = tr["y_settle"].astype(float).to_numpy()
    if len(tr) < MIN_TRAIN or y_tr.std() < 1e-9:
        return None
    ds = lgb.Dataset(tr[V31_FEATURES].to_numpy().astype(float), label=y_tr, free_raw_data=False)
    return lgb.train(_lgb_params(seed), train_set=ds, num_boost_round=CFG["n_rounds"],
                     valid_sets=[ds], callbacks=[lgb.log_evaluation(period=9999)])


def decide(p_up: float, c_yes: float, c_no: float, p_mid: float) -> int:
    p_down = 1.0 - p_up
    ev_yes = p_up   / max(c_yes, 1e-6) - 1.0
    ev_no  = p_down / max(c_no,  1e-6) - 1.0
    allow_yes = p_mid >= YES_GATE_LO
    if ev_no  > SKIP_BONUS and ev_no  >= ev_yes: return CLASS_NO
    if allow_yes and ev_yes > SKIP_BONUS and ev_yes > ev_no: return CLASS_YES
    return CLASS_SKIP


def model_pnl(pc: int, row: dict) -> float | None:
    y = float(row["y_settle"])
    ya = float(row["up_ask"]); na = float(row["down_ask"])
    if pc == CLASS_YES: return y / max(ya, 1e-6) - 1.0
    if pc == CLASS_NO:  return (1.0 - y) / max(na, 1e-6) - 1.0
    return None


# ── 5-fold chronological CV ───────────────────────────────────────────────────

def run_cv(df: pd.DataFrame, cids: list) -> pd.DataFrame:
    n         = len(cids)
    fold_size = n // 5
    ordered   = cids
    df_s      = df[df["contract_id"].isin(set(cids))].copy()

    records = []

    for fold in range(1, 5):
        tr_end = (fold + 1) * fold_size
        te_end = min(tr_end + fold_size, n)
        tr_ids = set(ordered[:tr_end])
        te_ids = set(ordered[tr_end:te_end])
        if len(tr_ids) < MIN_TRAIN or not te_ids:
            continue

        tr = df_s[df_s["contract_id"].isin(tr_ids)]
        te = df_s[df_s["contract_id"].isin(te_ids)]

        model = train_model(tr, 42)
        if model is None:
            continue

        x_te     = te[V31_FEATURES].to_numpy().astype(float)
        p_up_arr = model.predict(x_te)

        for i, (_, row) in enumerate(te.iterrows()):
            rd     = row.to_dict()
            action = decide(float(p_up_arr[i]), float(rd["c_yes"]), float(rd["c_no"]), float(rd["p_yes_mid"]))
            pnl    = model_pnl(action, rd)
            records.append({
                "fold":        fold,
                "contract_id": rd["contract_id"],
                "regime_sma":  rd["regime_sma"],
                "action":      {CLASS_YES: "YES", CLASS_NO: "NO", CLASS_SKIP: "SKIP"}[action],
                "pnl":         pnl,
                "y_settle":    rd["y_settle"],
            })

        n_te    = len(te_ids)
        all_pnl = [r["pnl"] for r in records if r["fold"] == fold and r["pnl"] is not None]
        reg_dist = tr["regime_sma"].value_counts().to_dict()
        print(f"Fold {fold}: train={len(tr_ids)} {reg_dist}  test={n_te}  "
              f"EV/avail={sum(all_pnl)/n_te:+.4f}  n_trades={len(all_pnl)}")

    return pd.DataFrame(records)


def summarise(rec: pd.DataFrame):
    n_total = len(rec)
    traded  = rec[rec["pnl"].notna()]
    all_pnl = traded["pnl"].to_numpy()

    print(f"\n{'='*68}")
    print(f"CV AGGREGATE — v3.1, no regime filter, SMA regime labels")
    print(f"signal = (spot_now - mean(spot[-4h:now])) / mean(spot[-4h:now])")
    print(f"threshold = ±{REGIME_THRESH}")
    print(f"{'='*68}")
    print(f"Total test contracts : {n_total}")
    print(f"Trades / skips       : {len(traded)} / {n_total - len(traded)}")
    print(f"Overall EV/avail     : {all_pnl.sum()/n_total:+.4f}  ({100*all_pnl.sum()/n_total:+.2f}%)")
    print(f"Win rate             : {(all_pnl > 0).mean():.3f}")

    rng = np.random.default_rng(0)

    print(f"\n{'─'*68}")
    print(f"Per-regime breakdown (SMA-based):")
    print(f"{'─'*68}")
    hdr = (f"{'Regime':8s} {'n_avail':>8} {'n_trade':>8} {'n_yes':>6} {'n_no':>6} "
           f"{'EV/avail':>10} {'EV/trig':>10} {'WR':>7} {'CI95_lo':>9} {'CI95_hi':>9}")
    print(hdr)

    rows_out = []
    for reg in ["UP", "FLAT", "DOWN", "ALL"]:
        sub  = rec if reg == "ALL" else rec[rec["regime_sma"] == reg]
        n_av = len(sub)
        if n_av == 0:
            continue
        tr_   = sub[sub["pnl"].notna()]
        n_tr  = len(tr_)
        n_yes = (tr_["action"] == "YES").sum()
        n_no  = (tr_["action"] == "NO").sum()
        pnl   = tr_["pnl"].to_numpy()

        ev_av = pnl.sum() / n_av
        ev_tr = pnl.mean() if n_tr > 0 else float("nan")
        wr    = (pnl > 0).mean() if n_tr > 0 else float("nan")

        padded = sub["pnl"].fillna(0).to_numpy()
        boots  = [padded[rng.integers(0, n_av, size=n_av)].sum() / n_av for _ in range(N_BOOT)]
        ci_lo, ci_hi = np.percentile(boots, [2.5, 97.5])

        print(f"{reg:8s} {n_av:>8d} {n_tr:>8d} {n_yes:>6d} {n_no:>6d} "
              f"{ev_av:>+10.4f} {ev_tr:>+10.4f} {wr:>7.3f} {ci_lo:>+9.4f} {ci_hi:>+9.4f}")
        rows_out.append({"regime": reg, "n_avail": n_av, "n_trade": n_tr,
                          "n_yes": int(n_yes), "n_no": int(n_no),
                          "ev_avail": ev_av, "ev_trig": ev_tr, "wr": wr,
                          "ci95_lo": ci_lo, "ci95_hi": ci_hi})

    # YES/NO breakdown per regime
    print(f"\n{'─'*68}")
    print(f"YES vs NO EV/trig per regime:")
    print(f"{'─'*68}")
    print(f"{'Regime':8s} {'YES EV/trig':>12} {'YES WR':>8} {'NO EV/trig':>12} {'NO WR':>8}")
    for reg in ["UP", "FLAT", "DOWN"]:
        sub  = rec[rec["regime_sma"] == reg]
        yes_ = sub[sub["action"] == "YES"]["pnl"].dropna().to_numpy()
        no_  = sub[sub["action"] == "NO"]["pnl"].dropna().to_numpy()
        yev  = yes_.mean() if len(yes_) else float("nan")
        nev  = no_.mean()  if len(no_)  else float("nan")
        ywr  = (yes_ > 0).mean() if len(yes_) else float("nan")
        nwr  = (no_  > 0).mean() if len(no_)  else float("nan")
        print(f"{reg:8s} {yev:>+12.4f} {ywr:>8.3f} {nev:>+12.4f} {nwr:>8.3f}")

    # Fold × regime
    print(f"\n{'─'*68}")
    print(f"Fold × regime EV/avail:")
    print(f"{'─'*68}")
    for fold in sorted(rec["fold"].unique()):
        fc = rec[rec["fold"] == fold]
        n_te = len(fc)
        overall_ev = fc["pnl"].fillna(0).sum() / n_te
        parts = []
        for reg in ["UP", "FLAT", "DOWN"]:
            sub = fc[fc["regime_sma"] == reg]
            n = len(sub)
            ev = sub["pnl"].fillna(0).sum() / n if n else float("nan")
            parts.append(f"{reg}(n={n},ev={ev:+.3f})")
        print(f"  Fold {fold}: EV/avail={overall_ev:+.4f}  {' | '.join(parts)}")

    pd.DataFrame(rows_out).to_csv(OUT_DIR / "cv_sma_regime_summary.csv", index=False)
    rec.to_csv(OUT_DIR / "cv_sma_regime_records.csv", index=False)
    print(f"\nSaved → {OUT_DIR}/cv_sma_regime_summary.csv")
    print(f"Saved → {OUT_DIR}/cv_sma_regime_records.csv")


def main():
    data_dir      = REPO_ROOT / "polymarket" / f"data_{COIN}_5m"
    outcomes_path = REPO_ROOT / "polymarket" / f"polymarket_{COIN.lower()}_5m_official_outcomes.csv"

    print("Building spot timeline…")
    spot_df = build_spot_timeline(data_dir)
    print(f"  {len(spot_df)} rows  {spot_df['ts'].min()} → {spot_df['ts'].max()}")

    print("Loading contracts…")
    contracts = load_data(data_dir, outcomes_path)
    cids, df  = build_df(contracts, T1_FOCUS, spot_df)
    print(f"  T1={T1_FOCUS}s  n_contracts={len(cids)}")

    reg_dist = df.drop_duplicates("contract_id")["regime_sma"].value_counts()
    up_rate  = df.groupby("regime_sma")["y_settle"].mean()
    print(f"\nRegime distribution (SMA ±{REGIME_THRESH}, 4h window):")
    for reg in ["UP", "FLAT", "DOWN"]:
        n  = reg_dist.get(reg, 0)
        ur = up_rate.get(reg, float("nan"))
        print(f"  {reg}: n={n}  up_rate={ur:.3f}")

    print("\nRunning 5-fold chronological CV…")
    records = run_cv(df, cids)
    summarise(records)


if __name__ == "__main__":
    main()
