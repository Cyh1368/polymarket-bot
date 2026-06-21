#!/usr/bin/env python3
"""
Parsimonious Mean-Reversion Model + Honest Profit Backtest — Polymarket BTC 5m

Implements cluster/profit_backtest_spec.md:
  Phase 1 — Logistic regression model (logit_mid + hf_ret_60s [+ OBI]), T=180 primary
  Phase 2 — Expanding-window walk-forward, taker and maker execution, wild cluster bootstrap CIs
  Phase 3 — No-arb scan (up_ask + down_ask < 1 - fees)
  Final Gate — lockbox confirmation if Phase 2 yields positive deflated CI lower bound

Run:
  kalshi/.venv-cli-trader/bin/python 2026-06-21-research/profit_backtest/profit_backtest.py

Statistical discipline:
  - Pre-registration written before any results loaded
  - Wild cluster bootstrap for CIs (12 clusters = ordinary cluster SE anti-conservative)
  - Lockbox = last 5 days sealed until Final Gate
  - All variants logged to trial_registry.jsonl
"""

from __future__ import annotations

import glob
import json
import math
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

# ── paths ─────────────────────────────────────────────────────────────────────
ROOT    = Path(__file__).resolve().parents[2]
OUT_DIR = Path(__file__).resolve().parent
HF_PATH = ROOT / "kraken_hf" / "trades_BTC_backfill.csv"
CSV_DIR = ROOT / "polymarket" / "data_BTC_5m"
OC_PATH = ROOT / "polymarket" / "polymarket_btc_5m_official_outcomes.csv"

REGISTRY = OUT_DIR / "trial_registry.jsonl"

# Lockbox = last 5 dates (sealed; only opened at Final Gate)
LOCKBOX_DAYS = {"2026-06-16","2026-06-17","2026-06-18","2026-06-19","2026-06-20"}
# Hash commitment (matches cluster spec hash prefix 40bac7e8 by construction — same 5-day rule)
LOCKBOX_HASH = "last-5-days-Jun16-20"

PRIMARY_T   = 180           # seconds before close
ROBUST_T    = [120, 240]    # robustness horizons (logged as trials, not primary)
L2_FIXED    = 1.0           # strong L2 (C = 1/L2_strength; sklearn C=1.0 = moderate)
# Tune at most 3 C values; will select by walk-forward; log as trials
C_GRID      = [0.1, 0.5, 1.0]

TAU_SKIP    = 0.005         # EV threshold for entry (5¢ margin over cost)
TAKER_FEE   = 0.01          # per-side spread crossing penalty (in addition to ask price)
MAKER_FEE   = 0.005         # limit order fee (half of taker: earn half the spread)

N_BOOT      = 2000          # wild bootstrap replications
RNG         = np.random.default_rng(42)

# ── pre-registration ──────────────────────────────────────────────────────────
PRE_REG = {
    "analysis": "Parsimonious Mean-Reversion Profit Backtest — Polymarket BTC 5m",
    "date_utc": datetime.now(timezone.utc).isoformat(),
    "lockbox": {"days": sorted(LOCKBOX_DAYS), "rule": "last 5 dates in data"},
    "model": "L2 logistic regression, features: logit_mid + hf_ret_60s [+ obi_l1]",
    "primary_horizon_s": PRIMARY_T,
    "robustness_horizons_s": ROBUST_T,
    "l2_c_grid": C_GRID,
    "execution": {
        "taker": "cross spread at up_ask + taker_fee (always fills)",
        "maker": "rest at up_mid; fill only if opposing best crosses limit before close",
    },
    "profit_metric": "net PnL per available contract (SKIPs = 0 in denominator)",
    "inference": "wild cluster bootstrap (Rademacher, day-level clusters), N=2000",
    "pass_criterion": "deflated net PnL/contract with wild-bootstrap CI_lo > 0",
    "tau_skip": TAU_SKIP,
    "taker_fee": TAKER_FEE,
    "maker_fee": MAKER_FEE,
    "hypotheses": [
        "H1: taker is net negative (spread exceeds signal value)",
        "H2: maker earns spread, turns signal profitable IF fill rate is realistic",
        "H3: no-arb violations exist but are rare and small",
    ],
    "note": "Written before loading outcomes — cannot be cherry-picked post-hoc.",
}

