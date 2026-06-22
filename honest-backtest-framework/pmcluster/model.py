#!/usr/bin/env python3
"""
pmcluster.model — the Huber two-regressor edge model + decision rules.

Two regressors:
  f_yes(x) ~= realized YES return = y/c_yes - 1
  f_no(x)  ~= realized NO  return = (1-y)/c_no - 1
Decision: trade the side with higher predicted edge above SKIP_BONUS (single), or
require seed-agreement consensus (ensemble). Identical to the CFES local code.
"""
from __future__ import annotations
import numpy as np
import lightgbm as lgb

from . import config as C


def lgb_params(seed: int, num_threads: int = 1) -> dict:
    cfg = C.MODEL_CFG
    return {
        "objective": "huber", "alpha": cfg["huber_alpha"], "metric": "huber",
        "num_leaves": cfg["num_leaves"], "max_depth": cfg["max_depth"],
        "min_child_samples": cfg["min_child_samples"], "subsample": cfg["subsample"],
        "feature_fraction": cfg["feature_fraction"], "lambda_l2": cfg["lambda_l2"],
        "lambda_l1": 0.0, "learning_rate": cfg["learning_rate"],
        "num_threads": num_threads, "seed": int(seed), "verbose": -1,
    }


def train_pair(tr, seed: int, num_threads: int = 1):
    X = tr[C.FEATURES].to_numpy().astype(float)
    y_yes = tr["y_settle"].to_numpy() / tr["c_yes"].to_numpy() - 1.0
    y_no = (1.0 - tr["y_settle"].to_numpy()) / tr["c_no"].to_numpy() - 1.0
    p = lgb_params(seed, num_threads=num_threads)
    n = C.MODEL_CFG["n_rounds"]
    m_yes = lgb.train(p, lgb.Dataset(X, label=y_yes), num_boost_round=n)
    m_no = lgb.train(p, lgb.Dataset(X, label=y_no), num_boost_round=n)
    return m_yes, m_no


def _pnl(side: str, y: float, c_yes: float, c_no: float) -> float:
    if side == C.CLASS_YES:
        return y / max(c_yes, 1e-6) - 1.0
    if side == C.CLASS_NO:
        return (1.0 - y) / max(c_no, 1e-6) - 1.0
    return 0.0


def decide_single(te, m_yes, m_no, skip=C.SKIP_BONUS, yes_gate=C.YES_GATE_LO):
    """Single-model decision. Returns arrays (side, pnl) aligned to te rows."""
    X = te[C.FEATURES].to_numpy().astype(float)
    py, pn = m_yes.predict(X), m_no.predict(X)
    y = te["y_settle"].to_numpy()
    cy = te["c_yes"].to_numpy(); cn = te["c_no"].to_numpy()
    mid = te["p_yes_mid"].to_numpy()
    sides, pnls = [], []
    for i in range(len(te)):
        a, b = py[i], pn[i]
        allow_yes = mid[i] >= yes_gate
        if b > skip and b >= a:
            side = C.CLASS_NO
        elif allow_yes and a > skip and a > b:
            side = C.CLASS_YES
        else:
            side = C.CLASS_SKIP
        sides.append(side)
        pnls.append(_pnl(side, y[i], cy[i], cn[i]))
    return np.array(sides, dtype=object), np.array(pnls, dtype=float)


def decide_ensemble(te, models, agree_min=C.AGREE_MIN, margin=C.EDGE_MARGIN,
                    skip=C.SKIP_BONUS, yes_gate=C.YES_GATE_LO):
    """Seed-agreement decision: trade only on cross-seed consensus + edge margin."""
    X = te[C.FEATURES].to_numpy().astype(float)
    yes_preds = np.column_stack([m_yes.predict(X) for m_yes, _ in models])  # (n, K)
    no_preds = np.column_stack([m_no.predict(X) for _, m_no in models])
    y = te["y_settle"].to_numpy()
    cy = te["c_yes"].to_numpy(); cn = te["c_no"].to_numpy()
    mid = te["p_yes_mid"].to_numpy()
    sides, pnls = [], []
    for i in range(len(te)):
        a, b = yes_preds[i], no_preds[i]
        yes_votes = int(np.sum((a > b) & (a > skip)))
        no_votes = int(np.sum((b > a) & (b > skip)))
        allow_yes = mid[i] >= yes_gate
        if (no_votes >= agree_min and yes_votes == 0 and np.median(b) > margin):
            side = C.CLASS_NO
        elif (allow_yes and yes_votes >= agree_min and no_votes == 0 and np.median(a) > margin):
            side = C.CLASS_YES
        else:
            side = C.CLASS_SKIP
        sides.append(side)
        pnls.append(_pnl(side, y[i], cy[i], cn[i]))
    return np.array(sides, dtype=object), np.array(pnls, dtype=float)
