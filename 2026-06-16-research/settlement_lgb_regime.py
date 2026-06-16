#!/usr/bin/env python3
"""
2026-06-16-research/settlement_lgb_regime.py

Regime-specific LightGBM models for Polymarket BTC 5m.

Regime is defined by btc_4h_ret (BTC price change over 4 hours prior to T1=180s),
computed from Kraken hourly OHLCV in contract_regime_labels.csv:
  UP   : btc_4h_ret > +0.3%
  FLAT : -0.3% ≤ btc_4h_ret ≤ +0.3%
  DOWN : btc_4h_ret < -0.3%

Three evaluations:
  1. CV (4 chronological folds, expanding train): per-regime OOS metrics
  2. 200-seed random split (within-regime): mean EV, CI95
  3. Paired bootstrap vs v3.1 on same regime-subset test contracts

Also trains v3.2 (single model + btc_4h_ret + btc_1h_ret as extra features)
for comparison.

Outputs: 2026-06-16-research/regime_models/
"""
from __future__ import annotations
import math, re
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import pandas as pd
import lightgbm as lgb

REPO_ROOT  = Path(__file__).resolve().parents[1]
RES_DIR    = Path(__file__).parent
OUT_DIR    = RES_DIR / "regime_models"
OUT_DIR.mkdir(exist_ok=True)

REGIME_CSV = RES_DIR / "contract_regime_labels.csv"

COIN        = "BTC"
COST_ADD    = 0.01
HORIZON_TOL = 12.0
IND_WINDOW  = 60.0
N_SEEDS     = 200
N_BOOT      = 5_000
MIN_TRAIN   = 30    # smaller than v3.1 (50) because regime subsets are smaller
N_FOLDS     = 4

T1_FOCUS   = 180
SKIP_BONUS = 0.05
YES_GATE_LO = 0.25

CLASS_YES, CLASS_NO, CLASS_SKIP = 0, 1, 2

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
V32_FEATURES = V31_FEATURES + ["btc_4h_ret", "btc_1h_ret"]

CFG = {
    "max_depth": 3, "num_leaves": 7, "min_child_samples": 20,
    "lambda_l2": 5.0, "subsample": 0.90, "feature_fraction": 0.90,
    "learning_rate": 0.05, "n_rounds": 300,
}

REGIMES = ["UP", "FLAT", "DOWN"]


# ── Utilities ────────────────────────────────────────────────────────────────

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


def ci95(arr):
    return float(np.percentile(arr, 2.5)), float(np.percentile(arr, 97.5))


def decide(p_up: float, c_yes: float, c_no: float, p_mid: float) -> int:
    p_down = 1.0 - p_up
    ev_yes = p_up   / max(c_yes, 1e-6) - 1.0
    ev_no  = p_down / max(c_no,  1e-6) - 1.0
    allow_yes = p_mid >= YES_GATE_LO
    if ev_no  > SKIP_BONUS and ev_no  >= ev_yes: return CLASS_NO
    if allow_yes and ev_yes > SKIP_BONUS and ev_yes > ev_no: return CLASS_YES
    return CLASS_SKIP


def model_pnl(pc: int, row: dict) -> float | None:
    y = float(row["y_settle"]); ya = float(row["up_ask"]); na = float(row["down_ask"])
    if pc == CLASS_YES: return y / max(ya, 1e-6) - 1.0
    if pc == CLASS_NO:  return (1.0 - y) / max(na, 1e-6) - 1.0
    return None


# ── Data loading ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContractData:
    slug: str; close_time: pd.Timestamp; label: int; df: pd.DataFrame


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
        close_time = pd.Timestamp(int(m.group(1)) + 300, unit="s", tz="UTC")
        contracts.append(ContractData(
            slug=slug, close_time=close_time,
            label=(1 if wo == "Up" else 0), df=df))
    contracts.sort(key=lambda c: c.close_time)
    return contracts


