#!/usr/bin/env python3
"""Compare fixed-N vs fixed-value position sizing for the [0.10,0.20)∪[0.40,0.50) filter.

Fixed-N:     buy 1 contract per trade (current strategy).
Fixed-value: spend $ref_cost per trade → buy ref_cost/eff_cost contracts.
             At 10c ask (eff=0.11) you buy ~4x more contracts than at 40c ask (eff=0.41).

ref_cost is set to the mean effective cost of all filtered trades across the 200-seed
OOS study, so both strategies deploy identical average capital per trade.

Key design: identical splits, model, and seed sequence as prior scripts (no extra info).
"""
from __future__ import annotations
import math, csv
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import pandas as pd
import lightgbm as lgb

REPO_ROOT = Path(__file__).resolve().parents[1]
COIN         = "SOL"
EVAL_HORIZON = 240
INDICATOR_WINDOW_SECONDS = 60.0
COST_ADD     = 0.01
MIN_TRAIN_ROWS = 30
N_SEEDS      = 200
CLASS_YES, CLASS_NO, CLASS_SKIP = 0, 1, 2
POLYMARKET_REQUIRED_QUOTE_COLS = ("up_best_bid","up_best_ask","down_best_bid","down_best_ask")
BUILD_HORIZONS = [30, 60, 120, 180, 240]

BASE_FEATURES = [
    "p_yes_mid",
    "yes_mid_z_60","yes_mid_vol_60",
    "yes_mid_z_20","yes_mid_vol_20",
    "mid_change_from_open",
    "book_qty_log",
    "OBI","OBI_vol_60","OBI_z_60","OBI_vol_20",
    "yes_book_imbalance_tau_1c",
    "yes_book_imbalance_tau_5c",
    "yes_book_imbalance_tau_10c",
    "obi_depth_slope",
]
CFG = {
    "features": BASE_FEATURES, "max_depth": 3, "num_leaves": None,
    "lambda_l2": 0.5, "lambda_l1": 0.0, "min_child_samples": 8,
    "n_rounds": 200, "feature_fraction": 0.90, "subsample": 0.90,
    "learning_rate": 0.06, "boosting": "gbdt",
}
FILTER_BANDS = ((0.10, 0.20), (0.40, 0.50))

def _passes(ec): return any(lo <= ec < hi for lo, hi in FILTER_BANDS)

# ── Data helpers (identical to all prior scripts) ─────────────────────────────

def fnum(v):
    try:
        out = float(v); return out if math.isfinite(out) else None
    except: return None

def first_valid(s):
    v = s.dropna(); return v.iloc[0] if not v.empty else None

def series_stats(values, last=None):
    c = pd.to_numeric(values, errors="coerce").dropna()
    if c.empty: return {"z": float("nan"), "vol": float("nan")}
    lv = float(c.iloc[-1] if last is None else last)
    mean = float(c.mean()); vol = float(c.std(ddof=0)) if len(c) > 1 else 0.0
    z = (lv - mean) / vol if vol > 1e-12 else 0.0
    return {"z": z, "vol": vol}

def _preflight_ok(path):
    try: df = pd.read_csv(path, low_memory=False)
    except: return False
    if len(df) == 0: return False
    for c in POLYMARKET_REQUIRED_QUOTE_COLS:
        if c not in df.columns: return False
    return bool(pd.concat([pd.to_numeric(df[c], errors="coerce").notna()
                            for c in POLYMARKET_REQUIRED_QUOTE_COLS], axis=1).all(axis=1).any())

def _valid_quote(row):
    if str(row.get("book_state_complete","")).strip() not in {"1","1.0","True","true"}: return False
    for p in ("up","down"):
        b, a = fnum(row.get(f"{p}_best_bid")), fnum(row.get(f"{p}_best_ask"))
        if b is None or a is None or not (0<=b<=1 and 0<a<1) or b>a: return False
    return True

