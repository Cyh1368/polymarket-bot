#!/usr/bin/env python3
"""OOS stability study for post-hoc confidence and cost filters on SOL T=240s.

Question: the in-sample analysis found the confidence [0.40, 0.50) bin shows
positive EV (+0.080/trade, 72 trades). Does this hold OOS across 200 seeds?

Filters tested:
  F1: conf ∈ [0.40, 0.50)          — model barely decides to trade
  F2: entry_cost ∈ [0.10, 0.30)    — low-cost entries (long-shot bets)
  F3: conf [0.40, 0.50) AND entry_cost [0.10, 0.30)  — combined
  baseline: no filter (same as prior 200-seed study)

EV reported two ways:
  ev_all      = sum(filtered_pnl) / n_all_test_contracts  (skips & filtered-out = 0)
  ev_filtered = sum(filtered_pnl) / n_filtered_trades     (only traded+filtered)

Structural invariants (identical to prior scripts):
  - build_candidates horizons=[30,60,120,180,240] → contract_ids=517
  - lgb_seed = seed * 1000 + 42
  - random_split: rng.permutation → sorted()
  - model: λ=0.5, mc=8, 200 rounds, depth=3, lr=0.06
"""
from __future__ import annotations
import math, csv
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

# Post-hoc filters: each maps (conf, entry_cost, side) → bool (True = keep trade)
FILTERS = {
    "baseline":     lambda c, ec, s: s != "skip",
    "conf_40_50":   lambda c, ec, s: s != "skip" and 0.40 <= c < 0.50,
    "cost_10_30":   lambda c, ec, s: s != "skip" and 0.10 <= ec < 0.30,
    "combined":     lambda c, ec, s: s != "skip" and 0.40 <= c < 0.50 and 0.10 <= ec < 0.30,
}

# -----------------------------------------------------------------------
# Data helpers (structurally identical to prior scripts)
# -----------------------------------------------------------------------

def fnum(v):
    try:
        out = float(v); return out if math.isfinite(out) else None
    except: return None

def first_valid(s):
    v = s.dropna(); return v.iloc[0] if not v.empty else None

def series_stats(values, last=None):
    c = pd.to_numeric(values, errors="coerce").dropna()
    if c.empty: return {"z": float("nan"), "vol": float("nan"), "change": float("nan")}
    lv = float(c.iloc[-1] if last is None else last)
    mean = float(c.mean()); vol = float(c.std(ddof=0)) if len(c) > 1 else 0.0
    z = (lv - mean) / vol if vol > 1e-12 else 0.0
    return {"z": z, "vol": vol, "change": float(lv - c.iloc[0])}

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
    """Train on train split, predict on test split. Returns per-contract (pnl, conf, side, entry_cost)."""
    features = CFG["features"]
    tr_ids, te_ids = random_split(contract_ids, seed)
    lgb_seed = seed * 1000 + 42
    tr_df = hdata[hdata["contract_id"].isin(set(tr_ids))].copy()
    te_df = hdata[hdata["contract_id"].isin(set(te_ids))].copy()
    y = tr_df["actual_label"].astype(float).to_numpy()
    if len(tr_df) < MIN_TRAIN_ROWS or len(np.unique(y.astype(int))) < 2:
        return te_ids, {}, {}
    c_yes = tr_df["yes_effective_cost"].to_numpy()
    c_no  = tr_df["no_effective_cost"].to_numpy()
    X     = tr_df[features].to_numpy().astype(float)
    ds    = lgb.Dataset(X, label=y, free_raw_data=False); ds.construct()
    m     = lgb.Booster(params=_lgb_params(lgb_seed), train_set=ds)
    for _ in range(CFG["n_rounds"]):
        m.update(fobj=_make_profit_objective(c_yes, c_no))
    if len(te_df) == 0:
        return te_ids, {}, {}
    raw  = np.asarray(m.predict(te_df[features].to_numpy().astype(float))).reshape(len(te_df), 3)
    q    = _softmax(raw)
    pred = np.argmax(q, axis=1)
    conf = q[np.arange(len(q)), pred]
    te_df = te_df.copy()
    te_df["pred"] = pred
    te_df["conf"] = conf

    pnl_map = {}
    meta_map = {}  # contract_id → (conf, side, entry_cost)
    for i, (_, row) in enumerate(te_df.iterrows()):
        pc = int(row["pred"]); actual = int(row["actual_label"])
        cid = str(row["contract_id"])
        cf  = float(conf[i])
        if pc == CLASS_YES:
            eff = float(row["yes_effective_cost"])
            ec  = float(row["yes_cost"])
            success = actual
            pnl_map[cid] = (1.0 - eff) if success else -eff
            meta_map[cid] = (cf, "yes", ec)
        elif pc == CLASS_NO:
            eff = float(row["no_effective_cost"])
            ec  = float(row["no_cost"])
            success = 1 - actual
            pnl_map[cid] = (1.0 - eff) if success else -eff
            meta_map[cid] = (cf, "no", ec)
        else:
            pnl_map[cid] = 0.0
            meta_map[cid] = (cf, "skip", min(float(row["yes_cost"]), float(row["no_cost"])))
    return te_ids, pnl_map, meta_map

