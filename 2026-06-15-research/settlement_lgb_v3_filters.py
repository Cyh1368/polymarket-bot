#!/usr/bin/env python3
"""
2026-06-15-research/settlement_lgb_v3_filters.py

Post-hoc execution filter test on v3 LightGBM at T1=180s, skip_bonus=0.05.

Anti-overfitting discipline:
  - Filters are pre-specified from yesterday's expanding-window CV analysis
    (independent dataset, not from this 200-seed run).
  - No threshold scanning. All cutoffs committed before running.
  - Tested on the full 200-seed random-split framework.

Filters tested (applied POST model prediction, no retraining):
  Baseline : trade as model says (no execution filter)
  A        : NO-only (drop all YES trades)
  B        : block YES if p_yes_mid < 0.25  (allow YES in [0.25, 1.0))
  C        : block YES if p_yes_mid < 0.25  AND block NO if p_yes_mid > 0.90
  D        : NO-only AND block NO if p_yes_mid > 0.90

Threshold justification (from 2026-06-14 CV backtest, independent data):
  0.25 YES gate : CV showed [0.00,0.15) YES = 0 wins/16 trades,
                  [0.15,0.25) YES net negative; [0.25,0.50) YES marginal.
  0.90 NO  gate : CV [0.95,1.00) NO = 0 wins/16 trades (market >95% UP
                  → DOWN almost never wins even at 10x payoff).
                  Using 0.90 (slightly more conservative than 0.95 seen in CV).
"""
from __future__ import annotations
import math, re
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import pandas as pd
import lightgbm as lgb

REPO_ROOT   = Path(__file__).resolve().parents[1]
OUT_DIR     = Path(__file__).parent / "settlement_lgb_v3_filter_results"
OUT_DIR.mkdir(exist_ok=True)

COIN        = "BTC"
COST_ADD    = 0.01
HORIZON_TOL = 12.0
IND_WINDOW  = 60.0
N_SEEDS     = 200
N_BOOT      = 5_000
MIN_TRAIN   = 50

THRESH_RULE = 0.15
T1_FOCUS    = 180
SKIP_BONUS  = 0.05   # best config from 200-seed run

CLASS_YES, CLASS_NO, CLASS_SKIP = 0, 1, 2

# Pre-specified filter thresholds — do not adjust after running
YES_GATE_LO = 0.25   # block YES if p_yes_mid < this
NO_GATE_HI  = 0.90   # block NO  if p_yes_mid > this

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

FILTERS = {
    "baseline": dict(no_only=False, yes_gate=None,  no_gate=None),
    "A_no_only": dict(no_only=True,  yes_gate=None,  no_gate=None),
    "B_yes_gate_025": dict(no_only=False, yes_gate=YES_GATE_LO, no_gate=None),
    "C_yes025_no090": dict(no_only=False, yes_gate=YES_GATE_LO, no_gate=NO_GATE_HI),
    "D_noonly_no090": dict(no_only=True,  yes_gate=None,         no_gate=NO_GATE_HI),
}


# ── Data loading (identical to v3) ──────────────────────────────────────────

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


def load_data(data_dir, outcomes_path):
    outcomes = {}
    for row in pd.read_csv(outcomes_path).to_dict(orient="records"):
        slug = str(row.get("market_slug") or "").strip()
        wo   = str(row.get("winning_outcome") or "").strip()
        if slug and wo in {"Up", "Down"}:
            outcomes[slug] = wo
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
        close_time = pd.Timestamp(int(m.group(1)) + 300, unit="s", tz="UTC")
        contracts.append(ContractData(
            slug=slug, close_time=close_time,
            label=(1 if wo == "Up" else 0), df=df))
    contracts.sort(key=lambda c: c.close_time)
    return contracts


