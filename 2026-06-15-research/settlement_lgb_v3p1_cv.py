#!/usr/bin/env python3
"""
2026-06-15-research/settlement_lgb_v3p1_cv.py

Expanding-window chronological CV for v3 and v3.1, evaluated on the same folds.
Mirrors the structure of 2026-06-14-research/settlement_lgb_v3_fullbacktest.py
but adds obi_depth_slope (NaN for pre-tau contracts), Filter B, and runs both
models simultaneously so per-fold comparison is apples-to-apples.

skip_bonus=0.05, Filter B (block YES if p_yes_mid < 0.25) — consistent with
the 200-seed validation in settlement_lgb_v3_filters.py and v3p1.py.
"""
from __future__ import annotations
import math, re
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import pandas as pd
import lightgbm as lgb

REPO_ROOT   = Path(__file__).resolve().parents[1]
OUT_DIR     = Path(__file__).parent / "settlement_lgb_v3p1_results"
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

CFG = {
    "max_depth": 3, "num_leaves": 7, "min_child_samples": 20,
    "lambda_l2": 5.0, "subsample": 0.90, "feature_fraction": 0.90,
    "learning_rate": 0.05, "n_rounds": 300,
}

TAU_LEVELS = [1, 2, 3, 5, 7, 10, 15, 20]
TAU_LOG_X  = np.log(np.array(TAU_LEVELS) / 100.0)
TAU_COLS   = [f"up_book_imbalance_tau_{t}c" for t in TAU_LEVELS]

V3_FEATURES = [
    "p_yes_mid",
    "yes_mid_z_60", "yes_mid_vol_60",
    "yes_mid_z_20", "yes_mid_vol_20",
    "mid_change_60",
    "book_qty_log",
    "OBI", "OBI_vol_60", "OBI_z_60",
    "spread_yes",
    "tod_sin", "tod_cos",
]
V31_FEATURES = V3_FEATURES + ["obi_depth_slope"]

CLASS_YES, CLASS_NO, CLASS_SKIP = 0, 1, 2


# ── Utilities ────────────────────────────────────────────────────────────────

def fnum(v):
    try:
        out = float(v); return out if math.isfinite(out) else None
    except Exception: return None

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
    xm = x.mean(); ym = y.mean()
    denom = ((x - xm) ** 2).sum()
    if denom < 1e-12: return float("nan")
    return float(((x - xm) * (y - ym)).sum() / denom)


# ── Data loading ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContractData:
    slug: str; close_time: pd.Timestamp; label: int; df: pd.DataFrame

def load_data(data_dir, outcomes_path):
    outcomes = {}
    for row in pd.read_csv(outcomes_path).to_dict(orient="records"):
        slug = str(row.get("market_slug") or "").strip()
        wo   = str(row.get("winning_outcome") or "").strip()
        if slug and wo in {"Up", "Down"}: outcomes[slug] = wo
    required = ("up_best_bid","up_best_ask","down_best_bid","down_best_ask","seconds_to_close")
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
        secs = t1_ts.hour*3600 + t1_ts.minute*60 + t1_ts.second
        tod_sin = math.sin(2*math.pi*secs/86400); tod_cos = math.cos(2*math.pi*secs/86400)
    else:
        tod_sin = tod_cos = 0.0
    tau_vals = np.array([fnum(t1r.get(c)) for c in TAU_COLS], dtype=float)
    rd = {
        "contract_id": cd.slug, "close_time": cd.close_time,
        "y_settle": cd.label, "c_yes": ya+COST_ADD, "c_no": na+COST_ADD,
        "up_ask": ya, "down_ask": na, "p_yes_mid": up_mid,
        "yes_mid_z_60": ym60["z"],  "yes_mid_vol_60": ym60["vol"],
        "yes_mid_z_20": ym20["z"],  "yes_mid_vol_20": ym20["vol"],
        "mid_change_60": mid_change, "book_qty_log": math.log1p(ubs+dbs),
        "OBI": obi_cur, "OBI_vol_60": ob60["vol"], "OBI_z_60": ob60["z"],
        "spread_yes": ya-yb, "tod_sin": tod_sin, "tod_cos": tod_cos,
        "obi_depth_slope": ols_slope(tau_vals),
        "tau_present": int(np.isfinite(tau_vals).sum() >= 4),
    }
    if any(not math.isfinite(float(rd.get(f, float("nan")))) for f in V3_FEATURES):
        return None
    return rd


