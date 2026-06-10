#!/usr/bin/env python3
"""Polymarket BTC 5m Up/Down live trader — btc_d3 T=30 LightGBM model (p=0.0055).

Features (15):
  p_yes_mid, yes_mid_z_60, yes_mid_vol_60, mid_change_from_open, book_qty_log,
  OBI, OBI_vol_60, OBI_z_60,
  yes_book_imbalance_tau_{1c,5c,10c},
  dir_bid_imbalance_tau_{1c,5c,10c},
  obi_depth_slope

Decision at T=30s before close (configurable): LightGBM 3-class argmax.
  CLASS_YES (0) → buy Up token
  CLASS_NO  (1) → buy Down token
  CLASS_SKIP(2) → skip

Usage:
  python polymarket_5m_trader.py                   # dry run
  python polymarket_5m_trader.py --live            # live trading

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
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
SPOT_SYMBOL = "btc/usd"
SPOT_TOPIC = "crypto_prices_chainlink"
SPOT_TOPIC_ALIASES: dict[str, set[str]] = {
    "crypto_prices": {"crypto_prices_chainlink"},
    "crypto_prices_chainlink": {"crypto_prices"},
}
COIN_SLUG_PREFIX = "btc"
FIVE_MINUTE_SECONDS = 5 * 60
POLYMARKET_CHAIN_ID = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

FEATURES = [
    "p_yes_mid",
    "yes_mid_z_60",
    "yes_mid_vol_60",
    "mid_change_from_open",
    "book_qty_log",
    "OBI",
    "OBI_vol_60",
    "OBI_z_60",
    "yes_book_imbalance_tau_1c",
    "yes_book_imbalance_tau_5c",
    "yes_book_imbalance_tau_10c",
    "dir_bid_imbalance_tau_1c",
    "dir_bid_imbalance_tau_5c",
    "dir_bid_imbalance_tau_10c",
    "obi_depth_slope",
]
CLASS_YES = 0
CLASS_NO = 1
CLASS_SKIP = 2

# LightGBM hyperparams (same as btc_d3 research)
LGB_NUM_LEAVES = 7
LGB_MAX_DEPTH = 3
LGB_LAMBDA_L2 = 1.0
LGB_NUM_BOOST_ROUNDS = 80
MIN_TRAIN_ROWS = 30
INDICATOR_WINDOW_SECONDS = 60.0
TOLERANCE_SECONDS = 5.0       # entry window: [T-5, T]
OUTCOME_POLL_INTERVAL = 15.0
OUTCOME_WAIT_LOG_INTERVAL = 30.0
OUTCOME_DELAY_SECONDS = -120.0  # check outcome 2 min after close
COST_ADD = 0.01               # spread penalty per leg

# Paths
APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

DEFAULT_DATA_DIR = APP_DIR / "data_BTC_5m"
DEFAULT_OUTCOMES_CSV = APP_DIR / "polymarket_btc_5m_official_outcomes.csv"
DEFAULT_MODEL_PATH = APP_DIR / "btc_5m_lgb_model.txt"
LOG_PATH = Path(
    os.getenv("POLYMARKET_TRADER_LOG", str(APP_DIR / "polymarket_5m_trader.log"))
)
TRADES_CSV_PATH = Path(
    os.getenv("POLYMARKET_TRADER_TRADES_CSV", str(APP_DIR / "polymarket_5m_trader_trades.csv"))
)
PORTFOLIO_CSV_PATH = Path(
    os.getenv("POLYMARKET_TRADER_PORTFOLIO_CSV", str(APP_DIR / "polymarket_5m_trader_portfolio.csv"))
)

TRADE_FIELDS = [
    "timestamp_utc", "event", "contract_id", "close_time", "remaining_seconds",
    "entry_seconds", "p_yes_mid", "up_ask", "down_ask", "up_bid_qty", "down_bid_qty",
    "selected_side", "selected_token_id", "selected_ask", "selected_ask_qty",
    "contracts", "dry_run", "order_status", "order_id", "fill_price", "filled_size",
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
        break  # stop after first file found


# Load credentials
load_dotenv(REPO_ROOT / "kalshi" / ".env", APP_DIR / ".env", REPO_ROOT / ".env")


# ---------------------------------------------------------------------------
# LightGBM model — training
# ---------------------------------------------------------------------------

def _series_stats(values: list[float], last_value: float | None = None) -> dict[str, float]:
    clean = [v for v in values if math.isfinite(v)]
    if not clean:
        return {"mean": float("nan"), "z": float("nan"), "vol": float("nan"), "change": float("nan")}
    last = float(clean[-1] if last_value is None else last_value)
    mean = sum(clean) / len(clean)
    if len(clean) > 1:
        vol = math.sqrt(sum((x - mean) ** 2 for x in clean) / len(clean))
    else:
        vol = 0.0
    z = (last - mean) / vol if vol > 1e-12 else 0.0
    change = last - clean[0]
    return {"mean": mean, "z": z, "vol": vol, "change": change}


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def _make_profit_objective(c_yes: np.ndarray, c_no: np.ndarray):
    def fobj(y_pred: np.ndarray, dataset) -> tuple[np.ndarray, np.ndarray]:
        n = len(dataset.get_label())
        raw = y_pred.reshape(n, 3)
        q = _softmax(raw)
        y = dataset.get_label().astype(float)
        q_yes, q_no, q_skip = q[:, 0], q[:, 1], q[:, 2]
        profit_yes = y - c_yes
        profit_no = (1 - y) - c_no
        p_yes_term = profit_yes * q_yes * (1 - q_yes) - profit_no * q_no * q_yes
        p_no_term = profit_no * q_no * (1 - q_no) - profit_yes * q_yes * q_no
        p_skip_term = -profit_yes * q_yes * q_skip - profit_no * q_no * q_skip
        grad = -np.column_stack([p_yes_term, p_no_term, p_skip_term]).ravel() / n
        raw2 = raw - raw.mean(axis=1, keepdims=True)
        q2 = _softmax(raw2)
        hess = np.column_stack([q2[:, k] * (1 - q2[:, k]) for k in range(3)]).ravel() / n + 1e-6
        return grad, hess
    return fobj


def _lgb_params(seed: int = 42) -> dict[str, Any]:
    return {
        "objective": "none",
        "num_class": 3,
        "num_leaves": LGB_NUM_LEAVES,
        "max_depth": LGB_MAX_DEPTH,
        "min_child_samples": 16,
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


def _effective_cost(ask: float) -> float:
    return ask + COST_ADD


def _build_candidates_from_csv(
    path: Path,
    outcomes: dict[str, dict[str, Any]],
    horizon: int = 30,
    tolerance: float = TOLERANCE_SECONDS,
) -> list[dict[str, Any]]:
    slug_from_path = path.stem
    for prefix in ("polymarket_data_BTC_5m_",):
        slug_from_path = slug_from_path.replace(prefix, "")
    try:
        df = pd.read_csv(path)
    except Exception:
        return []
    if df.empty:
        return []
    slug_col = df["market_slug"].dropna() if "market_slug" in df.columns else pd.Series(dtype=object)
    slug = str(slug_col.iloc[0]).strip() if not slug_col.empty else slug_from_path
    outcome = outcomes.get(slug)
    if not outcome:
        return []
    winning = str(outcome.get("winning_outcome", "")).strip()
    if winning not in ("Up", "Down"):
        return []
    actual_label = 1 if winning == "Up" else 0
    close_time_str = str(outcome.get("event_end_utc") or "").strip()
    try:
        close_ts = pd.to_datetime(close_time_str, utc=True)
    except Exception:
        return []

    df["_ts"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
    if "seconds_to_close" in df.columns:
        df["_rem"] = pd.to_numeric(df["seconds_to_close"], errors="coerce")
    else:
        df["_rem"] = (close_ts - df["_ts"]).dt.total_seconds()
    df = df[df["_rem"].notna() & (df["_rem"] >= 0)].copy()
    if df.empty:
        return []

    candidates = df[(df["_rem"] - horizon).abs() <= tolerance]
    if candidates.empty:
        return []
    idx = (candidates["_rem"] - horizon).abs().idxmin()
    quote = candidates.loc[idx]

    up_bid = finite_float(quote.get("up_best_bid"))
    up_ask = finite_float(quote.get("up_best_ask"))
    down_bid = finite_float(quote.get("down_best_bid"))
    down_ask = finite_float(quote.get("down_best_ask"))
    if any(v is None for v in (up_bid, up_ask, down_bid, down_ask)):
        return []
    if not (0.0 < up_ask < 1.0 and 0.0 < down_ask < 1.0):
        return []
    up_mid = (up_bid + up_ask) / 2.0

    row_ts = quote["_ts"]
    history = df[df["_ts"].notna() & (df["_ts"] <= row_ts) & (
        (row_ts - df["_ts"]).dt.total_seconds() <= INDICATOR_WINDOW_SECONDS
    )].copy()
    if history.empty:
        history = quote.to_frame().T

    # open mid from first valid row
    up_mid_open: float | None = None
    for _, r in df.sort_values("_ts").iterrows():
        ub = finite_float(r.get("up_best_bid"))
        ua = finite_float(r.get("up_best_ask"))
        if ub is not None and ua is not None and 0.0 < ua < 1.0:
            up_mid_open = (ub + ua) / 2.0
            break

    up_mid_hist = pd.to_numeric(history.get("up_mid", pd.Series(dtype=float)), errors="coerce").dropna().tolist()
    up_bid_qty_hist = pd.to_numeric(history.get("up_best_bid_size", pd.Series(dtype=float)), errors="coerce").fillna(0.0).tolist()
    down_bid_qty_hist = pd.to_numeric(history.get("down_best_bid_size", pd.Series(dtype=float)), errors="coerce").fillna(0.0).tolist()
    obi_hist = [
        (u - d) / (u + d + 1e-9)
        for u, d in zip(up_bid_qty_hist, down_bid_qty_hist)
    ]

    up_bid_qty = finite_float(quote.get("up_best_bid_size")) or 0.0
    down_bid_qty = finite_float(quote.get("down_best_bid_size")) or 0.0
    obi_current = (up_bid_qty - down_bid_qty) / (up_bid_qty + down_bid_qty + 1e-9)

    yes_mid_stats = _series_stats(up_mid_hist, up_mid)
    obi_stats = _series_stats(obi_hist, obi_current)

    yes_book_1c = finite_float(quote.get("up_book_imbalance_tau_1c"))
    yes_book_5c = finite_float(quote.get("up_book_imbalance_tau_5c"))
    yes_book_10c = finite_float(quote.get("up_book_imbalance_tau_10c"))
    dir_bid_1c = finite_float(quote.get("up_down_bid_imbalance_tau_1c"))
    dir_bid_5c = finite_float(quote.get("up_down_bid_imbalance_tau_5c"))
    dir_bid_10c = finite_float(quote.get("up_down_bid_imbalance_tau_10c"))

    for v in (yes_book_1c, yes_book_5c, yes_book_10c, dir_bid_1c, dir_bid_5c, dir_bid_10c):
        if v is None:
            return []

    row_data = {
        "actual_label": actual_label,
        "yes_cost": up_ask,
        "no_cost": down_ask,
        "yes_effective_cost": _effective_cost(up_ask),
        "no_effective_cost": _effective_cost(down_ask),
        "p_yes_mid": up_mid,
        "yes_mid_z_60": yes_mid_stats["z"],
        "yes_mid_vol_60": yes_mid_stats["vol"],
        "mid_change_from_open": (up_mid - up_mid_open) if up_mid_open is not None else float("nan"),
        "book_qty_log": math.log1p(up_bid_qty + down_bid_qty),
        "OBI": obi_current,
        "OBI_vol_60": obi_stats["vol"],
        "OBI_z_60": obi_stats["z"],
        "yes_book_imbalance_tau_1c": yes_book_1c,
        "yes_book_imbalance_tau_5c": yes_book_5c,
        "yes_book_imbalance_tau_10c": yes_book_10c,
        "dir_bid_imbalance_tau_1c": dir_bid_1c,
        "dir_bid_imbalance_tau_5c": dir_bid_5c,
        "dir_bid_imbalance_tau_10c": dir_bid_10c,
        "obi_depth_slope": yes_book_1c - yes_book_10c,  # type: ignore[operator]
    }
    if any(not math.isfinite(float(row_data.get(f, float("nan")))) for f in FEATURES):
        return []
    return [row_data]


def train_model(data_dir: Path, outcomes_csv: Path, save_path: Path | None = None) -> Any | None:
    """Train LightGBM on all historical BTC 5m data. Returns booster or None.

    If save_path is given, saves the model as a LightGBM text file after training.
    """
    import lightgbm as lgb

    outcomes = _load_outcomes(outcomes_csv)
    if not outcomes:
        append_log(f"MODEL: no outcomes in {outcomes_csv}")
        return None

    all_rows: list[dict[str, Any]] = []
    csv_files = sorted(data_dir.glob("*.csv"))
    for path in csv_files:
        rows = _build_candidates_from_csv(path, outcomes)
        all_rows.extend(rows)

    if len(all_rows) < MIN_TRAIN_ROWS:
        append_log(f"MODEL: only {len(all_rows)} training rows (need {MIN_TRAIN_ROWS}); skipping model")
        return None

    df = pd.DataFrame(all_rows)
    y = df["actual_label"].to_numpy().astype(float)
    if len(np.unique(y.astype(int))) < 2:
        append_log("MODEL: training data has only one class; skipping model")
        return None

    c_yes = df["yes_effective_cost"].to_numpy().astype(float)
    c_no = df["no_effective_cost"].to_numpy().astype(float)
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

    label_counts = {int(v): int((y == v).sum()) for v in np.unique(y)}
    append_log(
        f"MODEL trained: {len(all_rows)} rows from {len(csv_files)} contracts "
        f"label_dist={label_counts}"
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
    """Load model from file if it exists, otherwise train and save it."""
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
    """Return (pred_class, probs[3]). pred_class: CLASS_YES/NO/SKIP."""
    X = np.array([[features[f] for f in FEATURES]], dtype=float)
    raw = np.asarray(model.predict(X)).reshape(1, 3)
    probs = _softmax(raw)[0]
    pred_class = int(np.argmax(probs))
    return pred_class, probs


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SpotState:
    price: float | None = None
    timestamp_ms: int | None = None
    received_ms: int | None = None


@dataclass
class TokenBook:
    token_id: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    timestamp_ms: int | None = None
    last_trade_price: float | None = None
    event_count: int = 0
    fallback_best_bid: float | None = None
    fallback_best_ask: float | None = None

    def replace_from_book(self, book: dict[str, Any]) -> None:
        self.bids = _parse_levels(book.get("bids"))
        self.asks = _parse_levels(book.get("asks"))
        self.timestamp_ms = parse_epoch_ms(book.get("timestamp")) or utc_now_ms()
        self.last_trade_price = finite_float(book.get("last_trade_price"))
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
    price_target: float | None = None
    price_target_source: str = ""


class CollectorState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.market: CurrentMarket | None = None
        self.spot = SpotState()
        self.spot_history: deque[SpotState] = deque(maxlen=1000)
        self.books: dict[str, TokenBook] = {}


@dataclass
class QuoteSnapshot:
    timestamp: float
    up_mid: float
    up_bid_qty: float
    down_bid_qty: float
    up_book_imb_1c: float
    up_book_imb_5c: float
    up_book_imb_10c: float
    dir_bid_imb_1c: float
    dir_bid_imb_5c: float
    dir_bid_imb_10c: float


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
class ContractRuntime:
    market: CurrentMarket
    history: deque[QuoteSnapshot] = field(default_factory=lambda: deque(maxlen=300))
    up_mid_open: float | None = None
    decision: TradeDecision | None = None
    decision_logged: bool = False
    outcome_logged: bool = False
    last_status_log: float = 0.0
    last_outcome_wait_log: float = 0.0


# ---------------------------------------------------------------------------
# Helper: parse levels from CLOB book message
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


def _dir_bid_imbalance(up_book: TokenBook, down_book: TokenBook, tau: float) -> float | None:
    ud = up_book.depth_within_tau(tau, side="bid")
    dd = down_book.depth_within_tau(tau, side="bid")
    total = ud + dd
    if total <= 0:
        return None
    return (ud - dd) / total


# ---------------------------------------------------------------------------
# Feature extraction from live rolling history
# ---------------------------------------------------------------------------

def extract_features(
    runtime: ContractRuntime,
    up_book: TokenBook,
    down_book: TokenBook,
) -> dict[str, float] | None:
    """Return feature dict (15 values) or None if data is insufficient."""
    up_bid, up_bid_qty = up_book.best_bid()
    up_ask, up_ask_qty = up_book.best_ask()
    down_bid, down_bid_qty = down_book.best_bid()
    down_ask, down_ask_qty = down_book.best_ask()

    if any(v is None for v in (up_bid, up_ask, down_bid, down_ask)):
        return None
    if not (0.0 < up_ask < 1.0 and 0.0 < down_ask < 1.0):  # type: ignore[operator]
        return None

    up_mid = (up_bid + up_ask) / 2.0  # type: ignore[operator]
    up_bid_q = up_bid_qty or 0.0
    down_bid_q = down_bid_qty or 0.0
    obi_current = (up_bid_q - down_bid_q) / (up_bid_q + down_bid_q + 1e-9)

    # rolling 60s history
    now = time.time()
    cutoff = now - INDICATOR_WINDOW_SECONDS
    window = [snap for snap in runtime.history if snap.timestamp >= cutoff]

    up_mid_hist = [snap.up_mid for snap in window]
    obi_hist = [
        (snap.up_bid_qty - snap.down_bid_qty) / (snap.up_bid_qty + snap.down_bid_qty + 1e-9)
        for snap in window
    ]

    yes_mid_stats = _series_stats(up_mid_hist, up_mid)
    obi_stats = _series_stats(obi_hist, obi_current)

    mid_change_from_open = (
        up_mid - runtime.up_mid_open
        if runtime.up_mid_open is not None
        else 0.0
    )

    yes_book_1c = up_book.book_imbalance(0.01)
    yes_book_5c = up_book.book_imbalance(0.05)
    yes_book_10c = up_book.book_imbalance(0.10)
    dir_bid_1c = _dir_bid_imbalance(up_book, down_book, 0.01)
    dir_bid_5c = _dir_bid_imbalance(up_book, down_book, 0.05)
    dir_bid_10c = _dir_bid_imbalance(up_book, down_book, 0.10)

    for v in (yes_book_1c, yes_book_5c, yes_book_10c, dir_bid_1c, dir_bid_5c, dir_bid_10c):
        if v is None:
            return None

    features = {
        "p_yes_mid": up_mid,
        "yes_mid_z_60": yes_mid_stats["z"],
        "yes_mid_vol_60": yes_mid_stats["vol"],
        "mid_change_from_open": mid_change_from_open,
        "book_qty_log": math.log1p(up_bid_q + down_bid_q),
        "OBI": obi_current,
        "OBI_vol_60": obi_stats["vol"],
        "OBI_z_60": obi_stats["z"],
        "yes_book_imbalance_tau_1c": yes_book_1c,
        "yes_book_imbalance_tau_5c": yes_book_5c,
        "yes_book_imbalance_tau_10c": yes_book_10c,
        "dir_bid_imbalance_tau_1c": dir_bid_1c,
        "dir_bid_imbalance_tau_5c": dir_bid_5c,
        "dir_bid_imbalance_tau_10c": dir_bid_10c,
        "obi_depth_slope": yes_book_1c - yes_book_10c,  # type: ignore[operator]
    }
    if any(not math.isfinite(features[f]) for f in FEATURES):
        return None
    return features


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
            elif event_type == "last_trade_price":
                asset_id = str(item.get("asset_id") or "")
                book = state.books.get(asset_id)
                if book:
                    book.last_trade_price = finite_float(item.get("price")) or book.last_trade_price


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


def _spot_payloads(msg: dict[str, Any]) -> list[dict[str, Any]]:
    payload = msg.get("payload")
    parent_symbol = ""
    if isinstance(payload, list):
        items = [i for i in payload if isinstance(i, dict)]
    elif isinstance(payload, dict):
        parent_symbol = str(payload.get("symbol") or "")
        data = payload.get("data")
        items = [i for i in data if isinstance(i, dict)] if isinstance(data, list) else [payload]
    else:
        return []
    out = []
    for item in items:
        row = dict(item)
        row["symbol"] = str(row.get("symbol") or parent_symbol or SPOT_SYMBOL)
        out.append(row)
    return out


async def rtds_ws_loop(state: CollectorState, stop: asyncio.Event) -> None:
    topics = sorted({SPOT_TOPIC} | SPOT_TOPIC_ALIASES.get(SPOT_TOPIC, set()))
    sub = json.dumps({
        "action": "subscribe",
        "subscriptions": [
            {"topic": t, "type": "*", "filters": json.dumps({"symbol": SPOT_SYMBOL})}
            for t in topics
        ],
    })
    backoff = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(RTDS_WS_URL, ping_interval=None, open_timeout=10) as ws:
                await ws.send(sub)
                backoff = 1.0
                ping_task = asyncio.create_task(_text_ping_loop(ws, 5.0))
                try:
                    async for raw in ws:
                        if stop.is_set():
                            break
                        if isinstance(raw, str) and raw.strip().upper() in {"PING", "PONG"}:
                            continue
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        actual_topic = str(msg.get("topic") or "")
                        if actual_topic and actual_topic not in {SPOT_TOPIC} | SPOT_TOPIC_ALIASES.get(SPOT_TOPIC, set()):
                            continue
                        for payload in _spot_payloads(msg):
                            sym = str(payload.get("symbol") or "").lower().replace("-", "/")
                            if sym != SPOT_SYMBOL.lower():
                                continue
                            price = finite_float(payload.get("value"))
                            if price is None:
                                continue
                            spot = SpotState(
                                price=price,
                                timestamp_ms=parse_epoch_ms(payload.get("timestamp")) or utc_now_ms(),
                                received_ms=utc_now_ms(),
                            )
                            async with state.lock:
                                state.spot = spot
                                state.spot_history.append(spot)
                finally:
                    ping_task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            append_log(f"RTDS WS error: {type(exc).__name__}: {exc}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2.0, 30.0)


async def _text_ping_loop(ws: Any, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await ws.send("PING")
        except Exception:
            break


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

def _target_from_history(market: CurrentMarket, history: deque[SpotState]) -> float | None:
    if market.price_target is not None:
        return market.price_target
    start_ms = market.start_ts * 1000
    best: tuple[int, float] | None = None
    for item in history:
        if item.price is None or item.received_ms is None:
            continue
        dist = abs(item.received_ms - start_ms)
        if dist <= 2000 and (best is None or dist < best[0]):
            best = (dist, item.price)
    return best[1] if best else None


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

    meta = (event.get("eventMetadata") or event.get("metadata") or {})
    target: float | None = None
    for key in ("priceToBeat", "price_to_beat", "targetPrice", "initialPrice", "startPrice"):
        v = finite_float(meta.get(key) if isinstance(meta, dict) else None)
        if v is not None:
            target = v
            break

    return CurrentMarket(
        slug=event.get("slug") or market.get("slug") or "",
        start_ts=int(start_dt.timestamp()),
        end_ts=int(end_dt.timestamp()),
        up_token_id=up_token,
        down_token_id=down_token,
        price_target=target,
    )


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
    raise RuntimeError("No active BTC 5m Up/Down market found on Polymarket")


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


def _response_status(resp: dict[str, Any], contracts: int) -> tuple[str, str, float | None, float]:
    if resp.get("status") == "dry_run":
        return "dry_run", f"dry_run at {fmt_pct(resp.get('price'))}", resp.get("price"), float(contracts)
    if resp.get("error"):
        return "error", str(resp["error"]), None, 0.0
    verified = resp.get("verified_order") or resp
    status = str(verified.get("status") or resp.get("status") or "unknown")
    filled = float(verified.get("size_matched") or verified.get("amount_filled") or verified.get("filledAmount") or 0.0)
    fill_price = finite_float(verified.get("average_price") or verified.get("avgPrice") or resp.get("price"))
    order_id = str(resp.get("id") or resp.get("order_id") or "")
    if filled >= contracts:
        return "filled", f"filled {filled:g} @ {fmt_pct(fill_price)} id={order_id}", fill_price, filled
    if filled > 0:
        return "partial", f"partial {filled:g}/{contracts:g} id={order_id}", fill_price, filled
    return "unfilled", f"unfilled id={order_id}", None, 0.0


# ---------------------------------------------------------------------------
# Outcome fetching
# ---------------------------------------------------------------------------

async def fetch_outcome_from_gamma(slug: str, client: httpx.AsyncClient) -> tuple[int | None, str, str]:
    try:
        resp = await client.get(f"{GAMMA_BASE_URL}/events/slug/{slug}")
        if resp.status_code == 404:
            return None, "", "not_found"
        resp.raise_for_status()
        event = resp.json()
    except Exception as exc:
        return None, "", f"fetch_error:{exc}"
    markets = event.get("markets") or []
    for market in markets:
        outcomes = [str(i) for i in _parse_json_list(market.get("outcomes"))]
        prices = [str(i) for i in _parse_json_list(market.get("outcomePrices"))]
        if len(outcomes) == 2 and len(prices) == 2:
            try:
                up_idx = outcomes.index("Up") if "Up" in outcomes else 0
                down_idx = outcomes.index("Down") if "Down" in outcomes else 1
                up_price = float(prices[up_idx])
                if up_price >= 0.99:
                    return 1, "Up", "gamma.outcomePrices_final"
                if up_price <= 0.01:
                    return 0, "Down", "gamma.outcomePrices_final"
            except (ValueError, IndexError):
                pass
        winning = str(market.get("winningOutcome") or market.get("winning_outcome") or "").strip()
        if winning == "Up":
            return 1, "Up", "gamma.winningOutcome"
        if winning == "Down":
            return 0, "Down", "gamma.winningOutcome"
    return None, "", "unresolved"


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


# ---------------------------------------------------------------------------
# Trading actions
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
        "contracts": args.contracts,
        "dry_run": int(not args.live),
        "successful_count": counts.successful,
        "unsuccessful_count": counts.unsuccessful,
        "skipped_count": counts.skipped,
    }

    if up_book is None or down_book is None:
        counts.skipped += 1
        runtime.decision = TradeDecision(status="skip", contracts=args.contracts, dry_run=not args.live, reason="no book data")
        append_log(
            f"STATUS T={remaining:.1f}s {runtime.market.slug} | decision=SKIP reason=no book data | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({**base_row, "order_status": "skip", "reason": "no book data", "skipped_count": counts.skipped})
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
        runtime.decision = TradeDecision(status="skip", contracts=args.contracts, dry_run=not args.live, reason="no model")
        append_log(
            f"STATUS T={remaining:.1f}s {runtime.market.slug} | decision=SKIP reason=no model | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({**base_row, "order_status": "skip", "reason": "no model", "skipped_count": counts.skipped})
        return

    features = extract_features(runtime, up_book, down_book)
    if features is None:
        counts.skipped += 1
        runtime.decision = TradeDecision(status="skip", contracts=args.contracts, dry_run=not args.live, reason="feature_extraction_failed")
        append_log(
            f"STATUS T={remaining:.1f}s {runtime.market.slug} | decision=SKIP reason=features_failed | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({**base_row, "order_status": "skip", "reason": "features_failed", "skipped_count": counts.skipped})
        return

    pred_class, probs = predict(model, features)

    if pred_class == CLASS_SKIP:
        counts.skipped += 1
        runtime.decision = TradeDecision(
            status="skip", pred_class=CLASS_SKIP, pred_p_yes=float(probs[0]),
            pred_p_no=float(probs[1]), pred_p_skip=float(probs[2]),
            contracts=args.contracts, dry_run=not args.live, reason="model_skip",
        )
        um_str = f"{up_mid:.4f}" if up_mid is not None else "--"
        append_log(
            f"STATUS T={remaining:.1f}s {runtime.market.slug} | up_mid={um_str} "
            f"pred_yes={probs[0]:.3f} pred_no={probs[1]:.3f} pred_skip={probs[2]:.3f} decision=SKIP | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({
            **base_row,
            "pred_class": pred_class, "pred_p_yes": float(probs[0]),
            "pred_p_no": float(probs[1]), "pred_p_skip": float(probs[2]),
            "order_status": "skip", "reason": "model_skip", "skipped_count": counts.skipped,
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
        runtime.decision = TradeDecision(
            status="skip", pred_class=pred_class, contracts=args.contracts,
            dry_run=not args.live, reason=reason,
        )
        append_log(
            f"ORDER SKIP {runtime.market.slug} {side} {reason} | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({
            **base_row, "selected_side": side, "pred_class": pred_class,
            "pred_p_yes": float(probs[0]), "pred_p_no": float(probs[1]), "pred_p_skip": float(probs[2]),
            "order_status": "skip", "reason": reason, "skipped_count": counts.skipped,
        })
        return

    price_rounded = round(round(ask_price * 100) / 100, 2)
    um_str2 = f"{up_mid:.4f}" if up_mid is not None else "--"
    append_log(
        f"STATUS T={remaining:.1f}s {runtime.market.slug} | up_mid={um_str2} "
        f"pred={side} pred_yes={probs[0]:.3f} pred_no={probs[1]:.3f} pred_skip={probs[2]:.3f} "
        f"ask={fmt_pct(ask_price)} contracts={args.contracts}",
        prefix_timestamp=False,
    )

    resp = await asyncio.to_thread(place_order, token_id, price_rounded, args.contracts, dry_run=not args.live)
    order_status, order_reason, fill_price, filled_size = _response_status(resp, args.contracts)

    order_id = str(resp.get("id") or resp.get("order_id") or resp.get("order_id") or "")
    if order_status in ("dry_run",):
        order_id = str(resp.get("order_id", ""))
    outcome_eligible = order_status in ("dry_run", "filled") and filled_size >= args.contracts

    runtime.decision = TradeDecision(
        status=order_status,
        side=side,
        token_id=token_id,
        pred_class=pred_class,
        pred_p_yes=float(probs[0]),
        pred_p_no=float(probs[1]),
        pred_p_skip=float(probs[2]),
        selected_ask=ask_price,
        selected_ask_qty=ask_qty,
        contracts=args.contracts,
        dry_run=not args.live,
        order_id=order_id,
        fill_price=fill_price,
        filled_size=filled_size,
        reason=order_reason,
        outcome_eligible=outcome_eligible,
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


async def maybe_record_outcome(
    runtime: ContractRuntime,
    state: CollectorState,
    counts: Counts,
    args: argparse.Namespace,
    client: httpx.AsyncClient,
    completed_seen: set[str],
) -> tuple[bool, ContractRuntime | None]:
    if runtime is None or runtime.outcome_logged:
        return False, runtime
    remaining = runtime.market.end_ts - time.time()
    if remaining > args.outcome_delay_seconds:
        return False, runtime

    actual_label, actual_side, outcome_source = await fetch_outcome_from_gamma(runtime.market.slug, client)

    now = time.monotonic()
    if actual_label is None:
        if now - runtime.last_outcome_wait_log >= OUTCOME_WAIT_LOG_INTERVAL:
            runtime.last_outcome_wait_log = now
            append_log(
                f"OUTCOME WAIT {runtime.market.slug} | unresolved, retrying",
                prefix_timestamp=False,
            )
        return False, runtime

    decision = runtime.decision
    correct: int | str = ""
    latest = "skipped"
    if decision is not None and decision.outcome_eligible:
        pred_won = (decision.side == "YES" and actual_label == 1) or (decision.side == "NO" and actual_label == 0)
        correct = int(pred_won)
        if correct:
            counts.successful += 1
            latest = "successful"
        else:
            counts.unsuccessful += 1
            latest = "unsuccessful"

    async with state.lock:
        up_book = state.books.get(runtime.market.up_token_id)
        down_book = state.books.get(runtime.market.down_token_id)
    up_mid = None
    if up_book and down_book:
        ub, _ = up_book.best_bid()
        ua, _ = up_book.best_ask()
        if ub and ua:
            up_mid = (ub + ua) / 2.0

    close_time = iso_from_ms(runtime.market.end_ts * 1000)
    append_log(
        f"OUTCOME {runtime.market.slug} | actual={actual_side} source={outcome_source} "
        f"trade={decision.side if decision else '--'} status={decision.status if decision else 'none'} "
        f"result={latest} correct={correct if correct != '' else '--'} | "
        f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
        prefix_timestamp=False,
    )
    append_trade_row({
        "timestamp_utc": iso_utc(),
        "event": "outcome",
        "contract_id": runtime.market.slug,
        "close_time": close_time,
        "remaining_seconds": f"{remaining:.3f}",
        "entry_seconds": args.entry_seconds,
        "p_yes_mid": up_mid,
        "selected_side": decision.side if decision else "",
        "selected_token_id": decision.token_id if decision else "",
        "selected_ask": decision.selected_ask if decision else "",
        "selected_ask_qty": decision.selected_ask_qty if decision else "",
        "contracts": decision.contracts if decision else "",
        "dry_run": int(decision.dry_run) if decision else "",
        "order_status": decision.status if decision else "none",
        "order_id": decision.order_id if decision else "",
        "fill_price": decision.fill_price if decision else "",
        "filled_size": decision.filled_size if decision else "",
        "actual_side": actual_side,
        "actual_label": actual_label,
        "correct": correct,
        "official_outcome_source": outcome_source,
        "pred_class": decision.pred_class if decision else "",
        "pred_p_yes": decision.pred_p_yes if decision else "",
        "pred_p_no": decision.pred_p_no if decision else "",
        "pred_p_skip": decision.pred_p_skip if decision else "",
        "successful_count": counts.successful,
        "unsuccessful_count": counts.unsuccessful,
        "skipped_count": counts.skipped,
        "reason": latest,
    })
    runtime.outcome_logged = True
    completed_seen.add(runtime.market.slug)
    return True, None


# ---------------------------------------------------------------------------
# Main trading loop
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> None:
    append_log(
        f"START polymarket_5m_trader live={args.live} contracts={args.contracts} "
        f"entry_seconds={args.entry_seconds} tolerance={args.entry_tolerance}s "
        f"outcome_delay={args.outcome_delay_seconds}s "
        f"stop_loss={fmt_money(args.stop_loss)} model={args.model_path}"
    )

    model = await asyncio.to_thread(
        load_or_train_model, args.model_path, args.data_dir, args.outcomes_csv, args.retrain
    )
    if model is None:
        append_log("MODEL: proceeding without model (all entries will SKIP)")

    counts = Counts()
    completed_seen: set[str] = set()
    pending_outcomes: dict[str, ContractRuntime] = {}
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
    rtds_task: asyncio.Task[None] | None = None
    clob_task: asyncio.Task[None] | None = None
    rtds_task = asyncio.create_task(rtds_ws_loop(state, stop))

    try:
        await log_balance("START", None, None, args.stop_loss, args)
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            last_market_slug = ""

            while not stop.is_set():
                # Check pending outcomes from previous contracts
                for slug in list(pending_outcomes):
                    rt = pending_outcomes[slug]
                    done, _ = await maybe_record_outcome(rt, state, counts, args, client, completed_seen)
                    if done:
                        del pending_outcomes[slug]
                        await log_balance("OUTCOME", None, args.initial_balance, args.stop_loss, args)

                # Check current runtime outcome
                if runtime is not None and not runtime.outcome_logged:
                    done, runtime = await maybe_record_outcome(
                        runtime, state, counts, args, client, completed_seen
                    )
                    if done:
                        await log_balance("OUTCOME", None, args.initial_balance, args.stop_loss, args)

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
                    # Queue pending outcome for old runtime
                    if runtime is not None and not runtime.outcome_logged:
                        pending_outcomes[runtime.market.slug] = runtime

                    last_market_slug = market.slug
                    # Set price target from spot history
                    async with state.lock:
                        target = _target_from_history(market, state.spot_history)
                        if target is not None:
                            market.price_target = target
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
                        f"CONTRACT {market.slug} | "
                        f"close {iso_from_ms(market.end_ts * 1000)} | "
                        f"target {market.price_target}",
                        prefix_timestamp=False,
                    )

                # Update spot target if still missing
                if market.price_target is None:
                    async with state.lock:
                        t = _target_from_history(market, state.spot_history)
                        if t is not None:
                            market.price_target = t

                # Snapshot current books for rolling history
                async with state.lock:
                    up_book = state.books.get(market.up_token_id)
                    down_book = state.books.get(market.down_token_id)

                if up_book and down_book:
                    ub, ubq = up_book.best_bid()
                    ua, uaq = up_book.best_ask()
                    db, dbq = down_book.best_bid()
                    da, daq = down_book.best_ask()
                    if ub is not None and ua is not None and db is not None and da is not None:
                        up_mid_live = (ub + ua) / 2.0
                        if runtime.up_mid_open is None:
                            runtime.up_mid_open = up_mid_live
                        snap = QuoteSnapshot(
                            timestamp=time.time(),
                            up_mid=up_mid_live,
                            up_bid_qty=ubq or 0.0,
                            down_bid_qty=dbq or 0.0,
                            up_book_imb_1c=up_book.book_imbalance(0.01) or 0.0,
                            up_book_imb_5c=up_book.book_imbalance(0.05) or 0.0,
                            up_book_imb_10c=up_book.book_imbalance(0.10) or 0.0,
                            dir_bid_imb_1c=_dir_bid_imbalance(up_book, down_book, 0.01) or 0.0,
                            dir_bid_imb_5c=_dir_bid_imbalance(up_book, down_book, 0.05) or 0.0,
                            dir_bid_imb_10c=_dir_bid_imbalance(up_book, down_book, 0.10) or 0.0,
                        )
                        runtime.history.append(snap)

                # Status logging
                remaining = market.end_ts - time.time()
                now_mono = time.monotonic()
                if (args.log_interval <= 0 or runtime.last_status_log <= 0 or
                        now_mono - runtime.last_status_log >= args.log_interval):
                    async with state.lock:
                        up_b = state.books.get(market.up_token_id)
                        ub_val = None
                        ua_val = None
                        if up_b:
                            ub_val, _ = up_b.best_bid()
                            ua_val, _ = up_b.best_ask()
                        up_mid_log = (ub_val + ua_val) / 2.0 if ub_val and ua_val else None
                    status_str = runtime.decision.status if runtime.decision else "--"
                    um_str = f"{up_mid_log:.4f}" if up_mid_log is not None else "--"
                    append_log(
                        f"STATUS T={remaining:.1f}s | up_mid={um_str} "
                        f"target={market.price_target} trade={status_str}",
                        prefix_timestamp=False,
                    )
                    runtime.last_status_log = now_mono

                # Entry decision
                lower = max(0.0, args.entry_seconds - args.entry_tolerance)
                upper = args.entry_seconds
                in_window = lower <= remaining <= upper
                if in_window and not runtime.decision_logged and remaining >= 0:
                    runtime.decision_logged = True
                    await log_balance(
                        f"T{args.entry_seconds}s", remaining, args.initial_balance, args.stop_loss, args
                    )
                    await evaluate_entry(runtime, state, model, counts, args, remaining)
                elif remaining < lower and not runtime.decision_logged and remaining >= 0:
                    # Missed entry window
                    runtime.decision_logged = True
                    reason = f"missed entry window ({lower:.0f}<=T<={upper:.0f}s); T={remaining:.1f}s"
                    counts.skipped += 1
                    runtime.decision = TradeDecision(
                        status="skip", contracts=args.contracts, dry_run=not args.live, reason=reason
                    )
                    append_log(
                        f"ENTRY MISS {runtime.market.slug} | {reason} | "
                        f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
                        prefix_timestamp=False,
                    )

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
        if rtds_task:
            rtds_task.cancel()
            await asyncio.gather(rtds_task, return_exceptions=True)
        append_log("STOP polymarket_5m_trader")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m Up/Down live trader (btc_d3 T=30 model).")
    parser.add_argument("--live", action="store_true", help="Submit real orders. Omit for dry-run.")
    parser.add_argument("--contracts", type=int, default=1, help="Contracts (shares) per trade. Default: 1.")
    parser.add_argument("--entry-seconds", type=float, default=30.0, help="Entry time before close (seconds). Default: 30.")
    parser.add_argument("--entry-tolerance", type=float, default=5.0, help="Entry window tolerance (seconds). Default: 5.")
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH, help="Saved LightGBM model file. Default: btc_5m_lgb_model.txt.")
    parser.add_argument("--retrain", action="store_true", help="Force retrain even if model file exists.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Historical CSV directory (only used when retraining).")
    parser.add_argument("--outcomes-csv", type=Path, default=DEFAULT_OUTCOMES_CSV, help="Official outcomes CSV (only used when retraining).")
    parser.add_argument("--poll-interval", type=float, default=0.5, help="Poll interval (seconds). Default: 0.5.")
    parser.add_argument("--log-interval", type=float, default=30.0, help="Seconds between status log lines. Default: 30.")
    parser.add_argument("--stop-loss", type=float, default=5.0, help="Stop if balance drops this many USD. Default: 5.")
    parser.add_argument("--outcome-delay-seconds", type=float, default=OUTCOME_DELAY_SECONDS,
                        help="Check outcome this many seconds after close. Default: -120.")
    args = parser.parse_args()
    args.contracts = max(1, args.contracts)
    args.entry_tolerance = max(0.0, args.entry_tolerance)
    args.poll_interval = max(0.1, args.poll_interval)
    return args


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
