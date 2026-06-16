#!/usr/bin/env python3
"""
2026-06-15-research/settlement_lgb_v3_200seed.py

Settlement LightGBM v3 — 200-seed stability check at T1=180s.
Identical to 2026-06-14-research/settlement_lgb_v3.py except N_SEEDS=200.
Question: does the CI remain positive when we expand from 50 to 200 seeds?
"""
from __future__ import annotations
import math, re
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import pandas as pd
import lightgbm as lgb

REPO_ROOT    = Path(__file__).resolve().parents[1]
OUT_DIR      = Path(__file__).parent / "settlement_lgb_v3_200seed_results"
OUT_DIR.mkdir(exist_ok=True)

COIN         = "BTC"
COST_ADD     = 0.01
HORIZON_TOL  = 12.0
IND_WINDOW   = 60.0
N_SEEDS      = 200
N_BOOT       = 5_000
MIN_TRAIN    = 50

THRESH_RULE  = 0.15
T1_FOCUS     = 180
SKIP_BONUSES = [0.03, 0.05, 0.08, 0.12]

CLASS_YES, CLASS_NO, CLASS_SKIP = 0, 1, 2

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
    "max_depth":         3,
    "num_leaves":        7,
    "min_child_samples": 20,
    "lambda_l2":         5.0,
    "subsample":         0.90,
    "feature_fraction":  0.90,
    "learning_rate":     0.05,
    "n_rounds":          300,
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
    slug:       str
    close_time: pd.Timestamp
    label:      int
    df:         pd.DataFrame


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
            label=(1 if wo == "Up" else 0), df=df,
        ))
    contracts.sort(key=lambda c: c.close_time)
    return contracts


def extract_row(cd: ContractData, T1: int) -> dict | None:
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
        "contract_id": cd.slug,
        "close_time":  cd.close_time,
        "T1":          T1,
        "y_settle":    cd.label,
        "c_yes":       ya + COST_ADD,
        "c_no":        na + COST_ADD,
        "up_ask":      ya,
        "down_ask":    na,
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


def build_df(contracts: list[ContractData], T1: int) -> tuple[list[str], pd.DataFrame]:
    rows: list[dict] = []
    seen:  set[str]  = set()
    cids:  list[str] = []
    for cd in contracts:
        r = extract_row(cd, T1)
        if r is not None:
            rows.append(r)
            if cd.slug not in seen:
                seen.add(cd.slug)
                cids.append(cd.slug)
    if not rows:
        return [], pd.DataFrame()
    return cids, pd.DataFrame(rows)


def _lgb_params(seed: int) -> dict:
    return {
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
        "seed":              seed,
        "verbose":           -1,
        "is_unbalance":      False,
    }


def decide_action(p_up: float, c_yes: float, c_no: float, skip_bonus: float) -> int:
    p_down = 1.0 - p_up
    ev_yes = p_up   / max(c_yes, 1e-6) - 1.0
    ev_no  = p_down / max(c_no,  1e-6) - 1.0
    if ev_no > skip_bonus and ev_no >= ev_yes:
        return CLASS_NO
    if ev_yes > skip_bonus and ev_yes > ev_no:
        return CLASS_YES
    return CLASS_SKIP


def model_pnl(pred_class: int, row: dict) -> float | None:
    y  = float(row["y_settle"])
    ya = float(row["up_ask"])
    na = float(row["down_ask"])
    if pred_class == CLASS_YES:
        return y / max(ya, 1e-6) - 1.0
    if pred_class == CLASS_NO:
        return (1.0 - y) / max(na, 1e-6) - 1.0
    return None


def bench_pnl(row: dict) -> float | None:
    if float(row["p_yes_mid"]) < THRESH_RULE:
        return (1.0 - float(row["y_settle"])) / max(float(row["down_ask"]), 1e-6) - 1.0
    return None


def random_split(ids: list, seed: int, frac: float = 0.20):
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(ids))
    cut = int(len(ids) * (1.0 - frac))
    return [ids[i] for i in sorted(idx[:cut])], [ids[i] for i in sorted(idx[cut:])]