# ── Decision ─────────────────────────────────────────────────────────────────

def decide(p_up, c_yes, c_no, p_mid):
    p_down = 1.0 - p_up
    ev_yes = p_up   / max(c_yes, 1e-6) - 1.0
    ev_no  = p_down / max(c_no,  1e-6) - 1.0
    allow_yes = p_mid >= YES_GATE_LO
    if ev_no  > SKIP_BONUS and ev_no  >= ev_yes: return CLASS_NO
    if allow_yes and ev_yes > SKIP_BONUS and ev_yes > ev_no: return CLASS_YES
    return CLASS_SKIP

def lgb_params(seed=0):
    return {"objective":"binary","metric":"binary_logloss",
            "num_leaves":CFG["num_leaves"],"max_depth":CFG["max_depth"],
            "min_child_samples":CFG["min_child_samples"],"subsample":CFG["subsample"],
            "feature_fraction":CFG["feature_fraction"],"lambda_l2":CFG["lambda_l2"],
            "lambda_l1":0.0,"learning_rate":CFG["learning_rate"],
            "num_threads":4,"seed":seed,"verbose":-1}

def bench_pnl(row):
    return (1.0 - float(row["y_settle"])) / max(float(row["down_ask"]), 1e-6) - 1.0 \
           if float(row["p_yes_mid"]) < 0.15 else None


# ── Load data ─────────────────────────────────────────────────────────────────

print("Loading contracts…")
contracts = load_data(
    REPO_ROOT / "polymarket" / f"data_{COIN}_5m",
    REPO_ROOT / "polymarket" / f"polymarket_{COIN.lower()}_5m_official_outcomes.csv",
)
rows = [extract_row(cd) for cd in contracts]
rows = [r for r in rows if r is not None]
df = pd.DataFrame(rows)
tau_n = int(df["tau_present"].sum())
print(f"  {len(contracts)} contracts  →  {len(df)} rows at T1={T1}s")
print(f"  tau_present={tau_n} ({tau_n/len(df)*100:.1f}%)  tau_nan={len(df)-tau_n}\n")


# ── Expanding-window CV ───────────────────────────────────────────────────────

n         = len(df)
fold_size = n // N_FOLDS
records   = {m: [] for m in ["v3", "v3p1"]}

print(f"Expanding-window CV: {N_FOLDS} folds  fold_size≈{fold_size}")
print(f"skip_bonus={SKIP_BONUS}  Filter B: YES gate at p_yes_mid < {YES_GATE_LO}\n")

fold_summaries = []

