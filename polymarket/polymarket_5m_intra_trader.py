#!/usr/bin/env python3
"""Polymarket BTC 5m intra-period live trader — naive extreme-price strategy.

Entry at T1=180s before close:
  p_yes_mid < 0.25  →  buy Up   (YES) token at ask
  p_yes_mid > 0.75  →  buy Down (NO)  token at ask
  else              →  skip

Exit at T2=20s before close: sell held token at bid (FOK). If unfilled, keep
retrying at best bid until T=0 (contract expiry).

No model. Edge is structural mean-reversion in thin Polymarket 5m markets.
Multi-coin sweep (2026-06-12): BTC CI95 lower = +14.66 (best of 7 coins).

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
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import websockets


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAMMA_BASE_URL     = "https://gamma-api.polymarket.com"
CLOB_BASE_URL      = "https://clob.polymarket.com"
CLOB_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
COIN_SLUG_PREFIX   = "btc"
FIVE_MINUTE_SECONDS = 5 * 60
POLYMARKET_CHAIN_ID = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

T1_SECONDS    = 180.0   # entry time before close
T2_SECONDS    = 20.0    # exit time before close
EXTREME_LOW   = 0.25    # buy YES (up) if p_yes_mid < EXTREME_LOW
EXTREME_HIGH  = 0.75    # buy NO (down) if p_yes_mid > EXTREME_HIGH

COST_ADD         = 0.01
MAX_ORDER_ATTEMPTS = 10
ORDER_RETRY_DELAY  = 0.25

APP_DIR   = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

LOG_PATH = Path(
    os.getenv("POLYMARKET_INTRA_TRADER_LOG",
              str(APP_DIR / "polymarket_5m_btc_intra_trader.log"))
)
TRADES_CSV_PATH = Path(
    os.getenv("POLYMARKET_INTRA_TRADER_TRADES_CSV",
              str(APP_DIR / "polymarket_5m_btc_intra_trader_trades.csv"))
)
PORTFOLIO_CSV_PATH = Path(
    os.getenv("POLYMARKET_INTRA_TRADER_PORTFOLIO_CSV",
              str(APP_DIR / "polymarket_5m_btc_intra_trader_portfolio.csv"))
)

TRADE_FIELDS = [
    "timestamp_utc", "event", "contract_id", "close_time", "remaining_seconds",
    "entry_seconds", "exit_seconds", "p_yes_mid", "up_ask", "down_ask",
    "selected_side", "selected_token_id", "selected_ask",
    "contracts", "dry_run", "order_status", "order_id", "fill_price", "filled_size",
    "exit_bid", "exit_fill_price", "pnl_ratio",
    "correct", "successful_count", "unsuccessful_count", "skipped_count", "reason",
]
PORTFOLIO_FIELDS = [
    "timestamp_utc", "event", "remaining_seconds", "portfolio_value",
    "portfolio_available", "initial_balance", "drawdown_dollars",
]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def iso_utc(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


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
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TokenBook:
    token_id: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    timestamp_ms: int | None = None
    fallback_best_bid: float | None = None
    fallback_best_ask: float | None = None

    def replace_from_book(self, book: dict[str, Any]) -> None:
        self.bids = _parse_levels(book.get("bids"))
        self.asks = _parse_levels(book.get("asks"))
        self.timestamp_ms = parse_epoch_ms(book.get("timestamp")) or utc_now_ms()
        self.fallback_best_bid = None
        self.fallback_best_ask = None

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

    def apply_best_bid_ask(self, msg: dict[str, Any]) -> None:
        self.fallback_best_bid = finite_float(msg.get("best_bid"))
        self.fallback_best_ask = finite_float(msg.get("best_ask"))
        self.timestamp_ms = parse_epoch_ms(msg.get("timestamp")) or utc_now_ms()

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
class Counts:
    successful: int = 0
    unsuccessful: int = 0
    skipped: int = 0


@dataclass
class TradeDecision:
    status: str = ""
    side: str = ""
    token_id: str = ""
    selected_ask: float | None = None
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


# ---------------------------------------------------------------------------
# WebSocket
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
    outcomes  = [str(i) for i in _parse_json_list(market.get("outcomes"))]
    token_ids = [str(i) for i in _parse_json_list(market.get("clobTokenIds"))]
    if len(outcomes) != len(token_ids) or len(token_ids) < 2:
        return None
    tok = {o.lower(): t for o, t in zip(outcomes, token_ids)}
    up_token   = tok.get("up")   or tok.get("yes") or token_ids[0]
    down_token = tok.get("down") or tok.get("no")  or token_ids[1]
    start_dt = parse_dt(
        market.get("eventStartTime") or market.get("startDate") or event.get("startTime")
    )
    end_dt = parse_dt(market.get("endDate") or event.get("endDate"))
    if not start_dt or not end_dt:
        return None
    if not (start_dt <= datetime.now(timezone.utc) <= end_dt):
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


async def load_initial_books(
    client: httpx.AsyncClient, state: CollectorState, market: CurrentMarket
) -> None:
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
    up_book   = TokenBook(market.up_token_id)
    down_book = TokenBook(market.down_token_id)
    if market.up_token_id   in by_asset:
        up_book.replace_from_book(by_asset[market.up_token_id])
    if market.down_token_id in by_asset:
        down_book.replace_from_book(by_asset[market.down_token_id])
    async with state.lock:
        state.books = {market.up_token_id: up_book, market.down_token_id: down_book}


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def _polymarket_client() -> Any:
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient
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
        from py_clob_client_v2 import ClobClient as _C
        auth = _C(**kwargs)
        try:
            creds = auth.derive_api_key()
        except Exception:
            creds = auth.create_api_key()
    return ClobClient(**kwargs, creds=creds)


def polymarket_balance() -> tuple[float | None, float | None, str]:
    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        client = _polymarket_client()
        data = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        balance   = data.get("balance")   or data.get("usdc_balance")   or 0
        allowance = data.get("allowance") or data.get("usdc_allowance") or 0
        return float(balance) / 1_000_000.0, float(allowance) / 1_000_000.0, ""
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def _place(token_id: str, price: float, contracts: int, *, side_str: str, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {
            "status": "dry_run", "token_id": token_id,
            "price": price, "size": contracts,
            "order_id": f"dry-{side_str[:3]}-{uuid.uuid4().hex[:10]}",
        }
    try:
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
        side = Side.BUY if side_str == "BUY" else Side.SELL
        client = _polymarket_client()
        resp = client.create_and_post_order(
            order_args=OrderArgs(token_id=token_id, price=price, side=side, size=float(contracts)),
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )
        if not isinstance(resp, dict):
            return {"response": str(resp)}
        order_id = resp.get("id") or resp.get("order_id") or ""
        if order_id:
            try:
                resp["verified_order"] = client.get_order(str(order_id))
            except Exception:
                pass
        return resp
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _response_status(resp: dict[str, Any], contracts: int) -> tuple[str, str, float | None, float]:
    if resp.get("status") == "dry_run":
        return "dry_run", f"dry_run @ {fmt_pct(resp.get('price'))}", resp.get("price"), float(contracts)
    if resp.get("error"):
        return "error", str(resp["error"]), None, 0.0
    verified = resp.get("verified_order") or resp
    status = str(verified.get("status") or resp.get("status") or "unknown").lower()
    filled = float(
        verified.get("size_matched") or verified.get("amount_filled")
        or verified.get("filledAmount") or 0.0
    )
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
# Log & CSV
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
        append_log(f"BALANCE{rem_text} {event_label} | ERROR {error}")
        return None
    avail_text = "" if available is None else f" available={fmt_money(available)}"
    drawdown   = None if initial_balance is None else initial_balance - balance
    draw_text  = "" if drawdown is None else f" drawdown={drawdown:.4f}"
    append_log(f"BALANCE{rem_text} {event_label} | {fmt_money(balance)}{avail_text}{draw_text}")
    append_portfolio_row({
        "timestamp_utc":      iso_utc(),
        "event":              event_label,
        "remaining_seconds":  "" if remaining is None else f"{remaining:.3f}",
        "portfolio_value":    balance,
        "portfolio_available": available,
        "initial_balance":    initial_balance,
        "drawdown_dollars":   drawdown,
    })
    if initial_balance is None and balance is not None:
        args.initial_balance = balance
        if stop_loss > 0:
            append_log(f"STOP_LOSS baseline set to {fmt_money(balance)}")
    if initial_balance is not None and stop_loss > 0 and balance < initial_balance - stop_loss:
        raise RuntimeError(
            f"STOP_LOSS {fmt_money(balance)} < {fmt_money(initial_balance)} - {fmt_money(stop_loss)}"
        )
    return balance


# ---------------------------------------------------------------------------
# Entry at T1
# ---------------------------------------------------------------------------

async def evaluate_entry(
    runtime: ContractRuntime,
    state: CollectorState,
    counts: Counts,
    args: argparse.Namespace,
    remaining: float,
) -> None:
    async with state.lock:
        market    = state.market or runtime.market
        up_book   = state.books.get(market.up_token_id)
        down_book = state.books.get(market.down_token_id)

    close_time = iso_from_ms(runtime.market.end_ts * 1000)
    base_row = {
        "timestamp_utc":      iso_utc(),
        "event":              "decision",
        "contract_id":        runtime.market.slug,
        "close_time":         close_time,
        "remaining_seconds":  f"{remaining:.3f}",
        "entry_seconds":      args.entry_seconds,
        "exit_seconds":       args.exit_seconds,
        "dry_run":            int(not args.live),
        "successful_count":   counts.successful,
        "unsuccessful_count": counts.unsuccessful,
        "skipped_count":      counts.skipped,
    }

    def _skip(reason: str) -> None:
        counts.skipped += 1
        runtime.decision = TradeDecision(status="skip", reason=reason, dry_run=not args.live)
        append_log(
            f"SKIP T={remaining:.1f}s {runtime.market.slug} | {reason} | "
            f"S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        append_trade_row({**base_row, "order_status": "skip", "reason": reason})

    if up_book is None or down_book is None:
        return _skip("no book data")

    up_bid_p, _  = up_book.best_bid()
    up_ask_p, _  = up_book.best_ask()
    down_ask_p, _ = down_book.best_ask()

    if up_bid_p is None or up_ask_p is None:
        return _skip("no up book prices")
    if not (0.0 < up_ask_p < 1.0):
        return _skip(f"up_ask out of range: {up_ask_p}")

    up_mid = (up_bid_p + up_ask_p) / 2.0
    base_row.update({"p_yes_mid": up_mid, "up_ask": up_ask_p, "down_ask": down_ask_p})

    # Entry rule: buy less likely side at extreme prices
    if up_mid < EXTREME_LOW:
        side      = "YES"
        token_id  = runtime.market.up_token_id
        ask_price = up_ask_p
    elif up_mid > EXTREME_HIGH:
        side      = "NO"
        token_id  = runtime.market.down_token_id
        ask_price = down_ask_p
        if ask_price is None or not (0.0 < ask_price < 1.0):
            return _skip(f"down_ask out of range: {ask_price}")
    else:
        return _skip(f"p_yes_mid={fmt_pct(up_mid)} in neutral zone [{EXTREME_LOW},{EXTREME_HIGH}]")

    n_contracts = max(1, round(args.contract_value / ask_price))
    base_row.update({"selected_side": side, "selected_token_id": token_id,
                     "selected_ask": ask_price, "contracts": n_contracts})

    append_log(
        f"ENTRY T={remaining:.1f}s {runtime.market.slug} | p_yes_mid={fmt_pct(up_mid)} "
        f"→ {side} ask={fmt_pct(ask_price)} n={n_contracts} val=${args.contract_value:.2f}",
        prefix_timestamp=False,
    )

    order_status = "error"
    order_reason = "no attempts"
    fill_price: float | None = None
    filled_size: float = 0.0
    resp: dict[str, Any] = {}

    for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
        async with state.lock:
            cur_book = state.books.get(token_id)
        if cur_book is not None:
            fresh_ask, _ = cur_book.best_ask()
            if fresh_ask is not None and 0.0 < fresh_ask < 1.0:
                ask_price   = fresh_ask
                n_contracts = max(1, round(args.contract_value / ask_price))
        price_rounded = round(round(ask_price * 100) / 100, 2)
        if attempt > 1:
            append_log(
                f"ORDER RETRY {attempt}/{MAX_ORDER_ATTEMPTS} {runtime.market.slug} {side} "
                f"ask={fmt_pct(ask_price)} n={n_contracts}",
                prefix_timestamp=False,
            )
        resp = await asyncio.to_thread(
            _place, token_id, price_rounded, n_contracts,
            side_str="BUY", dry_run=not args.live,
        )
        order_status, order_reason, fill_price, filled_size = _response_status(resp, n_contracts)
        if order_status in ("filled", "dry_run", "partial"):
            break
        if attempt < MAX_ORDER_ATTEMPTS:
            await asyncio.sleep(ORDER_RETRY_DELAY)

    order_id = str(resp.get("id") or resp.get("order_id") or "")
    outcome_eligible = order_status in ("dry_run", "filled") and filled_size >= n_contracts

    runtime.decision = TradeDecision(
        status=order_status, side=side, token_id=token_id,
        selected_ask=ask_price, contracts=n_contracts,
        dry_run=not args.live, order_id=order_id,
        fill_price=fill_price, filled_size=filled_size,
        reason=order_reason, outcome_eligible=outcome_eligible,
    )

    append_log(
        f"ORDER {order_status.upper()} {runtime.market.slug} {side} | {order_reason} | "
        f"S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
        prefix_timestamp=False,
    )
    append_trade_row({
        **base_row,
        "order_status": order_status, "order_id": order_id,
        "fill_price": fill_price, "filled_size": filled_size,
        "reason": order_reason,
    })


# ---------------------------------------------------------------------------
# Exit at T2
# ---------------------------------------------------------------------------

async def evaluate_exit(
    runtime: ContractRuntime,
    state: CollectorState,
    counts: Counts,
    args: argparse.Namespace,
    remaining: float,
) -> None:
    decision   = runtime.decision
    close_time = iso_from_ms(runtime.market.end_ts * 1000)

    def _record(
        exit_status: str, exit_reason: str,
        bid_price: float | None, fill_price: float | None,
        filled_size: float, pnl_ratio: float | None,
        correct: Any, order_id: str,
    ) -> None:
        append_trade_row({
            "timestamp_utc":      iso_utc(),
            "event":              "outcome",
            "contract_id":        runtime.market.slug,
            "close_time":         close_time,
            "remaining_seconds":  f"{remaining:.3f}",
            "entry_seconds":      args.entry_seconds,
            "exit_seconds":       args.exit_seconds,
            "selected_side":      decision.side if decision else "",
            "selected_token_id":  decision.token_id if decision else "",
            "selected_ask":       decision.selected_ask if decision else "",
            "contracts":          decision.contracts if decision else "",
            "dry_run":            int(decision.dry_run) if decision else "",
            "order_status":       exit_status,
            "order_id":           order_id,
            "fill_price":         decision.fill_price if decision else "",
            "filled_size":        decision.filled_size if decision else "",
            "exit_bid":           bid_price,
            "exit_fill_price":    fill_price,
            "pnl_ratio":          pnl_ratio,
            "correct":            correct,
            "successful_count":   counts.successful,
            "unsuccessful_count": counts.unsuccessful,
            "skipped_count":      counts.skipped,
            "reason":             exit_reason,
        })

    if decision is None or not decision.outcome_eligible:
        runtime.exit_record  = ExitRecord(status="no_position", reason="no eligible entry")
        runtime.outcome_logged = True
        _record("no_position", "no eligible entry", None, None, 0.0, None, "", "")
        return

    token_id    = decision.token_id
    side        = decision.side
    n_contracts = decision.contracts

    async with state.lock:
        book = state.books.get(token_id)

    bid_price: float | None = None
    if book:
        bid_price, _ = book.best_bid()

    if bid_price is None or not (0.0 < bid_price < 1.0):
        reason = f"no valid bid at T2: {bid_price}"
        runtime.exit_record    = ExitRecord(status="error", reason=reason)
        runtime.outcome_logged = True
        append_log(
            f"EXIT FAIL T={remaining:.1f}s {runtime.market.slug} {side} | {reason}",
            prefix_timestamp=False,
        )
        _record("exit_error", reason, bid_price, None, 0.0, None, "", "")
        return

    entry_cost = (decision.selected_ask or 0.0) + COST_ADD

    exit_status = "error"
    exit_reason = "no attempts"
    fill_price: float | None  = None
    filled_size: float = 0.0
    resp: dict[str, Any] = {}

    attempt = 0
    while True:
        attempt += 1
        remaining_now = runtime.market.end_ts - time.time()
        if remaining_now <= 0:
            exit_status = "timeout"
            exit_reason = f"contract expired after {attempt - 1} exit attempts"
            break
        async with state.lock:
            cur_book = state.books.get(token_id)
        if cur_book is not None:
            fresh_bid, _ = cur_book.best_bid()
            if fresh_bid is not None and 0.0 < fresh_bid < 1.0:
                bid_price = fresh_bid
        price_rounded = round(round(bid_price * 100) / 100, 2)
        if attempt > 1:
            append_log(
                f"EXIT RETRY {attempt} T={remaining_now:.1f}s {runtime.market.slug} "
                f"bid={fmt_pct(bid_price)} n={n_contracts}",
                prefix_timestamp=False,
            )
        resp = await asyncio.to_thread(
            _place, token_id, price_rounded, n_contracts,
            side_str="SELL", dry_run=not args.live,
        )
        exit_status, exit_reason, fill_price, filled_size = _response_status(resp, n_contracts)
        if exit_status in ("filled", "dry_run", "partial"):
            break
        await asyncio.sleep(ORDER_RETRY_DELAY)

    exit_order_id      = str(resp.get("id") or resp.get("order_id") or "")
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

    runtime.exit_record    = ExitRecord(
        status=exit_status, bid_price=bid_price, fill_price=fill_price,
        filled_size=filled_size, pnl_ratio=pnl_ratio,
        order_id=exit_order_id, reason=exit_reason,
    )
    runtime.outcome_logged = True

    pnl_str = f"{pnl_ratio:+.4f}" if pnl_ratio is not None else "--"
    append_log(
        f"EXIT {exit_status.upper()} T={remaining:.1f}s {runtime.market.slug} {side} | "
        f"entry={fmt_pct(entry_cost - COST_ADD)} exit={fmt_pct(exit_price_for_pnl)} "
        f"pnl={pnl_str} correct={correct} | "
        f"S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
        prefix_timestamp=False,
    )
    _record(exit_status, exit_reason, bid_price, fill_price, filled_size, pnl_ratio,
            correct, exit_order_id)


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
# Main loop
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> None:
    _rotate_session_files()
    append_log(
        f"START polymarket_5m_intra_trader live={args.live} "
        f"contract_value=${args.contract_value:.2f} "
        f"entry={args.entry_seconds}s exit={args.exit_seconds}s "
        f"tolerance={args.entry_tolerance}s "
        f"extreme_filter=[p<{EXTREME_LOW},p>{EXTREME_HIGH}] "
        f"stop_loss={fmt_money(args.stop_loss)}"
    )

    counts          = Counts()
    completed_seen: set[str] = set()
    pending_exits: dict[str, ContractRuntime] = {}
    runtime: ContractRuntime | None = None
    args.initial_balance = None
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    state     = CollectorState()
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
                    rem_rt = rt.market.end_ts - time.time()
                    if rem_rt <= args.exit_seconds and not rt.exit_logged:
                        rt.exit_logged = True
                        await evaluate_exit(rt, state, counts, args, rem_rt)
                        await log_balance("EXIT", None, args.initial_balance, args.stop_loss, args)
                    if rt.outcome_logged or rem_rt < -60:
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
                        state.books  = {}

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

                remaining = market.end_ts - time.time()
                now_mono  = time.monotonic()

                # Status log
                if (args.log_interval <= 0 or runtime.last_status_log <= 0 or
                        now_mono - runtime.last_status_log >= args.log_interval):
                    async with state.lock:
                        up_b = state.books.get(market.up_token_id)
                        ub = ua = None
                        if up_b:
                            ub, _ = up_b.best_bid()
                            ua, _ = up_b.best_ask()
                    up_mid_log = (ub + ua) / 2.0 if ub and ua else None
                    um_str     = f"{up_mid_log:.4f}" if up_mid_log is not None else "--"
                    trade_str  = runtime.decision.status   if runtime.decision   else "--"
                    exit_str   = runtime.exit_record.status if runtime.exit_record else "--"
                    append_log(
                        f"STATUS T={remaining:.1f}s | p_yes_mid={um_str} "
                        f"entry={trade_str} exit={exit_str}",
                        prefix_timestamp=False,
                    )
                    runtime.last_status_log = now_mono

                # Entry window
                entry_lower = max(0.0, args.entry_seconds - args.entry_tolerance)
                if entry_lower <= remaining <= args.entry_seconds and \
                        not runtime.decision_logged and remaining >= 0:
                    runtime.decision_logged = True
                    await log_balance(
                        f"T{args.entry_seconds:.0f}s", remaining,
                        args.initial_balance, args.stop_loss, args,
                    )
                    await evaluate_entry(runtime, state, counts, args, remaining)
                elif remaining < entry_lower and not runtime.decision_logged and remaining >= 0:
                    runtime.decision_logged = True
                    reason = f"missed entry window; T={remaining:.1f}s"
                    counts.skipped += 1
                    runtime.decision = TradeDecision(
                        status="skip", dry_run=not args.live, reason=reason
                    )
                    append_log(
                        f"ENTRY MISS {runtime.market.slug} | {reason} | "
                        f"S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
                        prefix_timestamp=False,
                    )

                # Exit window
                exit_lower = max(0.0, args.exit_seconds - args.entry_tolerance)
                if exit_lower <= remaining <= args.exit_seconds and \
                        not runtime.exit_logged and remaining >= 0:
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
# Args
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Polymarket BTC 5m intra-period trader. "
            f"Buys less likely side when p_yes_mid < {EXTREME_LOW} or > {EXTREME_HIGH} "
            f"at T1={T1_SECONDS:.0f}s, exits at T2={T2_SECONDS:.0f}s (retries until T=0)."
        )
    )
    parser.add_argument("--live", action="store_true",
                        help="Submit real orders. Omit for dry-run.")
    parser.add_argument("--contract-value", type=float, default=1.05,
                        help="Dollar value per trade. Default: 1.05.")
    parser.add_argument("--entry-seconds", type=float, default=T1_SECONDS,
                        help=f"Entry time before close (s). Default: {T1_SECONDS}.")
    parser.add_argument("--exit-seconds", type=float, default=T2_SECONDS,
                        help=f"Exit time before close (s). Default: {T2_SECONDS}.")
    parser.add_argument("--entry-tolerance", type=float, default=5.0,
                        help="Window tolerance for entry/exit (s). Default: 5.")
    parser.add_argument("--poll-interval", type=float, default=0.5,
                        help="Poll interval (s). Default: 0.5.")
    parser.add_argument("--log-interval", type=float, default=30.0,
                        help="Seconds between status log lines. Default: 30.")
    parser.add_argument("--stop-loss", type=float, default=30.0,
                        help="Stop if balance drops this many USD. Default: 30.")
    args = parser.parse_args()
    args.contract_value  = max(0.01, args.contract_value)
    args.entry_tolerance = max(0.0, args.entry_tolerance)
    args.poll_interval   = max(0.1, args.poll_interval)
    return args


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