def extract_row(cd: ContractData, T1: int) -> dict | None:
    df  = cd.df
    t1c = df[(df["_stc"] - T1).abs() <= HORIZON_TOL]
    if t1c.empty: return None
    t1r = t1c.loc[(t1c["_stc"] - T1).abs().idxmin()]

    ya  = fnum(t1r.get("up_best_ask"));   yb  = fnum(t1r.get("up_best_bid"))
    na  = fnum(t1r.get("down_best_ask")); nb  = fnum(t1r.get("down_best_bid"))
    ubs = fnum(t1r.get("up_best_bid_size"))   or 0.0
    dbs = fnum(t1r.get("down_best_bid_size")) or 0.0

    if any(v is None for v in (ya, yb, na, nb)): return None
    if not (0 < ya < 1 and 0 < na < 1 and yb <= ya and nb <= na): return None

    up_mid = (yb + ya) / 2.0
    t1_ts  = t1r.get("_ts"); ts_ok = not pd.isna(t1_ts)

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
    ym60    = series_stats(_mids(h60), up_mid)
    ym20    = series_stats(_mids(h20), up_mid)
    ob60    = series_stats(_obis(h60), obi_cur)
    mids_60 = _mids(h60).dropna()
    mid_change = up_mid - float(mids_60.iloc[0]) if not mids_60.empty else 0.0

    if ts_ok:
        secs = t1_ts.hour * 3600 + t1_ts.minute * 60 + t1_ts.second
        tod_sin = math.sin(2 * math.pi * secs / 86400)
        tod_cos = math.cos(2 * math.pi * secs / 86400)
    else:
        tod_sin = tod_cos = 0.0

    tau_vals = np.array([fnum(t1r.get(c)) for c in TAU_COLS], dtype=float)
    slope    = ols_slope(tau_vals)

    rd = {
        "contract_id": cd.slug, "close_time": cd.close_time, "T1": T1,
        "y_settle": cd.label,
        "c_yes": ya + COST_ADD, "c_no": na + COST_ADD,
        "up_ask": ya, "down_ask": na, "p_yes_mid": up_mid,
        "yes_mid_z_60": ym60["z"],   "yes_mid_vol_60": ym60["vol"],
        "yes_mid_z_20": ym20["z"],   "yes_mid_vol_20": ym20["vol"],
        "mid_change_60": mid_change,
        "book_qty_log": math.log1p(ubs + dbs),
        "OBI": obi_cur, "OBI_vol_60": ob60["vol"], "OBI_z_60": ob60["z"],
        "spread_yes": ya - yb,
        "tod_sin": tod_sin, "tod_cos": tod_cos,
        "obi_depth_slope": slope,
    }
    if any(not math.isfinite(float(rd.get(f, float("nan")))) for f in V3_FEATURES):
        return None
    return rd


def build_df(contracts: list[ContractData], T1: int, regime_map: dict[str, dict]) -> tuple[list[str], pd.DataFrame]:
    rows, seen, cids = [], set(), []
    for cd in contracts:
        r = extract_row(cd, T1)
        if r is None:
            continue
        rinfo = regime_map.get(cd.slug)
        if rinfo is None:
            continue
        r["regime"]      = rinfo["regime"]
        r["btc_4h_ret"]  = float(rinfo["btc_4h_ret"])
        r["btc_1h_ret"]  = float(rinfo["btc_1h_ret"])
        rows.append(r)
        if cd.slug not in seen:
            seen.add(cd.slug); cids.append(cd.slug)
    return (cids, pd.DataFrame(rows)) if rows else ([], pd.DataFrame())


# ── Training helpers ──────────────────────────────────────────────────────────

def lgb_params(seed: int) -> dict:
    return {
        "objective": "binary", "metric": "binary_logloss",
        "num_leaves": CFG["num_leaves"], "max_depth": CFG["max_depth"],
        "min_child_samples": CFG["min_child_samples"],
        "subsample": CFG["subsample"], "feature_fraction": CFG["feature_fraction"],
        "lambda_l2": CFG["lambda_l2"], "lambda_l1": 0.0,
        "learning_rate": CFG["learning_rate"], "num_threads": 4,
        "seed": seed, "verbose": -1,
    }


