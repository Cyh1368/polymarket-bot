#!/usr/bin/env python3
"""Polymarket BTC 5m Up/Down live trader — Huber edge-regression model (t245_combined).

Entry rule (2026-06-18 research, saved_huber_model_t245_combined, min_child=150):
  At T=245s before close: two Huber LightGBM regressors predict the expected
  return of a YES bet and a NO bet directly (objective=huber, alpha=0.5).
  Trade the side with the higher predicted edge if it clears skip_bonus=0.05.
  NO post-hoc filters — the edge prediction is the only gate.
  14 features: v3 base (13) + obi_depth_slope (OLS slope of book imbalance
    vs log(tau) across 8 depth levels). NaN when <2 tau levels are available
    — LightGBM routes NaN to the optimal branch at each split.
  CV (5-fold expanding window): EV/available=+3.9%, win-capped=+0.040,
    trim10=+0.057, worst-seed CI lower bound +0.0061 (5/5 seeds pass).
  Model files: 2026-06-17-research/saved_huber_model_t245_combined/huber_yes_t245.txt,
    huber_no_t245.txt (train with 2026-06-17-research/train_t245_combined.py)

No exit: hold to official settlement outcome.

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
import hashlib
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
import lightgbm as lgb
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

# Huber edge-regression model (2026-06-18 research, t245_combined, min_child=150, alpha=0.5).
# Two regressors predict E[YES return] and E[NO return] directly; trade the side with the
# higher predicted edge if it clears SKIP_BONUS. NO post-hoc filters.
MODEL_YES_PATH = Path(__file__).resolve().parent / "huber_yes_t245.txt"
MODEL_NO_PATH  = Path(__file__).resolve().parent / "huber_no_t245.txt"
SKIP_BONUS  = 0.05    # minimum predicted edge (return-on-stake) to trade
FEATURES = [
    "p_yes_mid",
    "yes_mid_z_60", "yes_mid_vol_60",
    "yes_mid_z_20", "yes_mid_vol_20",
    "mid_change_60",
    "book_qty_log",
    "OBI", "OBI_vol_60", "OBI_z_60",
    "spread_yes",
    "tod_sin", "tod_cos",
    "obi_depth_slope",
]

# obi_depth_slope: tau levels in price units (0.01 = 1 cent)
_TAU_PRICES = [0.01, 0.02, 0.03, 0.05, 0.07, 0.10, 0.15, 0.20]
_TAU_LOG_X  = [math.log(t) for t in _TAU_PRICES]  # log(tau) for OLS x-axis

TOLERANCE_SECONDS = 5.0       # entry window: [T-5, T]
_model_yes: lgb.Booster | None = None   # E[YES return]; loaded at startup by run()
_model_no:  lgb.Booster | None = None   # E[NO  return]; loaded at startup by run()

OUTCOME_POLL_INTERVAL = 15.0
OUTCOME_WAIT_LOG_INTERVAL = 30.0
OUTCOME_DELAY_SECONDS = -270.0  # check outcome 4.5 min after close; observed resolution ~5.5 min

MAX_ORDER_ATTEMPTS = 10       # FOK retry limit; each attempt uses fresh best-ask from book
ORDER_RETRY_DELAY = 0.25      # seconds between retry attempts

# Paths
APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

DEFAULT_DATA_DIR = APP_DIR / "data_BTC_5m"
DEFAULT_OUTCOMES_CSV = APP_DIR / "polymarket_btc_5m_official_outcomes.csv"
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
    "successful_count", "unsuccessful_count", "skipped_count", "reason",
    "dollar_pnl",
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
    obi: float = 0.0


@dataclass
class Counts:
    successful: int = 0
    unsuccessful: int = 0
    skipped: int = 0
    yes_won: int = 0
    yes_lost: int = 0
    no_won: int = 0
    no_lost: int = 0
    total_pnl: float = 0.0


def _counts_str(counts: Counts, contract_value: float) -> str:
    n_yes    = counts.yes_won + counts.yes_lost
    n_no     = counts.no_won  + counts.no_lost
    n_traded = n_yes + n_no
    n_avail  = n_traded + counts.skipped

    def wr(w: int, n: int) -> str:
        return f"{w/n:.1%}" if n else "--"

    ev_str = (f"{counts.total_pnl / (contract_value * n_avail):+.3f}"
              if n_avail else "--")
    return (
        f"trades={n_traded}(Y:{n_yes}/N:{n_no}) skip={counts.skipped} "
        f"wr={wr(counts.yes_won + counts.no_won, n_traded)}"
        f"(Y:{wr(counts.yes_won, n_yes)}/N:{wr(counts.no_won, n_no)}) "
        f"pnl=${counts.total_pnl:+.2f} ev/avail={ev_str}"
    )


@dataclass
class TradeDecision:
    status: str = ""
    side: str = ""
    token_id: str = ""
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
        if dist <= 300_000 and (best is None or dist < best[0]):
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
    status = str(verified.get("status") or resp.get("status") or "unknown").lower()
    filled = float(verified.get("size_matched") or verified.get("amount_filled") or verified.get("filledAmount") or 0.0)
    fill_price = finite_float(
        verified.get("average_price") or verified.get("avgPrice")
        or verified.get("price") or resp.get("price")
    )
    order_id = str(resp.get("id") or resp.get("order_id") or "")
    # FOK orders on Polymarket return status="matched" when fully filled; size_matched may be absent.
    if status == "matched" and filled == 0.0:
        filled = float(contracts)
    if filled >= contracts:
        return "filled", f"filled {filled:g} @ {fmt_pct(fill_price)} id={order_id}", fill_price, filled
    if filled > 0:
        return "partial", f"partial {filled:g}/{contracts:g} id={order_id}", fill_price, filled
    return "unfilled", f"unfilled id={order_id}", None, 0.0


# ---------------------------------------------------------------------------
# Outcome fetching
# ---------------------------------------------------------------------------

def _infer_winner(market: dict[str, Any]) -> tuple[str, str]:
    """Mirror of fetch_all_outcomes.py _infer_winner. Returns (winning_outcome, source)."""
    outcomes = [str(o) for o in _parse_json_list(market.get("outcomes"))]
    prices_raw = _parse_json_list(market.get("outcomePrices"))
    prices = [finite_float(p) for p in prices_raw]

    # Priority 1: explicit resolution field
    resolution = str(market.get("resolution") or market.get("resolved_to") or "").strip()
    if resolution:
        return resolution, "gamma.resolution"

    # Priority 2: winner field (index or string)
    winner = str(market.get("winner") or "").strip()
    if winner:
        if winner.isdigit() and outcomes:
            idx = int(winner)
            if 0 <= idx < len(outcomes):
                return outcomes[idx], "gamma.winner_index"
        return winner, "gamma.winner"

    # Priority 3: outcomePrices settled to final (>= 0.999)
    if outcomes and len(prices) >= len(outcomes):
        resolved = [(o, p) for o, p in zip(outcomes, prices) if p is not None]
        ones = [(o, p) for o, p in resolved if p >= 0.999]
        zeros = [(o, p) for o, p in resolved if p <= 0.001]
        if len(ones) == 1 and len(zeros) >= len(outcomes) - 1:
            return ones[0][0], "gamma.outcomePrices_final"
        # Priority 4: near-final (> 0.9)
        highs = [(o, p) for o, p in resolved if p > 0.9]
        lows = [(o, p) for o, p in resolved if p < 0.1]
        if len(highs) == 1 and len(lows) >= len(outcomes) - 1:
            return highs[0][0], "gamma.outcomePrices_near_final"

    return "", ""


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
        wo, source = _infer_winner(market)
        if not wo:
            continue
        wo_lower = wo.lower().strip()
        if wo_lower == "up":
            return 1, "Up", source
        if wo_lower == "down":
            return 0, "Down", source
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


def _stats(hist: list[float], current: float) -> tuple[float, float]:
    """(z_score, vol_ddof0) of current vs history+current — mirrors research series_stats."""
    vals = hist + [current]
    if len(vals) < 2:
        return 0.0, 0.0
    mean = sum(vals) / len(vals)
    vol  = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
    z    = (current - mean) / vol if vol > 1e-12 else 0.0
    return z, vol


def _ols_slope(y_vals: list[float | None]) -> float:
    """OLS slope of y_vals vs _TAU_LOG_X. Returns NaN when <2 valid points.
    NaN is passed through to LightGBM, which routes it to the optimal branch.
    """
    pairs = [(x, y) for x, y in zip(_TAU_LOG_X, y_vals)
             if y is not None and math.isfinite(y)]
    if len(pairs) < 2:
        return float("nan")
    xs = [p[0] for p in pairs]; ys = [p[1] for p in pairs]
    n = len(xs)
    xm = sum(xs) / n; ym = sum(ys) / n
    denom = sum((x - xm) ** 2 for x in xs)
    if denom < 1e-12:
        return float("nan")
    return sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / denom


def _build_features(
    runtime: ContractRuntime,
    up_book: TokenBook,
    down_book: TokenBook,
) -> list[float] | None:
    """Extract v3.1 features from live state + rolling history.
    Feature order matches FEATURES list exactly.
    Returns None if essential price data is unavailable.
    obi_depth_slope may be float('nan') when the live book is too thin.
    """
    up_bid_p, up_bid_q   = up_book.best_bid()
    up_ask_p, _          = up_book.best_ask()
    _,        down_bid_q = down_book.best_bid()

    if up_bid_p is None or up_ask_p is None:
        return None
    if not (0.0 < up_ask_p < 1.0 and 0.0 < up_bid_p <= up_ask_p):
        return None

    up_mid  = (up_bid_p + up_ask_p) / 2.0
    ubs     = up_bid_q  or 0.0
    dbs     = down_bid_q or 0.0
    obi_cur = (ubs - dbs) / (ubs + dbs + 1e-9)

    now     = time.time()
    h60     = [s for s in runtime.history if now - s.timestamp <= 60.0]
    h20     = [s for s in runtime.history if now - s.timestamp <= 20.0]

    mids_60 = [s.up_mid for s in h60]
    mids_20 = [s.up_mid for s in h20]
    obis_60 = [s.obi    for s in h60]

    z60,  vol60  = _stats(mids_60, up_mid)
    z20,  vol20  = _stats(mids_20, up_mid)
    oz60, ovol60 = _stats(obis_60, obi_cur)

    # Stale-book guard: if we have history but every snapshot has the same mid
    # price (CLOB WS frozen), vol60 == 0 with len > 1. This produces all-zero
    # rolling features which are out-of-distribution for the model. Skip.
    if len(h60) > 1 and vol60 == 0.0 and ovol60 == 0.0:
        return None

    mid_change = up_mid - mids_60[0] if mids_60 else 0.0

    dt  = datetime.now(timezone.utc)
    sec = dt.hour * 3600 + dt.minute * 60 + dt.second

    # obi_depth_slope: OLS slope of book imbalance across 8 tau depth levels.
    # NaN when the live book has fewer than 2 non-empty tau levels.
    tau_obis = [up_book.book_imbalance(tau) for tau in _TAU_PRICES]
    slope = _ols_slope(tau_obis)

    return [
        up_mid,
        z60,  vol60,
        z20,  vol20,
        mid_change,
        math.log1p(ubs + dbs),
        obi_cur, ovol60, oz60,
        up_ask_p - up_bid_p,
        math.sin(2 * math.pi * sec / 86400),
        math.cos(2 * math.pi * sec / 86400),
        slope,
    ]



async def evaluate_entry(
    runtime: ContractRuntime,
    state: CollectorState,
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

    um_str = f"{up_mid:.4f}" if up_mid is not None else "--"

    # Huber edge model: predict E[YES return] and E[NO return], trade the higher edge.
    feats = _build_features(runtime, up_book, down_book)
    if args.log_features and feats is not None:
        feat_str = "  ".join(f"{n}={v:.7f}" for n, v in zip(FEATURES, feats))
        append_log(f"FEATURES {runtime.market.slug} | {feat_str}", prefix_timestamp=False)
    if feats is None or up_mid is None:
        counts.skipped += 1
        h60_len = sum(1 for s in runtime.history if time.time() - s.timestamp <= 60.0)
        mids = [s.up_mid for s in runtime.history if time.time() - s.timestamp <= 60.0]
        if h60_len > 1 and len(set(mids)) == 1:
            reason = "stale book: CLOB WS frozen (all history identical)"
        else:
            reason = "feature extraction failed (missing book data)"
        runtime.decision = TradeDecision(status="skip", contracts=0, dry_run=not args.live, reason=reason)
        append_log(
            f"STATUS T={remaining:.1f}s {runtime.market.slug} | up_mid={um_str} decision=SKIP {reason} | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({**base_row, "order_status": "skip", "reason": reason, "skipped_count": counts.skipped})
        return

    pred_yes = float(_model_yes.predict([feats])[0])
    pred_no  = float(_model_no.predict([feats])[0])

    # Pure edge rule, no post-hoc filters: take the side with the higher predicted edge
    # if it clears SKIP_BONUS.
    if pred_no > SKIP_BONUS and pred_no >= pred_yes:
        action = "NO"
    elif pred_yes > SKIP_BONUS and pred_yes > pred_no:
        action = "YES"
    else:
        counts.skipped += 1
        reason = f"edge below threshold: pred_no={pred_no:+.4f} pred_yes={pred_yes:+.4f}"
        runtime.decision = TradeDecision(status="skip", contracts=0, dry_run=not args.live, reason=reason)
        append_log(
            f"STATUS T={remaining:.1f}s {runtime.market.slug} | up_mid={um_str} "
            f"pred_yes={pred_yes:+.3f} pred_no={pred_no:+.3f} decision=SKIP {reason} | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({**base_row, "order_status": "skip", "reason": reason, "skipped_count": counts.skipped})
        return

    side = action
    if action == "NO":
        token_id  = runtime.market.down_token_id
        ask_price = down_ask_p
        ask_qty   = down_ask_q
    else:
        token_id  = runtime.market.up_token_id
        ask_price = up_ask_p
        ask_qty   = up_ask_q

    if ask_price is None or not (0.0 < ask_price < 1.0):
        counts.skipped += 1
        reason = f"{action}_ask missing or out of range: {ask_price}"
        runtime.decision = TradeDecision(status="skip", contracts=0, dry_run=not args.live, reason=reason)
        append_log(
            f"ORDER SKIP {runtime.market.slug} {side} {reason} | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({**base_row, "selected_side": side, "order_status": "skip", "reason": reason, "skipped_count": counts.skipped})
        return

    # Ensure total order value >= $1.00 (Polymarket minimum).
    # With small contract_value or high ask, round() alone can produce 1 contract
    # at e.g. $0.86 which is rejected.  ceil(1/ask) guarantees the minimum.
    n_contracts = max(math.ceil(1.0 / ask_price), round(args.contract_value / ask_price))
    base_row["contracts"] = n_contracts

    edge_acted = pred_no if action == "NO" else pred_yes
    append_log(
        f"STATUS T={remaining:.1f}s {runtime.market.slug} | up_mid={um_str} "
        f"pred_yes={pred_yes:+.3f} pred_no={pred_no:+.3f} decision=BUY_{action} edge={edge_acted:+.4f} "
        f"ask={fmt_pct(ask_price)} n={n_contracts} val=${args.contract_value:.2f}",
        prefix_timestamp=False,
    )

    # Retry loop: up to MAX_ORDER_ATTEMPTS FOK attempts, each using a fresh best-ask.
    order_status = "error"
    order_reason = "no attempts made"
    fill_price: float | None = None
    filled_size: float = 0.0
    order_id = ""
    resp: dict[str, Any] = {}
    for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
        # Refresh best ask from live book on every attempt.
        async with state.lock:
            cur_book = state.books.get(token_id)
        if cur_book is not None:
            fresh_ask, _ = cur_book.best_ask()
            if fresh_ask is not None and 0.0 < fresh_ask < 1.0:
                ask_price = fresh_ask
                n_contracts = max(math.ceil(1.0 / ask_price), round(args.contract_value / ask_price))
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
    if order_status in ("dry_run",):
        order_id = str(resp.get("order_id", ""))
    outcome_eligible = order_status in ("dry_run", "filled") and filled_size >= n_contracts

    runtime.decision = TradeDecision(
        status=order_status,
        side=side,
        token_id=token_id,
        selected_ask=ask_price,
        selected_ask_qty=ask_qty,
        contracts=n_contracts,
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

        # Dollar P&L: n_contracts × (outcome − fill_price) minus taker fee
        fp = decision.fill_price or decision.selected_ask or 0.0
        fs = decision.filled_size or float(decision.contracts)
        fee_per_contract = math.ceil(0.07 * fp * (1.0 - fp) * 100) / 100
        gross_pnl = fs * (1.0 - fp) if pred_won else -(fs * fp)
        dollar_pnl = gross_pnl - fs * fee_per_contract
        counts.total_pnl += dollar_pnl

        if decision.side == "YES":
            if pred_won: counts.yes_won  += 1
            else:        counts.yes_lost += 1
        elif decision.side == "NO":
            if pred_won: counts.no_won   += 1
            else:        counts.no_lost  += 1

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
        f"{_counts_str(counts, args.contract_value)}",
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
        "successful_count": counts.successful,
        "unsuccessful_count": counts.unsuccessful,
        "skipped_count": counts.skipped,
        "dollar_pnl": f"{dollar_pnl:.4f}" if decision and decision.outcome_eligible else "",
        "reason": latest,
    })
    runtime.outcome_logged = True
    completed_seen.add(runtime.market.slug)
    return True, None


# ---------------------------------------------------------------------------
# Session rotation — backup and clear history files on each run
# ---------------------------------------------------------------------------

def _rotate_session_files() -> None:
    """Backup log and trades files from the previous session, then remove them.

    CSV files are deleted (not truncated) so append_trade_row / append_portfolio_row
    see exists=False and write a fresh header on the next call.
    The log file is also deleted; append_log creates it on first write.
    """
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
    global _model_yes, _model_no
    for p in (MODEL_YES_PATH, MODEL_NO_PATH):
        if not p.exists():
            raise RuntimeError(f"Model file not found: {p}  — copy huber_yes_t245.txt / huber_no_t245.txt into polymarket/")
    _model_yes = lgb.Booster(model_file=str(MODEL_YES_PATH))
    _model_no  = lgb.Booster(model_file=str(MODEL_NO_PATH))
    _yes_md5 = hashlib.md5(MODEL_YES_PATH.read_bytes()).hexdigest()
    _no_md5  = hashlib.md5(MODEL_NO_PATH.read_bytes()).hexdigest()

    _rotate_session_files()
    mode_label = "*** LIVE TRADING ***" if args.live else "--- DRY TESTING ---"
    append_log(
        f"START {mode_label} polymarket_5m_trader contract_value=${args.contract_value:.2f} "
        f"entry_seconds={args.entry_seconds} tolerance={args.entry_tolerance}s "
        f"outcome_delay={args.outcome_delay_seconds}s "
        f"stop_loss={fmt_money(args.stop_loss)} "
        f"model=huber_edge_t245_combined[yes,no] skip_bonus={SKIP_BONUS} filters=none "
        f"model_md5_yes={_yes_md5[:12]} model_md5_no={_no_md5[:12]}"
    )

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
                        contract_spot = state.spot.price

                    await load_initial_books(client, state, market)
                    runtime = ContractRuntime(market=market)

                    if clob_task and not clob_task.done():
                        clob_task.cancel()
                        await asyncio.gather(clob_task, return_exceptions=True)
                    clob_task = asyncio.create_task(clob_ws_loop(state, market, stop))

                    contract_spot_str = f"{contract_spot:.2f}" if contract_spot is not None else "--"
                    contract_tgt_str = f"{market.price_target:.2f}" if market.price_target is not None else "--"

                    append_log("", prefix_timestamp=False)
                    append_log(
                        f"CONTRACT {market.slug} | "
                        f"close {iso_from_ms(market.end_ts * 1000)} | "
                        f"spot {contract_spot_str} target {contract_tgt_str}",
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
                        ubs_live = ubq or 0.0
                        dbs_live = dbq or 0.0
                        obi_live = (ubs_live - dbs_live) / (ubs_live + dbs_live + 1e-9)
                        snap = QuoteSnapshot(
                            timestamp=time.time(),
                            up_mid=up_mid_live,
                            obi=obi_live,
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
                        spot_price_log = state.spot.price
                    status_str = runtime.decision.status if runtime.decision else "--"
                    um_str = f"{up_mid_log:.4f}" if up_mid_log is not None else "--"
                    spot_str = f"{spot_price_log:.2f}" if spot_price_log is not None else "--"
                    tgt_str = f"{market.price_target:.2f}" if market.price_target is not None else "--"
                    mode_str = "LIVE" if args.live else "DRY"
                    append_log(
                        f"STATUS [{mode_str}] T={remaining:.1f}s | up_mid={um_str} "
                        f"spot={spot_str} target={tgt_str} trade={status_str} | "
                        f"{_counts_str(counts, args.contract_value)}",
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
                    await evaluate_entry(runtime, state, counts, args, remaining)
                elif remaining < lower and not runtime.decision_logged and remaining >= 0:
                    # Missed entry window
                    runtime.decision_logged = True
                    reason = f"missed entry window ({lower:.0f}<=T<={upper:.0f}s); T={remaining:.1f}s"
                    counts.skipped += 1
                    runtime.decision = TradeDecision(
                        status="skip", contracts=0, dry_run=not args.live, reason=reason
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
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m Up/Down live trader — Huber edge-regression model at T=180s.")
    parser.add_argument("--live", action="store_true", help="Submit real orders. Omit for dry-run.")
    parser.add_argument("--contract-value", type=float, default=1.05, help="Dollar value to spend per trade. Contracts = round(value / ask_price). Default: 1.05.")
    parser.add_argument("--entry-seconds", type=float, default=245.0, help="Entry time before close (seconds). Default: 245.")
    parser.add_argument("--entry-tolerance", type=float, default=5.0, help="Entry window tolerance (seconds). Default: 5.")
    parser.add_argument("--poll-interval", type=float, default=0.5, help="Poll interval (seconds). Default: 0.5.")
    parser.add_argument("--log-interval", type=float, default=30.0, help="Seconds between status log lines. Default: 30.")
    parser.add_argument("--stop-loss", type=float, default=30.0, help="Stop if balance drops this many USD. Default: 30.")
    parser.add_argument("--outcome-delay-seconds", type=float, default=OUTCOME_DELAY_SECONDS,
                        help="Check outcome this many seconds after close. Default: -270 (4.5 min; observed resolution ~5.5 min).")
    parser.add_argument("--log-features", action="store_true",
                        help="Log the 14 feature values at each entry decision.")
    args = parser.parse_args()
    args.contract_value = max(1.0, args.contract_value)
    args.entry_tolerance = max(0.0, args.entry_tolerance)
    args.poll_interval = max(0.1, args.poll_interval)
    return args


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