def _train_eval(tr: pd.DataFrame, te: pd.DataFrame, te_ids: list,
                seed: int, skip_bonus: float) -> dict:
    n_test = len(te_ids)
    y_tr   = tr["y_settle"].astype(float).to_numpy()

    empty = {"n_test": n_test, "trades": 0, "pnl_list": [], "bench_list": [],
             "ev_model": 0.0, "ev_bench": 0.0, "win": 0,
             "yes_trades": 0, "no_trades": 0, "yes_wins": 0, "no_wins": 0,
             "yes_pnl": [], "no_pnl": []}

    if len(tr) < MIN_TRAIN or y_tr.std() < 1e-9:
        return empty

    ds = lgb.Dataset(tr[FEATURES].to_numpy().astype(float), label=y_tr, free_raw_data=False)
    m  = lgb.train(
        _lgb_params(seed * 1000 + 42),
        train_set=ds,
        num_boost_round=CFG["n_rounds"],
        valid_sets=[ds],
        callbacks=[lgb.log_evaluation(period=9999)],
    )

    if te.empty:
        return empty

    p_up_arr = m.predict(te[FEATURES].to_numpy().astype(float))

    pnl_list:  list[float] = []
    bench_list: list[float] = []
    yes_pnl:   list[float] = []
    no_pnl:    list[float] = []
    win = yes_trades = no_trades = yes_wins = no_wins = 0

    for i, (_, row) in enumerate(te.iterrows()):
        row   = row.to_dict()
        p_up  = float(p_up_arr[i])
        pc    = decide_action(p_up, float(row["c_yes"]), float(row["c_no"]), skip_bonus)

        p = model_pnl(pc, row)
        if p is not None:
            pnl_list.append(p)
            if p > 0:
                win += 1
            if pc == CLASS_YES:
                yes_trades += 1
                yes_pnl.append(p)
                if p > 0: yes_wins += 1
            else:
                no_trades += 1
                no_pnl.append(p)
                if p > 0: no_wins += 1

        b = bench_pnl(row)
        if b is not None:
            bench_list.append(b)

    return {
        "n_test":    n_test,
        "trades":    len(pnl_list),
        "pnl_list":  pnl_list,
        "bench_list": bench_list,
        "ev_model":  sum(pnl_list)   / max(n_test, 1),
        "ev_bench":  sum(bench_list) / max(n_test, 1),
        "win":       win,
        "yes_trades": yes_trades, "no_trades": no_trades,
        "yes_wins":  yes_wins,   "no_wins":   no_wins,
        "yes_pnl":   yes_pnl,    "no_pnl":    no_pnl,
    }


def run_seed(seed: int, df: pd.DataFrame, cids: list, skip_bonus: float) -> dict:
    tr_ids, te_ids = random_split(cids, seed)
    tr = df[df["contract_id"].isin(set(tr_ids))].copy()
    te = df[df["contract_id"].isin(set(te_ids))].copy()
    return _train_eval(tr, te, te_ids, seed, skip_bonus)


def ci95(arr):
    return float(np.percentile(arr, 2.5)), float(np.percentile(arr, 97.5))


def evaluate(df: pd.DataFrame, cids: list, skip_bonus: float) -> dict:
    print(f"  Running {N_SEEDS} seeds (skip_bonus={skip_bonus:.2f})…", flush=True)
    seed_results = [run_seed(s, df, cids, skip_bonus) for s in range(N_SEEDS)]

    ev_m  = [r["ev_model"] for r in seed_results]
    ev_b  = [r["ev_bench"] for r in seed_results]
    pct_pos = sum(1 for v in ev_m if v > 0) / N_SEEDS

    rng      = np.random.default_rng(42)
    boot_m   = [float(np.mean(rng.choice(ev_m, size=N_SEEDS, replace=True))) for _ in range(N_BOOT)]
    boot_b   = [float(np.mean(rng.choice(ev_b, size=N_SEEDS, replace=True))) for _ in range(N_BOOT)]
    rng2     = np.random.default_rng(43)
    idx_boot = rng2.integers(0, N_SEEDS, size=(N_BOOT, N_SEEDS))
    ev_m_arr = np.array(ev_m)
    ev_b_arr = np.array(ev_b)
    boot_diff = (ev_m_arr[idx_boot].mean(axis=1) - ev_b_arr[idx_boot].mean(axis=1)).tolist()

    m_ci   = ci95(boot_m)
    b_ci   = ci95(boot_b)
    d_ci   = ci95(boot_diff)
    mean_m = float(np.mean(ev_m))
    mean_b = float(np.mean(ev_b))
    tr_r   = float(np.mean([r["trades"] / max(r["n_test"], 1) for r in seed_results]))
    wins   = [r["win"] / max(r["trades"], 1) for r in seed_results if r["trades"] > 0]
    wr     = float(np.mean(wins)) if wins else float("nan")

    # YES / NO breakdown across all seeds
    all_yes_pnl = [p for r in seed_results for p in r["yes_pnl"]]
    all_no_pnl  = [p for r in seed_results for p in r["no_pnl"]]
    yes_ev_trig = float(np.mean(all_yes_pnl)) if all_yes_pnl else float("nan")
    no_ev_trig  = float(np.mean(all_no_pnl))  if all_no_pnl  else float("nan")
    yes_wr = float(np.mean([p > 0 for p in all_yes_pnl])) if all_yes_pnl else float("nan")
    no_wr  = float(np.mean([p > 0 for p in all_no_pnl]))  if all_no_pnl  else float("nan")
    avg_yes = float(np.mean([r["yes_trades"] for r in seed_results]))
    avg_no  = float(np.mean([r["no_trades"]  for r in seed_results]))

    print(f"    Model:  mean={mean_m:+.5f}  CI=[{m_ci[0]:+.5f}, {m_ci[1]:+.5f}]  %pos={pct_pos:.1%}")
    print(f"    Bench:  mean={mean_b:+.5f}  CI=[{b_ci[0]:+.5f}, {b_ci[1]:+.5f}]")
    print(f"    Diff:   CI=[{d_ci[0]:+.5f}, {d_ci[1]:+.5f}]  trade%={tr_r:.3f}  win%={wr:.3f}")
    print(f"    YES:  avg_n={avg_yes:.0f}  EV/trig={yes_ev_trig:+.4f}  win%={yes_wr:.3f}")
    print(f"    NO:   avg_n={avg_no:.0f}  EV/trig={no_ev_trig:+.4f}  win%={no_wr:.3f}")

    return {
        "skip_bonus":    skip_bonus,
        "mean_model_ev": mean_m,
        "mean_bench_ev": mean_b,
        "model_ci_lo":   m_ci[0],
        "model_ci_hi":   m_ci[1],
        "bench_ci_lo":   b_ci[0],
        "bench_ci_hi":   b_ci[1],
        "diff_ci_lo":    d_ci[0],
        "diff_ci_hi":    d_ci[1],
        "pct_pos":       pct_pos,
        "trade_rate":    tr_r,
        "win_rate":      wr,
        "yes_ev_trig":   yes_ev_trig,
        "no_ev_trig":    no_ev_trig,
        "yes_wr":        yes_wr,
        "no_wr":         no_wr,
        "avg_yes":       avg_yes,
        "avg_no":        avg_no,
        "model_beats":   m_ci[0] > 0 and d_ci[0] > 0,
    }