def extract_row(cd, T1):
    df  = cd.df
    t1c = df[(df["_stc"] - T1).abs() <= HORIZON_TOL]
    if t1c.empty: return None
    t1r = t1c.loc[(t1c["_stc"] - T1).abs().idxmin()]
    ya  = fnum(t1r.get("up_best_ask"));   yb  = fnum(t1r.get("up_best_bid"))
    na  = fnum(t1r.get("down_best_ask")); nb  = fnum(t1r.get("down_best_bid"))
    ubs = fnum(t1r.get("up_best_bid_size")) or 0.0
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
    ym60 = series_stats(_mids(h60), up_mid); ym20 = series_stats(_mids(h20), up_mid)
    ob60 = series_stats(_obis(h60), obi_cur)
    mids_60 = _mids(h60).dropna()
    mid_change = up_mid - float(mids_60.iloc[0]) if not mids_60.empty else 0.0
    if ts_ok:
        secs = t1_ts.hour * 3600 + t1_ts.minute * 60 + t1_ts.second
        tod_sin = math.sin(2 * math.pi * secs / 86400)
        tod_cos = math.cos(2 * math.pi * secs / 86400)
    else:
        tod_sin = tod_cos = 0.0
    rd = {
        "contract_id": cd.slug, "close_time": cd.close_time, "T1": T1,
        "y_settle": cd.label, "c_yes": ya + COST_ADD, "c_no": na + COST_ADD,
        "up_ask": ya, "down_ask": na, "p_yes_mid": up_mid,
        "yes_mid_z_60": ym60["z"],   "yes_mid_vol_60": ym60["vol"],
        "yes_mid_z_20": ym20["z"],   "yes_mid_vol_20": ym20["vol"],
        "mid_change_60": mid_change, "book_qty_log": math.log1p(ubs + dbs),
        "OBI": obi_cur, "OBI_vol_60": ob60["vol"], "OBI_z_60": ob60["z"],
        "spread_yes": ya - yb, "tod_sin": tod_sin, "tod_cos": tod_cos,
    }
    if any(not math.isfinite(float(rd.get(f, float("nan")))) for f in FEATURES): return None
    return rd


def build_df(contracts, T1):
    rows, seen, cids = [], set(), []
    for cd in contracts:
        r = extract_row(cd, T1)
        if r is not None:
            rows.append(r)
            if cd.slug not in seen:
                seen.add(cd.slug); cids.append(cd.slug)
    return (cids, pd.DataFrame(rows)) if rows else ([], pd.DataFrame())


# ── Model ───────────────────────────────────────────────────────────────────

def _lgb_params(seed):
    return {"objective": "binary", "metric": "binary_logloss",
            "num_leaves": CFG["num_leaves"], "max_depth": CFG["max_depth"],
            "min_child_samples": CFG["min_child_samples"],
            "subsample": CFG["subsample"], "feature_fraction": CFG["feature_fraction"],
            "lambda_l2": CFG["lambda_l2"], "lambda_l1": 0.0,
            "learning_rate": CFG["learning_rate"], "num_threads": 4,
            "seed": seed, "verbose": -1, "is_unbalance": False}


def decide_action(p_up, c_yes, c_no, skip_bonus, p_yes_mid,
                  no_only, yes_gate, no_gate):
    p_down = 1.0 - p_up
    ev_yes = p_up   / max(c_yes, 1e-6) - 1.0
    ev_no  = p_down / max(c_no,  1e-6) - 1.0

    # Execution filters applied after EV calculation
    allow_yes = (not no_only) and (yes_gate is None or p_yes_mid >= yes_gate)
    allow_no  = (no_gate is None or p_yes_mid <= no_gate)

    if allow_no  and ev_no  > skip_bonus and ev_no  >= ev_yes: return CLASS_NO
    if allow_yes and ev_yes > skip_bonus and ev_yes > ev_no:   return CLASS_YES
    return CLASS_SKIP


def model_pnl(pred_class, row):
    y = float(row["y_settle"]); ya = float(row["up_ask"]); na = float(row["down_ask"])
    if pred_class == CLASS_YES: return y / max(ya, 1e-6) - 1.0
    if pred_class == CLASS_NO:  return (1.0 - y) / max(na, 1e-6) - 1.0
    return None


def bench_pnl(row):
    if float(row["p_yes_mid"]) < THRESH_RULE:
        return (1.0 - float(row["y_settle"])) / max(float(row["down_ask"]), 1e-6) - 1.0
    return None


def random_split(ids, seed, frac=0.20):
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(ids))
    cut = int(len(ids) * (1.0 - frac))
    return [ids[i] for i in sorted(idx[:cut])], [ids[i] for i in sorted(idx[cut:])]


# ── Per-seed evaluation for all filters simultaneously ──────────────────────