def train_model(tr: pd.DataFrame, feats: list[str], seed: int) -> lgb.Booster | None:
    y = tr["y_settle"].astype(float).to_numpy()
    if len(tr) < MIN_TRAIN or y.std() < 1e-9:
        return None
    ds = lgb.Dataset(tr[feats].to_numpy().astype(float), label=y, free_raw_data=False)
    return lgb.train(lgb_params(seed), train_set=ds,
                     num_boost_round=CFG["n_rounds"], valid_sets=[ds],
                     callbacks=[lgb.log_evaluation(period=9999)])


def eval_model(m: lgb.Booster, te: pd.DataFrame, feats: list[str], n_avail: int) -> dict:
    if m is None or te.empty:
        return {"ev": 0.0, "pnl": [], "yes_pnl": [], "no_pnl": [], "n_avail": n_avail}
    p_up_arr = m.predict(te[feats].to_numpy().astype(float))
    pnl, yes_pnl, no_pnl = [], [], []
    for i, (_, row) in enumerate(te.iterrows()):
        rd = row.to_dict()
        pc = decide(float(p_up_arr[i]), float(rd["c_yes"]), float(rd["c_no"]), float(rd["p_yes_mid"]))
        p = model_pnl(pc, rd)
        if p is not None:
            pnl.append(p)
            if pc == CLASS_YES: yes_pnl.append(p)
            else:               no_pnl.append(p)
    ev = sum(pnl) / max(n_avail, 1)
    return {"ev": ev, "pnl": pnl, "yes_pnl": yes_pnl, "no_pnl": no_pnl, "n_avail": n_avail}


# ── CV Evaluation ─────────────────────────────────────────────────────────────

def run_cv(df: pd.DataFrame, cids: list[str]) -> dict[str, list[dict]]:
    """4-fold expanding CV. Returns per-regime per-fold results."""
    n = len(cids)
    portion = n // (N_FOLDS + 1)

    # Build fold boundary indices into cids (sorted chronologically)
    cid_set = set(cids)
    ordered = [c for c in cids if c in cid_set]  # already sorted

    fold_results: dict[str, list[dict]] = {r: [] for r in REGIMES + ["ALL"]}

    for fold in range(1, N_FOLDS + 1):
        # Training: first fold×portion contracts; test: next portion
        train_end = fold * portion
        test_end  = min((fold + 1) * portion, n)
        if test_end <= train_end:
            continue

        tr_ids = set(ordered[:train_end])
        te_ids = set(ordered[train_end:test_end])
        tr_all = df[df["contract_id"].isin(tr_ids)].copy()
        te_all = df[df["contract_id"].isin(te_ids)].copy()

        # ALL regimes (v3.1 baseline replication)
        m_all = train_model(tr_all, V31_FEATURES, seed=fold * 1000 + 42)
        r_all = eval_model(m_all, te_all, V31_FEATURES, n_avail=len(te_ids))
        fold_results["ALL"].append({
            "fold": fold, "n_train": len(tr_ids), "n_test_avail": len(te_ids),
            "n_traded": len(r_all["pnl"]), "ev": r_all["ev"],
            "yes_ev": np.mean(r_all["yes_pnl"]) if r_all["yes_pnl"] else float("nan"),
            "no_ev":  np.mean(r_all["no_pnl"])  if r_all["no_pnl"]  else float("nan"),
        })

        # Per-regime
        for regime in REGIMES:
            tr_r = tr_all[tr_all["regime"] == regime]
            te_r = te_all[te_all["regime"] == regime]
            m_r  = train_model(tr_r, V31_FEATURES, seed=fold * 1000 + 42)
            r_r  = eval_model(m_r, te_r, V31_FEATURES, n_avail=len(te_r))
            fold_results[regime].append({
                "fold": fold,
                "n_train": len(tr_r), "n_test_avail": len(te_r),
                "n_traded": len(r_r["pnl"]), "ev": r_r["ev"],
                "yes_ev": np.mean(r_r["yes_pnl"]) if r_r["yes_pnl"] else float("nan"),
                "no_ev":  np.mean(r_r["no_pnl"])  if r_r["no_pnl"]  else float("nan"),
            })

    return fold_results


