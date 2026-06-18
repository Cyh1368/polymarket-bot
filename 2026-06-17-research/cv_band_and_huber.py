#!/usr/bin/env python3
"""
2026-06-17-research/cv_band_and_huber.py

Steps 2 & 3 of the lottery-dependence redesign (see lottery_dependence_redesign.md).

Runs FOUR strategy variants on identical expanding-window chronological folds, so the
comparison is apples-to-apples, and dumps a trade ledger per variant for scoring by
robust_metrics.py:

  1. baseline      v3.1 binary-logloss model + EV decision rule (reproduces 2026-06-15)
  2. band          baseline + cost-band filter: only trade if traded-side ask ∈ [0.40,0.80)
  3. huber         two Huber edge-regressors (f_yes, f_no), trade the higher predicted edge
  4. huber_band    huber + the same cost-band filter

Data loading / feature extraction mirror 2026-06-15-research/settlement_lgb_v3p1_cv.py
(T1=180s, Filter B YES-gate, skip_bonus=0.05, 5 expanding folds, MIN_TRAIN=200) so the
baseline ledger here matches cv_v3p1_records.csv.

Ledgers are written with the schema robust_metrics.py expects (action/pnl/up_ask/down_ask).
"""
from __future__ import annotations
import math, re
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import pandas as pd
import lightgbm as lgb

REPO_ROOT   = Path(__file__).resolve().parents[1]
OUT_DIR     = Path(__file__).parent / "band_huber_results"
OUT_DIR.mkdir(exist_ok=True)

COIN        = "BTC"
COST_ADD    = 0.01
HORIZON_TOL = 12.0
IND_WINDOW  = 60.0
T1          = 180
SKIP_BONUS  = 0.05
YES_GATE_LO = 0.25
N_FOLDS     = 5
MIN_TRAIN   = 200
BAND_LO, BAND_HI = 0.40, 0.80     # cost-band filter on the traded side's ask
CUT_LO, CUT_HI   = 0.25, 0.75     # "non-lottery" training+trading cut on p_yes_mid
HUBER_ALPHA = 1.0                 # Huber delta (LightGBM `alpha` for objective="huber")

CFG = {"max_depth": 3, "num_leaves": 7, "min_child_samples": 20,
       "lambda_l2": 5.0, "subsample": 0.90, "feature_fraction": 0.90,
       "learning_rate": 0.05, "n_rounds": 300}

TAU_LEVELS = [1, 2, 3, 5, 7, 10, 15, 20]
TAU_LOG_X  = np.log(np.array(TAU_LEVELS) / 100.0)
TAU_COLS   = [f"up_book_imbalance_tau_{t}c" for t in TAU_LEVELS]

FEATURES = [
    "p_yes_mid", "yes_mid_z_60", "yes_mid_vol_60", "yes_mid_z_20", "yes_mid_vol_20",
    "mid_change_60", "book_qty_log", "OBI", "OBI_vol_60", "OBI_z_60",
    "spread_yes", "tod_sin", "tod_cos", "obi_depth_slope",
]
CLASS_YES, CLASS_NO, CLASS_SKIP = 0, 1, 2


# ── Utilities (copied from settlement_lgb_v3p1_cv.py for parity) ───────────────

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

def load_data(data_dir, outcomes_path):
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


# ── Decision rules ─────────────────────────────────────────────────────────────

def in_band(price: float) -> bool:
    return BAND_LO <= price < BAND_HI

def in_cut(p_mid: float) -> bool:
    return CUT_LO <= p_mid < CUT_HI

def decide_ev(p_up, c_yes, c_no, p_mid, up_ask, down_ask, band: bool, cut: bool = False):
    """Binary-model EV rule. Optionally require traded-side ask in the cost band, and/or
    require p_yes_mid in the non-lottery cut (matches the cut training regime)."""
    if cut and not in_cut(p_mid):
        return CLASS_SKIP
    ev_yes = p_up / max(c_yes, 1e-6) - 1.0
    ev_no  = (1.0 - p_up) / max(c_no, 1e-6) - 1.0
    allow_yes = p_mid >= YES_GATE_LO
    side = CLASS_SKIP
    if ev_no > SKIP_BONUS and ev_no >= ev_yes:
        side = CLASS_NO
    elif allow_yes and ev_yes > SKIP_BONUS and ev_yes > ev_no:
        side = CLASS_YES
    if band and side != CLASS_SKIP:
        ask = up_ask if side == CLASS_YES else down_ask
        if not in_band(ask):
            side = CLASS_SKIP
    return side

