#!/usr/bin/env python3
"""
pmcluster.config — single source of truth for paths, constants, the horizon grid,
the frozen model config, and the CFES gate thresholds.

Design notes
------------
* Everything here is consistent with the local 2026-06-17/2026-06-20 research:
  the Huber two-regressor edge model, COST_ADD=0.01, entry at T seconds-to-close,
  PnL/contract = y/c_yes - 1 (YES) or (1-y)/c_no - 1 (NO).
* The "unit of analysis is the trading day" rule (CFES) is enforced everywhere
  downstream; nothing below the day level is ever bootstrapped or split.
* Paths default to the cluster layout (~/project/pm) but can be overridden by the
  PM_ROOT environment variable so the same code runs locally for smoke tests.
"""
from __future__ import annotations
import os
from pathlib import Path

# ── Filesystem layout ─────────────────────────────────────────────────────────
PM_ROOT   = Path(os.environ.get("PM_ROOT", str(Path.home() / "project" / "pm")))
DATA_DIR  = PM_ROOT / "data"          # data/data_<COIN>_5m/*.csv
OUT_DIR   = PM_ROOT / "outcomes"      # outcomes/polymarket_<coin>_5m_official_outcomes.csv
FEAT_DIR  = PM_ROOT / "features"      # features/<coin>.parquet  (built by build_features.py)
RESULTS   = PM_ROOT / "cluster" / "2026-06-21-results"
REGISTRY  = RESULTS / "trial_registry.jsonl"
LOCKBOX   = RESULTS / "lockbox_split.json"

COINS = ["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"]

# ── Market / cost model (must match prior research exactly) ───────────────────
MARKET_SECONDS = 300        # 5-minute up/down markets
COST_ADD       = 0.01       # ask + 0.01 entry penalty
HORIZON_TOL    = 12.0       # seconds: snapshot must be within +-TOL of target horizon
IND_WINDOW     = 60.0       # seconds lookback for rolling indicator features
SKIP_BONUS     = 0.05       # predicted edge must exceed this to trade
YES_GATE_LO    = 0.25       # Filter B: only take YES when mid >= this

# ── Horizon grid (seconds-to-close at entry) ──────────────────────────────────
# This is part of the search space; every horizon evaluated is logged to the
# trial registry so it is paid for in the deflation math (Phase 2).
HORIZONS = [45, 60, 90, 120, 150, 180, 210, 240, 270]

# ── Features (identical set to huber_common; obi_depth_slope dropped because the
#     tau imbalance columns are absent from the Polymarket 5m schema) ───────────
FEATURES = [
    "p_yes_mid", "yes_mid_z_60", "yes_mid_vol_60", "yes_mid_z_20", "yes_mid_vol_20",
    "mid_change_60", "book_qty_log", "OBI", "OBI_vol_60", "OBI_z_60",
    "spread_yes", "tod_sin", "tod_cos",
]

# ── Frozen model config (same HP as the CFES pipeline; HP are NOT the problem) ─
# BASE_CFG (huber_common) updated with the CFES overrides:
#   max_depth=3, num_leaves=7, min_child_samples=150, lambda_l2=5.0
MODEL_CFG = {
    "max_depth": 3, "num_leaves": 7, "min_child_samples": 150,
    "lambda_l2": 5.0, "subsample": 0.90, "feature_fraction": 0.90,
    "learning_rate": 0.05, "n_rounds": 300, "huber_alpha": 1.0,
}

# ── CFES day-level evaluation parameters ──────────────────────────────────────
PURGE_DAYS     = 1     # embargo gap (days) between train and a test day
MIN_TRAIN_DAYS = 4     # need this many train days before a day becomes OOS
ENSEMBLE_SEEDS = 9     # models in the seed-agreement ensemble
AGREE_MIN      = 7     # >= this many models must vote the same side, 0 the other
EDGE_MARGIN    = 0.05  # median predicted edge across agreeing models must exceed this
N_BOOT         = 20000 # day-block bootstrap resamples
MIN_TRAIN_ROWS = 200   # minimum training contracts before a day is scored

CLASS_YES, CLASS_NO, CLASS_SKIP = "YES", "NO", "SKIP"


# ── CFES gate thresholds (the bar for a shadow-test candidate) ────────────────
class Gate:
    min_oos_days:      int   = 20
    min_positive_frac: float = 0.58
    min_daily_sharpe:  float = 0.50
    min_tstat:         float = 2.00
    dayblock_ci_lo_gt: float = 0.0
    max_cvar20:        float = -0.04
    max_topday_share:  float = 0.50


def coin_data_dir(coin: str) -> Path:
    return DATA_DIR / f"data_{coin}_5m"


def coin_outcomes_path(coin: str) -> Path:
    return OUT_DIR / f"polymarket_{coin.lower()}_5m_official_outcomes.csv"


def coin_feature_path(coin: str) -> Path:
    return FEAT_DIR / f"{coin}.parquet"