# ── 200-seed random splits ────────────────────────────────────────────────────

def random_split(ids: list, seed: int, frac: float = 0.20):
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(ids))
    cut = int(len(ids) * (1.0 - frac))
    return [ids[i] for i in sorted(idx[:cut])], [ids[i] for i in sorted(idx[cut:])]


def run_200seeds(df: pd.DataFrame, cids: list[str]) -> dict[str, dict]:
    """
    For each regime R and for v3.2:
      - 200 seeds, each: train on 80% of R-subset, eval on 20%
      - Paired comparison: same seed's 20% test set used by v3.1 (trained on all data)
        to compute baseline EV on R-subset.
    Returns dict keyed by regime / "ALL" / "v3.2".
    """
    print(f"  Running {N_SEEDS} seeds...")

    # Pre-compute per-regime cid lists
    regime_cids: dict[str, list[str]] = {r: [] for r in REGIMES + ["ALL"]}
    for cid in cids:
        rows = df[df["contract_id"] == cid]
        if rows.empty:
            continue
        reg = rows.iloc[0]["regime"]
        regime_cids[reg].append(cid)
        regime_cids["ALL"].append(cid)

    # Storage: per-seed EV for each configuration
    ev_store: dict[str, list[float]] = {r: [] for r in REGIMES + ["ALL", "v3.2",
                                                                     "v3p1_on_UP",
                                                                     "v3p1_on_FLAT",
                                                                     "v3p1_on_DOWN"]}

    for seed in range(N_SEEDS):
        if seed % 50 == 0:
            print(f"    seed {seed}/{N_SEEDS}", flush=True)

        # --- v3.1 trained on ALL contracts (baseline) ---
        tr_all_ids, te_all_ids = random_split(regime_cids["ALL"], seed)
        tr_all = df[df["contract_id"].isin(set(tr_all_ids))].copy()
        m_all  = train_model(tr_all, V31_FEATURES, seed=seed * 1000 + 42)
        te_all = df[df["contract_id"].isin(set(te_all_ids))].copy()

        r_all = eval_model(m_all, te_all, V31_FEATURES, n_avail=len(te_all_ids))
        ev_store["ALL"].append(r_all["ev"])

        # v3.1 evaluated on each regime subset of the test set
        for regime in REGIMES:
            te_r_from_all = te_all[te_all["regime"] == regime]
            r_baseline = eval_model(m_all, te_r_from_all, V31_FEATURES,
                                    n_avail=len(te_all_ids))
            ev_store[f"v3p1_on_{regime}"].append(r_baseline["ev"])

        # --- v3.2: ALL contracts + btc_4h_ret + btc_1h_ret ---
        m_v32 = train_model(tr_all, V32_FEATURES, seed=seed * 1000 + 42)
        r_v32 = eval_model(m_v32, te_all, V32_FEATURES, n_avail=len(te_all_ids))
        ev_store["v3.2"].append(r_v32["ev"])

        # --- regime-specific models ---
        for regime in REGIMES:
            rcids = regime_cids[regime]
            if len(rcids) < MIN_TRAIN * 2:
                ev_store[regime].append(0.0)
                continue
            tr_r_ids, te_r_ids = random_split(rcids, seed)
            tr_r = df[df["contract_id"].isin(set(tr_r_ids))].copy()
            te_r = df[df["contract_id"].isin(set(te_r_ids))].copy()
            m_r  = train_model(tr_r, V31_FEATURES, seed=seed * 1000 + 42)
            r_r  = eval_model(m_r, te_r, V31_FEATURES, n_avail=len(te_r_ids))
            ev_store[regime].append(r_r["ev"])

    return ev_store


# ── Summary helpers ───────────────────────────────────────────────────────────