for k in range(N_FOLDS):
    te_start = k * fold_size
    te_end   = (k+1)*fold_size if k < N_FOLDS-1 else n
    tr_end   = te_start

    if tr_end < MIN_TRAIN:
        print(f"  fold {k}: skip ({tr_end} training contracts < MIN_TRAIN={MIN_TRAIN})")
        continue

    tr = df.iloc[:tr_end].copy()
    te = df.iloc[te_start:te_end].copy()
    tau_in_tr = int(tr["tau_present"].sum())
    tau_in_te = int(te["tau_present"].sum())

    print(f"  fold {k}: train={tr_end}  test={len(te)}  "
          f"[train tau={tau_in_tr} ({tau_in_tr/tr_end*100:.0f}%)  "
          f"test tau={tau_in_te} ({tau_in_te/len(te)*100:.0f}%)]")

    fold_ev = {}
    for label, feats in [("v3", V3_FEATURES), ("v3p1", V31_FEATURES)]:
        y_tr = tr["y_settle"].to_numpy().astype(float)
        X_tr = tr[feats].to_numpy().astype(float)
        ds   = lgb.Dataset(X_tr, label=y_tr, free_raw_data=False)
        m    = lgb.train(lgb_params(seed=k), train_set=ds,
                         num_boost_round=CFG["n_rounds"],
                         callbacks=[lgb.log_evaluation(period=9999)])

        p_up_arr = m.predict(te[feats].to_numpy().astype(float))

        for i, (_, row) in enumerate(te.iterrows()):
            row_d = row.to_dict()
            p_up  = float(p_up_arr[i])
            pc    = decide(p_up, float(row_d["c_yes"]), float(row_d["c_no"]),
                           float(row_d["p_yes_mid"]))
            y  = float(row_d["y_settle"])
            ya = float(row_d["up_ask"]); na = float(row_d["down_ask"])
            pnl = (y/max(ya,1e-6)-1.0 if pc==CLASS_YES else
                   (1-y)/max(na,1e-6)-1.0 if pc==CLASS_NO else None)
            records[label].append({
                "fold": k, "contract_id": row_d["contract_id"],
                "p_yes_mid": round(float(row_d["p_yes_mid"]), 4),
                "action": ["YES","NO","SKIP"][pc],
                "y_settle": int(y), "pnl": pnl,
                "bench_pnl": bench_pnl(row_d),
                "tau_present": int(row_d["tau_present"]),
                "up_ask": round(ya,4), "down_ask": round(na,4),
            })

        fold_recs = [r for r in records[label] if r["fold"] == k]
        traded = [r for r in fold_recs if r["pnl"] is not None]
        ev_a = sum(r["pnl"] for r in traded) / max(len(fold_recs), 1)
        fold_ev[label] = ev_a

    print(f"    v3  EV/avail={fold_ev['v3']:+.4f}    "
          f"v3.1 EV/avail={fold_ev['v3p1']:+.4f}    "
          f"Δ={fold_ev['v3p1']-fold_ev['v3']:+.4f}")
    fold_summaries.append({"fold": k, "train": tr_end, "test": len(te),
                            "ev_v3": fold_ev["v3"], "ev_v3p1": fold_ev["v3p1"],
                            "delta": fold_ev["v3p1"]-fold_ev["v3"]})


# ── Aggregate ─────────────────────────────────────────────────────────────────

def summarise(label, recs_list):
    out = pd.DataFrame(recs_list)
    n_total   = len(out)
    traded    = out[out["action"] != "SKIP"]
    yes_df    = out[out["action"] == "YES"]
    no_df     = out[out["action"] == "NO"]
    bench_df  = out[out["bench_pnl"].notna()]

    ev_avail  = traded["pnl"].sum() / max(n_total, 1)
    ev_trig   = traded["pnl"].mean() if len(traded) else float("nan")
    wr        = (traded["pnl"] > 0).mean() if len(traded) else float("nan")
    yes_ev    = yes_df["pnl"].mean() if len(yes_df) else float("nan")
    no_ev     = no_df["pnl"].mean()  if len(no_df)  else float("nan")
    yes_wr    = (yes_df["pnl"] > 0).mean() if len(yes_df) else float("nan")
    no_wr     = (no_df["pnl"] > 0).mean()  if len(no_df)  else float("nan")
    bench_ev  = bench_df["bench_pnl"].sum() / max(n_total, 1)

    return {
        "model": label, "n_oos": n_total,
        "n_yes": len(yes_df), "n_no": len(no_df), "n_skip": n_total-len(yes_df)-len(no_df),
        "ev_avail": ev_avail, "ev_trig": ev_trig, "win_rate": wr,
        "yes_ev": yes_ev, "yes_wr": yes_wr,
        "no_ev":  no_ev,  "no_wr":  no_wr,
        "bench_ev": bench_ev,
    }

print(f"\n{'='*70}")
print(f"EXPANDING-WINDOW CV SUMMARY — T1={T1}s  skip_bonus={SKIP_BONUS}  Filter B")
print(f"{'='*70}\n")