def run_seed_all_filters(seed, df, cids):
    tr_ids, te_ids = random_split(cids, seed)
    tr = df[df["contract_id"].isin(set(tr_ids))].copy()
    te = df[df["contract_id"].isin(set(te_ids))].copy()
    n_test = len(te_ids)

    results = {name: {"n_test": n_test, "pnl": [], "bench": [],
                      "yes_pnl": [], "no_pnl": []} for name in FILTERS}

    y_tr = tr["y_settle"].astype(float).to_numpy()
    if len(tr) < MIN_TRAIN or y_tr.std() < 1e-9:
        for r in results.values():
            r["ev_model"] = 0.0; r["ev_bench"] = 0.0
        return results

    ds = lgb.Dataset(tr[FEATURES].to_numpy().astype(float), label=y_tr, free_raw_data=False)
    m  = lgb.train(_lgb_params(seed * 1000 + 42), train_set=ds,
                   num_boost_round=CFG["n_rounds"], valid_sets=[ds],
                   callbacks=[lgb.log_evaluation(period=9999)])

    p_up_arr = m.predict(te[FEATURES].to_numpy().astype(float))

    for i, (_, row) in enumerate(te.iterrows()):
        row_d   = row.to_dict()
        p_up    = float(p_up_arr[i])
        p_mid   = float(row_d["p_yes_mid"])
        c_yes   = float(row_d["c_yes"]); c_no = float(row_d["c_no"])

        b = bench_pnl(row_d)

        for name, flt in FILTERS.items():
            pc = decide_action(p_up, c_yes, c_no, SKIP_BONUS, p_mid,
                               flt["no_only"], flt["yes_gate"], flt["no_gate"])
            p  = model_pnl(pc, row_d)
            if p is not None:
                results[name]["pnl"].append(p)
                if pc == CLASS_YES: results[name]["yes_pnl"].append(p)
                else:               results[name]["no_pnl"].append(p)
            if b is not None and name == "baseline":
                results[name]["bench"].append(b)
            elif b is not None:
                results[name]["bench"].append(b)

    for r in results.values():
        r["ev_model"] = sum(r["pnl"]) / max(n_test, 1)
        r["ev_bench"] = sum(r["bench"]) / max(n_test, 1)

    return results