def summarize_ev(label: str, ev_list: list[float], n_seeds: int = N_SEEDS) -> dict:
    arr = np.array(ev_list)
    mean = float(arr.mean())
    rng  = np.random.default_rng(42)
    boot = [float(np.mean(rng.choice(arr, size=n_seeds, replace=True))) for _ in range(N_BOOT)]
    lo, hi = ci95(boot)
    pct_pos = float((arr > 0).mean())
    return {"label": label, "mean_ev": mean, "ci_lo": lo, "ci_hi": hi, "pct_pos": pct_pos,
            "n_seeds": n_seeds}


def paired_delta(label: str, regime_ev: list, baseline_ev: list) -> dict:
    diff = np.array(regime_ev) - np.array(baseline_ev)
    mean = float(diff.mean())
    rng  = np.random.default_rng(44)
    idx  = rng.integers(0, len(diff), size=(N_BOOT, len(diff)))
    boot = diff[idx].mean(axis=1)
    lo, hi = ci95(boot.tolist())
    if lo > 0:    verdict = "✓ better"
    elif hi < 0:  verdict = "✗ worse"
    else:         verdict = "~ same"
    return {"label": label, "delta_mean": mean, "paired_lo": lo, "paired_hi": hi, "verdict": verdict}


# ── Train and save final models ───────────────────────────────────────────────

