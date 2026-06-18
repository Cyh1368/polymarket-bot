#!/usr/bin/env python3
"""
2026-06-17-research/backtest_lib.py

Shared backtest engine for the multi-strategy DRY-TESTING dashboard.

>>> DRY TESTING / RESEARCH ONLY. NOTHING HERE PLACES ORDERS OR TOUCHES LIVE CAPITAL. <<<

Each Strategy is the frozen Huber edge architecture (two regressors f_yes/f_no) with its own
entry time T1, hyperparameter config, and post-model decision filters. Every strategy is
evaluated by the SAME honest out-of-sample protocol used throughout this study:
expanding-window CV (5 folds, MIN_TRAIN=200), models trained inside each fold (no leakage),
multi-seed averaged, then scored with the lottery-robust gate (true-EV day-block CI > 0 AND
win-capped > 0 AND trim10 > 0).

A strategy "uses a different model" via its `cfg`/`t1`; it "uses different post-model
filters" via skip_bonus / yes_gate_lo / block_above / band. This is what the dashboard tabs
compare.
"""
from __future__ import annotations
import copy
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import lightgbm as lgb
from scipy import stats

import huber_common as hc
from robust_metrics import win_capped_ev, day_block_bootstrap

N_FOLDS, MIN_TRAIN = 5, 200
CLASS_YES, CLASS_NO, CLASS_SKIP = 0, 1, 2


# ── Strategy spec ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Strategy:
    name: str
    t1: int
    cfg: dict
    desc: str = ""
    skip_bonus: float = 0.05
    yes_gate_lo: float = 0.25          # Filter B: block YES below this p_yes_mid
    block_above: float = 1.01          # Filter B+: skip ALL trades when p_yes_mid >= this
    band: Optional[Tuple[float, float]] = None   # cost-band on traded-side ask [lo, hi)


def _base_mc150() -> dict:
    c = copy.deepcopy(hc.BASE_CFG); c.update(min_child_samples=150); return c

def _combined() -> dict:
    c = _base_mc150(); c.update(huber_alpha=0.5, learning_rate=0.02, n_rounds=750); return c


def default_strategies() -> list[Strategy]:
    """The dashboard tabs. Add/edit here to compare more strategies."""
    return [
        Strategy("prod_t180_mc150", 180, _base_mc150(),
                 "Currently DEPLOYED (dry): baseline mc150 @ T1=180. Lone-spike gate pass (2/5)."),
        Strategy("t245_combined", 245, _combined(),
                 "NEW candidate: mc150 + huber_alpha=0.5 + lr0.02/750 @ T1=245. Most "
                 "seed-robust pass in study (5/5, worst-seed CI +0.006)."),
        Strategy("t250_combined", 250, _combined(),
                 "Sibling of t245_combined @ T1=250 — confirms a contiguous 245-250 pass region (5/5)."),
        Strategy("t235_baseline", 235, _base_mc150(),
                 "Baseline mc150 @ T1=235 — best baseline-config point (5/5), but isolated/knife-edge."),
        Strategy("t245_combined_band", 245, _combined(),
                 "t245_combined + cost-band [0.40,0.80) post-filter — drawdown-control variant.",
                 band=(0.40, 0.80)),
        Strategy("t245_combined_filterBplus", 245, _combined(),
                 "t245_combined + Filter B+ (skip all when p_yes_mid>=0.95) — removes the "
                 "near-certain longshots.", block_above=0.95),
    ]


# ── Decision rule (generalized hc.decide_huber with per-strategy filters) ─────────
def decide(pred_yes, pred_no, p_mid, up_ask, down_ask, s: Strategy) -> int:
    if p_mid >= s.block_above:                      # Filter B+
        return CLASS_SKIP
    allow_yes = p_mid >= s.yes_gate_lo              # Filter B
    side = CLASS_SKIP
    if pred_no > s.skip_bonus and pred_no >= pred_yes:
        side = CLASS_NO
    elif allow_yes and pred_yes > s.skip_bonus and pred_yes > pred_no:
        side = CLASS_YES
    if s.band is not None and side != CLASS_SKIP:   # cost-band on traded side
        ask = up_ask if side == CLASS_YES else down_ask
        if not (s.band[0] <= ask < s.band[1]):
            side = CLASS_SKIP
    return side


def _pnl(side, y, ya, na):
    if side == CLASS_YES: return y / max(ya, 1e-6) - 1.0
    if side == CLASS_NO:  return (1.0 - y) / max(na, 1e-6) - 1.0
    return None


def build_frame_at(contracts, t1: int) -> pd.DataFrame:
    hc.T1 = t1
    rows = [hc.extract_row(cd) for cd in contracts]
    df = pd.DataFrame([r for r in rows if r is not None])
    if not df.empty:
        df = df.sort_values("close_time").reset_index(drop=True)   # chronological for equity curve
    return df


