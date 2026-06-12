#!/usr/bin/env python3
"""Exhaustive cost-bin search for SOL T=240s post-hoc filters.

Tests all single 0.10-wide intervals in [0.10, 0.90) plus every non-adjacent
pair of those intervals through the 200-seed OOS framework.

Key design choices to avoid overfitting:
  1. Candidate bins defined BEFORE reading any results (symmetric enumeration)
  2. All candidates evaluated in the same 200-seed run → Bonferroni correction
     applied to the final table
  3. Model predictions reused across filters within each seed (no extra training)
  4. Filters with avg_n < 3/seed flagged as too sparse to interpret

Each filter is a list of (lo, hi) half-open intervals.
A trade passes the filter if entry_cost falls in ANY interval AND model ≠ skip.

Structural invariants (identical to all prior scripts):
  build_candidates horizons=[30,60,120,180,240] → 517 contracts
  lgb_seed = seed * 1000 + 42
  random_split: rng.permutation → sorted()
  model: λ=0.5, mc=8, 200 rounds, depth=3, lr=0.06
"""
from __future__ import annotations
import math, csv
from itertools import combinations
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import pandas as pd
import lightgbm as lgb

REPO_ROOT = Path(__file__).resolve().parents[1]
COIN      = "SOL"
EVAL_HORIZON = 240
INDICATOR_WINDOW_SECONDS = 60.0
COST_ADD       = 0.01
MIN_TRAIN_ROWS = 30
N_SEEDS        = 200
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

# ── Candidate filter definitions ────────────────────────────────────────────
# Base intervals: every 0.10-wide window where trades actually occur
BASE_INTERVALS = [(lo/10, lo/10 + 0.10) for lo in range(1, 9)]  # [0.10,0.20)...[0.80,0.90)

def _filter_name(intervals: list[tuple[float,float]]) -> str:
    parts = [f"[{lo:.2f},{hi:.2f})" for lo, hi in intervals]
    return "∪".join(parts)

def _passes(entry_cost: float, intervals: list[tuple[float,float]]) -> bool:
    return any(lo <= entry_cost < hi for lo, hi in intervals)

# All single intervals
FILTERS: list[tuple[str, list[tuple[float,float]]]] = []
for iv in BASE_INTERVALS:
    FILTERS.append((_filter_name([iv]), [iv]))

# All pairs of non-adjacent intervals (gap ≥ 0.10 between them)
for i, iv_a in enumerate(BASE_INTERVALS):
    for iv_b in BASE_INTERVALS[i+2:]:  # skip adjacent (i+1)
        FILTERS.append((_filter_name([iv_a, iv_b]), [iv_a, iv_b]))

# All adjacent-pair (0.20-wide) intervals
for i in range(len(BASE_INTERVALS) - 1):
    pair = [BASE_INTERVALS[i], BASE_INTERVALS[i+1]]
    merged = [(BASE_INTERVALS[i][0], BASE_INTERVALS[i+1][1])]
    FILTERS.append((_filter_name(merged), merged))

# Baseline (all trades, no cost filter)
FILTERS.append(("baseline", [(0.0, 1.0)]))

print(f"Total filters to test: {len(FILTERS)}")
N_FILTERS = len(FILTERS)
ALPHA = 0.05
BONFERRONI_ALPHA = ALPHA / N_FILTERS  # threshold per test

# ── Data helpers ─────────────────────────────────────────────────────────────

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
    """Train on train split, return per-contract (pnl, entry_cost, side) for ALL test contracts."""
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

    results = {}  # contract_id → (pnl, entry_cost, side)
    for i, (_, row) in enumerate(te_df.iterrows()):
        cid = str(row["contract_id"])
        pc  = int(pred[i])
        actual = int(row["actual_label"])
        if pc == CLASS_YES:
            eff = float(row["yes_effective_cost"])
            ec  = float(row["yes_cost"])
            pnl = (1.0 - eff) if actual else -eff
            results[cid] = (pnl, ec, "yes")
        elif pc == CLASS_NO:
            eff = float(row["no_effective_cost"])
            ec  = float(row["no_cost"])
            pnl = (1.0 - eff) if (1 - actual) else -eff
            results[cid] = (pnl, ec, "no")
        else:
            ec = min(float(row["yes_cost"]), float(row["no_cost"]))
            results[cid] = (0.0, ec, "skip")
    return te_ids, results