def main():
    data_dir      = REPO_ROOT / "polymarket" / f"data_{COIN}_5m"
    outcomes_path = REPO_ROOT / "polymarket" / f"polymarket_{COIN.lower()}_5m_official_outcomes.csv"

    print("Loading BTC contracts…")
    contracts = load_data(data_dir, outcomes_path)
    print(f"  {len(contracts)} contracts\n")

    cids, df = build_df(contracts, T1_FOCUS)
    print(f"T1={T1_FOCUS}s  n={len(cids)}  "
          f"UP={int(df['y_settle'].sum())}  DOWN={int((df['y_settle']==0).sum())}\n")

    results = []
    for bonus in SKIP_BONUSES:
        print(f"\n--- skip_bonus={bonus:.2f} ---")
        r = evaluate(df, cids, bonus)
        r["T1"] = T1_FOCUS
        r["n"]  = len(cids)
        results.append(r)

    print(f"\n\n{'='*75}")
    print(f"SUMMARY — v3 LightGBM  T1={T1_FOCUS}s  N_SEEDS={N_SEEDS}")
    print(f"{'='*75}")
    hdr = f"{'bonus':>7} {'model_EV':>10} {'CI_lo':>9} {'CI_hi':>9} {'%pos':>6} {'trade%':>7} {'win%':>6} {'beats?':>7}"
    print(hdr)
    for r in results:
        flag = "✓" if r["model_beats"] else ""
        print(f"{r['skip_bonus']:>7.2f} {r['mean_model_ev']:>+10.5f} "
              f"{r['model_ci_lo']:>+9.5f} {r['model_ci_hi']:>+9.5f} "
              f"{r['pct_pos']:>6.1%} {r['trade_rate']:>7.3f} {r['win_rate']:>6.3f} {flag:>7}")

    print(f"\nYES/NO breakdown (pooled across all {N_SEEDS} seeds):")
    print(f"{'bonus':>7} {'YES_EV/trig':>12} {'YES_wr':>8} {'avg_YES':>9} "
          f"{'NO_EV/trig':>12} {'NO_wr':>8} {'avg_NO':>9}")
    for r in results:
        print(f"{r['skip_bonus']:>7.2f} {r['yes_ev_trig']:>+12.4f} {r['yes_wr']:>8.3f} {r['avg_yes']:>9.0f} "
              f"{r['no_ev_trig']:>+12.4f} {r['no_wr']:>8.3f} {r['avg_no']:>9.0f}")

    out_csv = OUT_DIR / "v3_200seed_t180_results.csv"
    pd.DataFrame(results).to_csv(out_csv, index=False)
    print(f"\nResults → {out_csv}")
    print("Done.")


if __name__ == "__main__":
    main()