summaries = []
for label in ["v3", "v3p1"]:
    s = summarise(label, records[label])
    summaries.append(s)
    print(f"{'─'*60}")
    print(f"  {label}  ({len(V3_FEATURES) if label=='v3' else len(V31_FEATURES)} features)  "
          f"OOS contracts: {s['n_oos']}")
    print(f"  Action: YES={s['n_yes']}  NO={s['n_no']}  SKIP={s['n_skip']}  "
          f"trade%={( s['n_yes']+s['n_no'])/s['n_oos']*100:.1f}%")
    print(f"  EV/available : {s['ev_avail']:+.5f}")
    print(f"  EV/triggered : {s['ev_trig']:+.5f}   win% : {s['win_rate']:.3f}")
    print(f"  YES EV/trig  : {s['yes_ev']:+.5f}   YES win% : {s['yes_wr']:.3f}   n={s['n_yes']}")
    print(f"  NO  EV/trig  : {s['no_ev']:+.5f}    NO  win% : {s['no_wr']:.3f}   n={s['n_no']}")
    print(f"  Bench EV/avail: {s['bench_ev']:+.5f}")

print(f"\n{'─'*60}")
v3_ev  = summaries[0]["ev_avail"]
v31_ev = summaries[1]["ev_avail"]
print(f"  Δ EV/available (v3.1 − v3) : {v31_ev - v3_ev:+.5f}")

print(f"\nPer-fold breakdown:")
print(f"  {'fold':>4}  {'train':>6}  {'test':>5}  {'v3 EV/avail':>13}  {'v3.1 EV/avail':>14}  {'Δ':>8}")
for fs in fold_summaries:
    print(f"  {fs['fold']:>4}  {fs['train']:>6}  {fs['test']:>5}  "
          f"{fs['ev_v3']:>+13.5f}  {fs['ev_v3p1']:>+14.5f}  {fs['delta']:>+8.5f}")

# Price bucket breakdown for v3.1
print(f"\nv3.1 breakdown by p_yes_mid bucket:")
out31 = pd.DataFrame(records["v3p1"])
buckets = [(0,.05),(.05,.10),(.10,.15),(.15,.25),(.25,.50),
           (.50,.75),(.75,.85),(.85,.90),(.90,.95),(.95,1.0)]
print(f"  {'range':14s}  {'pool':>5}  {'trades':>7}  {'yes':>4}  {'no':>4}  "
      f"{'wins':>5}  {'EV/trig':>9}  {'EV/pool':>9}")
for lo, hi in buckets:
    pool = out31[(out31["p_yes_mid"]>=lo) & (out31["p_yes_mid"]<hi)]
    if pool.empty: continue
    trd  = pool[pool["action"]!="SKIP"]
    if trd.empty:
        print(f"  [{lo:.2f},{hi:.2f})  {len(pool):>5}  {'— all SKIP':>19}")
        continue
    w    = (trd["pnl"]>0).sum()
    ev_t = trd["pnl"].mean()
    ev_p = trd["pnl"].sum()/max(len(pool),1)
    ny   = (trd["action"]=="YES").sum()
    nn   = (trd["action"]=="NO").sum()
    print(f"  [{lo:.2f},{hi:.2f})  {len(pool):>5}  {len(trd):>7}  {ny:>4}  {nn:>4}  "
          f"{w:>5}  {ev_t:>+9.4f}  {ev_p:>+9.4f}")

# Save
pd.DataFrame(summaries).to_csv(OUT_DIR / "cv_summary.csv", index=False)
pd.DataFrame(fold_summaries).to_csv(OUT_DIR / "cv_fold_detail.csv", index=False)
pd.DataFrame(records["v3"]).to_csv(OUT_DIR / "cv_v3_records.csv", index=False)
pd.DataFrame(records["v3p1"]).to_csv(OUT_DIR / "cv_v3p1_records.csv", index=False)
print(f"\nResults → {OUT_DIR}/")