def decide_huber(pred_yes, pred_no, p_mid, up_ask, down_ask, band: bool, cut: bool = False):
    """Edge-regression rule: trade the side with higher predicted edge above threshold."""
    if cut and not in_cut(p_mid):
        return CLASS_SKIP
    allow_yes = p_mid >= YES_GATE_LO
    side = CLASS_SKIP
    if pred_no > SKIP_BONUS and pred_no >= pred_yes:
        side = CLASS_NO
    elif allow_yes and pred_yes > SKIP_BONUS and pred_yes > pred_no:
        side = CLASS_YES
    if band and side != CLASS_SKIP:
        ask = up_ask if side == CLASS_YES else down_ask
        if not in_band(ask):
            side = CLASS_SKIP
    return side


def binary_params(seed=0):
    return {"objective": "binary", "metric": "binary_logloss",
            "num_leaves": CFG["num_leaves"], "max_depth": CFG["max_depth"],
            "min_child_samples": CFG["min_child_samples"], "subsample": CFG["subsample"],
            "feature_fraction": CFG["feature_fraction"], "lambda_l2": CFG["lambda_l2"],
            "lambda_l1": 0.0, "learning_rate": CFG["learning_rate"],
            "num_threads": 4, "seed": seed, "verbose": -1}

def huber_params(seed=0):
    return {"objective": "huber", "alpha": HUBER_ALPHA, "metric": "huber",
            "num_leaves": CFG["num_leaves"], "max_depth": CFG["max_depth"],
            "min_child_samples": CFG["min_child_samples"], "subsample": CFG["subsample"],
            "feature_fraction": CFG["feature_fraction"], "lambda_l2": CFG["lambda_l2"],
            "lambda_l1": 0.0, "learning_rate": CFG["learning_rate"],
            "num_threads": 4, "seed": seed, "verbose": -1}


def pnl_of(side, y, ya, na):
    if side == CLASS_YES: return y / max(ya, 1e-6) - 1.0
    if side == CLASS_NO:  return (1.0 - y) / max(na, 1e-6) - 1.0
    return None


# ── CV ─────────────────────────────────────────────────────────────────────────

print("Loading contracts…")
contracts = load_data(
    REPO_ROOT / "polymarket" / f"data_{COIN}_5m",
    REPO_ROOT / "polymarket" / f"polymarket_{COIN.lower()}_5m_official_outcomes.csv",
)
rows = [extract_row(cd) for cd in contracts]
rows = [r for r in rows if r is not None]
df = pd.DataFrame(rows)
n = len(df)
print(f"  {len(contracts)} contracts → {n} rows at T1={T1}s\n")

fold_size = n // N_FOLDS
VARIANTS = ["baseline", "band", "huber", "huber_band", "binary_cut", "huber_cut"]
records = {v: [] for v in VARIANTS}

print(f"Expanding-window CV: {N_FOLDS} folds  fold_size≈{fold_size}")
print(f"band=[{BAND_LO},{BAND_HI})  cut=[{CUT_LO},{CUT_HI})  "
      f"huber_alpha={HUBER_ALPHA}  skip_bonus={SKIP_BONUS}\n")

