#!/usr/bin/env python3
"""
2026-06-17-research/robust_metrics.py

Re-score any trade ledger with lottery-robust metrics, reported ALONGSIDE true EV.

Motivation (see lottery_dependence_redesign.md): with constant-value (fixed-dollar)
staking, per-trade return on a $1 stake is r = 1/p - 1 on a win, -1 on a loss. Mean EV
of this payoff is dominated by rare low-price ("lottery") wins. We therefore report:

  - TRUE metrics      (mean EV, median, day-block bootstrap CI)   -> does it make money?
  - LOTTERY-FREE      (win-capped EV, 10% trimmed mean, Sharpe)   -> is the edge broad?
  - TAIL concentration (top-k PnL share, trades-to-flip-negative)
  - GATE verdict       (the §3 conjunction)

Deployment GATE (conjunction):
    true-EV day-block bootstrap lower-CI > 0   AND
    win-capped EV > 0                          AND
    10% trimmed mean > 0

Usage:
    python robust_metrics.py LEDGER.csv [LEDGER2.csv ...] [--alpha 0.10] [--boot 5000]

Supported ledger schemas (auto-detected):
  A) CV ledger      : columns {action, pnl, contract_id[, up_ask, down_ask, y_settle]}
  B) regime ledger  : columns {action, pnl, contract_id, y_settle}
  C) live trader    : event-based; uses event==outcome rows with
                      {dollar_pnl, fill_price, filled_size, correct, timestamp_utc}
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats


# ── Ledger adapters ────────────────────────────────────────────────────────────
#
# Each adapter returns a normalized DataFrame with columns:
#   r     : per-trade return on a $1 stake  (win: 1/p-1 ; loss: -1)
#   fill  : price actually paid on the traded side  (NaN if unknown)
#   day   : UTC date string for block bootstrap
#   won   : bool

def _day_from_contract_id(cid: pd.Series) -> pd.Series:
    """Extract the UTC date from a slug like 'btc-updown-5m-1781004300'."""
    epoch = cid.astype(str).str.extract(r"(\d{10})$")[0].astype("float")
    return pd.to_datetime(epoch, unit="s", utc=True).dt.date.astype(str)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    cols = set(df.columns)

    # ── Schema C: live trader (event-based) ──────────────────────────────────
    if {"event", "dollar_pnl", "fill_price"} <= cols:
        out = df[df["event"] == "outcome"].copy()
        for c in ("dollar_pnl", "fill_price", "filled_size", "correct"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=["dollar_pnl", "fill_price", "filled_size"])
        stake = out["fill_price"] * out["filled_size"]
        r = out["dollar_pnl"] / stake.replace(0, np.nan)
        day = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce").dt.date.astype(str)
        return pd.DataFrame({
            "r": r.to_numpy(),
            "fill": out["fill_price"].to_numpy(),
            "day": day.to_numpy(),
            "won": (out["correct"] == 1).to_numpy(),
        }).dropna(subset=["r"])

    # ── Schema A/B: CV / regime ledger (one row per OOS contract) ─────────────
    if {"action", "pnl"} <= cols:
        out = df[df["action"] != "SKIP"].copy()
        out["pnl"] = pd.to_numeric(out["pnl"], errors="coerce")
        out = out.dropna(subset=["pnl"])
        if {"up_ask", "down_ask"} <= cols:
            fill = np.where(out["action"] == "YES",
                            pd.to_numeric(out["up_ask"], errors="coerce"),
                            pd.to_numeric(out["down_ask"], errors="coerce"))
        else:
            fill = np.full(len(out), np.nan)
        day = (_day_from_contract_id(out["contract_id"])
               if "contract_id" in cols else pd.Series(["all"] * len(out)))
        return pd.DataFrame({
            "r": out["pnl"].to_numpy(),
            "fill": fill,
            "day": np.asarray(day),
            "won": (out["pnl"] > 0).to_numpy(),
        })

    raise ValueError(f"Unrecognized ledger schema; columns={sorted(cols)[:12]}…")


# ── Metrics ──────────────────────────────────────────────────────────────────

def win_capped_ev(r: np.ndarray) -> float:
    """Every win counted as flat +1 (losses stay -1) -> lottery-free EV."""
    return float(np.minimum(r, 1.0).mean())


def day_block_bootstrap(df: pd.DataFrame, statfn, b: int, seed: int = 0):
    """Resample whole days with replacement; return (lo, hi) 95% CI and P(stat<=0)."""
    days = df["day"].unique()
    by_day = {d: df.loc[df["day"] == d, "r"].to_numpy() for d in days}
    rng = np.random.default_rng(seed)
    vals = np.empty(b)
    for i in range(b):
        chosen = rng.choice(days, size=len(days), replace=True)
        sample = np.concatenate([by_day[d] for d in chosen])
        vals[i] = statfn(sample)
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5)), float((vals <= 0).mean())


def score(df: pd.DataFrame, alpha: float, boot: int) -> dict:
    r = df["r"].to_numpy()
    n = len(r)
    total = r.sum()
    srt = np.sort(r)[::-1]

    # trades-to-flip-negative
    flip_k = None
    if total > 0:
        for k in range(1, n + 1):
            if total - srt[:k].sum() <= 0:
                flip_k = k
                break

    true_lo, true_hi, true_pneg = day_block_bootstrap(df, np.mean, boot)
    cap_lo, cap_hi, cap_pneg = day_block_bootstrap(df, win_capped_ev, boot)

    capped = win_capped_ev(r)
    trim = float(stats.trim_mean(r, alpha))

    gate = (true_lo > 0) and (capped > 0) and (trim > 0)

    return {
        "n_trades": n, "n_days": df["day"].nunique(),
        "win_rate": float(df["won"].mean()),
        "true_mean_ev": float(r.mean()),
        "true_ev_ci_lo": true_lo, "true_ev_ci_hi": true_hi, "true_ev_p_neg": true_pneg,
        "median": float(np.median(r)),
        "win_capped_ev": capped,
        "win_capped_ci_lo": cap_lo, "win_capped_ci_hi": cap_hi,
        "trim10_mean": trim,
        "sharpe_per_trade": float(r.mean() / r.std()) if r.std() > 0 else float("nan"),
        "top1_share": float(srt[:1].sum() / total) if total else float("nan"),
        "top5_share": float(srt[:5].sum() / total) if total else float("nan"),
        "top10_share": float(srt[:10].sum() / total) if total else float("nan"),
        "trades_to_flip_negative": flip_k,
        "GATE_PASS": gate,
    }


def price_band_table(df: pd.DataFrame) -> pd.DataFrame:
    if df["fill"].isna().all():
        return pd.DataFrame()
    bands = [(0.0, 0.20), (0.20, 0.30), (0.30, 0.50),
             (0.50, 0.70), (0.70, 0.85), (0.85, 1.0)]
    rows = []
    for lo, hi in bands:
        sub = df[(df["fill"] >= lo) & (df["fill"] < hi)]
        if sub.empty:
            continue
        r = sub["r"].to_numpy()
        rows.append({
            "band": f"[{lo:.2f},{hi:.2f})", "N": len(sub),
            "win_rate": round(float(sub["won"].mean()), 3),
            "true_mean": round(float(r.mean()), 3),
            "win_capped": round(win_capped_ev(r), 3),
            "trim10": round(float(stats.trim_mean(r, 0.10)), 3),
            "sharpe": round(float(r.mean() / r.std()), 3) if r.std() > 0 else float("nan"),
        })
    return pd.DataFrame(rows)


# ── Report ─────────────────────────────────────────────────────────────────────

def report_one(path: Path, alpha: float, boot: int) -> dict:
    raw = pd.read_csv(path, low_memory=False)
    norm = _normalize(raw)
    s = score(norm, alpha, boot)

    print(f"\n{'='*72}\n{path}\n{'='*72}")
    print(f"  trades={s['n_trades']}  days={s['n_days']}  win_rate={s['win_rate']:.1%}")
    print(f"  ── TRUE (does it make money?) ─────────────────────────────────")
    print(f"     mean EV          : {s['true_mean_ev']:+.4f}")
    print(f"     day-block 95% CI : [{s['true_ev_ci_lo']:+.4f}, {s['true_ev_ci_hi']:+.4f}]"
          f"   P(EV<=0)={s['true_ev_p_neg']:.1%}")
    print(f"     median           : {s['median']:+.4f}")
    print(f"  ── LOTTERY-FREE (is the edge broad?) ──────────────────────────")
    print(f"     win-capped EV    : {s['win_capped_ev']:+.4f}"
          f"   (95% CI [{s['win_capped_ci_lo']:+.4f}, {s['win_capped_ci_hi']:+.4f}])")
    print(f"     10% trimmed mean : {s['trim10_mean']:+.4f}")
    print(f"     Sharpe / trade   : {s['sharpe_per_trade']:+.4f}")
    print(f"  ── TAIL concentration ─────────────────────────────────────────")
    print(f"     top1/top5/top10 share of total PnL : "
          f"{s['top1_share']:.0%} / {s['top5_share']:.0%} / {s['top10_share']:.0%}")
    print(f"     trades to flip negative            : {s['trades_to_flip_negative']}")
    print(f"  ── GATE (true-CI>0 AND capped>0 AND trim10>0) ─────────────────")
    print(f"     {'PASS ✅' if s['GATE_PASS'] else 'FAIL ❌'}")

    band = price_band_table(norm)
    if not band.empty:
        print(f"  ── by fill-price band ─────────────────────────────────────────")
        print("     " + band.to_string(index=False).replace("\n", "\n     "))

    s["ledger"] = str(path)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ledgers", nargs="+", help="trade ledger CSV(s)")
    ap.add_argument("--alpha", type=float, default=0.10, help="trim fraction each tail")
    ap.add_argument("--boot", type=int, default=5000, help="bootstrap resamples")
    ap.add_argument("--out", default=None, help="optional summary CSV path")
    args = ap.parse_args()

    rows = []
    for lp in args.ledgers:
        p = Path(lp)
        if not p.exists():
            print(f"!! skip (not found): {p}", file=sys.stderr)
            continue
        try:
            rows.append(report_one(p, args.alpha, args.boot))
        except Exception as e:  # noqa: BLE001
            print(f"!! error scoring {p}: {e}", file=sys.stderr)

    if rows and args.out:
        cols = ["ledger", "n_trades", "n_days", "win_rate", "true_mean_ev",
                "true_ev_ci_lo", "true_ev_ci_hi", "true_ev_p_neg", "median",
                "win_capped_ev", "win_capped_ci_lo", "win_capped_ci_hi",
                "trim10_mean", "sharpe_per_trade", "top1_share", "top5_share",
                "top10_share", "trades_to_flip_negative", "GATE_PASS"]
        pd.DataFrame(rows)[cols].to_csv(args.out, index=False)
        print(f"\nSummary → {args.out}")


if __name__ == "__main__":
    main()