def bootstrap_ci(evs, n_iter=2000, alpha=0.05):
    arr = np.array([e for e in evs if not np.isnan(e)])
    if len(arr) < 2: return float("nan"), float("nan")
    rng  = np.random.default_rng(999)
    means = [rng.choice(arr, len(arr), replace=True).mean() for _ in range(n_iter)]
    return float(np.percentile(means, 100*alpha/2)), float(np.percentile(means, 100*(1-alpha/2)))

def main():
    data_dir     = REPO_ROOT / "polymarket" / f"data_{COIN}_5m"
    outcomes_csv = REPO_ROOT / "polymarket" / f"polymarket_{COIN.lower()}_5m_official_outcomes.csv"
    out_dir      = Path(__file__).parent

    print("Building candidates …")
    outcomes = load_outcomes(outcomes_csv)
    contract_ids, data = build_candidates(data_dir, outcomes)
    hdata = data[data["t_seconds"] == EVAL_HORIZON].copy()
    print(f"  contract_ids: {len(contract_ids)}  hdata: {len(hdata)} rows")

    # Per filter: list of (ev_all, n_filtered) per seed
    filter_ev_all:  dict[str, list[float]] = {fname: [] for fname, _ in FILTERS}
    filter_n_filt:  dict[str, list[int]]   = {fname: [] for fname, _ in FILTERS}

    for seed in range(N_SEEDS):
        te_ids, results = run_seed(seed, hdata, contract_ids)
        n_test = len(te_ids)
        for fname, intervals in FILTERS:
            pnl_sum = 0.0; n_filt = 0
            for cid in te_ids:
                if cid not in results: continue
                pnl, ec, side = results[cid]
                if side != "skip" and _passes(ec, intervals):
                    pnl_sum += pnl; n_filt += 1
            filter_ev_all[fname].append(pnl_sum / n_test if n_test > 0 else float("nan"))
            filter_n_filt[fname].append(n_filt)
        if (seed + 1) % 50 == 0:
            # Show running stats for top-5 filters by mean ev_all so far
            running = []
            for fname, _ in FILTERS:
                evs = [e for e in filter_ev_all[fname] if not math.isnan(e)]
                if not evs: continue
                running.append((np.mean(evs), fname))
            running.sort(reverse=True)
            print(f"  seed {seed+1:3d}: top-3 filters by mean ev_all:")
            for mean_ev, fname in running[:3]:
                evs = [e for e in filter_ev_all[fname] if not math.isnan(e)]
                n_pos = sum(e > 0 for e in evs)
                avg_n = np.mean(filter_n_filt[fname])
                print(f"           {fname:<35} ev_all={mean_ev:+.5f}  {n_pos}/{len(evs)} pos  avg_n={avg_n:.1f}")

    # Compute final stats
    records = []
    for fname, intervals in FILTERS:
        evs = [e for e in filter_ev_all[fname] if not math.isnan(e)]
        ns  = filter_n_filt[fname]
        if not evs:
            continue
        n_pos  = sum(e > 0 for e in evs)
        pct    = n_pos / len(evs)
        mean   = float(np.mean(evs))
        ci_lo, ci_hi = bootstrap_ci(evs, alpha=ALPHA)
        bonf_lo, bonf_hi = bootstrap_ci(evs, alpha=BONFERRONI_ALPHA)
        avg_n  = float(np.mean(ns))

        # Slice stability
        e50a  = [e for e in filter_ev_all[fname][:50] if not math.isnan(e)]
        e50b  = [e for e in filter_ev_all[fname][50:100] if not math.isnan(e)]
        e100b = [e for e in filter_ev_all[fname][100:] if not math.isnan(e)]
        p50a  = sum(e>0 for e in e50a)/len(e50a) if e50a else 0
        p50b  = sum(e>0 for e in e50b)/len(e50b) if e50b else 0
        p100b = sum(e>0 for e in e100b)/len(e100b) if e100b else 0
        m50a  = float(np.mean(e50a)) if e50a else float("nan")
        m50b  = float(np.mean(e50b)) if e50b else float("nan")
        m100b = float(np.mean(e100b)) if e100b else float("nan")

        records.append({
            "filter": fname, "intervals": str(intervals),
            "pct_pos": pct, "n_pos": n_pos,
            "mean_ev_all": mean,
            "ci95_lo": ci_lo, "ci95_hi": ci_hi,
            "bonf_lo": bonf_lo, "bonf_hi": bonf_hi,
            "avg_n": avg_n,
            "p_s0_49": p50a, "m_s0_49": m50a,
            "p_s50_99": p50b, "m_s50_99": m50b,
            "p_s100_199": p100b, "m_s100_199": m100b,
        })

    records.sort(key=lambda r: -r["mean_ev_all"])

    # Save CSV
    csv_path = out_dir / "sol_t240_bin_search_results.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        w.writeheader(); w.writerows(records)

    # Print ranked table
    print(f"\n{'='*120}")
    print(f"  FULL BIN SEARCH — {N_FILTERS} filters, N_SEEDS={N_SEEDS}, Bonferroni α={BONFERRONI_ALPHA:.4f}")
    print(f"{'='*120}")
    print(f"  {'Filter':<38} {'%Pos':>5} {'MeanEV':>9} {'CI95_lo':>9} {'CI95_hi':>9} "
          f"{'Bonf_lo':>9} {'Bonf_hi':>9} {'AvgN':>6} {'s0-49':>6} {'s50-99':>7} {'s100-199':>9} {'Sparse':>7}")
    print(f"  {'-'*116}")
    for r in records:
        sparse = "SPARSE" if r["avg_n"] < 3 else ""
        bonf_star = "*" if r["bonf_lo"] > 0 else (" " if r["ci95_lo"] > 0 else "")
        print(f"  {r['filter']:<38} {r['pct_pos']:>5.1%} {r['mean_ev_all']:>+9.5f} "
              f"{r['ci95_lo']:>+9.5f} {r['ci95_hi']:>+9.5f} "
              f"{r['bonf_lo']:>+9.5f} {r['bonf_hi']:>+9.5f} "
              f"{r['avg_n']:>6.1f} "
              f"{r['p_s0_49']:>6.1%} {r['p_s50_99']:>7.1%} {r['p_s100_199']:>9.1%} "
              f"{sparse:>7}{bonf_star}")

    print(f"\n  * = CI95_lo > 0 after Bonferroni correction  (α/N = {BONFERRONI_ALPHA:.4f})")
    print(f"\n  TOP CANDIDATES (CI95_lo > 0, avg_n ≥ 3/seed):")
    top = [r for r in records if r["ci95_lo"] > 0 and r["avg_n"] >= 3]
    if not top:
        print("  None — no filter clears CI95_lo > 0 with avg_n ≥ 3")
    for r in top:
        bonf_note = "(also Bonferroni-significant)" if r["bonf_lo"] > 0 else "(CI95 only, not Bonferroni)"
        print(f"  {r['filter']:<38} mean={r['mean_ev_all']:+.5f} CI95=[{r['ci95_lo']:+.5f},{r['ci95_hi']:+.5f}] "
              f"avg_n={r['avg_n']:.1f}  {bonf_note}")

    print(f"\nResults → {csv_path}")

if __name__ == "__main__":
    main()