def _run_cv(df: pd.DataFrame, s: Strategy, seed: int) -> pd.DataFrame:
    n = len(df); fold = n // N_FOLDS
    recs = []
    for k in range(N_FOLDS):
        te_s = k * fold
        te_e = (k + 1) * fold if k < N_FOLDS - 1 else n
        if te_s < MIN_TRAIN:
            continue
        tr, te = df.iloc[:te_s], df.iloc[te_s:te_e]
        Xtr = tr[hc.FEATURES].to_numpy().astype(float)
        Xte = te[hc.FEATURES].to_numpy().astype(float)
        ty = tr["y_settle"].to_numpy().astype(float) / tr["c_yes"].to_numpy() - 1.0
        tn = (1.0 - tr["y_settle"].to_numpy().astype(float)) / tr["c_no"].to_numpy() - 1.0
        my = lgb.train(hc.huber_params(s.cfg, seed=seed + k),
                       lgb.Dataset(Xtr, label=ty, free_raw_data=False), num_boost_round=s.cfg["n_rounds"])
        mn = lgb.train(hc.huber_params(s.cfg, seed=seed + k),
                       lgb.Dataset(Xtr, label=tn, free_raw_data=False), num_boost_round=s.cfg["n_rounds"])
        py, pn = my.predict(Xte), mn.predict(Xte)
        for i, (_, row) in enumerate(te.iterrows()):
            d = row.to_dict()
            side = decide(float(py[i]), float(pn[i]), float(d["p_yes_mid"]),
                          float(d["up_ask"]), float(d["down_ask"]), s)
            recs.append({
                "contract_id": d["contract_id"], "close_time": d["close_time"],
                "action": ["YES", "NO", "SKIP"][side],
                "pnl": _pnl(side, float(d["y_settle"]), float(d["up_ask"]), float(d["down_ask"])),
                "p_yes_mid": round(float(d["p_yes_mid"]), 3),
                "up_ask": round(float(d["up_ask"]), 3), "down_ask": round(float(d["down_ask"]), 3),
            })
    return pd.DataFrame(recs)


def _score(ledger: pd.DataFrame, boot: int) -> dict:
    traded = ledger[ledger["action"] != "SKIP"].dropna(subset=["pnl"]).copy()
    pool = len(ledger)
    if traded.empty:
        return {"trades": 0, "pool": pool, "ev_avail": float("nan"), "ev_trig": float("nan"),
                "win_rate": float("nan"), "n_days": 0, "true_ev_ci_lo": float("nan"),
                "win_capped": float("nan"), "trim10": float("nan"), "gate": False}
    r = traded["pnl"].to_numpy()
    epoch = traded["contract_id"].astype(str).str.extract(r"(\d{10})$")[0].astype(float)
    day = pd.to_datetime(epoch, unit="s", utc=True).dt.date.astype(str)
    bdf = pd.DataFrame({"r": r, "day": day.to_numpy()})
    lo, _, _ = day_block_bootstrap(bdf, np.mean, boot)
    capped = win_capped_ev(r); trim = float(stats.trim_mean(r, 0.10))
    return {"trades": len(traded), "pool": pool, "ev_avail": float(r.sum() / pool),
            "ev_trig": float(r.mean()), "win_rate": float((r > 0).mean()),
            "n_days": int(day.nunique()), "true_ev_ci_lo": float(lo),
            "win_capped": float(capped), "trim10": float(trim),
            "gate": bool(lo > 0 and capped > 0 and trim > 0)}


def run_strategy(contracts, s: Strategy, n_seeds: int = 5, boot: int = 2000) -> dict:
    """Full evaluation of one strategy. Returns metrics + seed-0 equity curve + recent trades."""
    df = build_frame_at(contracts, s.t1)
    if len(df) < MIN_TRAIN + 50:
        return {"name": s.name, "error": f"too few rows ({len(df)}) at T1={s.t1}"}

    seed_scores, led0 = [], None
    for si in range(n_seeds):
        led = _run_cv(df, s, seed=si * 100)
        if si == 0:
            led0 = led
        seed_scores.append(_score(led, boot))

    def mean(k): return float(np.mean([x[k] for x in seed_scores]))
    def wmin(k): return float(np.min([x[k] for x in seed_scores]))

    # seed-0 equity curve (chronological OOS) for the chart + recent trades table
    traded0 = led0[led0["action"] != "SKIP"].dropna(subset=["pnl"]).copy()
    traded0 = traded0.sort_values("close_time")
    equity = np.cumsum(traded0["pnl"].to_numpy()) if len(traded0) else np.array([])
    total_pnl = float(equity[-1]) if len(equity) else 0.0
    cols = ["close_time", "contract_id", "action", "p_yes_mid", "up_ask", "down_ask", "pnl"]
    led_df = traded0[cols].copy()
    led_df["close_time"] = led_df["close_time"].astype(str)   # plain str (JSON-safe)
    led_df["cum_pnl"] = equity.round(4) if len(equity) else []
    ledger = led_df.to_dict("records")          # full seed-0 ledger → persisted to disk
    recent = led_df.tail(15)[["close_time", "action", "p_yes_mid",
                              "up_ask", "down_ask", "pnl"]].to_dict("records")

    return {
        "name": s.name, "desc": s.desc, "t1": s.t1,
        "cfg": {k: s.cfg[k] for k in ("min_child_samples", "huber_alpha", "lambda_l2",
                                      "learning_rate", "n_rounds", "max_depth")},
        "filters": {"skip_bonus": s.skip_bonus, "yes_gate_lo": s.yes_gate_lo,
                    "block_above": s.block_above, "band": s.band},
        "rows": int(len(df)), "n_seeds": n_seeds,
        "trades": int(mean("trades")), "n_days": int(mean("n_days")),
        "ev_avail": mean("ev_avail"), "ev_trig": mean("ev_trig"),
        "win_rate": mean("win_rate"), "win_capped": mean("win_capped"),
        "trim10": mean("trim10"), "true_ev_ci_lo": mean("true_ev_ci_lo"),
        "ci_lo_worst": wmin("true_ev_ci_lo"), "capped_worst": wmin("win_capped"),
        "seeds_pass": int(sum(x["gate"] for x in seed_scores)),
        "total_pnl": total_pnl,                 # cumulative seed-0 OOS PnL (return units, R)
        "equity": [round(float(v), 4) for v in equity],
        "recent": recent,
        "ledger": ledger,                       # full seed-0 OOS ledger (worker writes to disk, then pops)
    }