for k in range(N_FOLDS):
    te_start = k * fold_size
    te_end   = (k + 1) * fold_size if k < N_FOLDS - 1 else n
    tr_end   = te_start
    if tr_end < MIN_TRAIN:
        print(f"  fold {k}: skip (train {tr_end} < {MIN_TRAIN})")
        continue
    tr = df.iloc[:tr_end].copy()
    te = df.iloc[te_start:te_end].copy()
    print(f"  fold {k}: train={tr_end}  test={len(te)}")

    X_tr = tr[FEATURES].to_numpy().astype(float)
    X_te = te[FEATURES].to_numpy().astype(float)

    # Binary model (baseline + band share it)
    y_bin = tr["y_settle"].to_numpy().astype(float)
    m_bin = lgb.train(binary_params(seed=k),
                      lgb.Dataset(X_tr, label=y_bin, free_raw_data=False),
                      num_boost_round=CFG["n_rounds"])
    p_up_te = m_bin.predict(X_te)

    # Huber edge regressors (yes / no), trained on realized side return
    t_yes = (tr["y_settle"].to_numpy().astype(float)) / tr["c_yes"].to_numpy() - 1.0
    t_no  = (1.0 - tr["y_settle"].to_numpy().astype(float)) / tr["c_no"].to_numpy() - 1.0
    m_yes = lgb.train(huber_params(seed=k),
                      lgb.Dataset(X_tr, label=t_yes, free_raw_data=False),
                      num_boost_round=CFG["n_rounds"])
    m_no  = lgb.train(huber_params(seed=k),
                      lgb.Dataset(X_tr, label=t_no, free_raw_data=False),
                      num_boost_round=CFG["n_rounds"])
    pred_yes_te = m_yes.predict(X_te)
    pred_no_te  = m_no.predict(X_te)

    # ── Cut models: train ONLY on non-lottery contracts 0.25 ≤ p_yes_mid < 0.75 ──
    cut_mask = (tr["p_yes_mid"] >= CUT_LO) & (tr["p_yes_mid"] < CUT_HI)
    tr_cut = tr[cut_mask]
    X_tr_cut = tr_cut[FEATURES].to_numpy().astype(float)
    print(f"          cut train rows: {len(tr_cut)}/{tr_end} ({len(tr_cut)/tr_end*100:.0f}%)")

    y_bin_cut = tr_cut["y_settle"].to_numpy().astype(float)
    m_bin_cut = lgb.train(binary_params(seed=k),
                          lgb.Dataset(X_tr_cut, label=y_bin_cut, free_raw_data=False),
                          num_boost_round=CFG["n_rounds"])
    p_up_cut_te = m_bin_cut.predict(X_te)

    t_yes_cut = (tr_cut["y_settle"].to_numpy().astype(float)) / tr_cut["c_yes"].to_numpy() - 1.0
    t_no_cut  = (1.0 - tr_cut["y_settle"].to_numpy().astype(float)) / tr_cut["c_no"].to_numpy() - 1.0
    m_yes_cut = lgb.train(huber_params(seed=k),
                          lgb.Dataset(X_tr_cut, label=t_yes_cut, free_raw_data=False),
                          num_boost_round=CFG["n_rounds"])
    m_no_cut  = lgb.train(huber_params(seed=k),
                          lgb.Dataset(X_tr_cut, label=t_no_cut, free_raw_data=False),
                          num_boost_round=CFG["n_rounds"])
    pred_yes_cut_te = m_yes_cut.predict(X_te)
    pred_no_cut_te  = m_no_cut.predict(X_te)

    for i, (_, row) in enumerate(te.iterrows()):
        d = row.to_dict()
        y, ya, na, pm = float(d["y_settle"]), float(d["up_ask"]), float(d["down_ask"]), float(d["p_yes_mid"])
        cy, cn = float(d["c_yes"]), float(d["c_no"])
        dec = {
            "baseline":   decide_ev(float(p_up_te[i]), cy, cn, pm, ya, na, band=False),
            "band":       decide_ev(float(p_up_te[i]), cy, cn, pm, ya, na, band=True),
            "huber":      decide_huber(float(pred_yes_te[i]), float(pred_no_te[i]), pm, ya, na, band=False),
            "huber_band": decide_huber(float(pred_yes_te[i]), float(pred_no_te[i]), pm, ya, na, band=True),
            "binary_cut": decide_ev(float(p_up_cut_te[i]), cy, cn, pm, ya, na, band=False, cut=True),
            "huber_cut":  decide_huber(float(pred_yes_cut_te[i]), float(pred_no_cut_te[i]), pm, ya, na, band=False, cut=True),
        }
        for v, side in dec.items():
            records[v].append({
                "fold": k, "contract_id": d["contract_id"],
                "p_yes_mid": round(pm, 4),
                "action": ["YES", "NO", "SKIP"][side],
                "y_settle": int(y), "pnl": pnl_of(side, y, ya, na),
                "up_ask": round(ya, 4), "down_ask": round(na, 4),
            })

# ── Write ledgers + quick summary ──────────────────────────────────────────────

print(f"\n{'='*60}\nVariant summary (raw — full robustness via robust_metrics.py)\n{'='*60}")
print(f"  {'variant':12s}  {'trades':>7}  {'pool':>5}  {'EV/avail':>9}  {'EV/trig':>9}  {'WR':>6}")
summ = []
for v in VARIANTS:
    out = pd.DataFrame(records[v])
    out.to_csv(OUT_DIR / f"cv_{v}_records.csv", index=False)
    traded = out[out["action"] != "SKIP"].dropna(subset=["pnl"])
    pool = len(out)
    ev_a = traded["pnl"].sum() / max(pool, 1)
    ev_t = traded["pnl"].mean() if len(traded) else float("nan")
    wr   = (traded["pnl"] > 0).mean() if len(traded) else float("nan")
    summ.append({"variant": v, "trades": len(traded), "pool": pool,
                 "ev_avail": ev_a, "ev_trig": ev_t, "win_rate": wr})
    print(f"  {v:12s}  {len(traded):>7}  {pool:>5}  {ev_a:>+9.4f}  {ev_t:>+9.4f}  {wr:>6.1%}")

pd.DataFrame(summ).to_csv(OUT_DIR / "variant_raw_summary.csv", index=False)
print(f"\nLedgers + summary → {OUT_DIR}/")
print("Next: score each cv_<variant>_records.csv with robust_metrics.py")
