"""
pmcluster.features_hf — derive high-frequency Kraken spot features for Polymarket 5m contracts.

Takes the kraken_hf/trades_<COIN>.csv and kraken_hf/ticker_<COIN>.csv files collected by
the live Lightsail collector and computes strictly pre-trade features aligned to each
Polymarket contract snapshot row.

Features produced (one row per contract × entry-horizon):
  hf_spot_return_5s    — log-return of mid over preceding 5 s
  hf_spot_return_15s   — log-return over preceding 15 s
  hf_spot_return_30s   — log-return over preceding 30 s
  hf_spot_return_60s   — log-return over preceding 60 s
  hf_spot_accel_60s    — return_30s − return_60s (recent acceleration)
  hf_realized_vol_60s  — realized vol from ticks in preceding 60 s (std of log-returns)
  hf_trade_flow_60s    — signed buy−sell volume imbalance in preceding 60 s
  hf_microprice        — (bid*ask_qty + ask*bid_qty) / (bid_qty + ask_qty) at snapshot time
  hf_l1_imbalance      — (bid_qty − ask_qty) / (bid_qty + ask_qty) at snapshot time
  hf_distance_hf       — (mid − price_target) / price_target, using live HF mid

Usage:
    from pmcluster.features_hf import load_kraken_hf, add_hf_features
    hf = load_kraken_hf(coin, kraken_hf_dir)       # loads + sorts trade & ticker data
    df_with_features = add_hf_features(df, hf)     # df has columns: sample_epoch_ms, price_target
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class KrakenHF:
    coin: str
    trades: pd.DataFrame   # columns: recv_utc_ms, kraken_ts_ms, price, qty, side
    ticker: pd.DataFrame   # columns: recv_utc_ms, bid, bid_qty, ask, ask_qty


def load_kraken_hf(coin: str, kraken_hf_dir: Path) -> KrakenHF | None:
    """Load HF data for a coin. Returns None if files are missing or empty.

    Combines backfill (trades_{coin}_backfill.csv) and live (trades_{coin}.csv)
    trade data if both exist, deduplicating by kraken_ts_ms.
    """
    trades_path          = kraken_hf_dir / f"trades_{coin}.csv"
    trades_backfill_path = kraken_hf_dir / f"trades_{coin}_backfill.csv"
    ticker_path          = kraken_hf_dir / f"ticker_{coin}.csv"

    trade_parts = []
    for p in [trades_backfill_path, trades_path]:
        if p.exists() and p.stat().st_size > 0:
            try:
                part = pd.read_csv(p, dtype={"recv_utc_ms": "Int64", "kraken_ts_ms": "Int64"})
                trade_parts.append(part)
            except Exception:
                pass

    if not trade_parts:
        return None

    if not ticker_path.exists():
        return None

    try:
        trades = pd.concat(trade_parts, ignore_index=True)
        ticker = pd.read_csv(ticker_path, dtype={"recv_utc_ms": "Int64"})
    except Exception:
        return None

    for col in ("price", "qty"):
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    for col in ("bid", "bid_qty", "ask", "ask_qty"):
        if col in ticker.columns:
            ticker[col] = pd.to_numeric(ticker[col], errors="coerce")

    trades = (trades
              .dropna(subset=["price", "qty"])
              .drop_duplicates(subset=["kraken_ts_ms", "price", "qty"])
              .sort_values("kraken_ts_ms")
              .reset_index(drop=True))
    ticker = ticker.dropna(subset=["recv_utc_ms", "bid", "ask"]).sort_values("recv_utc_ms").reset_index(drop=True)

    if trades.empty or ticker.empty:
        return None

    return KrakenHF(coin=coin, trades=trades, ticker=ticker)


def _mid_at(ticker: pd.DataFrame, ts_ms: int) -> float | None:
    """Best bid/ask mid strictly before ts_ms."""
    idx = ticker["recv_utc_ms"].searchsorted(ts_ms, side="right") - 1
    if idx < 0:
        return None
    row = ticker.iloc[idx]
    bid, ask = row["bid"], row["ask"]
    if pd.isna(bid) or pd.isna(ask) or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


def _mid_at_offset(ticker: pd.DataFrame, ts_ms: int, lookback_ms: int) -> float | None:
    """Mid at ts_ms - lookback_ms (for log-return numerics)."""
    return _mid_at(ticker, ts_ms - lookback_ms)


def _realized_vol(ticker: pd.DataFrame, ts_ms: int, lookback_ms: int) -> float | None:
    """Realized vol (std of log-price-changes between consecutive ticks) in the lookback window."""
    start_ms = ts_ms - lookback_ms
    mask = (ticker["recv_utc_ms"] >= start_ms) & (ticker["recv_utc_ms"] < ts_ms)
    sub = ticker[mask]
    if len(sub) < 3:
        return None
    mids = (sub["bid"] + sub["ask"]) / 2.0
    logrets = np.log(mids.values[1:] / mids.values[:-1])
    logrets = logrets[np.isfinite(logrets)]
    if len(logrets) < 2:
        return None
    return float(np.std(logrets, ddof=0))


def _trade_flow_imbalance(trades: pd.DataFrame, ts_ms: int, lookback_ms: int) -> float | None:
    """(buy_volume - sell_volume) / (buy_volume + sell_volume) in the lookback window."""
    start_ms = ts_ms - lookback_ms
    mask = (trades["recv_utc_ms"] >= start_ms) & (trades["recv_utc_ms"] < ts_ms)
    sub = trades[mask]
    if sub.empty:
        return None
    buy_vol  = sub.loc[sub["side"] == "buy",  "qty"].sum()
    sell_vol = sub.loc[sub["side"] == "sell", "qty"].sum()
    total = buy_vol + sell_vol
    if total <= 0:
        return None
    return float((buy_vol - sell_vol) / total)


def _l1_snapshot(ticker: pd.DataFrame, ts_ms: int) -> dict[str, float | None]:
    """L1 bid/ask at ts_ms for microprice and imbalance."""
    idx = ticker["recv_utc_ms"].searchsorted(ts_ms, side="right") - 1
    if idx < 0:
        return {"bid": None, "ask": None, "bid_qty": None, "ask_qty": None}
    row = ticker.iloc[idx]
    return {
        "bid":     row["bid"]     if pd.notna(row["bid"])     else None,
        "ask":     row["ask"]     if pd.notna(row["ask"])     else None,
        "bid_qty": row["bid_qty"] if pd.notna(row["bid_qty"]) else None,
        "ask_qty": row["ask_qty"] if pd.notna(row["ask_qty"]) else None,
    }


def _safe_log_return(price_now: float | None, price_before: float | None) -> float | None:
    if price_now is None or price_before is None:
        return None
    if price_before <= 0 or price_now <= 0:
        return None
    return float(math.log(price_now / price_before))


def _compute_hf_row(ts_ms: int, price_target: float | None, hf: KrakenHF) -> dict[str, float | None]:
    """Compute all HF features at a single snapshot timestamp."""
    mid_now = _mid_at(hf.ticker, ts_ms)

    mid_5s  = _mid_at_offset(hf.ticker, ts_ms, 5_000)
    mid_15s = _mid_at_offset(hf.ticker, ts_ms, 15_000)
    mid_30s = _mid_at_offset(hf.ticker, ts_ms, 30_000)
    mid_60s = _mid_at_offset(hf.ticker, ts_ms, 60_000)

    ret_5s  = _safe_log_return(mid_now, mid_5s)
    ret_15s = _safe_log_return(mid_now, mid_15s)
    ret_30s = _safe_log_return(mid_now, mid_30s)
    ret_60s = _safe_log_return(mid_now, mid_60s)

    accel = (ret_30s - ret_60s) if (ret_30s is not None and ret_60s is not None) else None
    rvol  = _realized_vol(hf.ticker, ts_ms, 60_000)
    flow  = _trade_flow_imbalance(hf.trades, ts_ms, 60_000)

    l1 = _l1_snapshot(hf.ticker, ts_ms)
    bid, ask = l1["bid"], l1["ask"]
    bq,  aq  = l1["bid_qty"], l1["ask_qty"]

    microprice = None
    if bid and ask and bq and aq and (bq + aq) > 0:
        microprice = (bid * aq + ask * bq) / (bq + aq)

    l1_imbalance = None
    if bq is not None and aq is not None and (bq + aq) > 0:
        l1_imbalance = (bq - aq) / (bq + aq)

    dist_hf = None
    if mid_now and price_target and price_target > 0:
        dist_hf = (mid_now - price_target) / price_target

    return {
        "hf_spot_return_5s":  ret_5s,
        "hf_spot_return_15s": ret_15s,
        "hf_spot_return_30s": ret_30s,
        "hf_spot_return_60s": ret_60s,
        "hf_spot_accel_60s":  accel,
        "hf_realized_vol_60s": rvol,
        "hf_trade_flow_60s":  flow,
        "hf_microprice":      microprice,
        "hf_l1_imbalance":    l1_imbalance,
        "hf_distance_hf":     dist_hf,
    }


HF_FEATURE_COLS = [
    "hf_spot_return_5s",
    "hf_spot_return_15s",
    "hf_spot_return_30s",
    "hf_spot_return_60s",
    "hf_spot_accel_60s",
    "hf_realized_vol_60s",
    "hf_trade_flow_60s",
    "hf_microprice",
    "hf_l1_imbalance",
    "hf_distance_hf",
]


def add_hf_features(df: pd.DataFrame, hf: KrakenHF) -> pd.DataFrame:
    """
    Add HF feature columns to df in-place.

    df must have columns: sample_epoch_ms (int, UTC milliseconds), price_target (float|NaN).
    Returns df with HF_FEATURE_COLS added (NaN where HF data is unavailable).
    """
    rows = []
    for _, row in df.iterrows():
        ts_ms = int(row["sample_epoch_ms"])
        pt    = float(row["price_target"]) if pd.notna(row.get("price_target")) else None
        rows.append(_compute_hf_row(ts_ms, pt, hf))

    hf_df = pd.DataFrame(rows, index=df.index)
    for col in HF_FEATURE_COLS:
        df[col] = hf_df[col]
    return df


def sanity_check(hf: KrakenHF, window_minutes: int = 60) -> dict:
    """
    Quick sanity check: what fraction of 60s windows have nonzero spot move?
    Compare old RTDS (would be ~2%) to HF (should be ~100%).
    """
    if hf.ticker.empty:
        return {"nonzero_60s_move_pct": None, "n_windows": 0}

    ts = hf.ticker["recv_utc_ms"].values
    t_start, t_end = ts[0], ts[-1]
    step_ms = 60_000
    nonzero = total = 0

    t = t_start + 60_000  # first window needs 60s of history
    while t <= t_end:
        mid_now  = _mid_at(hf.ticker, t)
        mid_prev = _mid_at_offset(hf.ticker, t, 60_000)
        if mid_now is not None and mid_prev is not None:
            total += 1
            if abs(mid_now - mid_prev) > 0:
                nonzero += 1
        t += step_ms

    return {
        "nonzero_60s_move_pct": round(100 * nonzero / total, 1) if total else None,
        "n_windows": total,
    }