def train_final_models(df: pd.DataFrame) -> dict[str, lgb.Booster]:
    """Train on ALL labeled data for deployment."""
    models = {}
    for regime in REGIMES:
        sub = df[df["regime"] == regime]
        m = train_model(sub, V31_FEATURES, seed=42)
        if m is not None:
            out_path = OUT_DIR / f"lgb_regime_{regime.lower()}_t180.txt"
            m.save_model(str(out_path))
            models[regime] = m
            print(f"  Saved {out_path.name}  ({len(sub)} contracts)")
    m_all = train_model(df, V31_FEATURES, seed=42)
    if m_all:
        p = OUT_DIR / "lgb_v3p1_all_t180.txt"
        m_all.save_model(str(p))
        print(f"  Saved {p.name}  ({len(df)} contracts, v3.1 replication)")
    m_v32 = train_model(df, V32_FEATURES, seed=42)
    if m_v32:
        p = OUT_DIR / "lgb_v3p2_t180.txt"
        m_v32.save_model(str(p))
        print(f"  Saved {p.name}  ({len(df)} contracts, v3.2 with regime features)")
    return models


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    data_dir      = REPO_ROOT / "polymarket" / f"data_{COIN}_5m"
    outcomes_path = REPO_ROOT / "polymarket" / f"polymarket_{COIN.lower()}_5m_official_outcomes.csv"

    print("Loading regime labels...")
    regime_df = pd.read_csv(REGIME_CSV)
    regime_map = {r["market_slug"]: r for r in regime_df.to_dict(orient="records")}
    for regime in REGIMES:
        n = (regime_df["regime"] == regime).sum()
        print(f"  {regime:5s}: {n} contracts")

    print("\nLoading BTC contracts...")
    contracts = load_data(data_dir, outcomes_path)
    cids, df  = build_df(contracts, T1_FOCUS, regime_map)
    print(f"  T1={T1_FOCUS}s  n={len(cids)} contracts with regime labels\n")

    # Distribution check
    for regime in REGIMES:
        n = (df["regime"] == regime).nunique() if False else (df.drop_duplicates("contract_id")["regime"] == regime).sum()
        print(f"  In training set — {regime}: {n}")

    # ── CV ───────────────────────────────────────────────────────────────────
    print("\n" + "="*70)
    print("CV EVALUATION (4 chronological expanding folds)")
    print("="*70)
    fold_results = run_cv(df, cids)

    cv_rows = []
    for target in ["ALL"] + REGIMES:
        folds = fold_results[target]
        if not folds:
            continue
        total_traded = sum(f["n_traded"] for f in folds)
        total_avail  = sum(f["n_test_avail"] for f in folds)
        total_pnl    = sum(f["ev"] * f["n_test_avail"] for f in folds)
        overall_ev   = total_pnl / max(total_avail, 1)
        print(f"\n  {target} model:")
        print(f"  {'fold':>5} {'n_train':>8} {'n_avail':>8} {'n_traded':>9} {'ev/avail':>10}")
        for f in folds:
            print(f"  {f['fold']:>5} {f['n_train']:>8} {f['n_test_avail']:>8} "
                  f"{f['n_traded']:>9} {f['ev']:>+10.5f}")
        print(f"  {'TOTAL':>5} {'':>8} {total_avail:>8} {total_traded:>9} {overall_ev:>+10.5f}")
        cv_rows.append({"target": target, "total_avail": total_avail,
                        "total_traded": total_traded, "overall_ev": overall_ev})

    pd.DataFrame(cv_rows).to_csv(OUT_DIR / "regime_cv_summary.csv", index=False)

    # ── 200-seed random splits ────────────────────────────────────────────────
    print("\n" + "="*70)
    print(f"200-SEED RANDOM SPLITS")
    print("="*70)
    ev_store = run_200seeds(df, cids)

    seed_summary = []
    print(f"\n{'label':20s} {'mean_ev':>9} {'ci_lo':>9} {'ci_hi':>9} {'%pos':>6}")
    print("-" * 58)
    for label in ["ALL", "UP", "FLAT", "DOWN", "v3.2"]:
        s = summarize_ev(label, ev_store[label])
        seed_summary.append(s)
        print(f"{label:20s} {s['mean_ev']:>+9.5f} {s['ci_lo']:>+9.5f} {s['ci_hi']:>+9.5f} {s['pct_pos']:>6.1%}")

    pd.DataFrame(seed_summary).to_csv(OUT_DIR / "regime_200seed_summary.csv", index=False)

    # ── Paired comparison: regime model vs v3.1 on matching regime test contracts
    print(f"\n{'Paired bootstrap: regime-specific model vs v3.1 on same regime test contracts'}")
    print(f"{'label':20s} {'Δmean':>9} {'paired_lo':>10} {'paired_hi':>10} {'verdict':>12}")
    print("-" * 65)
    paired_rows = []
    for regime in REGIMES:
        d = paired_delta(
            f"regime_{regime} vs v3p1",
            ev_store[regime],
            ev_store[f"v3p1_on_{regime}"]
        )
        paired_rows.append(d)
        print(f"{d['label']:20s} {d['delta_mean']:>+9.5f} {d['paired_lo']:>+10.5f} "
              f"{d['paired_hi']:>+10.5f} {d['verdict']:>12}")

    # v3.2 vs ALL (v3.1)
    d32 = paired_delta("v3.2 vs v3.1_all", ev_store["v3.2"], ev_store["ALL"])
    paired_rows.append(d32)
    print(f"{d32['label']:20s} {d32['delta_mean']:>+9.5f} {d32['paired_lo']:>+10.5f} "
          f"{d32['paired_hi']:>+10.5f} {d32['verdict']:>12}")

    pd.DataFrame(paired_rows).to_csv(OUT_DIR / "regime_paired_vs_v3p1.csv", index=False)

    # ── Save per-seed EV table ────────────────────────────────────────────────
    ev_df = pd.DataFrame({k: v for k, v in ev_store.items()})
    ev_df.to_csv(OUT_DIR / "regime_ev_by_seed.csv", index=False)

    # ── Train and save final models ───────────────────────────────────────────
    print("\n" + "="*70)
    print("TRAINING FINAL MODELS (on all labeled data)")
    print("="*70)
    train_final_models(df)

    # ── Distribution CSV ──────────────────────────────────────────────────────
    dist = df.drop_duplicates("contract_id").groupby("regime")["contract_id"].count()
    dist.to_csv(OUT_DIR / "regime_distribution.csv", header=["n_contracts"])

    print(f"\nAll outputs → {OUT_DIR}/")
    print("Done.")


if __name__ == "__main__":
    main()