def bootstrap_ci(evs, n_iter=2000, alpha=0.05):
    arr = np.array([e for e in evs if not np.isnan(e)])
    if len(arr) == 0: return float("nan"), float("nan")
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

    _, seed0_te = random_split(contract_ids, 0)
    print(f"  contract_ids : {len(contract_ids)}")
    print(f"  hdata T={EVAL_HORIZON}s  : {len(hdata)} rows")
    print(f"  seed=0 test  : {len(seed0_te)}")

    # Per-filter results: each filter gets a list of (ev_all, ev_filtered, n_filtered) per seed
    filter_results: dict[str, list[tuple[float,float,int]]] = {k: [] for k in FILTERS}

    for seed in range(N_SEEDS):
        te_ids, pnl_map, meta_map = run_seed(seed, hdata, contract_ids)
        n_test = len(te_ids)
        for fname, ffn in FILTERS.items():
            filtered_pnls = []
            for cid in te_ids:
                cf, side, ec = meta_map.get(cid, (0.0, "skip", 0.0))
                if ffn(cf, ec, side):
                    filtered_pnls.append(pnl_map.get(cid, 0.0))
            total_pnl = sum(filtered_pnls)
            n_filt = len(filtered_pnls)
            ev_all      = total_pnl / n_test if n_test > 0 else float("nan")
            ev_filtered = total_pnl / n_filt if n_filt > 0 else float("nan")
            filter_results[fname].append((ev_all, ev_filtered, n_filt))
        if (seed + 1) % 50 == 0:
            # Print running stats for conf_40_50 filter
            evs_f = [r[1] for r in filter_results["conf_40_50"] if not math.isnan(r[1])]
            evs_b = [r[0] for r in filter_results["baseline"] if not math.isnan(r[0])]
            n_pos_f = sum(e > 0 for e in evs_f)
            n_pos_b = sum(e > 0 for e in evs_b)
            print(f"  seed {seed+1:3d}: conf_40_50 {n_pos_f}/{len(evs_f)} pos ev_filt={np.mean(evs_f):+.5f} | "
                  f"baseline {n_pos_b}/{len(evs_b)} pos ev_all={np.mean(evs_b):+.5f}")

    print("\n" + "="*110)
    print(f"{'Filter':<16} {'%Pos(all)':>9} {'MeanEV(all)':>12} {'CI_lo(all)':>11} {'CI_hi(all)':>11} "
          f"{'%Pos(filt)':>10} {'MeanEV(filt)':>13} {'CI_lo(filt)':>12} {'CI_hi(filt)':>12} {'AvgN/seed':>10}")
    print("="*110)

    all_rows = []
    for fname, results in filter_results.items():
        evs_all  = [r[0] for r in results if not math.isnan(r[0])]
        evs_filt = [r[1] for r in results if not math.isnan(r[1])]
        ns       = [r[2] for r in results]

        n_pos_all  = sum(e > 0 for e in evs_all)
        n_pos_filt = sum(e > 0 for e in evs_filt)
        mean_all   = float(np.mean(evs_all)) if evs_all else float("nan")
        mean_filt  = float(np.mean(evs_filt)) if evs_filt else float("nan")
        ci_all     = bootstrap_ci(evs_all)
        ci_filt    = bootstrap_ci(evs_filt)
        avg_n      = float(np.mean(ns)) if ns else 0.0

        pct_all  = n_pos_all / len(evs_all) if evs_all else 0.0
        pct_filt = n_pos_filt / len(evs_filt) if evs_filt else 0.0

        print(f"  {fname:<14} {pct_all:>9.1%} {mean_all:>+12.5f} {ci_all[0]:>+11.5f} {ci_all[1]:>+11.5f} "
              f"{pct_filt:>10.1%} {mean_filt:>+13.5f} {ci_filt[0]:>+12.5f} {ci_filt[1]:>+12.5f} {avg_n:>10.1f}")

        # Slice analysis for the most interesting filter
        if fname in ("conf_40_50", "baseline"):
            ef50a  = [r[1] for r in results[:50]  if not math.isnan(r[1])]
            ef50b  = [r[1] for r in results[50:100] if not math.isnan(r[1])]
            ef100b = [r[1] for r in results[100:]  if not math.isnan(r[1])]
            print(f"    slices ev_filt: s0-49={np.mean(ef50a):+.5f} ({sum(e>0 for e in ef50a)/len(ef50a):.0%} pos)  "
                  f"s50-99={np.mean(ef50b):+.5f} ({sum(e>0 for e in ef50b)/len(ef50b):.0%} pos)  "
                  f"s100-199={np.mean(ef100b):+.5f} ({sum(e>0 for e in ef100b)/len(ef100b):.0%} pos)")

        all_rows.extend([{
            "filter": fname, "seed": i, "ev_all": round(r[0], 6),
            "ev_filtered": round(r[1], 6) if not math.isnan(r[1]) else "",
            "n_filtered": r[2]
        } for i, r in enumerate(results)])

    csv_path = out_dir / "sol_t240_filter_study_results.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["filter","seed","ev_all","ev_filtered","n_filtered"])
        w.writeheader(); w.writerows(all_rows)
    print(f"\nResults → {csv_path}")

    print("\n--- VERDICT ---")
    print("Shadow-test criteria for a filtered strategy:")
    print("  1. >50% positive seeds (ev_filtered basis)")
    print("  2. CI95_lo(ev_filtered) > 0 — OR — CI95_lo(ev_all) > 0")
    print("  3. avg N/seed >= 5 (enough trades per seed to be meaningful)")
    for fname in ("conf_40_50", "cost_10_30", "combined"):
        results = filter_results[fname]
        evs_filt = [r[1] for r in results if not math.isnan(r[1])]
        avg_n = float(np.mean([r[2] for r in results]))
        n_pos = sum(e > 0 for e in evs_filt)
        pct   = n_pos / len(evs_filt) if evs_filt else 0.0
        ci    = bootstrap_ci(evs_filt)
        ok1 = "✓" if pct > 0.50 else "✗"
        ok2 = "✓" if ci[0] > 0 else "✗"
        ok3 = "✓" if avg_n >= 5 else "✗"
        print(f"  [{fname}]: {ok1} {pct:.0%} pos | {ok2} CI95=[{ci[0]:+.5f},{ci[1]:+.5f}] | "
              f"{ok3} avg_n={avg_n:.1f}/seed")

if __name__ == "__main__":
    main()