def ci95(arr):
    return float(np.percentile(arr, 2.5)), float(np.percentile(arr, 97.5))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    data_dir      = REPO_ROOT / "polymarket" / f"data_{COIN}_5m"
    outcomes_path = REPO_ROOT / "polymarket" / f"polymarket_{COIN.lower()}_5m_official_outcomes.csv"

    print("Loading BTC contracts…")
    contracts = load_data(data_dir, outcomes_path)
    cids, df  = build_df(contracts, T1_FOCUS)
    print(f"  {len(contracts)} contracts  |  T1={T1_FOCUS}s  n={len(cids)}\n")

    print(f"Running {N_SEEDS} seeds for {len(FILTERS)} filters simultaneously…", flush=True)
    all_seed_results = [run_seed_all_filters(s, df, cids) for s in range(N_SEEDS)]
    print("Done.\n")

    # Aggregate
    summary = []
    ev_m_by_filter = {name: [] for name in FILTERS}

    for name, flt in FILTERS.items():
        ev_m  = [r[name]["ev_model"] for r in all_seed_results]
        ev_b  = [r[name]["ev_bench"] for r in all_seed_results]

        all_yes = [p for r in all_seed_results for p in r[name]["yes_pnl"]]
        all_no  = [p for r in all_seed_results for p in r[name]["no_pnl"]]
        avg_yes = float(np.mean([len(r[name]["yes_pnl"]) for r in all_seed_results]))
        avg_no  = float(np.mean([len(r[name]["no_pnl"])  for r in all_seed_results]))
        avg_tr  = float(np.mean([len(r[name]["pnl"])     for r in all_seed_results]))
        n_test  = float(np.mean([r[name]["n_test"]       for r in all_seed_results]))

        rng      = np.random.default_rng(42)
        boot_m   = [float(np.mean(rng.choice(ev_m, size=N_SEEDS, replace=True)))
                    for _ in range(N_BOOT)]
        rng2     = np.random.default_rng(43)
        idx_b    = rng2.integers(0, N_SEEDS, size=(N_BOOT, N_SEEDS))
        ev_m_arr = np.array(ev_m); ev_b_arr = np.array(ev_b)
        boot_diff = (ev_m_arr[idx_b].mean(axis=1) - ev_b_arr[idx_b].mean(axis=1)).tolist()

        m_ci   = ci95(boot_m)
        d_ci   = ci95(boot_diff)
        mean_m = float(np.mean(ev_m))
        pct_pos = sum(1 for v in ev_m if v > 0) / N_SEEDS
        tr_rate = avg_tr / max(n_test, 1)
        wins    = [p > 0 for r in all_seed_results for p in r[name]["pnl"]]
        wr      = float(np.mean(wins)) if wins else float("nan")

        yes_ev = float(np.mean(all_yes)) if all_yes else float("nan")
        no_ev  = float(np.mean(all_no))  if all_no  else float("nan")
        yes_wr = float(np.mean([p > 0 for p in all_yes])) if all_yes else float("nan")
        no_wr  = float(np.mean([p > 0 for p in all_no]))  if all_no  else float("nan")

        ev_m_by_filter[name] = ev_m

        summary.append({
            "filter": name, "mean_ev": mean_m,
            "ci_lo": m_ci[0], "ci_hi": m_ci[1],
            "diff_ci_lo": d_ci[0], "diff_ci_hi": d_ci[1],
            "pct_pos": pct_pos, "trade_rate": tr_rate, "win_rate": wr,
            "avg_yes": avg_yes, "avg_no": avg_no,
            "yes_ev_trig": yes_ev, "no_ev_trig": no_ev,
            "yes_wr": yes_wr, "no_wr": no_wr,
            "beats": m_ci[0] > 0 and d_ci[0] > 0,
        })

    # Cross-filter paired bootstrap: is each filter significantly better than baseline?
    baseline_ev = np.array(ev_m_by_filter["baseline"])
    print("Paired bootstrap vs baseline (same seeds → no variance inflation):")
    paired = {}
    for name in FILTERS:
        if name == "baseline": continue
        diff_arr = np.array(ev_m_by_filter[name]) - baseline_ev
        rng3 = np.random.default_rng(44)
        idx3 = rng3.integers(0, N_SEEDS, size=(N_BOOT, N_SEEDS))
        boot_pd = diff_arr[idx3].mean(axis=1)
        pd_ci   = ci95(boot_pd.tolist())
        pmean   = float(diff_arr.mean())
        paired[name] = {"mean_diff": pmean, "ci_lo": pd_ci[0], "ci_hi": pd_ci[1]}
        sig = "✓ better" if pd_ci[0] > 0 else ("✗ worse" if pd_ci[1] < 0 else "~ same")
        print(f"  {name:22s}  Δ={pmean:+.5f}  CI=[{pd_ci[0]:+.5f}, {pd_ci[1]:+.5f}]  {sig}")

    print(f"\n{'='*85}")
    print(f"FILTER COMPARISON — v3 LightGBM  T1={T1_FOCUS}s  skip_bonus={SKIP_BONUS}  N_SEEDS={N_SEEDS}")
    print(f"{'='*85}")
    hdr = (f"{'filter':24s} {'mean_EV':>9} {'CI_lo':>9} {'CI_hi':>9} "
           f"{'%pos':>6} {'trade%':>7} {'win%':>6} {'beats_thresh':>13}")
    print(hdr)
    for r in summary:
        flag = "✓" if r["beats"] else ""
        print(f"{r['filter']:24s} {r['mean_ev']:>+9.5f} {r['ci_lo']:>+9.5f} {r['ci_hi']:>+9.5f} "
              f"{r['pct_pos']:>6.1%} {r['trade_rate']:>7.3f} {r['win_rate']:>6.3f} {flag:>13}")

    print(f"\nYES / NO breakdown (pooled across {N_SEEDS} seeds):")
    hdr2 = (f"{'filter':24s} {'avg_YES':>8} {'YES_EV':>9} {'YES_wr':>7} "
            f"{'avg_NO':>8} {'NO_EV':>9} {'NO_wr':>7}")
    print(hdr2)
    for r in summary:
        print(f"{r['filter']:24s} {r['avg_yes']:>8.0f} {r['yes_ev_trig']:>+9.4f} {r['yes_wr']:>7.3f} "
              f"{r['avg_no']:>8.0f} {r['no_ev_trig']:>+9.4f} {r['no_wr']:>7.3f}")

    pd.DataFrame(summary).to_csv(OUT_DIR / "filter_comparison.csv", index=False)
    print(f"\nResults → {OUT_DIR}/filter_comparison.csv")
    print("Done.")


if __name__ == "__main__":
    main()