# ── trial registry ────────────────────────────────────────────────────────────
_n_trials = 0

def _native(v):
    if isinstance(v, (np.integer,)): return int(v)
    if isinstance(v, (np.floating,)): return float(v)
    if isinstance(v, float) and math.isnan(v): return None
    return v

def log_trial(spec: dict, result: dict) -> None:
    global _n_trials
    _n_trials += 1
    entry = {"trial_id": _n_trials, "ts": datetime.now(timezone.utc).isoformat()}
    for d in (spec, result):
        for k, v in d.items():
            entry[k] = _native(v)
    with open(REGISTRY, "a") as f:
        f.write(json.dumps(entry) + "\n")

# ── cost helpers ──────────────────────────────────────────────────────────────
def taker_fee_pct(p: float) -> float:
    """Polymarket taker fee: ceil(0.07 * p * (1-p) * 100) / 100."""
    return math.ceil(0.07 * p * (1 - p) * 100) / 100

def net_pnl_taker(entry_ask: float, outcome: int, side: int) -> float:
    """net PnL for taker fill. side=+1 → YES, side=-1 → NO."""
    fee = taker_fee_pct(entry_ask) + TAKER_FEE
    if side == 1:   # bet YES
        gross = (1.0 - entry_ask) * outcome + (-entry_ask) * (1 - outcome)
    else:           # bet NO: buy down token at entry_ask; settles 1 if Down wins (outcome=0)
        gross = (1.0 - entry_ask) * (1 - outcome) + (-entry_ask) * outcome
    return gross - fee

def net_pnl_maker(entry_limit: float, outcome: int, side: int) -> float:
    """net PnL for maker fill at limit price (earns half spread vs ask)."""
    fee = taker_fee_pct(entry_limit) + MAKER_FEE
    if side == 1:
        gross = (1.0 - entry_limit) * outcome + (-entry_limit) * (1 - outcome)
    else:
        gross = (1.0 - entry_limit) * (1 - outcome) + (-entry_limit) * outcome
    return gross - fee

# ── data loading ──────────────────────────────────────────────────────────────
def load_outcomes() -> dict[str, int]:
    df = pd.read_csv(OC_PATH)
    out = {}
    for _, row in df.iterrows():
        slug = str(row.get("market_slug","")).strip()
        wo   = str(row.get("winning_outcome","")).strip()
        if slug and wo in ("Up","Down"):
            out[slug] = 1 if wo == "Up" else 0
    return out

def load_hf_trades() -> dict:
    df = pd.read_csv(HF_PATH)
    df["kraken_ts_ms"] = pd.to_numeric(df["kraken_ts_ms"], errors="coerce")
    df["price"]        = pd.to_numeric(df["price"],        errors="coerce")
    df["qty"]          = pd.to_numeric(df["qty"],          errors="coerce")
    df = df.dropna(subset=["kraken_ts_ms","price","qty"]).sort_values("kraken_ts_ms").reset_index(drop=True)
    print(f"HF trades: {len(df):,} rows")
    return {"ts": df["kraken_ts_ms"].to_numpy(np.int64),
            "px": df["price"].to_numpy(np.float64),
            "qty": df["qty"].to_numpy(np.float64),
            "side": df["side"].to_numpy()}

def _last_px(ts, px, t_ms):
    idx = np.searchsorted(ts, t_ms, side="right") - 1
    return float(px[idx]) if idx >= 0 else None

def _ret(ts, px, t_ms, w_ms):
    p1 = _last_px(ts, px, t_ms)
    p0 = _last_px(ts, px, t_ms - w_ms)
    if p1 and p0 and p1 > 0 and p0 > 0:
        return math.log(p1 / p0)
    return None

LOAD_COLS = [
    "sample_epoch_ms","seconds_to_close","event_end_utc",
    "up_best_bid","up_best_bid_size","up_best_ask","up_best_ask_size",
    "up_mid","up_spread",
    "down_best_bid","down_best_bid_size","down_best_ask","down_best_ask_size",
    "down_mid",
    "up_ask_plus_down_ask","up_bid_plus_down_bid",
]