def _slug(path):
    s = path.stem; return s.split("_5m_",1)[1] if "_5m_" in s else s

def load_outcomes(path):
    out = {}
    for row in pd.read_csv(path).to_dict(orient="records"):
        slug = str(row.get("market_slug") or "").strip()
        w    = str(row.get("winning_outcome") or "").strip()
        if slug and w in {"Up","Down"}: out[slug] = row
    return out

def build_candidates(data_dir, outcomes):
    @dataclass(frozen=True)
    class Meta:
        contract_id: str; close_time: pd.Timestamp
    all_paths = [p for p in sorted(data_dir.glob("*.csv")) if _preflight_ok(p)]
    metas = []; rows = []
    for path in all_paths:
        try: df = pd.read_csv(path, low_memory=False)
        except: continue
        slug_val = first_valid(df.get("market_slug", pd.Series(dtype=object)))
        slug     = str(slug_val).strip() if slug_val and str(slug_val).strip() else _slug(path)
        outcome  = outcomes.get(slug)
        if not outcome: continue
        actual   = 1 if str(outcome.get("winning_outcome")) == "Up" else 0
        ct       = pd.to_datetime(outcome.get("event_end_utc"), utc=True, errors="coerce")
        if pd.isna(ct): continue
        df = df.copy()
        df["_ts"]  = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
        df["_rem"] = (ct - df["_ts"]).dt.total_seconds() if "seconds_to_close" not in df.columns \
                     else pd.to_numeric(df["seconds_to_close"], errors="coerce")
        df = df[df["_rem"].notna() & (df["_rem"] >= 0)].copy()
        if df.empty: continue
        up_mid_open = None
        for _, r in df.iterrows():
            ub, ua = fnum(r.get("up_best_bid")), fnum(r.get("up_best_ask"))
            if ub is not None and ua is not None and 0 < ua < 1:
                up_mid_open = (ub + ua) / 2.0; break
        contract_rows = []
        for horizon in BUILD_HORIZONS:
            cands = df[(df["_rem"] - horizon).abs() <= 5.0]
            if cands.empty: continue
            idx   = (cands["_rem"] - horizon).abs().idxmin()
            quote = cands.loc[idx]
            if not _valid_quote(quote): continue
            rt  = quote["_ts"]
            h60 = df[df["_ts"].notna() & (df["_ts"] <= rt) &
                     ((rt - df["_ts"]).dt.total_seconds() <= INDICATOR_WINDOW_SECONDS)].copy()
            if h60.empty: h60 = quote.to_frame().T
            h20 = h60[(rt - h60["_ts"]).dt.total_seconds() <= 20.0].copy()
            if h20.empty: h20 = quote.to_frame().T
            ya, yb = fnum(quote.get("up_best_ask")), fnum(quote.get("up_best_bid"))
            na, nb = fnum(quote.get("down_best_ask")), fnum(quote.get("down_best_bid"))
            if any(v is None for v in (ya, yb, na, nb)): continue
            up_mid = (yb + ya) / 2.0
            if not (0 < ya < 1 and 0 < na < 1 and 0 <= up_mid <= 1): continue
            ubq = fnum(quote.get("up_best_bid_size")) or 0.0
            dbq = fnum(quote.get("down_best_bid_size")) or 0.0
            def _ms(h): return pd.to_numeric(h.get("up_mid", pd.Series(dtype=float)), errors="coerce")
            def _os(h):
                u = pd.to_numeric(h.get("up_best_bid_size",   pd.Series(dtype=float)), errors="coerce").fillna(0.0)
                d = pd.to_numeric(h.get("down_best_bid_size", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
                return (u - d) / (u + d + 1e-9)
            obi_cur = (ubq - dbq) / (ubq + dbq + 1e-9)
            ym60 = series_stats(_ms(h60), up_mid); ym20 = series_stats(_ms(h20), up_mid)
            ob60 = series_stats(_os(h60), obi_cur); ob20 = series_stats(_os(h20), obi_cur)
            yb1  = fnum(quote.get("up_book_imbalance_tau_1c"))
            yb5  = fnum(quote.get("up_book_imbalance_tau_5c"))
            yb10 = fnum(quote.get("up_book_imbalance_tau_10c"))
            if any(v is None for v in (yb1, yb5, yb10)): continue
            rd = {
                "contract_id": slug, "close_time": ct, "t_seconds": int(horizon),
                "actual_label": actual,
                "yes_cost": ya, "no_cost": na,
                "yes_effective_cost": ya + COST_ADD, "no_effective_cost": na + COST_ADD,
                "p_yes_mid": up_mid,
                "yes_mid_z_60": ym60["z"], "yes_mid_vol_60": ym60["vol"],
                "yes_mid_z_20": ym20["z"], "yes_mid_vol_20": ym20["vol"],
                "mid_change_from_open": (up_mid - up_mid_open) if up_mid_open is not None else float("nan"),
                "book_qty_log": math.log1p(ubq + dbq),
                "OBI": obi_cur, "OBI_vol_60": ob60["vol"], "OBI_z_60": ob60["z"], "OBI_vol_20": ob20["vol"],
                "yes_book_imbalance_tau_1c": yb1, "yes_book_imbalance_tau_5c": yb5,
                "yes_book_imbalance_tau_10c": yb10, "obi_depth_slope": yb1 - yb10,
            }
            if any(not math.isfinite(float(rd.get(f, float("nan")))) for f in BASE_FEATURES):
                continue
            contract_rows.append(rd)
        if contract_rows:
            metas.append(Meta(slug, ct)); rows.extend(contract_rows)
    metas = sorted(metas, key=lambda m: m.close_time)
    return [m.contract_id for m in metas], pd.DataFrame(rows) if rows else pd.DataFrame()

def _softmax(x):
    e = np.exp(x - x.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)

def _make_profit_objective(c_yes, c_no):
    n = len(c_yes)
    def fobj(y_pred, dataset):
        y = dataset.get_label().astype(float)
        q = _softmax(np.asarray(y_pred).reshape(n, 3))
        v = np.column_stack([y - c_yes, (1.0 - y) - c_no, np.zeros(n)])
        ws = (v * q).sum(axis=1, keepdims=True)
        return -(q * (v - ws)).ravel(), np.maximum(q * (1.0 - q), 1e-6).ravel()
    return fobj

def _lgb_params(lgb_seed):
    nl = CFG.get("num_leaves") or max(4, 2 ** CFG["max_depth"] - 1)
    return {
        "boosting_type": CFG.get("boosting", "gbdt"),
        "objective": "none", "num_class": 3,
        "num_leaves": nl, "max_depth": CFG["max_depth"],
        "min_child_samples": CFG["min_child_samples"],
        "subsample": CFG["subsample"], "feature_fraction": CFG["feature_fraction"],
        "lambda_l2": CFG["lambda_l2"], "lambda_l1": CFG.get("lambda_l1", 0.0),
        "learning_rate": CFG["learning_rate"],
        "num_threads": 4, "seed": lgb_seed, "verbose": -1,
    }

def random_split(ids, seed, frac=0.20):
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(ids))
    cut = int(len(ids) * (1.0 - frac))
    return [ids[i] for i in sorted(idx[:cut])], [ids[i] for i in sorted(idx[cut:])]

def run_seed(seed, hdata, contract_ids):
    """Returns test_ids and per-contract (pnl, entry_cost, eff_cost, side)."""
    features = CFG["features"]
    tr_ids, te_ids = random_split(contract_ids, seed)
    lgb_seed = seed * 1000 + 42
    tr_df = hdata[hdata["contract_id"].isin(set(tr_ids))].copy()
    te_df = hdata[hdata["contract_id"].isin(set(te_ids))].copy()
    y = tr_df["actual_label"].astype(float).to_numpy()
    if len(tr_df) < MIN_TRAIN_ROWS or len(np.unique(y.astype(int))) < 2:
        return te_ids, {}
    c_yes = tr_df["yes_effective_cost"].to_numpy()
    c_no  = tr_df["no_effective_cost"].to_numpy()
    X     = tr_df[features].to_numpy().astype(float)
    ds    = lgb.Dataset(X, label=y, free_raw_data=False); ds.construct()
    m     = lgb.Booster(params=_lgb_params(lgb_seed), train_set=ds)
    for _ in range(CFG["n_rounds"]):
        m.update(fobj=_make_profit_objective(c_yes, c_no))
    if len(te_df) == 0:
        return te_ids, {}
    raw  = np.asarray(m.predict(te_df[features].to_numpy().astype(float))).reshape(len(te_df), 3)
    q    = _softmax(raw)
    pred = np.argmax(q, axis=1)
    results = {}
    for i, (_, row) in enumerate(te_df.iterrows()):
        cid    = str(row["contract_id"])
        pc     = int(pred[i])
        actual = int(row["actual_label"])
        if pc == CLASS_YES:
            eff = float(row["yes_effective_cost"]); ec = float(row["yes_cost"])
            pnl = (1.0 - eff) if actual else -eff
            results[cid] = (pnl, ec, eff, "yes")
        elif pc == CLASS_NO:
            eff = float(row["no_effective_cost"]); ec = float(row["no_cost"])
            pnl = (1.0 - eff) if (1 - actual) else -eff
            results[cid] = (pnl, ec, eff, "no")
        else:
            ec = min(float(row["yes_cost"]), float(row["no_cost"]))
            results[cid] = (0.0, ec, ec + COST_ADD, "skip")
    return te_ids, results

def bootstrap_ci(arr, n_iter=2000, alpha=0.05):
    a = np.array([x for x in arr if not math.isnan(x)])
    if len(a) < 2: return float("nan"), float("nan")
    rng = np.random.default_rng(999)
    means = [rng.choice(a, len(a), replace=True).mean() for _ in range(n_iter)]
    return float(np.percentile(means, 100*alpha/2)), float(np.percentile(means, 100*(1-alpha/2)))

def main():
    data_dir     = REPO_ROOT / "polymarket" / f"data_{COIN}_5m"
    outcomes_csv = REPO_ROOT / "polymarket" / f"polymarket_{COIN.lower()}_5m_official_outcomes.csv"

    print("Building candidates …")
    outcomes = load_outcomes(outcomes_csv)
    contract_ids, data = build_candidates(data_dir, outcomes)
    hdata = data[data["t_seconds"] == EVAL_HORIZON].copy()
    n_contracts = len(contract_ids)
    print(f"  {n_contracts} contracts  {len(hdata)} T=240s rows")

    # Collect all per-seed results first (store raw data for two-pass analysis)
    all_seed_data: list[tuple[list, list, list, int]] = []
    # Each entry: (pnl_list, eff_list, bin_id_list, n_test)
    # bin_id: 0=[0.10,0.20), 1=[0.40,0.50)

    print(f"Running {N_SEEDS} seeds …")
    for seed in range(N_SEEDS):
        te_ids, results = run_seed(seed, hdata, contract_ids)
        n_test = len(te_ids)
        pnl_list, eff_list, bid_list = [], [], []
        for cid in te_ids:
            if cid not in results: continue
            pnl, ec, eff, side = results[cid]
            if side == "skip" or not _passes(ec): continue
            pnl_list.append(pnl); eff_list.append(eff)
            bid_list.append(0 if 0.10 <= ec < 0.20 else 1)
        all_seed_data.append((pnl_list, eff_list, bid_list, n_test))
        if (seed + 1) % 50 == 0:
            print(f"  seed {seed+1:3d}: avg {np.mean([len(d[0]) for d in all_seed_data]):.1f} filtered/seed")

    # Compute ref_cost = mean eff_cost across all filtered trades (all seeds pooled)
    all_eff = [e for pnl_l, eff_l, _, _ in all_seed_data for e in eff_l]
    if not all_eff:
        print("ERROR: no filtered trades found"); return
    ref_cost = float(np.mean(all_eff))

    # Bin-level average eff_cost
    eff_lo = [e for _, eff_l, bid_l, _ in all_seed_data
              for e, b in zip(eff_l, bid_l) if b == 0]
    eff_hi = [e for _, eff_l, bid_l, _ in all_seed_data
              for e, b in zip(eff_l, bid_l) if b == 1]
    avg_eff_lo = float(np.mean(eff_lo)) if eff_lo else float("nan")
    avg_eff_hi = float(np.mean(eff_hi)) if eff_hi else float("nan")
    scale_lo = ref_cost / avg_eff_lo
    scale_hi = ref_cost / avg_eff_hi

    print(f"\n  ref_cost = {ref_cost:.4f}  (mean eff_cost of all filtered OOS trades)")
    print(f"  [0.10,0.20) avg eff_cost = {avg_eff_lo:.4f}  → fixed-V scale = {scale_lo:.2f}x")
    print(f"  [0.40,0.50) avg eff_cost = {avg_eff_hi:.4f}  → fixed-V scale = {scale_hi:.2f}x")

    # Compute per-seed EV/all for both strategies
    ev_N_all, ev_V_all, avg_n_list = [], [], []
    bin_N_lo, bin_V_lo = [], []
    bin_N_hi, bin_V_hi = [], []

    for pnl_list, eff_list, bid_list, n_test in all_seed_data:
        avg_n_list.append(len(pnl_list))
        if n_test == 0:
            ev_N_all.append(float("nan")); ev_V_all.append(float("nan"))
            bin_N_lo.append(float("nan")); bin_V_lo.append(float("nan"))
            bin_N_hi.append(float("nan")); bin_V_hi.append(float("nan"))
            continue
        pnl_N = sum(pnl_list) / n_test
        pnl_V = sum(p * ref_cost / e for p, e in zip(pnl_list, eff_list)) / n_test
        ev_N_all.append(pnl_N); ev_V_all.append(pnl_V)
        plo_N = [p for p, b in zip(pnl_list, bid_list) if b == 0]
        plo_V = [p * ref_cost / e for p, e, b in zip(pnl_list, eff_list, bid_list) if b == 0]
        phi_N = [p for p, b in zip(pnl_list, bid_list) if b == 1]
        phi_V = [p * ref_cost / e for p, e, b in zip(pnl_list, eff_list, bid_list) if b == 1]
        bin_N_lo.append(float(np.mean(plo_N)) if plo_N else float("nan"))
        bin_V_lo.append(float(np.mean(plo_V)) if plo_V else float("nan"))
        bin_N_hi.append(float(np.mean(phi_N)) if phi_N else float("nan"))
        bin_V_hi.append(float(np.mean(phi_V)) if phi_V else float("nan"))

    def stats(arr):
        a = [x for x in arr if not math.isnan(x)]
        if not a: return {"mean": float("nan"), "ci_lo": float("nan"), "ci_hi": float("nan"), "pct_pos": 0.0}
        mean = float(np.mean(a)); ci = bootstrap_ci(a)
        return {"mean": mean, "ci_lo": ci[0], "ci_hi": ci[1], "pct_pos": sum(x>0 for x in a)/len(a)}

    sN = stats(ev_N_all); sV = stats(ev_V_all)
    avg_n = float(np.mean(avg_n_list))
    n_test_avg = n_contracts * 0.20

    # EV per filtered trade
    ev_N_t = sN["mean"] * n_test_avg / avg_n if avg_n > 0 else float("nan")
    ev_V_t = sV["mean"] * n_test_avg / avg_n if avg_n > 0 else float("nan")

    print(f"\n{'='*80}")
    print(f"  FIXED-N vs FIXED-VALUE SIZING  (filter: [0.10,0.20)∪[0.40,0.50))")
    print(f"  Model: λ=0.5, mc=8, 200 rounds, depth=3, T=240s | N_SEEDS={N_SEEDS}")
    print(f"  ref_cost={ref_cost:.4f}  avg_filtered/seed={avg_n:.1f}  n_contracts={n_contracts}")
    print(f"{'='*80}")
    print(f"\n  {'Metric':<34} {'Fixed-N (1 contract)':>22} {'Fixed-V ($ref each)':>22}")
    print(f"  {'-'*80}")
    print(f"  {'EV/all (mean)':<34} {sN['mean']:>+22.5f} {sV['mean']:>+22.5f}")
    print(f"  {'EV/all CI95_lo':<34} {sN['ci_lo']:>+22.5f} {sV['ci_lo']:>+22.5f}")
    print(f"  {'EV/all CI95_hi':<34} {sN['ci_hi']:>+22.5f} {sV['ci_hi']:>+22.5f}")
    print(f"  {'% positive seeds':<34} {sN['pct_pos']:>22.1%} {sV['pct_pos']:>22.1%}")
    print(f"  {'EV/trade (approx)':<34} {ev_N_t:>+22.5f} {ev_V_t:>+22.5f}")
    print(f"  {'ROI/$ (EV/trade / ref_cost)':<34} {ev_N_t/ref_cost:>+22.4f} {ev_V_t/ref_cost:>+22.4f}")

    print(f"\n  SEED SLICE STABILITY:")
    print(f"  {'Slice':<12} {'N mean':>9} {'N %pos':>8} {'V mean':>9} {'V %pos':>8}")
    for label, sl in [("s0-49", slice(0,50)), ("s50-99", slice(50,100)), ("s100-199", slice(100,200))]:
        sN_sl = stats(ev_N_all[sl]); sV_sl = stats(ev_V_all[sl])
        print(f"  {label:<12} {sN_sl['mean']:>+9.5f} {sN_sl['pct_pos']:>8.1%} "
              f"{sV_sl['mean']:>+9.5f} {sV_sl['pct_pos']:>8.1%}")

    print(f"\n  PER-BIN BREAKDOWN (EV/trade for contracts in that bin only):")
    print(f"  {'Bin':<18} {'eff_cost':>9} {'scale':>7} | "
          f"{'N mean':>9} {'N CI95_lo':>11} {'N CI95_hi':>11} | "
          f"{'V mean':>9} {'V CI95_lo':>11} {'V CI95_hi':>11}")
    for bname, bn, bv, avg_eff in [
        ("[0.10,0.20)", bin_N_lo, bin_V_lo, avg_eff_lo),
        ("[0.40,0.50)", bin_N_hi, bin_V_hi, avg_eff_hi),
    ]:
        sc = ref_cost / avg_eff
        sNb = stats(bn); sVb = stats(bv)
        print(f"  {bname:<18} {avg_eff:>9.4f} {sc:>7.2f}x | "
              f"{sNb['mean']:>+9.5f} {sNb['ci_lo']:>+11.5f} {sNb['ci_hi']:>+11.5f} | "
              f"{sVb['mean']:>+9.5f} {sVb['ci_lo']:>+11.5f} {sVb['ci_hi']:>+11.5f}")

    impr = (sV["mean"] / sN["mean"] - 1) * 100 if abs(sN["mean"]) > 1e-10 else float("nan")
    print(f"\n  EV/all improvement: {impr:+.1f}%  (positive = fixed-V is better)")
    if sV["mean"] > sN["mean"]:
        print(f"  [0.10,0.20) has higher EV/$ → amplifying cheap trades improves total EV.")
        print(f"  But CI95 width determines if this difference is statistically meaningful.")

if __name__ == "__main__":
    main()
