#!/usr/bin/env python3
"""Polymarket XRP 5m intra-period live trader — no_price model (T1=180s, T2=30s).

Features (15, no price/mid features):
  OBI, OBI_vol_60, OBI_z_60, OBI_vol_20, book_qty_log,
  yes_book_imbalance_tau_{1,3,5,7,10}c,
  up_down_bid_tau_{1,5,10}c,
  obi_depth_slope, cross_bid_slope

Decision at T1=180s before close: LightGBM 3-class argmax.
  CLASS_YES (0) → buy Up token
  CLASS_NO  (1) → buy Down token
  CLASS_SKIP(2) → skip

Extreme-20 filter (applied after model check, using p_yes_mid):
  Only execute if p_yes_mid < 0.20 or p_yes_mid > 0.80 at T1.
  Source: 200-seed OOS analysis (2026-06-12), EV/avail=+0.253 for extreme_20.

Exit at T2=30s: sell the held token at bid price (FOK).

Usage:
  python polymarket_5m_intra_trader.py             # dry run
  python polymarket_5m_intra_trader.py --live      # live trading

Credentials: set POLYMARKET_PRIVATE_KEY (and optionally POLYMARKET_ADDRESS,
POLYMARKET_CHAIN_ID) in kalshi/.env or polymarket/.env.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import shutil
import signal
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd
import websockets


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"
CLOB_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
COIN_SLUG_PREFIX = "xrp"
FIVE_MINUTE_SECONDS = 5 * 60
POLYMARKET_CHAIN_ID = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

FEATURES = [
    "OBI",
    "OBI_vol_60",
    "OBI_z_60",
    "OBI_vol_20",
    "book_qty_log",
    "yes_book_imbalance_tau_1c",
    "yes_book_imbalance_tau_3c",
    "yes_book_imbalance_tau_5c",
    "yes_book_imbalance_tau_7c",
    "yes_book_imbalance_tau_10c",
    "up_down_bid_tau_1c",
    "up_down_bid_tau_5c",
    "up_down_bid_tau_10c",
    "obi_depth_slope",
    "cross_bid_slope",
]
CLASS_YES = 0
CLASS_NO = 1
CLASS_SKIP = 2

# LightGBM hyperparams (no_price model: λ=5.0, mc=32, depth=3, 120 rounds)
LGB_NUM_LEAVES = 7
LGB_MAX_DEPTH = 3
LGB_LAMBDA_L2 = 5.0
LGB_MIN_CHILD_SAMPLES = 32
LGB_NUM_BOOST_ROUNDS = 120

# Required CSV columns to qualify a contract for training
TAU_REQUIRED_COLS = (
    "up_book_imbalance_tau_1c",
    "up_book_imbalance_tau_3c",
    "up_book_imbalance_tau_5c",
    "up_book_imbalance_tau_7c",
    "up_book_imbalance_tau_10c",
)

T1_SECONDS = 180.0          # entry time before close
T2_SECONDS = 30.0           # exit time before close
EXTREME_LOW = 0.20          # extreme_20 filter: only trade if p_yes_mid < EXTREME_LOW
EXTREME_HIGH = 0.80         # or p_yes_mid > EXTREME_HIGH

MIN_TRAIN_ROWS = 30
INDICATOR_WINDOW_SECONDS = 60.0
TOLERANCE_SECONDS = 5.0
COST_ADD = 0.01

MAX_ORDER_ATTEMPTS = 10
ORDER_RETRY_DELAY = 0.25

# Paths
APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

DEFAULT_DATA_DIR = APP_DIR / "data_XRP_5m"
DEFAULT_OUTCOMES_CSV = APP_DIR / "polymarket_xrp_5m_official_outcomes.csv"
DEFAULT_MODEL_PATH = REPO_ROOT / "2026-06-12-research" / "xrp_intra_t180_model.lgb"
LOG_PATH = Path(
    os.getenv("POLYMARKET_INTRA_TRADER_LOG", str(APP_DIR / "polymarket_5m_intra_trader.log"))
)
TRADES_CSV_PATH = Path(
    os.getenv("POLYMARKET_INTRA_TRADER_TRADES_CSV", str(APP_DIR / "polymarket_5m_intra_trader_trades.csv"))
)
PORTFOLIO_CSV_PATH = Path(
    os.getenv("POLYMARKET_INTRA_TRADER_PORTFOLIO_CSV", str(APP_DIR / "polymarket_5m_intra_trader_portfolio.csv"))
)
FEATURES_CSV_PATH = Path(
    os.getenv("POLYMARKET_INTRA_TRADER_FEATURES_CSV", str(APP_DIR / "polymarket_5m_intra_trader_features.csv"))
)

TRADE_FIELDS = [
    "timestamp_utc", "event", "contract_id", "close_time", "remaining_seconds",
    "entry_seconds", "exit_seconds", "p_yes_mid", "up_ask", "down_ask",
    "up_bid_qty", "down_bid_qty",
    "selected_side", "selected_token_id", "selected_ask", "selected_ask_qty",
    "contracts", "dry_run", "order_status", "order_id", "fill_price", "filled_size",
    "exit_bid", "exit_fill_price", "pnl_ratio",
    "actual_side", "actual_label", "correct", "official_outcome_source",
    "pred_class", "pred_p_yes", "pred_p_no", "pred_p_skip",
    "successful_count", "unsuccessful_count", "skipped_count", "reason",
]
PORTFOLIO_FIELDS = [
    "timestamp_utc", "event", "remaining_seconds", "portfolio_value", "portfolio_available",
    "initial_balance", "drawdown_dollars",
]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def iso_utc(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def utc_now_ms() -> int:
    return int(time.time() * 1000)


def iso_from_ms(ms: int | float | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def fmt_money(value: Any) -> str:
    n = finite_float(value)
    return "--" if n is None else f"${n:.4f}"


def fmt_pct(value: Any) -> str:
    n = finite_float(value)
    return "--" if n is None else f"{n * 100:.1f}c"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_epoch_ms(value: Any) -> int | None:
    n = finite_float(value)
    if n is None:
        return None
    if n > 10_000_000_000:
        return int(n)
    if n > 1_000_000_000:
        return int(n * 1000)
    return None


def load_dotenv(*paths: Path) -> None:
    for p in paths:
        if not p.exists():
            continue
        pending_key: str | None = None
        pending_value: list[str] = []
        for raw_line in p.read_text().splitlines():
            if pending_key:
                pending_value.append(raw_line)
                if "END " in raw_line and "PRIVATE KEY" in raw_line:
                    os.environ.setdefault(pending_key, "\n".join(pending_value))
                    pending_key = None
                    pending_value = []
                continue
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if "BEGIN " in value and "PRIVATE KEY" in value and "END " not in value:
                pending_key = key
                pending_value = [value]
                continue
            if key:
                os.environ.setdefault(key, value.replace("\\n", "\n"))
        break


load_dotenv(REPO_ROOT / "kalshi" / ".env", APP_DIR / ".env", REPO_ROOT / ".env")


# ---------------------------------------------------------------------------
# LightGBM — model training
# ---------------------------------------------------------------------------

def _series_stats(values: list[float], last_value: float | None = None) -> dict[str, float]:
    clean = [v for v in values if math.isfinite(v)]
    if not clean:
        return {"mean": float("nan"), "z": float("nan"), "vol": float("nan"), "change": float("nan")}
    last = float(clean[-1] if last_value is None else last_value)
    mean = sum(clean) / len(clean)
    vol = math.sqrt(sum((x - mean) ** 2 for x in clean) / len(clean)) if len(clean) > 1 else 0.0
    z = (last - mean) / vol if vol > 1e-12 else 0.0
    return {"mean": mean, "z": z, "vol": vol, "change": last - clean[0]}


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def _make_profit_objective(c_yes: np.ndarray, c_no: np.ndarray):
    """3-class softmax profit objective using continuous exit-mid label."""
    n = len(c_yes)
    def fobj(y_pred: np.ndarray, dataset) -> tuple[np.ndarray, np.ndarray]:
        y = dataset.get_label().astype(float)
        fee = 0.07 * y * (1.0 - y)
        raw = np.asarray(y_pred).reshape(n, 3)
        q = _softmax(raw)
        v = np.column_stack([
            (y - c_yes - fee) / np.maximum(c_yes, 1e-6),
            ((1.0 - y) - c_no - fee) / np.maximum(c_no, 1e-6),
            np.zeros(n),
        ])
        ws = (v * q).sum(axis=1, keepdims=True)
        grad = -(q * (v - ws)).ravel() / n
        hess = np.maximum(q * (1.0 - q), 1e-6).ravel() / n
        return grad, hess
    return fobj


def _lgb_params(seed: int = 42) -> dict[str, Any]:
    return {
        "objective": "none",
        "num_class": 3,
        "num_leaves": LGB_NUM_LEAVES,
        "max_depth": LGB_MAX_DEPTH,
        "min_child_samples": LGB_MIN_CHILD_SAMPLES,
        "subsample": 0.90,
        "feature_fraction": 0.90,
        "lambda_l2": LGB_LAMBDA_L2,
        "learning_rate": 0.06,
        "num_threads": 4,
        "seed": seed,
        "verbose": -1,
    }


def _load_outcomes(outcomes_csv: Path) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    if not outcomes_csv.exists():
        return outcomes
    with outcomes_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = row.get("market_slug", "").strip()
            if slug:
                outcomes[slug] = row
    return outcomes


def _build_candidates_from_csv_intra(
    path: Path,
    outcomes: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build one intra-period training row from a historical XRP CSV contract."""
    try:
        df = pd.read_csv(path, low_memory=False)
    except Exception:
        return []
    if df.empty:
        return []

    # Require tau_3c and tau_7c columns (added ~2026-06-10)
    for col in TAU_REQUIRED_COLS:
        if col not in df.columns:
            return []

    slug_col = df["market_slug"].dropna() if "market_slug" in df.columns else pd.Series(dtype=object)
    slug_from_path = path.stem.replace("polymarket_data_XRP_5m_", "")
    slug = str(slug_col.iloc[0]).strip() if not slug_col.empty else slug_from_path

    outcome = outcomes.get(slug)
    if not outcome:
        return []
    winning = str(outcome.get("winning_outcome", "")).strip()
    if winning not in ("Up", "Down"):
        return []

    df["_rem"] = pd.to_numeric(df.get("seconds_to_close"), errors="coerce")
    df = df[df["_rem"].notna() & (df["_rem"] >= 0)].copy()
    if df.empty:
        return []

    df["_ts"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")

    # Find T1=180s row
    t1_cands = df[(df["_rem"] - T1_SECONDS).abs() <= TOLERANCE_SECONDS]
    if t1_cands.empty:
        return []
    t1r = t1_cands.loc[(t1_cands["_rem"] - T1_SECONDS).abs().idxmin()]

    # Find T2=30s row
    t2_cands = df[(df["_rem"] - T2_SECONDS).abs() <= TOLERANCE_SECONDS]
    if t2_cands.empty:
        return []
    t2r = t2_cands.loc[(t2_cands["_rem"] - T2_SECONDS).abs().idxmin()]

    # T1 prices
    ya = finite_float(t1r.get("up_best_ask"))
    yb = finite_float(t1r.get("up_best_bid"))
    na = finite_float(t1r.get("down_best_ask"))
    nb = finite_float(t1r.get("down_best_bid"))
    ubs = finite_float(t1r.get("up_best_bid_size")) or 0.0
    dbs = finite_float(t1r.get("down_best_bid_size")) or 0.0
    if any(v is None for v in (ya, yb, na, nb)):
        return []
    if not (0 < ya < 1 and 0 < na < 1 and yb <= ya and nb <= na):
        return []

    # T2 prices
    t2_ub = finite_float(t2r.get("up_best_bid"))
    t2_ua = finite_float(t2r.get("up_best_ask"))
    t2_db = finite_float(t2r.get("down_best_bid"))
    t2_da = finite_float(t2r.get("down_best_ask"))
    if any(v is None for v in (t2_ub, t2_ua, t2_db, t2_da)):
        return []
    if not (0 < t2_ua < 1 and 0 < t2_da < 1):
        return []
    y_exit = (t2_ub + t2_ua) / 2.0  # up_mid at T2 (training label)

    # Tau features at T1
    yb1  = finite_float(t1r.get("up_book_imbalance_tau_1c"))
    yb3  = finite_float(t1r.get("up_book_imbalance_tau_3c"))
    yb5  = finite_float(t1r.get("up_book_imbalance_tau_5c"))
    yb7  = finite_float(t1r.get("up_book_imbalance_tau_7c"))
    yb10 = finite_float(t1r.get("up_book_imbalance_tau_10c"))
    if any(v is None for v in (yb1, yb3, yb5, yb7, yb10)):
        return []

    cs1b  = finite_float(t1r.get("up_down_bid_imbalance_tau_1c"))
    cs5b  = finite_float(t1r.get("up_down_bid_imbalance_tau_5c"))
    cs10b = finite_float(t1r.get("up_down_bid_imbalance_tau_10c"))
    if any(v is None for v in (cs1b, cs5b, cs10b)):
        return []

    # Rolling OBI history within 60s and 20s of T1
    t1_ts = t1r.get("_ts")
    if pd.isna(t1_ts):
        return []
    h60 = df[df["_ts"].notna() & (df["_ts"] <= t1_ts) &
             ((t1_ts - df["_ts"]).dt.total_seconds() <= INDICATOR_WINDOW_SECONDS)].copy()
    h20 = df[df["_ts"].notna() & (df["_ts"] <= t1_ts) &
             ((t1_ts - df["_ts"]).dt.total_seconds() <= 20.0)].copy()
    if h60.empty:
        h60 = t1r.to_frame().T
    if h20.empty:
        h20 = t1r.to_frame().T

    def _obi_series(h: pd.DataFrame) -> list[float]:
        u = pd.to_numeric(h.get("up_best_bid_size", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
        d = pd.to_numeric(h.get("down_best_bid_size", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
        return ((u - d) / (u + d + 1e-9)).tolist()

    obi_cur = (ubs - dbs) / (ubs + dbs + 1e-9)
    obi_60 = _series_stats(_obi_series(h60), obi_cur)
    obi_20 = _series_stats(_obi_series(h20), obi_cur)

    row_data = {
        "y_exit":       y_exit,
        "c_yes":        ya + COST_ADD,
        "c_no":         na + COST_ADD,
        "exit_yes_bid": t2_ub,
        "exit_no_bid":  t2_db,
        "OBI":                          obi_cur,
        "OBI_vol_60":                   obi_60["vol"],
        "OBI_z_60":                     obi_60["z"],
        "OBI_vol_20":                   obi_20["vol"],
        "book_qty_log":                 math.log1p(ubs + dbs),
        "yes_book_imbalance_tau_1c":    yb1,
        "yes_book_imbalance_tau_3c":    yb3,
        "yes_book_imbalance_tau_5c":    yb5,
        "yes_book_imbalance_tau_7c":    yb7,
        "yes_book_imbalance_tau_10c":   yb10,
        "up_down_bid_tau_1c":           cs1b,
        "up_down_bid_tau_5c":           cs5b,
        "up_down_bid_tau_10c":          cs10b,
        "obi_depth_slope":              yb1 - yb10,
        "cross_bid_slope":              cs1b - cs10b,
    }

    if any(not math.isfinite(float(row_data.get(f, float("nan")))) for f in FEATURES):
        return []
    return [row_data]


def train_model(data_dir: Path, outcomes_csv: Path, save_path: Path | None = None) -> Any | None:
    import lightgbm as lgb

    outcomes = _load_outcomes(outcomes_csv)
    if not outcomes:
        append_log(f"MODEL: no outcomes in {outcomes_csv}")
        return None

    all_rows: list[dict[str, Any]] = []
    csv_files = sorted(data_dir.glob("*.csv"))
    for path in csv_files:
        all_rows.extend(_build_candidates_from_csv_intra(path, outcomes))

    if len(all_rows) < MIN_TRAIN_ROWS:
        append_log(f"MODEL: only {len(all_rows)} training rows (need {MIN_TRAIN_ROWS}); skipping")
        return None

    df = pd.DataFrame(all_rows)
    y = df["y_exit"].to_numpy().astype(float)
    c_yes = df["c_yes"].to_numpy().astype(float)
    c_no = df["c_no"].to_numpy().astype(float)
    X = df[FEATURES].to_numpy().astype(float)

    dtrain = lgb.Dataset(X, label=y, free_raw_data=False)
    dtrain.construct()
    fobj = _make_profit_objective(c_yes, c_no)
    model = lgb.Booster(params=_lgb_params(), train_set=dtrain)
    try:
        for _ in range(LGB_NUM_BOOST_ROUNDS):
            model.update(fobj=fobj)
    except Exception as exc:
        append_log(f"MODEL: training error {exc}")
        return None

    append_log(
        f"MODEL trained: {len(all_rows)} rows from {len(csv_files)} contracts "
        f"(tau-qualified: {len(all_rows)})"
    )
    if save_path is not None:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        model.save_model(str(save_path))
        append_log(f"MODEL saved → {save_path}")
    return model


def load_or_train_model(
    model_path: Path,
    data_dir: Path,
    outcomes_csv: Path,
    retrain: bool = False,
) -> Any | None:
    import lightgbm as lgb

    if not retrain and model_path.exists():
        try:
            model = lgb.Booster(model_file=str(model_path))
            append_log(f"MODEL loaded from {model_path}")
            return model
        except Exception as exc:
            append_log(f"MODEL load error ({exc}); retraining...")

    append_log(f"MODEL training from {data_dir} + {outcomes_csv}...")
    return train_model(data_dir, outcomes_csv, save_path=model_path)


def predict(model: Any, features: dict[str, float]) -> tuple[int, np.ndarray]:
    X = np.array([[features[f] for f in FEATURES]], dtype=float)
    raw = np.asarray(model.predict(X)).reshape(1, 3)
    probs = _softmax(raw)[0]
    return int(np.argmax(probs)), probs


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TokenBook:
    token_id: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    timestamp_ms: int | None = None
    event_count: int = 0
    fallback_best_bid: float | None = None
    fallback_best_ask: float | None = None

    def replace_from_book(self, book: dict[str, Any]) -> None:
        self.bids = _parse_levels(book.get("bids"))
        self.asks = _parse_levels(book.get("asks"))
        self.timestamp_ms = parse_epoch_ms(book.get("timestamp")) or utc_now_ms()
        self.fallback_best_bid = None
        self.fallback_best_ask = None
        self.event_count += 1

    def apply_price_change(self, change: dict[str, Any], ts_ms: int | None) -> None:
        side = str(change.get("side") or "").upper()
        price = finite_float(change.get("price"))
        size = finite_float(change.get("size"))
        if price is not None and size is not None:
            levels = self.bids if side == "BUY" else self.asks if side == "SELL" else None
            if levels is not None:
                if size <= 0:
                    levels.pop(price, None)
                else:
                    levels[price] = size
        self.fallback_best_bid = finite_float(change.get("best_bid")) or self.fallback_best_bid
        self.fallback_best_ask = finite_float(change.get("best_ask")) or self.fallback_best_ask
        self.timestamp_ms = ts_ms or utc_now_ms()
        self.event_count += 1

    def apply_best_bid_ask(self, msg: dict[str, Any]) -> None:
        self.fallback_best_bid = finite_float(msg.get("best_bid"))
        self.fallback_best_ask = finite_float(msg.get("best_ask"))
        self.timestamp_ms = parse_epoch_ms(msg.get("timestamp")) or utc_now_ms()
        self.event_count += 1

    def best_bid(self) -> tuple[float | None, float | None]:
        if self.bids:
            p = max(self.bids)
            return p, self.bids[p]
        return self.fallback_best_bid, None

    def best_ask(self) -> tuple[float | None, float | None]:
        if self.asks:
            p = min(self.asks)
            return p, self.asks[p]
        return self.fallback_best_ask, None

    def depth_within_tau(self, tau: float, *, side: str) -> float:
        levels = self.bids if side == "bid" else self.asks
        if not levels:
            return 0.0
        ref = max(levels) if side == "bid" else min(levels)
        if side == "bid":
            return sum(s for p, s in levels.items() if s > 0 and p >= ref - tau - 1e-12)
        return sum(s for p, s in levels.items() if s > 0 and p <= ref + tau + 1e-12)

    def book_imbalance(self, tau: float) -> float | None:
        bd = self.depth_within_tau(tau, side="bid")
        ad = self.depth_within_tau(tau, side="ask")
        total = bd + ad
        if total <= 0:
            return None
        return (bd - ad) / total


@dataclass
class CurrentMarket:
    slug: str
    start_ts: int
    end_ts: int
    up_token_id: str
    down_token_id: str


class CollectorState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.market: CurrentMarket | None = None
        self.books: dict[str, TokenBook] = {}


@dataclass
class QuoteSnapshot:
    timestamp: float
    up_bid_qty: float
    down_bid_qty: float


@dataclass
class Counts:
    successful: int = 0
    unsuccessful: int = 0
    skipped: int = 0


@dataclass
class TradeDecision:
    status: str = ""
    side: str = ""
    token_id: str = ""
    pred_class: int = CLASS_SKIP
    pred_p_yes: float = 0.0
    pred_p_no: float = 0.0
    pred_p_skip: float = 1.0
    selected_ask: float | None = None
    selected_ask_qty: float | None = None
    contracts: int = 0
    dry_run: bool = True
    order_id: str = ""
    fill_price: float | None = None
    filled_size: float = 0.0
    reason: str = ""
    outcome_eligible: bool = False


@dataclass
class ExitRecord:
    status: str = ""
    bid_price: float | None = None
    fill_price: float | None = None
    filled_size: float = 0.0
    pnl_ratio: float | None = None
    order_id: str = ""
    reason: str = ""


@dataclass
class ContractRuntime:
    market: CurrentMarket
    history: deque[QuoteSnapshot] = field(default_factory=lambda: deque(maxlen=300))
    decision: TradeDecision | None = None
    decision_logged: bool = False
    exit_record: ExitRecord | None = None
    exit_logged: bool = False
    outcome_logged: bool = False
    last_status_log: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_levels(value: Any) -> dict[float, float]:
    levels: dict[float, float] = {}
    if not isinstance(value, list):
        return levels
    for item in value:
        if not isinstance(item, dict):
            continue
        price = finite_float(item.get("price") or item.get("px"))
        size = finite_float(item.get("size") or item.get("qty"))
        if price is not None and size is not None and size > 0:
            levels[price] = size
    return levels


def _cross_bid_imbalance(up_book: TokenBook, down_book: TokenBook, tau: float) -> float | None:
    ud = up_book.depth_within_tau(tau, side="bid")
    dd = down_book.depth_within_tau(tau, side="bid")
    total = ud + dd
    if total <= 1e-12:
        return None
    return (ud - dd) / total


# ---------------------------------------------------------------------------
# Feature extraction from live books
# ---------------------------------------------------------------------------

def extract_features(
    runtime: ContractRuntime,
    up_book: TokenBook,
    down_book: TokenBook,
) -> tuple[dict[str, float] | None, float | None, str]:
    """Returns (features, p_yes_mid, error_reason). p_yes_mid is always returned if books valid."""
    up_bid, up_bid_qty = up_book.best_bid()
    up_ask, up_ask_qty = up_book.best_ask()
    down_bid, down_bid_qty = down_book.best_bid()
    down_ask, down_ask_qty = down_book.best_ask()

    if any(v is None for v in (up_bid, up_ask, down_bid, down_ask)):
        missing = [n for n, v in [("up_bid", up_bid), ("up_ask", up_ask),
                                   ("down_bid", down_bid), ("down_ask", down_ask)] if v is None]
        return None, None, f"missing book prices: {missing}"
    if not (0.0 < up_ask < 1.0 and 0.0 < down_ask < 1.0):  # type: ignore[operator]
        return None, None, f"asks out of range: up_ask={up_ask} down_ask={down_ask}"

    up_mid = (up_bid + up_ask) / 2.0  # type: ignore[operator]
    up_bid_q = up_bid_qty or 0.0
    down_bid_q = down_bid_qty or 0.0
    obi_current = (up_bid_q - down_bid_q) / (up_bid_q + down_bid_q + 1e-9)

    now = time.time()
    window_60 = [snap for snap in runtime.history if snap.timestamp >= now - INDICATOR_WINDOW_SECONDS]
    window_20 = [snap for snap in runtime.history if snap.timestamp >= now - 20.0]

    def _obi_list(snaps: list[QuoteSnapshot]) -> list[float]:
        return [(s.up_bid_qty - s.down_bid_qty) / (s.up_bid_qty + s.down_bid_qty + 1e-9) for s in snaps]

    obi_stats_60 = _series_stats(_obi_list(window_60), obi_current)
    obi_stats_20 = _series_stats(_obi_list(window_20), obi_current)

    # Yes-side tau book imbalance
    yes_1c  = up_book.book_imbalance(0.01)
    yes_3c  = up_book.book_imbalance(0.03)
    yes_5c  = up_book.book_imbalance(0.05)
    yes_7c  = up_book.book_imbalance(0.07)
    yes_10c = up_book.book_imbalance(0.10)

    null_yes = [k for k, v in [("1c", yes_1c), ("3c", yes_3c), ("5c", yes_5c),
                                 ("7c", yes_7c), ("10c", yes_10c)] if v is None]
    if null_yes:
        diag = f"yes_1c={yes_1c} yes_3c={yes_3c} yes_5c={yes_5c} yes_7c={yes_7c} yes_10c={yes_10c}"
        return None, up_mid, f"yes_book_imbalance_tau=None for {null_yes} || {diag}"

    # Cross-side bid imbalance
    cb_1c  = _cross_bid_imbalance(up_book, down_book, 0.01)
    cb_5c  = _cross_bid_imbalance(up_book, down_book, 0.05)
    cb_10c = _cross_bid_imbalance(up_book, down_book, 0.10)

    null_cross = [k for k, v in [("1c", cb_1c), ("5c", cb_5c), ("10c", cb_10c)] if v is None]
    if null_cross:
        return None, up_mid, f"up_down_bid_tau=None for {null_cross}"

    features: dict[str, float] = {
        "OBI":                          obi_current,
        "OBI_vol_60":                   obi_stats_60["vol"],
        "OBI_z_60":                     obi_stats_60["z"],
        "OBI_vol_20":                   obi_stats_20["vol"],
        "book_qty_log":                 math.log1p(up_bid_q + down_bid_q),
        "yes_book_imbalance_tau_1c":    yes_1c,   # type: ignore[assignment]
        "yes_book_imbalance_tau_3c":    yes_3c,   # type: ignore[assignment]
        "yes_book_imbalance_tau_5c":    yes_5c,   # type: ignore[assignment]
        "yes_book_imbalance_tau_7c":    yes_7c,   # type: ignore[assignment]
        "yes_book_imbalance_tau_10c":   yes_10c,  # type: ignore[assignment]
        "up_down_bid_tau_1c":           cb_1c,    # type: ignore[assignment]
        "up_down_bid_tau_5c":           cb_5c,    # type: ignore[assignment]
        "up_down_bid_tau_10c":          cb_10c,   # type: ignore[assignment]
        "obi_depth_slope":              yes_1c - yes_10c,   # type: ignore[operator]
        "cross_bid_slope":              cb_1c - cb_10c,     # type: ignore[operator]
    }

    bad = [f for f in FEATURES if not math.isfinite(features[f])]
    if bad:
        diag = "  ".join(f"{f}={features[f]:.4f}" for f in FEATURES)
        return None, up_mid, f"non-finite features: {bad} || {diag}"

    return features, up_mid, ""


# ---------------------------------------------------------------------------
# WebSocket loops
# ---------------------------------------------------------------------------

async def _apply_clob_message(state: CollectorState, msg: Any) -> None:
    messages = msg if isinstance(msg, list) else [msg]
    async with state.lock:
        for item in messages:
            if not isinstance(item, dict):
                continue
            event_type = item.get("event_type")
            if event_type == "book":
                asset_id = str(item.get("asset_id") or "")
                book = state.books.get(asset_id)
                if book:
                    book.replace_from_book(item)
            elif event_type == "price_change":
                ts_ms = parse_epoch_ms(item.get("timestamp")) or utc_now_ms()
                for change in (item.get("price_changes") or []):
                    if not isinstance(change, dict):
                        continue
                    asset_id = str(change.get("asset_id") or "")
                    book = state.books.get(asset_id)
                    if book:
                        book.apply_price_change(change, ts_ms)
            elif event_type == "best_bid_ask":
                asset_id = str(item.get("asset_id") or "")
                book = state.books.get(asset_id)
                if book:
                    book.apply_best_bid_ask(item)


async def clob_ws_loop(
    state: CollectorState,
    market: CurrentMarket,
    stop: asyncio.Event,
) -> None:
    sub = json.dumps({
        "assets_ids": [market.up_token_id, market.down_token_id],
        "type": "market",
        "custom_feature_enabled": True,
    })
    backoff = 1.0
    while not stop.is_set() and time.time() <= market.end_ts + 30:
        try:
            async with websockets.connect(
                CLOB_MARKET_WS_URL, ping_interval=20, ping_timeout=10, open_timeout=10
            ) as ws:
                await ws.send(sub)
                backoff = 1.0
                async for raw in ws:
                    if stop.is_set():
                        break
                    try:
                        await _apply_clob_message(state, json.loads(raw))
                    except json.JSONDecodeError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            append_log(f"CLOB WS error: {type(exc).__name__}: {exc}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2.0, 20.0)


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            r = json.loads(value)
            return r if isinstance(r, list) else []
        except json.JSONDecodeError:
            pass
    return []


def _parse_market_from_event(event: dict[str, Any]) -> CurrentMarket | None:
    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    if market.get("closed") is True or market.get("active") is False:
        return None
    outcomes = [str(i) for i in _parse_json_list(market.get("outcomes"))]
    token_ids = [str(i) for i in _parse_json_list(market.get("clobTokenIds"))]
    if len(outcomes) != len(token_ids) or len(token_ids) < 2:
        return None
    tok = {o.lower(): t for o, t in zip(outcomes, token_ids)}
    up_token = tok.get("up") or tok.get("yes") or token_ids[0]
    down_token = tok.get("down") or tok.get("no") or token_ids[1]

    start_dt = parse_dt(market.get("eventStartTime") or market.get("startDate") or event.get("startTime"))
    end_dt = parse_dt(market.get("endDate") or event.get("endDate"))
    if not start_dt or not end_dt:
        return None
    now = datetime.now(timezone.utc)
    if not (start_dt <= now <= end_dt):
        return None

    return CurrentMarket(
        slug=event.get("slug") or market.get("slug") or "",
        start_ts=int(start_dt.timestamp()),
        end_ts=int(end_dt.timestamp()),
        up_token_id=up_token,
        down_token_id=down_token,
    )


async def discover_current_market(client: httpx.AsyncClient) -> CurrentMarket:
    current_slot = int(time.time()) // FIVE_MINUTE_SECONDS * FIVE_MINUTE_SECONDS
    slots = [current_slot + off * FIVE_MINUTE_SECONDS for off in (0, -1, 1, -2, 2, -3, 3)]
    for slot in slots:
        slug = f"{COIN_SLUG_PREFIX}-updown-5m-{slot}"
        try:
            resp = await client.get(f"{GAMMA_BASE_URL}/events/slug/{slug}")
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            event = resp.json()
        except Exception:
            continue
        market = _parse_market_from_event(event)
        if market:
            return market
    raise RuntimeError("No active XRP 5m Up/Down market found on Polymarket")


async def load_initial_books(client: httpx.AsyncClient, state: CollectorState, market: CurrentMarket) -> None:
    body = [{"token_id": market.up_token_id}, {"token_id": market.down_token_id}]
    resp = await client.post(f"{CLOB_BASE_URL}/books", json=body)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        raise RuntimeError("/books response was not a list")
    by_asset: dict[str, dict[str, Any]] = {}
    for book in payload:
        if isinstance(book, dict):
            aid = str(book.get("asset_id") or book.get("token_id") or "")
            if aid:
                by_asset[aid] = book
    up_book = TokenBook(market.up_token_id)
    down_book = TokenBook(market.down_token_id)
    if market.up_token_id in by_asset:
        up_book.replace_from_book(by_asset[market.up_token_id])
    if market.down_token_id in by_asset:
        down_book.replace_from_book(by_asset[market.down_token_id])
    async with state.lock:
        state.books = {market.up_token_id: up_book, market.down_token_id: down_book}


# ---------------------------------------------------------------------------
# Polymarket balance & order placement
# ---------------------------------------------------------------------------

def _polymarket_client() -> Any:
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2
    except ImportError as exc:
        raise RuntimeError("py_clob_client_v2 not installed") from exc
    key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PK")
    if not key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY not set")
    funder = (
        os.getenv("POLYMARET_ADDRESS")
        or os.getenv("POLYMARKET_ADDRESS")
        or os.getenv("POLYMARKET_FUNDER")
    )
    kwargs: dict[str, Any] = {"host": CLOB_BASE_URL, "chain_id": POLYMARKET_CHAIN_ID, "key": key}
    if funder:
        from py_clob_client_v2 import SignatureTypeV2 as ST
        kwargs["signature_type"] = int(ST.POLY_1271)
        kwargs["funder"] = funder
    if os.getenv("CLOB_API_KEY") and os.getenv("CLOB_SECRET") and os.getenv("CLOB_PASS_PHRASE"):
        creds = ApiCreds(
            api_key=os.environ["CLOB_API_KEY"],
            api_secret=os.environ["CLOB_SECRET"],
            api_passphrase=os.environ["CLOB_PASS_PHRASE"],
        )
    else:
        auth = ClobClient(**kwargs)
        try:
            creds = auth.derive_api_key()
        except Exception:
            creds = auth.create_api_key()
    return ClobClient(**kwargs, creds=creds)


def polymarket_balance() -> tuple[float | None, float | None, str]:
    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        client = _polymarket_client()
        data = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = data.get("balance") or data.get("usdc_balance") or data.get("collateral") or 0
        allowance = data.get("allowance") or data.get("usdc_allowance") or 0
        return float(balance) / 1_000_000.0, float(allowance) / 1_000_000.0, ""
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def place_order(
    token_id: str,
    price: float,
    contracts: int,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return {
            "status": "dry_run",
            "token_id": token_id,
            "price": price,
            "size": contracts,
            "order_id": f"dry-{uuid.uuid4().hex[:12]}",
        }
    try:
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
        client = _polymarket_client()
        resp = client.create_and_post_order(
            order_args=OrderArgs(token_id=token_id, price=price, side=Side.BUY, size=float(contracts)),
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )
        if not isinstance(resp, dict):
            return {"response": str(resp)}
        order_id = resp.get("id") or resp.get("order_id") or resp.get("orderId") or ""
        if order_id:
            try:
                verified = client.get_order(str(order_id))
                resp["verified_order"] = verified
            except Exception:
                pass
        return resp
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def place_sell_order(
    token_id: str,
    price: float,
    contracts: int,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return {
            "status": "dry_run",
            "token_id": token_id,
            "price": price,
            "size": contracts,
            "order_id": f"dry-exit-{uuid.uuid4().hex[:12]}",
        }
    try:
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
        client = _polymarket_client()
        resp = client.create_and_post_order(
            order_args=OrderArgs(token_id=token_id, price=price, side=Side.SELL, size=float(contracts)),
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )
        if not isinstance(resp, dict):
            return {"response": str(resp)}
        order_id = resp.get("id") or resp.get("order_id") or resp.get("orderId") or ""
        if order_id:
            try:
                verified = client.get_order(str(order_id))
                resp["verified_order"] = verified
            except Exception:
                pass
        return resp
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _response_status(resp: dict[str, Any], contracts: int) -> tuple[str, str, float | None, float]:
    if resp.get("status") == "dry_run":
        return "dry_run", f"dry_run at {fmt_pct(resp.get('price'))}", resp.get("price"), float(contracts)
    if resp.get("error"):
        return "error", str(resp["error"]), None, 0.0
    verified = resp.get("verified_order") or resp
    status = str(verified.get("status") or resp.get("status") or "unknown").lower()
    filled = float(verified.get("size_matched") or verified.get("amount_filled") or verified.get("filledAmount") or 0.0)
    fill_price = finite_float(
        verified.get("average_price") or verified.get("avgPrice")
        or verified.get("price") or resp.get("price")
    )
    order_id = str(resp.get("id") or resp.get("order_id") or "")
    if status == "matched" and filled == 0.0:
        filled = float(contracts)
    if filled >= contracts:
        return "filled", f"filled {filled:g} @ {fmt_pct(fill_price)} id={order_id}", fill_price, filled
    if filled > 0:
        return "partial", f"partial {filled:g}/{contracts:g} id={order_id}", fill_price, filled
    return "unfilled", f"unfilled id={order_id}", None, 0.0


# ---------------------------------------------------------------------------
# Log & CSV management
# ---------------------------------------------------------------------------

def append_log(message: str, *, prefix_timestamp: bool = True) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{iso_utc()} | {message}" if prefix_timestamp else message
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")
    print(line, flush=True)


def append_trade_row(row: dict[str, Any]) -> None:
    TRADES_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = TRADES_CSV_PATH.exists()
    with TRADES_CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def append_portfolio_row(row: dict[str, Any]) -> None:
    PORTFOLIO_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = PORTFOLIO_CSV_PATH.exists()
    with PORTFOLIO_CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PORTFOLIO_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in PORTFOLIO_FIELDS})


FEATURES_FIELDS = ["timestamp_utc", "contract_id", "remaining_seconds"] + FEATURES


def append_features_row(row: dict[str, Any]) -> None:
    FEATURES_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = FEATURES_CSV_PATH.exists()
    with FEATURES_CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FEATURES_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in FEATURES_FIELDS})


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------

async def log_balance(
    event_label: str,
    remaining: float | None,
    initial_balance: float | None,
    stop_loss: float,
    args: argparse.Namespace,
) -> float | None:
    balance, available, error = await asyncio.to_thread(polymarket_balance)
    rem_text = f" T={remaining:.1f}s" if remaining is not None else ""
    if error:
        append_log(f"BALANCE{rem_text} {event_label} | Polymarket ERROR {error}")
        return None
    avail_text = "" if available is None else f" available={fmt_money(available)}"
    drawdown = None if initial_balance is None else initial_balance - balance
    draw_text = "" if drawdown is None else f" drawdown={drawdown:.4f}"
    append_log(f"BALANCE{rem_text} {event_label} | Polymarket {fmt_money(balance)}{avail_text}{draw_text}")
    append_portfolio_row({
        "timestamp_utc": iso_utc(),
        "event": event_label,
        "remaining_seconds": "" if remaining is None else f"{remaining:.3f}",
        "portfolio_value": balance,
        "portfolio_available": available,
        "initial_balance": initial_balance,
        "drawdown_dollars": drawdown,
    })
    if initial_balance is None and balance is not None:
        args.initial_balance = balance
        if stop_loss > 0:
            append_log(f"STOP_LOSS baseline set to {fmt_money(balance)}")
    if initial_balance is not None and stop_loss > 0 and balance < initial_balance - stop_loss:
        raise RuntimeError(
            f"STOP_LOSS balance {fmt_money(balance)} < initial {fmt_money(initial_balance)} - {fmt_money(stop_loss)}"
        )
    return balance


# ---------------------------------------------------------------------------
# Trading actions — entry
# ---------------------------------------------------------------------------

async def evaluate_entry(
    runtime: ContractRuntime,
    state: CollectorState,
    model: Any,
    counts: Counts,
    args: argparse.Namespace,
    remaining: float,
) -> None:
    async with state.lock:
        market = state.market or runtime.market
        up_book = state.books.get(market.up_token_id)
        down_book = state.books.get(market.down_token_id)

    close_time = iso_from_ms(runtime.market.end_ts * 1000)
    base_row = {
        "timestamp_utc": iso_utc(),
        "event": "decision",
        "contract_id": runtime.market.slug,
        "close_time": close_time,
        "remaining_seconds": f"{remaining:.3f}",
        "entry_seconds": args.entry_seconds,
        "exit_seconds": args.exit_seconds,
        "contracts": "",
        "dry_run": int(not args.live),
        "successful_count": counts.successful,
        "unsuccessful_count": counts.unsuccessful,
        "skipped_count": counts.skipped,
    }

    if up_book is None or down_book is None:
        counts.skipped += 1
        runtime.decision = TradeDecision(status="skip", contracts=0, dry_run=not args.live, reason="no book data")
        append_log(
            f"STATUS T={remaining:.1f}s {runtime.market.slug} | decision=SKIP reason=no book data | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({**base_row, "order_status": "skip", "reason": "no book data"})
        return

    up_bid_p, up_bid_q = up_book.best_bid()
    up_ask_p, up_ask_q = up_book.best_ask()
    down_bid_p, down_bid_q = down_book.best_bid()
    down_ask_p, down_ask_q = down_book.best_ask()
    up_mid = ((up_bid_p or 0.0) + (up_ask_p or 0.0)) / 2.0 if up_bid_p and up_ask_p else None

    base_row.update({
        "p_yes_mid": up_mid,
        "up_ask": up_ask_p,
        "down_ask": down_ask_p,
        "up_bid_qty": up_bid_q,
        "down_bid_qty": down_bid_q,
    })

    if model is None:
        counts.skipped += 1
        runtime.decision = TradeDecision(status="skip", contracts=0, dry_run=not args.live, reason="no model")
        append_log(
            f"STATUS T={remaining:.1f}s {runtime.market.slug} | decision=SKIP reason=no model | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({**base_row, "order_status": "skip", "reason": "no model"})
        return

    features, p_yes_mid, feat_fail_reason = extract_features(runtime, up_book, down_book)
    if features is None:
        counts.skipped += 1
        runtime.decision = TradeDecision(status="skip", contracts=0, dry_run=not args.live, reason="features_failed")
        append_log(
            f"FEATURES_FAILED T={remaining:.1f}s {runtime.market.slug} | {feat_fail_reason} | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({**base_row, "order_status": "skip", "reason": f"features_failed: {feat_fail_reason}"})
        return

    # extreme_20 filter: only trade at extreme price regions
    if p_yes_mid is not None and EXTREME_LOW <= p_yes_mid <= EXTREME_HIGH:
        counts.skipped += 1
        reason = f"extreme_20_filter: p_yes_mid={fmt_pct(p_yes_mid)} in [{EXTREME_LOW:.2f},{EXTREME_HIGH:.2f}]"
        runtime.decision = TradeDecision(status="skip", contracts=0, dry_run=not args.live, reason=reason)
        append_log(
            f"ORDER SKIP {runtime.market.slug} {reason} | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({**base_row, "order_status": "skip", "reason": reason})
        return

    # Log features before inference
    feat_parts = "  ".join(f"{f}={features[f]:.4f}" for f in FEATURES)
    pm_str = f"{p_yes_mid:.4f}" if p_yes_mid is not None else "--"
    append_log(f"FEATURES T={remaining:.1f}s {runtime.market.slug} p_yes_mid={pm_str} | {feat_parts}", prefix_timestamp=False)
    append_features_row({
        "timestamp_utc": iso_utc(),
        "contract_id": runtime.market.slug,
        "remaining_seconds": f"{remaining:.3f}",
        **{f: features[f] for f in FEATURES},
    })

    pred_class, probs = predict(model, features)

    if pred_class == CLASS_SKIP:
        counts.skipped += 1
        runtime.decision = TradeDecision(
            status="skip", pred_class=CLASS_SKIP, pred_p_yes=float(probs[0]),
            pred_p_no=float(probs[1]), pred_p_skip=float(probs[2]),
            contracts=0, dry_run=not args.live, reason="model_skip",
        )
        append_log(
            f"STATUS T={remaining:.1f}s {runtime.market.slug} | p_yes_mid={pm_str} "
            f"pred_yes={probs[0]:.3f} pred_no={probs[1]:.3f} pred_skip={probs[2]:.3f} decision=SKIP | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({
            **base_row,
            "pred_class": pred_class, "pred_p_yes": float(probs[0]),
            "pred_p_no": float(probs[1]), "pred_p_skip": float(probs[2]),
            "order_status": "skip", "reason": "model_skip",
        })
        return

    # YES → buy Up token; NO → buy Down token
    if pred_class == CLASS_YES:
        side = "YES"
        token_id = runtime.market.up_token_id
        ask_price = up_ask_p
        ask_qty = up_ask_q
    else:
        side = "NO"
        token_id = runtime.market.down_token_id
        ask_price = down_ask_p
        ask_qty = down_ask_q

    if ask_price is None or not (0.0 < ask_price < 1.0):
        counts.skipped += 1
        reason = f"{side}_ask missing or out of range: {ask_price}"
        runtime.decision = TradeDecision(status="skip", pred_class=pred_class, contracts=0,
                                         dry_run=not args.live, reason=reason)
        append_log(
            f"ORDER SKIP {runtime.market.slug} {side} {reason} | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({
            **base_row, "selected_side": side, "pred_class": pred_class,
            "pred_p_yes": float(probs[0]), "pred_p_no": float(probs[1]), "pred_p_skip": float(probs[2]),
            "order_status": "skip", "reason": reason,
        })
        return

    n_contracts = max(1, round(args.contract_value / ask_price))
    base_row["contracts"] = n_contracts

    append_log(
        f"STATUS T={remaining:.1f}s {runtime.market.slug} | p_yes_mid={pm_str} "
        f"pred={side} pred_yes={probs[0]:.3f} pred_no={probs[1]:.3f} pred_skip={probs[2]:.3f} "
        f"ask={fmt_pct(ask_price)} n={n_contracts} val=${args.contract_value:.2f}",
        prefix_timestamp=False,
    )

    order_status = "error"
    order_reason = "no attempts made"
    fill_price: float | None = None
    filled_size: float = 0.0
    order_id = ""
    resp: dict[str, Any] = {}
    for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
        async with state.lock:
            cur_book = state.books.get(token_id)
        if cur_book is not None:
            fresh_ask, _ = cur_book.best_ask()
            if fresh_ask is not None and 0.0 < fresh_ask < 1.0:
                ask_price = fresh_ask
                n_contracts = max(1, round(args.contract_value / ask_price))
        price_rounded = round(round(ask_price * 100) / 100, 2)
        if attempt > 1:
            append_log(
                f"ORDER RETRY {attempt}/{MAX_ORDER_ATTEMPTS} {runtime.market.slug} {side} "
                f"ask={fmt_pct(ask_price)} n={n_contracts}",
                prefix_timestamp=False,
            )
        resp = await asyncio.to_thread(place_order, token_id, price_rounded, n_contracts, dry_run=not args.live)
        order_status, order_reason, fill_price, filled_size = _response_status(resp, n_contracts)
        if order_status in ("filled", "dry_run", "partial"):
            break
        if attempt < MAX_ORDER_ATTEMPTS:
            await asyncio.sleep(ORDER_RETRY_DELAY)

    order_id = str(resp.get("id") or resp.get("order_id") or "")
    outcome_eligible = order_status in ("dry_run", "filled") and filled_size >= n_contracts

    runtime.decision = TradeDecision(
        status=order_status, side=side, token_id=token_id,
        pred_class=pred_class, pred_p_yes=float(probs[0]),
        pred_p_no=float(probs[1]), pred_p_skip=float(probs[2]),
        selected_ask=ask_price, selected_ask_qty=ask_qty,
        contracts=n_contracts, dry_run=not args.live,
        order_id=order_id, fill_price=fill_price, filled_size=filled_size,
        reason=order_reason, outcome_eligible=outcome_eligible,
    )

    append_log(
        f"ORDER {order_status.upper()} {runtime.market.slug} {side} | {order_reason} | "
        f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
        prefix_timestamp=False,
    )
    append_trade_row({
        **base_row,
        "selected_side": side, "selected_token_id": token_id,
        "selected_ask": ask_price, "selected_ask_qty": ask_qty,
        "order_status": order_status, "order_id": order_id,
        "fill_price": fill_price, "filled_size": filled_size,
        "pred_class": pred_class,
        "pred_p_yes": float(probs[0]), "pred_p_no": float(probs[1]), "pred_p_skip": float(probs[2]),
        "reason": order_reason,
    })


# ---------------------------------------------------------------------------
# Trading actions — exit at T2
# ---------------------------------------------------------------------------

async def evaluate_exit(
    runtime: ContractRuntime,
    state: CollectorState,
    counts: Counts,
    args: argparse.Namespace,
    remaining: float,
) -> None:
    decision = runtime.decision
    close_time = iso_from_ms(runtime.market.end_ts * 1000)

    def _record_outcome(
        exit_status: str, exit_reason: str,
        bid_price: float | None, fill_price: float | None,
        filled_size: float, pnl_ratio: float | None,
        correct: Any, exit_order_id: str,
    ) -> None:
        append_trade_row({
            "timestamp_utc": iso_utc(),
            "event": "outcome",
            "contract_id": runtime.market.slug,
            "close_time": close_time,
            "remaining_seconds": f"{remaining:.3f}",
            "entry_seconds": args.entry_seconds,
            "exit_seconds": args.exit_seconds,
            "selected_side": decision.side if decision else "",
            "selected_token_id": decision.token_id if decision else "",
            "selected_ask": decision.selected_ask if decision else "",
            "selected_ask_qty": decision.selected_ask_qty if decision else "",
            "contracts": decision.contracts if decision else "",
            "dry_run": int(decision.dry_run) if decision else "",
            "order_status": exit_status,
            "order_id": exit_order_id,
            "fill_price": decision.fill_price if decision else "",
            "filled_size": decision.filled_size if decision else "",
            "exit_bid": bid_price,
            "exit_fill_price": fill_price,
            "pnl_ratio": pnl_ratio,
            "correct": correct,
            "pred_class": decision.pred_class if decision else "",
            "pred_p_yes": decision.pred_p_yes if decision else "",
            "pred_p_no": decision.pred_p_no if decision else "",
            "pred_p_skip": decision.pred_p_skip if decision else "",
            "successful_count": counts.successful,
            "unsuccessful_count": counts.unsuccessful,
            "skipped_count": counts.skipped,
            "reason": exit_reason,
        })

    if decision is None or not decision.outcome_eligible:
        runtime.exit_record = ExitRecord(status="no_position", reason="no eligible entry")
        runtime.outcome_logged = True
        _record_outcome("no_position", "no eligible entry", None, None, 0.0, None, "", "")
        return

    token_id = decision.token_id
    side = decision.side
    n_contracts = decision.contracts

    async with state.lock:
        book = state.books.get(token_id)

    bid_price: float | None = None
    if book:
        bid_price, _ = book.best_bid()

    if bid_price is None or not (0.0 < bid_price < 1.0):
        reason = f"no valid bid at T2: bid_price={bid_price}"
        runtime.exit_record = ExitRecord(status="error", reason=reason)
        runtime.outcome_logged = True
        append_log(
            f"EXIT FAIL T={remaining:.1f}s {runtime.market.slug} {side} | {reason} | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        _record_outcome("exit_error", reason, bid_price, None, 0.0, None, "", "")
        return

    entry_cost = (decision.selected_ask or 0.0) + COST_ADD

    exit_status = "error"
    exit_reason = "no attempts"
    fill_price: float | None = None
    filled_size: float = 0.0
    exit_order_id = ""
    resp: dict[str, Any] = {}
    for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
        async with state.lock:
            cur_book = state.books.get(token_id)
        if cur_book is not None:
            fresh_bid, _ = cur_book.best_bid()
            if fresh_bid is not None and 0.0 < fresh_bid < 1.0:
                bid_price = fresh_bid
        price_rounded = round(round(bid_price * 100) / 100, 2)
        if attempt > 1:
            append_log(
                f"EXIT RETRY {attempt}/{MAX_ORDER_ATTEMPTS} {runtime.market.slug} {side} "
                f"bid={fmt_pct(bid_price)} n={n_contracts}",
                prefix_timestamp=False,
            )
        resp = await asyncio.to_thread(
            place_sell_order, token_id, price_rounded, n_contracts, dry_run=not args.live
        )
        exit_status, exit_reason, fill_price, filled_size = _response_status(resp, n_contracts)
        if exit_status in ("filled", "dry_run", "partial"):
            break
        if attempt < MAX_ORDER_ATTEMPTS:
            await asyncio.sleep(ORDER_RETRY_DELAY)

    exit_order_id = str(resp.get("id") or resp.get("order_id") or "")

    exit_price_for_pnl = fill_price or bid_price
    pnl_ratio: float | None = None
    if exit_price_for_pnl is not None and entry_cost > 1e-9:
        fee = 0.07 * exit_price_for_pnl * (1.0 - exit_price_for_pnl)
        pnl_ratio = (exit_price_for_pnl - entry_cost - fee) / entry_cost

    correct: Any = ""
    if pnl_ratio is not None:
        if pnl_ratio > 0:
            correct = 1
            counts.successful += 1
        else:
            correct = 0
            counts.unsuccessful += 1

    runtime.exit_record = ExitRecord(
        status=exit_status, bid_price=bid_price, fill_price=fill_price,
        filled_size=filled_size, pnl_ratio=pnl_ratio,
        order_id=exit_order_id, reason=exit_reason,
    )
    runtime.outcome_logged = True

    pnl_str = f"{pnl_ratio:+.4f}" if pnl_ratio is not None else "--"
    append_log(
        f"EXIT {exit_status.upper()} T={remaining:.1f}s {runtime.market.slug} {side} | "
        f"bid={fmt_pct(bid_price)} exit={fmt_pct(exit_price_for_pnl)} pnl={pnl_str} correct={correct} | "
        f"{exit_reason} | counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
        prefix_timestamp=False,
    )
    _record_outcome(exit_status, exit_reason, bid_price, fill_price, filled_size, pnl_ratio, correct, exit_order_id)


# ---------------------------------------------------------------------------
# Session rotation
# ---------------------------------------------------------------------------

def _rotate_session_files() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    for src in (LOG_PATH, TRADES_CSV_PATH, PORTFOLIO_CSV_PATH):
        if src.exists() and src.stat().st_size > 0:
            dst = src.with_stem(f"{src.stem}_{stamp}")
            shutil.copy2(src, dst)
        if src.exists():
            src.unlink()


# ---------------------------------------------------------------------------
# Main trading loop
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> None:
    _rotate_session_files()
    append_log(
        f"START polymarket_5m_intra_trader live={args.live} contract_value=${args.contract_value:.2f} "
        f"entry_seconds={args.entry_seconds} exit_seconds={args.exit_seconds} "
        f"tolerance={args.entry_tolerance}s extreme_20_filter "
        f"stop_loss={fmt_money(args.stop_loss)} model={args.model_path}"
    )

    model = await asyncio.to_thread(
        load_or_train_model, args.model_path, args.data_dir, args.outcomes_csv, args.retrain
    )
    if model is None:
        append_log("MODEL: proceeding without model (all entries will SKIP)")

    counts = Counts()
    completed_seen: set[str] = set()
    pending_exits: dict[str, ContractRuntime] = {}
    runtime: ContractRuntime | None = None
    args.initial_balance = None
    stop = asyncio.Event()

    def handle_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_stop)
        except NotImplementedError:
            pass

    state = CollectorState()
    clob_task: asyncio.Task[None] | None = None

    try:
        await log_balance("START", None, None, args.stop_loss, args)
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            last_market_slug = ""

            while not stop.is_set():
                # Process pending exits from previous contracts
                for slug in list(pending_exits):
                    rt = pending_exits[slug]
                    remaining_rt = rt.market.end_ts - time.time()
                    exit_lower = max(0.0, args.exit_seconds - args.entry_tolerance)
                    if remaining_rt <= args.exit_seconds and not rt.exit_logged:
                        rt.exit_logged = True
                        await evaluate_exit(rt, state, counts, args, remaining_rt)
                        await log_balance("EXIT", None, args.initial_balance, args.stop_loss, args)
                    if rt.outcome_logged or remaining_rt < -60:
                        completed_seen.add(slug)
                        del pending_exits[slug]

                # Discover current market
                try:
                    market = await discover_current_market(client)
                except Exception as exc:
                    append_log(f"MARKET DISCOVERY error: {exc}")
                    await asyncio.sleep(args.poll_interval)
                    continue

                if market.slug in completed_seen:
                    await asyncio.sleep(args.poll_interval)
                    continue

                # Contract transition
                if market.slug != last_market_slug:
                    if runtime is not None and not runtime.outcome_logged:
                        pending_exits[runtime.market.slug] = runtime

                    last_market_slug = market.slug
                    async with state.lock:
                        state.market = market
                        state.books = {}

                    await load_initial_books(client, state, market)
                    runtime = ContractRuntime(market=market)

                    if clob_task and not clob_task.done():
                        clob_task.cancel()
                        await asyncio.gather(clob_task, return_exceptions=True)
                    clob_task = asyncio.create_task(clob_ws_loop(state, market, stop))

                    append_log("", prefix_timestamp=False)
                    append_log(
                        f"CONTRACT {market.slug} | close {iso_from_ms(market.end_ts * 1000)}",
                        prefix_timestamp=False,
                    )

                # Snapshot current books for rolling OBI history
                async with state.lock:
                    up_book = state.books.get(market.up_token_id)
                    down_book = state.books.get(market.down_token_id)

                if up_book and down_book:
                    ub, ubq = up_book.best_bid()
                    db, dbq = down_book.best_bid()
                    if ub is not None and db is not None:
                        runtime.history.append(QuoteSnapshot(
                            timestamp=time.time(),
                            up_bid_qty=ubq or 0.0,
                            down_bid_qty=dbq or 0.0,
                        ))

                remaining = market.end_ts - time.time()
                now_mono = time.monotonic()

                # Status logging
                if (args.log_interval <= 0 or runtime.last_status_log <= 0 or
                        now_mono - runtime.last_status_log >= args.log_interval):
                    async with state.lock:
                        up_b = state.books.get(market.up_token_id)
                        ub_val = ua_val = None
                        if up_b:
                            ub_val, _ = up_b.best_bid()
                            ua_val, _ = up_b.best_ask()
                    up_mid_log = (ub_val + ua_val) / 2.0 if ub_val and ua_val else None
                    um_str = f"{up_mid_log:.4f}" if up_mid_log is not None else "--"
                    trade_str = runtime.decision.status if runtime.decision else "--"
                    exit_str = runtime.exit_record.status if runtime.exit_record else "--"
                    append_log(
                        f"STATUS T={remaining:.1f}s | p_yes_mid={um_str} entry={trade_str} exit={exit_str}",
                        prefix_timestamp=False,
                    )
                    runtime.last_status_log = now_mono

                # Entry window: T1 = entry_seconds
                entry_lower = max(0.0, args.entry_seconds - args.entry_tolerance)
                entry_upper = args.entry_seconds
                in_entry_window = entry_lower <= remaining <= entry_upper
                if in_entry_window and not runtime.decision_logged and remaining >= 0:
                    runtime.decision_logged = True
                    await log_balance(f"T{args.entry_seconds:.0f}s", remaining,
                                      args.initial_balance, args.stop_loss, args)
                    await evaluate_entry(runtime, state, model, counts, args, remaining)
                elif remaining < entry_lower and not runtime.decision_logged and remaining >= 0:
                    runtime.decision_logged = True
                    reason = f"missed entry window ({entry_lower:.0f}<=T<={entry_upper:.0f}s); T={remaining:.1f}s"
                    counts.skipped += 1
                    runtime.decision = TradeDecision(
                        status="skip", contracts=0, dry_run=not args.live, reason=reason
                    )
                    append_log(
                        f"ENTRY MISS {runtime.market.slug} | {reason} | "
                        f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
                        prefix_timestamp=False,
                    )

                # Exit window: T2 = exit_seconds
                exit_upper = args.exit_seconds
                exit_lower = max(0.0, args.exit_seconds - args.entry_tolerance)
                in_exit_window = exit_lower <= remaining <= exit_upper
                if in_exit_window and not runtime.exit_logged and remaining >= 0:
                    runtime.exit_logged = True
                    await evaluate_exit(runtime, state, counts, args, remaining)
                    await log_balance("EXIT", remaining, args.initial_balance, args.stop_loss, args)

                await asyncio.sleep(args.poll_interval)

    except RuntimeError as exc:
        if "STOP_LOSS" in str(exc):
            append_log(str(exc))
        else:
            import traceback
            append_log(f"FATAL {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    except Exception as exc:
        import traceback
        append_log(f"FATAL {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    finally:
        stop.set()
        if clob_task:
            clob_task.cancel()
            await asyncio.gather(clob_task, return_exceptions=True)
        append_log("STOP polymarket_5m_intra_trader")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket XRP 5m intra-period trader (no_price model, extreme_20 filter, T1=180s entry, T2=30s exit)."
    )
    parser.add_argument("--live", action="store_true", help="Submit real orders. Omit for dry-run.")
    parser.add_argument("--contract-value", type=float, default=2.0,
                        help="Dollar value to spend per trade. Contracts = round(value / ask_price). Default: 2.")
    parser.add_argument("--entry-seconds", type=float, default=T1_SECONDS,
                        help=f"Entry time before close (seconds). Default: {T1_SECONDS}.")
    parser.add_argument("--exit-seconds", type=float, default=T2_SECONDS,
                        help=f"Exit time before close (seconds). Default: {T2_SECONDS}.")
    parser.add_argument("--entry-tolerance", type=float, default=5.0,
                        help="Window tolerance for entry/exit (seconds). Default: 5.")
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH,
                        help="Saved LightGBM model file.")
    parser.add_argument("--retrain", action="store_true",
                        help="Force retrain even if model file exists.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="Historical XRP CSV directory (used for training).")
    parser.add_argument("--outcomes-csv", type=Path, default=DEFAULT_OUTCOMES_CSV,
                        help="Official outcomes CSV (used for training).")
    parser.add_argument("--poll-interval", type=float, default=0.5,
                        help="Poll interval (seconds). Default: 0.5.")
    parser.add_argument("--log-interval", type=float, default=30.0,
                        help="Seconds between status log lines. Default: 30.")
    parser.add_argument("--stop-loss", type=float, default=5.0,
                        help="Stop if balance drops this many USD. Default: 5.")
    args = parser.parse_args()
    args.contract_value = max(1.0, args.contract_value)
    args.entry_tolerance = max(0.0, args.entry_tolerance)
    args.poll_interval = max(0.1, args.poll_interval)
    return args


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