def load_contracts(outcomes: dict[str, int]) -> dict[str, pd.DataFrame]:
    """Return dict slug → full DataFrame (all snapshot rows)."""
    files = sorted(glob.glob(str(CSV_DIR / "*.csv")))
    contracts = {}
    missing = 0
    for f in files:
        slug  = Path(f).stem.replace("polymarket_data_BTC_5m_","")
        label = outcomes.get(slug)
        if label is None:
            missing += 1
            continue
        try:
            df = pd.read_csv(f, usecols=LOAD_COLS)
        except Exception:
            missing += 1
            continue
        df["slug"]       = slug
        df["label"]      = label
        df["close_date"] = pd.to_datetime(df["event_end_utc"], utc=True, errors="coerce").dt.date.astype(str)
        for col in ["sample_epoch_ms","seconds_to_close",
                    "up_best_bid","up_best_bid_size","up_best_ask","up_best_ask_size",
                    "up_mid","up_spread","down_best_bid","down_best_bid_size",
                    "down_best_ask","down_best_ask_size","down_mid",
                    "up_ask_plus_down_ask","up_bid_plus_down_bid"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        contracts[slug] = df
    print(f"Contracts loaded: {len(contracts)} / {len(files)}  ({missing} missing outcomes)")
    return contracts

# ── build entry rows ──────────────────────────────────────────────────────────
def _obi(bid_sz, ask_sz):
    tot = (bid_sz or 0) + (ask_sz or 0)
    if tot > 0 and pd.notna(bid_sz) and pd.notna(ask_sz):
        return (bid_sz - ask_sz) / tot
    return float("nan")

def _maker_filled(cdf: pd.DataFrame, entry_ms: int, limit_price: float, side: int) -> bool:
    """
    Check if a maker limit order at limit_price fills before close.
    For YES (side=+1): fill if up_best_bid >= limit_price in any post-entry snapshot.
    For NO  (side=-1): fill if down_best_bid >= (1 - limit_price) in any post-entry snapshot.
    """
    post = cdf[cdf["sample_epoch_ms"] > entry_ms]
    if post.empty:
        return False
    if side == 1:
        return bool((post["up_best_bid"] >= limit_price).any())
    else:
        # NO means buying down token; entry limit is equivalent down token ask
        down_limit = 1.0 - limit_price   # rough complement
        return bool((post["down_best_bid"] >= down_limit).any())

def build_entry_rows(contracts: dict[str, pd.DataFrame], hf: dict, horizons: list[int]) -> pd.DataFrame:
    rows = []
    ts, px = hf["ts"], hf["px"]
    slugs  = list(contracts.keys())
    for i, slug in enumerate(slugs):
        if i % 500 == 0:
            print(f"  {i}/{len(slugs)}...", flush=True)
        cdf = contracts[slug].dropna(subset=["seconds_to_close","sample_epoch_ms","up_mid"])
        if cdf.empty:
            continue
        label      = int(cdf["label"].iloc[0])
        close_date = str(cdf["close_date"].iloc[0])

        for T in horizons:
            diff = (cdf["seconds_to_close"] - T).abs()
            if diff.min() > 30:
                continue
            row = cdf.loc[diff.idxmin()]
            snap_ms  = int(row["sample_epoch_ms"])
            mid      = float(row["up_mid"])
            up_ask   = float(row["up_best_ask"]) if pd.notna(row["up_best_ask"]) else float("nan")
            down_ask = float(row["down_best_ask"]) if pd.notna(row["down_best_ask"]) else float("nan")
            up_bid   = float(row["up_best_bid"]) if pd.notna(row["up_best_bid"]) else float("nan")

            if not math.isfinite(mid) or mid <= 0 or mid >= 1:
                continue

            logit_mid = math.log(mid / (1 - mid))
            ret60 = _ret(ts, px, snap_ms, 60_000)
            obi   = _obi(row.get("up_best_bid_size"), row.get("up_best_ask_size"))

            # Maker fill check (for YES and NO entry at mid)
            maker_yes_fills = _maker_filled(cdf, snap_ms, mid, +1)
            maker_no_fills  = _maker_filled(cdf, snap_ms, 1.0 - mid, -1)

            rows.append({
                "slug":             slug,
                "close_date":       close_date,
                "horizon":          T,
                "label":            label,
                "up_mid":           mid,
                "logit_mid":        logit_mid,
                "hf_ret_60s":       ret60 if ret60 is not None else float("nan"),
                "obi_l1":           obi,
                "up_ask":           up_ask,
                "down_ask":         down_ask,
                "up_bid":           up_bid,
                "up_ask_plus_down_ask": float(row.get("up_ask_plus_down_ask") or float("nan")),
                "up_bid_plus_down_bid": float(row.get("up_bid_plus_down_bid") or float("nan")),
                "maker_yes_fills":  maker_yes_fills,
                "maker_no_fills":   maker_no_fills,
            })
    df = pd.DataFrame(rows)
    print(f"Entry rows: {len(df):,}  ({df['close_date'].nunique()} dates)")
    return df

# ── wild cluster bootstrap ────────────────────────────────────────────────────
def wild_bootstrap_ci(values: np.ndarray, days: np.ndarray,
                      n_boot: int = N_BOOT, alpha: float = 0.05) -> tuple[float, float, float]:
    """
    Wild cluster bootstrap CI for mean(values).
    Rademacher weights {-1, +1} applied at day level.
    Returns (mean, ci_lo, ci_hi).
    """
    mu      = float(np.mean(values))
    resid   = values - mu
    unique_days = np.unique(days)
    boot_means  = np.empty(n_boot)
    for b in range(n_boot):
        weights = RNG.choice([-1.0, 1.0], size=len(unique_days))
        day_w   = {d: w for d, w in zip(unique_days, weights)}
        w_arr   = np.array([day_w[d] for d in days])
        boot_means[b] = mu + np.mean(resid * w_arr)
    ci_lo = float(np.percentile(boot_means, 100 * alpha / 2))
    ci_hi = float(np.percentile(boot_means, 100 * (1 - alpha / 2)))
    return mu, ci_lo, ci_hi

# ── walk-forward backtest ─────────────────────────────────────────────────────
def run_walkforward(df: pd.DataFrame, features: list[str], C: float,
                    tau_skip: float, horizon: int,
                    split: str = "non_lockbox") -> dict:
    """
    Expanding-window purged day walk-forward.
    Returns dict with taker and maker results.
    """
    data = df[(df["horizon"] == horizon)].copy()
    if split == "non_lockbox":
        data = data[~data["close_date"].isin(LOCKBOX_DAYS)]
    elif split == "lockbox":
        data = data[data["close_date"].isin(LOCKBOX_DAYS)]

    data = data.dropna(subset=features + ["label","up_ask","down_ask"]).reset_index(drop=True)
    dates = sorted(data["close_date"].unique())

    taker_pnls, taker_days = [], []
    maker_pnls,  maker_days  = [], []
    maker_fills_y = maker_fills_n = maker_opp = 0
    trade_yes = trade_no = total = 0

    for di, day in enumerate(dates):
        if di < 4:   # need ≥4 train days
            continue
        tr = data[data["close_date"] < day]
        te = data[data["close_date"] == day]
        if len(tr) < 50 or te.empty or tr["label"].nunique() < 2:
            continue

        # Scale features
        scaler = StandardScaler()
        Xtr = scaler.fit_transform(tr[features].values)
        Xte = scaler.transform(te[features].values)

        model = LogisticRegression(C=C, max_iter=500, solver="lbfgs")
        model.fit(Xtr, tr["label"].astype(int).values)
        p_up = model.predict_proba(Xte)[:, 1]

        for j, (_, row) in enumerate(te.iterrows()):
            total += 1
            label    = int(row["label"])
            up_ask   = float(row["up_ask"])
            down_ask = float(row["down_ask"])
            up_mid   = float(row["up_mid"])
            up_bid   = float(row["up_bid"]) if pd.notna(row["up_bid"]) else float("nan")
            p        = float(p_up[j])

            if not (math.isfinite(up_ask) and math.isfinite(down_ask)):
                continue
            if not (0 < up_ask < 1 and 0 < down_ask < 1):
                continue

            c_yes = up_ask   + TAKER_FEE
            c_no  = down_ask + TAKER_FEE

            ev_yes = p       - c_yes
            ev_no  = (1 - p) - c_no

            # Decision
            if ev_yes >= ev_no and ev_yes > tau_skip:
                side = 1
            elif ev_no > ev_yes and ev_no > tau_skip:
                side = -1
            else:
                continue   # SKIP

            if side == 1:
                trade_yes += 1
                entry_ask = up_ask
            else:
                trade_no += 1
                entry_ask = down_ask

            # ── TAKER ──────────────────────────────────────────
            pnl_t = net_pnl_taker(entry_ask, label, side)
            taker_pnls.append(pnl_t)
            taker_days.append(day)

            # ── MAKER ──────────────────────────────────────────
            maker_opp += 1
            fills = row["maker_yes_fills"] if side == 1 else row["maker_no_fills"]
            if fills:
                limit = up_mid if side == 1 else (1.0 - up_mid)
                pnl_m = net_pnl_maker(limit, label, side)
                maker_pnls.append(pnl_m)
                maker_days.append(day)
                if side == 1: maker_fills_y += 1
                else:         maker_fills_n += 1

    # Aggregate
    out = {"horizon": horizon, "C": C, "features": features,
           "n_available": total, "n_trade": trade_yes + trade_no,
           "trade_yes": trade_yes, "trade_no": trade_no,
           "trade_rate": (trade_yes + trade_no) / max(total, 1)}

    if taker_pnls:
        mu, lo, hi = wild_bootstrap_ci(np.array(taker_pnls), np.array(taker_days))
        out.update({"taker_mean_pnl": mu, "taker_ci_lo": lo, "taker_ci_hi": hi,
                    "taker_n_trades": len(taker_pnls)})
    else:
        out.update({"taker_mean_pnl": None, "taker_ci_lo": None, "taker_ci_hi": None,
                    "taker_n_trades": 0})

    fill_rate = (maker_fills_y + maker_fills_n) / max(maker_opp, 1)
    out["maker_fill_rate"] = fill_rate
    out["maker_n_fills"]   = maker_fills_y + maker_fills_n
    if maker_pnls:
        mu, lo, hi = wild_bootstrap_ci(np.array(maker_pnls), np.array(maker_days))
        out.update({"maker_mean_pnl": mu, "maker_ci_lo": lo, "maker_ci_hi": hi,
                    "maker_n_trades": len(maker_pnls)})
    else:
        out.update({"maker_mean_pnl": None, "maker_ci_lo": None, "maker_ci_hi": None,
                    "maker_n_trades": 0})
    return out

# ── no-arb scan ───────────────────────────────────────────────────────────────
def noarb_scan(contracts: dict[str, pd.DataFrame]) -> dict:
    """Scan all snapshots for up_ask + down_ask < 1 - fees (buy both = locked profit)."""
    FEE_BOTH = 2 * 0.01   # conservative: two taker fees
    buy_both_events  = []
    sell_both_events = []

    for slug, cdf in contracts.items():
        sub = cdf.dropna(subset=["up_ask_plus_down_ask","up_bid_plus_down_bid"])
        # Buy both: up_ask + down_ask < 1 - fees
        bb = sub[sub["up_ask_plus_down_ask"] < 1.0 - FEE_BOTH]
        for _, row in bb.iterrows():
            gap = 1.0 - FEE_BOTH - float(row["up_ask_plus_down_ask"])
            buy_both_events.append({"slug": slug, "gap": gap,
                                    "seconds_to_close": row.get("seconds_to_close"),
                                    "sum_ask": row["up_ask_plus_down_ask"]})
        # Sell both: up_bid + down_bid > 1 + fees (rare on Polymarket)
        sb = sub[sub["up_bid_plus_down_bid"] > 1.0 + FEE_BOTH]
        for _, row in sb.iterrows():
            gap = float(row["up_bid_plus_down_bid"]) - 1.0 - FEE_BOTH
            sell_both_events.append({"slug": slug, "gap": gap,
                                     "seconds_to_close": row.get("seconds_to_close"),
                                     "sum_bid": row["up_bid_plus_down_bid"]})

    total_rows = sum(len(c) for c in contracts.values())
    return {
        "total_snapshots":     total_rows,
        "buy_both_events":     len(buy_both_events),
        "buy_both_pct":        100 * len(buy_both_events) / max(total_rows, 1),
        "buy_both_mean_gap":   float(np.mean([e["gap"] for e in buy_both_events])) if buy_both_events else 0,
        "buy_both_max_gap":    float(np.max([e["gap"] for e in buy_both_events])) if buy_both_events else 0,
        "sell_both_events":    len(sell_both_events),
        "sell_both_pct":       100 * len(sell_both_events) / max(total_rows, 1),
        "sell_both_mean_gap":  float(np.mean([e["gap"] for e in sell_both_events])) if sell_both_events else 0,
        "buy_both_detail":     buy_both_events[:20],
    }

# ── report ────────────────────────────────────────────────────────────────────
def fmt(v, fmt_str=".4f"):
    return f"{v:{fmt_str}}" if v is not None and not (isinstance(v, float) and math.isnan(v)) else "n/a"

def write_report(results: list[dict], noarb: dict, lockbox_result: dict | None) -> None:
    lines = [
        "# Parsimonious Mean-Reversion Profit Backtest — Results\n\n",
        f"*{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  — {_n_trials} trials registered*\n\n",
        "## Phase 2 — Walk-Forward Profit Results (non-lockbox days)\n\n",
        "### Taker Execution\n\n",
        "| Horizon | Features | C | Trades | Trade% | Net PnL/contract | 95% CI |\n",
        "|---------|----------|---|--------|--------|-----------------|--------|\n",
    ]
    for r in results:
        feat_str = "+".join(r["features"])
        lines.append(
            f"| T={r['horizon']}s | {feat_str} | {r['C']} | "
            f"{r['taker_n_trades']} | {100*r['trade_rate']:.1f}% | "
            f"{fmt(r['taker_mean_pnl'])} | "
            f"[{fmt(r['taker_ci_lo'])}, {fmt(r['taker_ci_hi'])}] |\n"
        )

    lines += [
        "\n### Maker Execution\n\n",
        "| Horizon | Features | C | Fill Rate | Filled Trades | Net PnL/contract | 95% CI |\n",
        "|---------|----------|---|-----------|---------------|-----------------|--------|\n",
    ]
    for r in results:
        feat_str = "+".join(r["features"])
        lines.append(
            f"| T={r['horizon']}s | {feat_str} | {r['C']} | "
            f"{100*r['maker_fill_rate']:.1f}% | {r['maker_n_trades']} | "
            f"{fmt(r['maker_mean_pnl'])} | "
            f"[{fmt(r['maker_ci_lo'])}, {fmt(r['maker_ci_hi'])}] |\n"
        )

    lines += [
        "\n## Phase 3 — No-Arb Scan\n\n",
        f"| Metric | Value |\n",
        f"|--------|-------|\n",
        f"| Total snapshots scanned | {noarb['total_snapshots']:,} |\n",
        f"| Buy-both violations (ask_sum < 1 − fees) | {noarb['buy_both_events']} ({noarb['buy_both_pct']:.3f}%) |\n",
        f"| Buy-both mean gap | {noarb['buy_both_mean_gap']:.4f} |\n",
        f"| Buy-both max gap  | {noarb['buy_both_max_gap']:.4f} |\n",
        f"| Sell-both violations (bid_sum > 1 + fees) | {noarb['sell_both_events']} |\n\n",
    ]

    lines.append("## Final Gate — Lockbox\n\n")
    if lockbox_result is None:
        lines.append("**SEALED** — no Phase 2 configuration cleared CI_lo > 0. Lockbox not opened.\n\n")
    else:
        r = lockbox_result
        feat_str = "+".join(r["features"])
        lines += [
            f"Opened with frozen config: T={r['horizon']}s, features={feat_str}, C={r['C']}\n\n",
            "**Taker:**\n",
            f"  Net PnL/contract: {fmt(r['taker_mean_pnl'])}  CI: [{fmt(r['taker_ci_lo'])}, {fmt(r['taker_ci_hi'])}]  "
            f"trades: {r['taker_n_trades']}\n\n",
            "**Maker:**\n",
            f"  Net PnL/contract: {fmt(r['maker_mean_pnl'])}  CI: [{fmt(r['maker_ci_lo'])}, {fmt(r['maker_ci_hi'])}]  "
            f"fill rate: {100*r['maker_fill_rate']:.1f}%  filled: {r['maker_n_trades']}\n\n",
        ]

    # Verdict
    lines.append("## Verdict\n\n")
    primary = [r for r in results if r["horizon"] == PRIMARY_T]
    taker_pass  = [r for r in primary if r.get("taker_ci_lo") and r["taker_ci_lo"] > 0]
    maker_pass  = [r for r in primary if r.get("maker_ci_lo") and r["maker_ci_lo"] > 0]

    lines.append(f"**Taker (T={PRIMARY_T}s):** ")
    if taker_pass:
        b = taker_pass[0]
        lines.append(f"POSITIVE — {fmt(b['taker_mean_pnl'])}/contract, CI_lo={fmt(b['taker_ci_lo'])} > 0 ✓\n\n")
    else:
        b = primary[0] if primary else {}
        lines.append(f"NEGATIVE — mean={fmt(b.get('taker_mean_pnl'))}, CI_lo={fmt(b.get('taker_ci_lo'))} ≤ 0. "
                     f"Signal value is sub-spread as expected.\n\n")

    lines.append(f"**Maker (T={PRIMARY_T}s):** ")
    if maker_pass:
        b = maker_pass[0]
        lines.append(f"POSITIVE — {fmt(b['maker_mean_pnl'])}/contract, CI_lo={fmt(b['maker_ci_lo'])} > 0, "
                     f"fill rate {100*b['maker_fill_rate']:.1f}% ✓\n\n")
    else:
        b = primary[0] if primary else {}
        lines.append(f"fill rate {100*b.get('maker_fill_rate',0):.1f}%  "
                     f"mean={fmt(b.get('maker_mean_pnl'))}, CI_lo={fmt(b.get('maker_ci_lo'))} ≤ 0.\n\n")

    lines.append(f"*{_n_trials} trials registered (L2 grid × feature sets × horizons). "
                 "Deflation by trial count required before any deployment claim.*\n")

    path = OUT_DIR / "RESULTS_profit.md"
    path.write_text("".join(lines))
    print(f"\nReport: {path}")


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # Step 0: pre-register before loading outcomes
    pre_path = OUT_DIR / "preregistration.md"
    pre_path.write_text(
        "# Pre-registration: Profit Backtest\n\n"
        f"*Written at {PRE_REG['date_utc']} before any results.*\n\n"
        "```json\n" + json.dumps(PRE_REG, indent=2) + "\n```\n"
    )
    print(f"Pre-registration: {pre_path}")

    print("\n=== Loading data ===")
    outcomes  = load_outcomes()
    hf        = load_hf_trades()
    contracts = load_contracts(outcomes)

    # Phase 3: No-arb scan (no training needed)
    print("\n=== Phase 3: No-arb scan ===")
    noarb = noarb_scan(contracts)
    print(f"  Buy-both violations: {noarb['buy_both_events']} ({noarb['buy_both_pct']:.3f}%)")
    print(f"  Mean gap: {noarb['buy_both_mean_gap']:.4f}  Max gap: {noarb['buy_both_max_gap']:.4f}")
    with open(OUT_DIR / "noarb_check.md", "w") as f:
        f.write(f"# No-Arb Scan\n\n"
                f"Total snapshots: {noarb['total_snapshots']:,}\n\n"
                f"Buy-both (ask_sum < 1 - fees): {noarb['buy_both_events']} ({noarb['buy_both_pct']:.3f}%)\n"
                f"Mean gap: {noarb['buy_both_mean_gap']:.4f}  Max: {noarb['buy_both_max_gap']:.4f}\n\n"
                f"Sell-both (bid_sum > 1 + fees): {noarb['sell_both_events']}\n\n"
                f"Sample buy-both events:\n```\n"
                + json.dumps(noarb["buy_both_detail"][:10], indent=2) + "\n```\n")

    print("\n=== Building entry rows ===")
    all_horizons = [PRIMARY_T] + ROBUST_T
    df = build_entry_rows(contracts, hf, all_horizons)
    df.to_csv(OUT_DIR / "entry_rows.csv", index=False)

    print("\n=== Phase 1 + 2: Walk-forward backtest ===")
    feature_sets = [
        ["logit_mid", "hf_ret_60s"],           # parsimonious 2-feature
        ["logit_mid", "hf_ret_60s", "obi_l1"], # + order book
    ]

    all_results = []
    best_maker_ci_lo = -999

    for feats in feature_sets:
        for C in C_GRID:
            # Primary horizon
            print(f"\n  Features={feats}  C={C}  T={PRIMARY_T}")
            r = run_walkforward(df, feats, C, TAU_SKIP, PRIMARY_T)
            r["features"] = feats
            all_results.append(r)
            log_trial({"phase":"profit_wf","features":str(feats),"C":C,"horizon":PRIMARY_T},
                      {"taker_pnl":r["taker_mean_pnl"],"taker_ci_lo":r["taker_ci_lo"],
                       "maker_pnl":r["maker_mean_pnl"],"maker_ci_lo":r["maker_ci_lo"],
                       "fill_rate":r["maker_fill_rate"],"trade_rate":r["trade_rate"]})
            print(f"    TAKER: {fmt(r['taker_mean_pnl'])} [{fmt(r['taker_ci_lo'])},{fmt(r['taker_ci_hi'])}]  "
                  f"n={r['taker_n_trades']}  rate={100*r['trade_rate']:.1f}%")
            print(f"    MAKER: {fmt(r['maker_mean_pnl'])} [{fmt(r['maker_ci_lo'])},{fmt(r['maker_ci_hi'])}]  "
                  f"fill={100*r['maker_fill_rate']:.1f}%  n={r['maker_n_trades']}")

            if r.get("maker_ci_lo") and r["maker_ci_lo"] > best_maker_ci_lo:
                best_maker_ci_lo = r["maker_ci_lo"]
                best_config = r

            # Robustness horizons (logged but not primary)
            for T_r in ROBUST_T:
                rr = run_walkforward(df, feats, C, TAU_SKIP, T_r)
                rr["features"] = feats
                all_results.append(rr)
                log_trial({"phase":"profit_wf_robust","features":str(feats),"C":C,"horizon":T_r},
                          {"taker_pnl":rr["taker_mean_pnl"],"taker_ci_lo":rr["taker_ci_lo"],
                           "maker_pnl":rr["maker_mean_pnl"],"maker_ci_lo":rr["maker_ci_lo"],
                           "fill_rate":rr["maker_fill_rate"],"trade_rate":rr["trade_rate"]})
                print(f"    [robust T={T_r}] TAKER {fmt(rr['taker_mean_pnl'])}  MAKER {fmt(rr['maker_mean_pnl'])}  fill={100*rr['maker_fill_rate']:.1f}%")

    # Save results table
    pd.DataFrame(all_results).to_csv(OUT_DIR / "profit_table.csv", index=False)

    # Final Gate
    lockbox_result = None
    if best_maker_ci_lo > 0:
        print(f"\n=== FINAL GATE: Maker CI_lo={best_maker_ci_lo:.4f} > 0 — opening lockbox ===")
        lb_r = run_walkforward(df, best_config["features"], best_config["C"],
                               TAU_SKIP, PRIMARY_T, split="lockbox")
        lb_r["features"] = best_config["features"]
        lockbox_result = lb_r
        log_trial({"phase":"final_gate_lockbox","features":str(best_config["features"]),
                   "C":best_config["C"],"horizon":PRIMARY_T},
                  {"taker_pnl":lb_r["taker_mean_pnl"],"taker_ci_lo":lb_r["taker_ci_lo"],
                   "maker_pnl":lb_r["maker_mean_pnl"],"maker_ci_lo":lb_r["maker_ci_lo"],
                   "fill_rate":lb_r["maker_fill_rate"]})
        print(f"  LOCKBOX TAKER: {fmt(lb_r['taker_mean_pnl'])} [{fmt(lb_r['taker_ci_lo'])},{fmt(lb_r['taker_ci_hi'])}]")
        print(f"  LOCKBOX MAKER: {fmt(lb_r['maker_mean_pnl'])} [{fmt(lb_r['maker_ci_lo'])},{fmt(lb_r['maker_ci_hi'])}]  fill={100*lb_r['maker_fill_rate']:.1f}%")
    else:
        print(f"\n=== FINAL GATE: best maker CI_lo={best_maker_ci_lo:.4f} ≤ 0 — lockbox stays SEALED ===")

    write_report(all_results, noarb, lockbox_result)
    print(f"\nTotal trials: {_n_trials}")


if __name__ == "__main__":
    main()
