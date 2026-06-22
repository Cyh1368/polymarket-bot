#!/usr/bin/env python3
"""
Polymarket XRP 5m Up/Down — Mean-Reversion Maker Trader

Strategy (pre-registered 2026-06-21):
  At T=180s before close:
    - Compute logit_mid + hf_ret_60s (60s log-return of XRP/USD from Kraken HF)
    - Run L2 logistic regression (C=1.0, trained on Jun 8-15 non-lockbox data)
    - EV_yes = P(up) - (up_ask + TAKER_FEE); EV_no = (1-P(up)) - (down_ask + TAKER_FEE)
    - If max(EV_yes, EV_no) > TAU_SKIP=0.005: post GTC limit at up_mid or down_mid
    - Heartbeat every 10s; cancel outstanding order at T-10s if unfilled

Usage:
  python polymarket_xrp_maker_trader.py            # dry run (no real orders)
  python polymarket_xrp_maker_trader.py --live     # live trading

Credentials (in kalshi/.env or polymarket/.env or environment):
  POLYMARKET_PRIVATE_KEY   your wallet private key (0x...)
  POLYMARKET_ADDRESS       your proxy wallet address on Polygon (optional: EOA mode if absent)
  CLOB_API_KEY / CLOB_SECRET / CLOB_PASS_PHRASE   optional cached L2 credentials

The live Kraken HF spot feed is sourced from the running kraken_hf_collector.py process
(reads kraken_hf/trades_XRP.csv). Alternatively, subscribes to Kraken WS v2 directly.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import pickle
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import websockets
import logging
logging.getLogger("py_clob_client_v2").setLevel(logging.CRITICAL)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COIN               = "XRP"
COIN_SLUG_PREFIX   = "xrp"
SPOT_SYMBOL        = "XRP/USD"     # Kraken WS v2 pair
CLOB_BASE_URL      = "https://clob.polymarket.com"
GAMMA_BASE_URL     = "https://gamma-api.polymarket.com"
CLOB_MARKET_WS     = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_USER_WS       = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
KRAKEN_WS          = "wss://ws.kraken.com/v2"
POLYMARKET_CHAIN_ID = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

ENTRY_HORIZON      = 180.0   # seconds before close to enter
ENTRY_WINDOW       = 5.0     # ±5s tolerance
CANCEL_AT          = 10.0    # cancel open order this many seconds before close
TAU_SKIP           = 0.005   # minimum EV to trade
TAKER_FEE          = 0.01    # additional cost buffer vs maker (conservative)
MAKER_FEE          = 0.005   # actual maker fee (for PnL logging)
HF_WINDOW_MS       = 60_000  # 60-second spot lookback

HEARTBEAT_INTERVAL = 8.0     # seconds between heartbeats (must be < 10s)

APP_DIR   = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
MODEL_PATH = APP_DIR / "xrp_maker_model.pkl"
HF_CSV     = REPO_ROOT / "kraken_hf" / f"trades_{COIN}.csv"

LOG_PATH    = APP_DIR / "xrp_maker_trader.log"
TRADES_CSV  = APP_DIR / "xrp_maker_trades.csv"
LIVE_JSON   = APP_DIR / "xrp_maker_live.json"   # for display server

TRADE_FIELDS = [
    "timestamp_utc", "event", "condition_id", "market_slug", "close_time_utc",
    "seconds_to_close_at_entry", "up_mid", "up_ask", "down_ask",
    "hf_ret_60s", "p_up_model", "ev_yes", "ev_no",
    "side", "token_id", "limit_price", "size_shares",
    "dry_run", "order_id", "order_status",
    "fill_price", "filled_shares",
    "official_outcome", "net_pnl",
]

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_log_file: Any = None
_trades_writer: Any = None
_trades_file: Any = None
_SEARCHING_PREFIX = "Searching for active XRP 5m market"


def _erase_last_searching_line() -> None:
    """If the last line in the log file is a 'Searching…' line, remove it."""
    if not _log_file:
        return
    try:
        pos = _log_file.seek(0, 2)          # go to end
        if pos == 0:
            return
        # Read up to 256 bytes from end to find last newline
        read_back = min(pos, 256)
        _log_file.seek(pos - read_back)
        chunk = _log_file.read(read_back)
        lines = chunk.split(b"\n") if isinstance(chunk, bytes) else chunk.encode().split(b"\n")
        # Last element after split is "" (trailing newline), second-to-last is the last line
        last_line = (lines[-2] if len(lines) >= 2 else lines[-1]).decode("utf-8", errors="replace")
        if _SEARCHING_PREFIX in last_line:
            # Truncate to remove that line (+1 for its newline)
            new_pos = pos - len(last_line.encode()) - 1
            _log_file.truncate(max(0, new_pos))
            _log_file.seek(0, 2)
    except Exception:
        pass


def log(msg: str, *, overwrite_searching: bool = False) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    line = f"{ts} {msg}"
    if overwrite_searching and _log_file:
        _erase_last_searching_line()
    print(line, flush=True)
    if _log_file:
        _log_file.write(line + "\n")
        _log_file.flush()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Load model
# ---------------------------------------------------------------------------

def load_model() -> dict:
    with open(MODEL_PATH, "rb") as f:
        bundle = pickle.load(f)
    log(f"Model loaded: trained on {bundle['n_train']} rows, {len(bundle['train_days'])} days, "
        f"coef(logit_mid)={bundle['model'].coef_[0][0]:+.4f} "
        f"coef(hf_ret_60s)={bundle['model'].coef_[0][1]:+.4f}")
    return bundle


def predict_p_up(bundle: dict, up_mid: float, hf_ret_60s: float) -> float:
    from scipy.special import logit
    lm = logit(max(1e-6, min(1 - 1e-6, up_mid)))
    X = bundle["scaler"].transform([[lm, hf_ret_60s]])
    return float(bundle["model"].predict_proba(X)[0, 1])


# ---------------------------------------------------------------------------
# Live HF spot from Kraken WS v2
# ---------------------------------------------------------------------------

@dataclass
class SpotState:
    prices: list[tuple[float, float]] = field(default_factory=list)  # (ts_ms, price)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def add(self, ts_ms: float, price: float) -> None:
        async with self.lock:
            self.prices.append((ts_ms, price))
            # prune older than 5 minutes
            cutoff = ts_ms - 300_000
            self.prices = [(t, p) for t, p in self.prices if t >= cutoff]

    async def ret_60s(self, now_ms: float) -> float | None:
        """60-second log-return: log(price_now / price_60s_ago).
        If no price exists from 60s ago, uses oldest available (handles startup warmup)."""
        async with self.lock:
            if not self.prices:
                return None
            now_prices = [(t, p) for t, p in self.prices if t <= now_ms]
            if not now_prices:
                return None
            p_now = now_prices[-1][1]
            ago_prices = [(t, p) for t, p in self.prices if t <= now_ms - HF_WINDOW_MS]
            if ago_prices:
                p_ago = ago_prices[-1][1]
            else:
                # Use oldest available price (startup warmup period)
                p_ago = self.prices[0][1]
            if p_ago <= 0:
                return None
            return float(np.log(p_now / p_ago))


def seed_spot_from_csv(spot: SpotState) -> int:
    """Pre-load last 5 minutes of XRP prices from local HF CSV (if available)."""
    count = 0
    for csv_path in [HF_CSV, REPO_ROOT / "kraken_hf" / f"trades_{COIN}_backfill.csv"]:
        if not csv_path.exists():
            continue
        try:
            import pandas as pd
            df = pd.read_csv(csv_path, usecols=["kraken_ts_ms", "price"])
            cutoff = time.time() * 1000 - 300_000  # last 5 min
            df = df[df["kraken_ts_ms"] >= cutoff]
            for _, row in df.iterrows():
                spot.prices.append((float(row["kraken_ts_ms"]), float(row["price"])))
            count += len(df)
            if count > 0:
                break
        except Exception:
            pass
    if count:
        spot.prices.sort(key=lambda x: x[0])
    return count


async def run_kraken_spot(spot: SpotState) -> None:
    """Stream Kraken WS v2 trades for XRP/USD and update SpotState."""
    sub_msg = json.dumps({
        "method": "subscribe",
        "params": {"channel": "trade", "symbol": [SPOT_SYMBOL]},
    })
    while True:
        try:
            async with websockets.connect(KRAKEN_WS, ping_interval=20) as ws:
                await ws.send(sub_msg)
                log(f"Kraken WS: subscribed to {SPOT_SYMBOL} trades")
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("channel") != "trade" or msg.get("type") != "update":
                        continue
                    for trade in msg.get("data", []):
                        raw_ts = trade.get("timestamp", 0)
                        try:
                            # Kraken WS v2 sends ISO string e.g. "2026-06-21T21:17:03.357Z"
                            if isinstance(raw_ts, str):
                                ts_ms = datetime.fromisoformat(
                                    raw_ts.replace("Z", "+00:00")).timestamp() * 1000
                            else:
                                ts_ms = float(raw_ts) * (1 if float(raw_ts) > 1e12 else 1000)
                        except Exception:
                            ts_ms = time.time() * 1000
                        price = float(trade["price"])
                        await spot.add(ts_ms, price)
        except Exception as exc:
            log(f"Kraken WS error: {exc} — reconnecting in 5s")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

async def find_active_market(client: httpx.AsyncClient) -> dict | None:
    """Find the currently active XRP 5m Up/Down market via Gamma API events endpoint.

    Uses timestamp-slot discovery (same as the data collector): tries slugs
    xrp-updown-5m-<slot> for the current 5m slot and adjacent ones.
    """
    FIVE_MIN = 300
    now_ts   = time.time()
    current_slot = int(now_ts) // FIVE_MIN * FIVE_MIN
    offsets = [0, 1, -1, 2, -2, 3, -3]

    for offset in offsets:
        slot = current_slot + offset * FIVE_MIN
        if slot < now_ts - FIVE_MIN:
            continue  # already closed
        slug = f"{COIN_SLUG_PREFIX}-updown-5m-{slot}"
        try:
            resp = await client.get(f"{GAMMA_BASE_URL}/events/slug/{slug}", timeout=8)
            if resp.status_code != 200:
                continue
            event = resp.json()
            markets = event.get("markets", [])
            if not markets:
                continue
            m = markets[0]
            # Normalise close time — Gamma puts it on the market as "endDate" (full ISO)
            close_iso = (m.get("endDate") or event.get("endDate") or
                         m.get("end_date_iso") or m.get("endDateIso") or "")
            m["end_date_iso"] = close_iso
            m["slug"] = slug
            # Verify it hasn't already closed
            try:
                close_s = datetime.fromisoformat(close_iso.replace("Z","+00:00")).timestamp()
                if close_s < time.time() - 30:
                    continue   # already settled
            except Exception:
                pass
            return m
        except Exception:
            continue
    return None


async def get_market_clob_info(client: httpx.AsyncClient, condition_id: str) -> dict | None:
    resp = await client.get(
        f"{CLOB_BASE_URL}/markets/{condition_id}",
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    return resp.json()


# ---------------------------------------------------------------------------
# CLOB order book state
# ---------------------------------------------------------------------------

@dataclass
class BookState:
    up_bid: float = 0.0
    up_ask: float = 1.0
    up_mid: float = 0.5
    up_bid_qty: float = 0.0
    up_ask_qty: float = 0.0
    down_bid: float = 0.0
    down_ask: float = 1.0
    down_mid: float = 0.5
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    updated_at: float = 0.0

    async def update(self, token_id: str, up_token_id: str, bids: list, asks: list) -> None:
        """Update from a 'book' event: bids/asks are lists of {price, size} dicts."""
        async with self.lock:
            def best(levels: list, side: str):
                valid = [(float(l["price"]), float(l["size"]))
                         for l in levels if isinstance(l, dict)
                         and float(l.get("size", 0)) > 0]
                if not valid:
                    return (0.0, 0.0) if side == "bid" else (1.0, 0.0)
                return max(valid) if side == "bid" else min(valid)

            bb, bbq = best(bids, "bid")
            ba, baq = best(asks, "ask")
            mid = round((bb + ba) / 2, 4)

            if token_id == up_token_id:
                self.up_bid, self.up_ask, self.up_mid = bb, ba, mid
                self.up_bid_qty, self.up_ask_qty = bbq, baq
                self.down_ask = round(1 - bb, 4)
                self.down_bid = round(1 - ba, 4)
                self.down_mid = round(1 - mid, 4)
            else:
                self.down_bid, self.down_ask, self.down_mid = bb, ba, mid
                self.up_ask = round(1 - bb, 4)
                self.up_bid = round(1 - ba, 4)
                self.up_mid = round(1 - mid, 4)
            self.updated_at = time.time()

    async def apply_price_change(self, token_id: str, up_token_id: str, chg: dict) -> None:
        """Update from a 'price_change' event: single side+price+size delta."""
        async with self.lock:
            side  = str(chg.get("side","")).upper()
            price = chg.get("price")
            fa    = chg.get("best_ask") or chg.get("bestAsk")
            fb    = chg.get("best_bid") or chg.get("bestBid")
            if fa is not None and fb is not None:
                bb, ba = float(fb), float(fa)
                mid = round((bb + ba) / 2, 4)
                if token_id == up_token_id:
                    self.up_bid, self.up_ask, self.up_mid = bb, ba, mid
                    self.down_ask = round(1-bb,4); self.down_bid = round(1-ba,4); self.down_mid = round(1-mid,4)
                else:
                    self.down_bid, self.down_ask, self.down_mid = bb, ba, mid
                    self.up_ask = round(1-bb,4); self.up_bid = round(1-ba,4); self.up_mid = round(1-mid,4)
                self.updated_at = time.time()


async def run_clob_book(book: BookState, condition_id: str,
                        up_token_id: str, down_token_id: str) -> None:
    sub = json.dumps({
        "assets_ids": [up_token_id, down_token_id],
        "type": "market",
    })
    while True:
        try:
            async with websockets.connect(CLOB_MARKET_WS, ping_interval=20) as ws:
                await ws.send(sub)
                log(f"CLOB book WS: subscribed to {condition_id}")
                async for raw in ws:
                    msgs = json.loads(raw)
                    # CLOB WS sends a list of event objects
                    if not isinstance(msgs, list):
                        msgs = [msgs]
                    for msg in msgs:
                        if not isinstance(msg, dict):
                            continue
                        event_type = msg.get("event_type", "")
                        token_id   = msg.get("asset_id") or msg.get("token_id") or ""
                        if event_type == "book" and token_id in (up_token_id, down_token_id):
                            bids = msg.get("bids", [])
                            asks = msg.get("asks", [])
                            await book.update(token_id, up_token_id, bids, asks)
                        elif event_type == "price_change":
                            changes = msg.get("price_changes") or [msg]
                            for chg in changes:
                                tid = chg.get("asset_id","")
                                if tid in (up_token_id, down_token_id):
                                    await book.apply_price_change(tid, up_token_id, chg)
                        elif event_type == "best_bid_ask":
                            tid = msg.get("asset_id","")
                            if tid in (up_token_id, down_token_id):
                                await book.apply_price_change(tid, up_token_id, msg)
        except Exception as exc:
            log(f"CLOB book WS error: {exc} — reconnecting in 3s")
            await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Polymarket CLOB client (reuses py_clob_client_v2 pattern from existing traders)
# ---------------------------------------------------------------------------

def _load_env() -> None:
    for env_file in [REPO_ROOT / "kalshi" / ".env", APP_DIR / ".env",
                     REPO_ROOT / ".env"]:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _polymarket_client() -> Any:
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2 as ST
    except ImportError as exc:
        raise RuntimeError("py_clob_client_v2 not installed — use local venv") from exc
    key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PK")
    if not key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY not set")
    funder = os.getenv("POLYMARKET_ADDRESS") or os.getenv("POLYMARKET_FUNDER")
    kwargs: dict[str, Any] = {"host": CLOB_BASE_URL, "chain_id": POLYMARKET_CHAIN_ID, "key": key}
    if funder:
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


def place_gtc_limit(token_id: str, price: float, size: float, *, dry_run: bool) -> dict:
    """Post a GTC limit BUY order at price (market-making: rest at mid)."""
    if dry_run:
        return {"status": "dry_run", "order_id": f"dry-{uuid.uuid4().hex[:12]}",
                "token_id": token_id, "price": price, "size": size}
    try:
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
        client = _polymarket_client()
        resp = client.create_and_post_order(
            order_args=OrderArgs(token_id=token_id, price=price,
                                 side=Side.BUY, size=float(size)),
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.GTC,
        )
        if not isinstance(resp, dict):
            resp = {"raw": str(resp)}
        return resp
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def cancel_order_by_id(order_id: str) -> dict:
    try:
        client = _polymarket_client()
        return client.cancel(order_id) or {}
    except Exception as exc:
        return {"error": str(exc)}


def send_heartbeat() -> None:
    key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PK")
    if not key:
        return  # skip heartbeat in dry-run without credentials
    try:
        client = _polymarket_client()
        client.post_heartbeat()
    except Exception:
        pass


def get_balance() -> float | None:
    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        client = _polymarket_client()
        data = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        raw = data.get("balance") or data.get("usdc_balance") or 0
        return float(raw) / 1_000_000.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Trade logging
# ---------------------------------------------------------------------------

def init_trades_csv() -> None:
    global _trades_writer, _trades_file
    new_file = not TRADES_CSV.exists()
    _trades_file = open(TRADES_CSV, "a", newline="")
    _trades_writer = csv.DictWriter(_trades_file, fieldnames=TRADE_FIELDS, extrasaction="ignore")
    if new_file:
        _trades_writer.writeheader()


def log_trade(row: dict) -> None:
    if _trades_writer:
        _trades_writer.writerow(row)
        _trades_file.flush()


def update_live_json(state: dict) -> None:
    try:
        LIVE_JSON.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session statistics
# ---------------------------------------------------------------------------

@dataclass
class SessionStats:
    start_time: float = field(default_factory=time.time)
    contracts_seen: int = 0
    trades_attempted: int = 0
    trades_filled: int = 0
    trades_skipped: int = 0
    pnl_realized: float = 0.0
    open_position: dict | None = None   # {condition_id, side, limit_price, size, order_id}

    def to_dict(self) -> dict:
        return {
            "start_time_utc": iso_utc(datetime.fromtimestamp(self.start_time, tz=timezone.utc)),
            "uptime_s": int(time.time() - self.start_time),
            "contracts_seen": self.contracts_seen,
            "trades_attempted": self.trades_attempted,
            "trades_filled": self.trades_filled,
            "trades_skipped": self.trades_skipped,
            "fill_rate": self.trades_filled / max(self.trades_attempted, 1),
            "pnl_realized": round(self.pnl_realized, 4),
            "open_position": self.open_position,
        }


# ---------------------------------------------------------------------------
# Main trading loop
# ---------------------------------------------------------------------------

async def trade_contract(
    market: dict,
    clob_info: dict,
    book: BookState,
    spot: SpotState,
    bundle: dict,
    stats: SessionStats,
    *,
    dry_run: bool,
    size_shares: float,
) -> None:
    """Manage one XRP 5m contract: wait for T=180s, decide, post limit, track fill."""

    condition_id  = clob_info.get("condition_id") or market.get("conditionId", "")
    market_slug   = market.get("slug") or market.get("market_slug", "")
    close_iso = (market.get("end_date_iso") or market.get("endDate") or
                 market.get("event_end_utc") or market.get("endDateIso") or "")
    try:
        close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        close_time_s = close_dt.timestamp()
    except Exception:
        log(f"SKIP {market_slug}: cannot parse close time from '{close_iso}'")
        return

    tokens = clob_info.get("tokens", [])
    up_token_id   = next((t["token_id"] for t in tokens if t.get("outcome") in ("Yes","Up")), None)
    down_token_id = next((t["token_id"] for t in tokens if t.get("outcome") in ("No","Down")), None)
    if not up_token_id or not down_token_id:
        log(f"SKIP {market_slug}: missing token IDs")
        return

    stats.contracts_seen += 1
    log(f"Watching {market_slug}  close={iso_utc(close_dt)}  up_token={up_token_id[:12]}…")

    order_id    = None
    order_side  = None
    limit_price = None
    entered     = False
    last_pmid_log = 0.0  # track last p_mid log time

    while True:
        now_s = time.time()
        secs_to_close = close_time_s - now_s

        if secs_to_close < -30:
            # Contract well past close — done
            break

        # ── Periodic p_mid log (every 60s while watching) ──
        if not entered and now_s - last_pmid_log >= 60 and book.updated_at > 0:
            async with book.lock:
                um = book.up_mid
            log(f"{market_slug} T={secs_to_close:.0f}s  p_mid={um:.3f}")
            last_pmid_log = now_s

        # ── Cancel window: T < CANCEL_AT and we have an open order ──
        if secs_to_close < CANCEL_AT and order_id and not entered:
            log(f"{market_slug} T={secs_to_close:.0f}s: cancelling unfilled order {order_id}")
            if not dry_run:
                cancel_order_by_id(order_id)
            order_id = None
            break

        # ── Entry window: T in [ENTRY_HORIZON-ENTRY_WINDOW, ENTRY_HORIZON+ENTRY_WINDOW] ──
        if not entered and abs(secs_to_close - ENTRY_HORIZON) <= ENTRY_WINDOW:
            now_ms = now_s * 1000
            hf_ret = await spot.ret_60s(now_ms)
            if hf_ret is None:
                log(f"{market_slug} T={secs_to_close:.0f}s: no HF spot data — skip")
                stats.trades_skipped += 1
                break

            async with book.lock:
                um   = book.up_mid
                ua   = book.up_ask
                da   = book.down_ask

            if book.updated_at == 0 or time.time() - book.updated_at > 30:
                log(f"{market_slug}: stale book — skip")
                stats.trades_skipped += 1
                break

            p_up   = predict_p_up(bundle, um, hf_ret)
            ev_yes = p_up - (ua + TAKER_FEE)
            ev_no  = (1 - p_up) - (da + TAKER_FEE)
            ev_max = max(ev_yes, ev_no)

            log(f"{market_slug} T={secs_to_close:.0f}s  um={um:.3f}  hf60s={hf_ret*100:+.3f}%  "
                f"p_up={p_up:.3f}  EV_yes={ev_yes:+.4f}  EV_no={ev_no:+.4f}")

            if ev_max <= TAU_SKIP:
                log(f"{market_slug}: EV={ev_max:+.4f} below tau={TAU_SKIP} — skip")
                stats.trades_skipped += 1
                break

            if ev_yes >= ev_no:
                order_side = "YES"
                token_id   = up_token_id
                limit_price = round(max(0.01, min(0.99, um)), 2)
            else:
                order_side = "NO"
                token_id   = down_token_id
                limit_price = round(max(0.01, min(0.99, 1 - um)), 2)

            log(f"{market_slug}: posting GTC {order_side} at {limit_price:.2f}  size={size_shares}  "
                f"{'DRY-RUN' if dry_run else 'LIVE'}")

            resp = place_gtc_limit(token_id, limit_price, size_shares, dry_run=dry_run)
            order_id = resp.get("order_id") or resp.get("id") or ""
            err = resp.get("error")

            if err:
                log(f"{market_slug}: order error — {err}")
                stats.trades_skipped += 1
                break

            stats.trades_attempted += 1
            stats.open_position = {
                "condition_id": condition_id, "slug": market_slug,
                "side": order_side, "limit_price": limit_price,
                "size": size_shares, "order_id": order_id,
            }
            entered = True

            log_trade({
                "timestamp_utc": iso_utc(),
                "event": "order_placed",
                "condition_id": condition_id,
                "market_slug": market_slug,
                "close_time_utc": iso_utc(close_dt),
                "seconds_to_close_at_entry": round(secs_to_close, 1),
                "up_mid": um, "up_ask": ua, "down_ask": da,
                "hf_ret_60s": round(hf_ret, 6),
                "p_up_model": round(p_up, 4),
                "ev_yes": round(ev_yes, 5), "ev_no": round(ev_no, 5),
                "side": order_side, "token_id": token_id,
                "limit_price": limit_price, "size_shares": size_shares,
                "dry_run": dry_run, "order_id": order_id, "order_status": "open",
            })

        await asyncio.sleep(0.5)

    # Contract closed — wait for outcome then log PnL
    if entered:
        await asyncio.sleep(60)  # wait ~1 min after close for settlement
        if dry_run:
            # Assume full fill at limit price for dry-run PnL simulation
            order_info = {}
            filled  = float(size_shares)
            fill_px = float(limit_price or 0)
            status  = "dry_run"
        else:
            try:
                client = _polymarket_client()
                order_info = client.get_order(order_id) if order_id else {}
            except Exception:
                order_info = {}
            filled  = float(order_info.get("size_matched") or 0)
            fill_px = float(order_info.get("average_price") or limit_price or 0)
            status  = str(order_info.get("status", "unknown"))

        # Try to get official outcome from CLOB trades (retry up to 3 times, 60s apart)
        official_outcome = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient() as http:
                    r = await http.get(f"{CLOB_BASE_URL}/data/trades",
                                       params={"market": condition_id, "taker_only": "false"},
                                       timeout=10)
                    if r.status_code == 200:
                        trades_data = r.json()
                        for t in (trades_data.get("data") or trades_data or []):
                            price_val = float(t.get("price") or 0)
                            if price_val >= 0.99:
                                official_outcome = 1
                            elif price_val <= 0.01:
                                official_outcome = 0
            except Exception:
                pass
            if official_outcome is not None:
                break
            if attempt < 2:
                log(f"{market_slug}: outcome not yet available — retrying in 60s")
                await asyncio.sleep(60)

        if filled > 0 and official_outcome is not None:
            # Compute net PnL
            if order_side == "YES":
                gross = (1.0 - fill_px) * official_outcome + (-fill_px) * (1 - official_outcome)
            else:
                gross = (1.0 - fill_px) * (1 - official_outcome) + (-fill_px) * official_outcome
            fee   = math.ceil(0.07 * fill_px * (1 - fill_px) * 100) / 100 + MAKER_FEE
            net_pnl_per = gross - fee
            net_pnl_total = net_pnl_per * filled
            stats.pnl_realized += net_pnl_total
            stats.trades_filled += 1
            correct = (order_side == "YES" and official_outcome == 1) or \
                      (order_side == "NO"  and official_outcome == 0)
            log(f"{market_slug}: SETTLED  filled={filled:.1f}  outcome={'Up' if official_outcome else 'Down'}  "
                f"correct={correct}  pnl={net_pnl_total:+.4f}  cumulative={stats.pnl_realized:+.4f}")
        elif filled == 0:
            log(f"{market_slug}: order expired unfilled (status={status})")
        else:
            log(f"{market_slug}: filled={filled:.1f} but outcome unknown yet")

        stats.open_position = None

        log_trade({
            "timestamp_utc": iso_utc(),
            "event": "settled",
            "condition_id": condition_id,
            "market_slug": market_slug,
            "order_status": status,
            "fill_price": fill_px,
            "filled_shares": filled,
            "official_outcome": official_outcome,
            "net_pnl": round(stats.pnl_realized, 6),
        })

    update_live_json(stats.to_dict())


async def run_heartbeat() -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        # Run blocking heartbeat in thread pool so it doesn't stall the event loop
        await asyncio.get_event_loop().run_in_executor(None, send_heartbeat)


async def main_loop(*, dry_run: bool, size_shares: float) -> None:
    bundle = load_model()
    spot   = SpotState()
    seeded = seed_spot_from_csv(spot)
    if seeded:
        log(f"Seeded {seeded} recent spot prices from local HF CSV")
    stats  = SessionStats()
    init_trades_csv()

    balance = get_balance()
    log(f"Starting XRP maker trader  dry_run={dry_run}  size={size_shares}  "
        f"balance={f'${balance:.2f}' if balance else 'unknown'}")

    # Background tasks: Kraken spot + heartbeat
    tasks: list[asyncio.Task] = [
        asyncio.create_task(run_kraken_spot(spot)),
        asyncio.create_task(run_heartbeat()),
    ]

    seen_conditions: set[str] = set()
    current_book_task: asyncio.Task | None = None
    current_condition: str = ""

    while True:
        try:
            log("Searching for active XRP 5m market…", overwrite_searching=True)
            # Fresh client each iteration — avoids stale connection pool hangs
            async with httpx.AsyncClient(timeout=12) as http:
                market = await asyncio.wait_for(find_active_market(http), timeout=30)
            if market is None:
                log("No active XRP 5m market found — waiting 15s")
                await asyncio.sleep(15)
                continue

            condition_id = (market.get("conditionId") or
                            market.get("condition_id") or "")
            if not condition_id:
                await asyncio.sleep(15)
                continue

            if condition_id in seen_conditions:
                # Already handled this contract, wait for next
                await asyncio.sleep(10)
                continue

            async with httpx.AsyncClient(timeout=12) as http:
                clob_info = await asyncio.wait_for(
                    get_market_clob_info(http, condition_id), timeout=20)
            if not clob_info:
                await asyncio.sleep(10)
                continue

            tokens        = clob_info.get("tokens", [])
            up_token_id   = next((t["token_id"] for t in tokens if t.get("outcome") in ("Yes","Up")), None)
            down_token_id = next((t["token_id"] for t in tokens if t.get("outcome") in ("No","Down")), None)
            if not up_token_id or not down_token_id:
                log(f"Could not find Up/Down token IDs in {[t.get('outcome') for t in tokens]} — skip")
                await asyncio.sleep(10)
                continue

            # Start new book subscription if contract changed
            if condition_id != current_condition:
                if current_book_task:
                    current_book_task.cancel()
                book = BookState()
                current_book_task = asyncio.create_task(
                    run_clob_book(book, condition_id, up_token_id, down_token_id)
                )
                tasks.append(current_book_task)
                current_condition = condition_id
                await asyncio.sleep(1)  # let book populate

            await trade_contract(market, clob_info, book, spot, bundle, stats,
                                 dry_run=dry_run, size_shares=size_shares)
            seen_conditions.add(condition_id)
            update_live_json(stats.to_dict())

        except Exception as exc:
            log(f"Main loop error: {exc}")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(description="XRP Polymarket Maker Trader")
    parser.add_argument("--live", action="store_true",
                        help="Place real orders (default: dry run)")
    parser.add_argument("--size", type=float, default=10.0,
                        help="Shares per order (default: 10 = ~$5 at mid=0.50)")
    args = parser.parse_args()

    global _log_file
    _log_file = open(LOG_PATH, "a")
    log(f"=== XRP Maker Trader starting  live={args.live}  size={args.size} ===")

    try:
        asyncio.run(main_loop(dry_run=not args.live, size_shares=args.size))
    except KeyboardInterrupt:
        log("Interrupted by user")
    finally:
        if _log_file:
            _log_file.close()
        if _trades_file:
            _trades_file.close()


if __name__ == "__main__":
    main()
