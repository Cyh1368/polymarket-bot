#!/usr/bin/env python3
"""Collect live Polymarket crypto 5-minute Up/Down market data.

The collector uses:
  - Gamma API for active-market discovery.
  - CLOB /books for one-call, simultaneous initial Up/Down order-book snapshots.
  - CLOB market websocket for live order-book/BBO updates.
  - Polymarket RTDS websocket for Chainlink spot updates.

Rows are sampled every 5 seconds and written to one CSV per market slug.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import csv
import json
import math
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import httpx
    import websockets
except ImportError as exc:  # pragma: no cover - operator environment guard
    raise SystemExit(
        f"Missing dependency `{exc.name}`. Run with the existing project venv, for example:\n"
        "  /home/chengyou1368/polymarket-bot/kalshi/.venv-cli-trader/bin/python "
        "polymarket/polymarket_data_BTC_5m.py"
    ) from exc


GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"
CLOB_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"

APP_DIR = Path(__file__).resolve().parent
FIVE_MINUTE_SECONDS = 5 * 60
DEFAULT_COIN = "BTC"
DEFAULT_SPOT_TOPIC = "crypto_prices_chainlink"
ORDER_IMBALANCE_TAUS_RAW = os.getenv("ORDER_IMBALANCE_TAUS", "0.01,0.02,0.03,0.05,0.07,0.10,0.15,0.20")
SPOT_TOPIC_ALIASES = {
    "crypto_prices": {"crypto_prices_chainlink"},
    "crypto_prices_chainlink": {"crypto_prices"},
}


@dataclass(frozen=True)
class CoinConfig:
    symbol: str
    slug_prefix: str
    spot_symbol: str
    display_name: str
    data_dir_name: str


COIN_CONFIGS: dict[str, CoinConfig] = {
    "BTC": CoinConfig("BTC", "btc", "btc/usd", "Bitcoin", "data_BTC_5m"),
    "ETH": CoinConfig("ETH", "eth", "eth/usd", "Ethereum", "data_ETH_5m"),
    "SOL": CoinConfig("SOL", "sol", "sol/usd", "Solana", "data_SOL_5m"),
    "XRP": CoinConfig("XRP", "xrp", "xrp/usd", "XRP", "data_XRP_5m"),
    "HYPE": CoinConfig("HYPE", "hype", "hype/usd", "Hyperliquid", "data_HYPE_5m"),
    "DOGE": CoinConfig("DOGE", "doge", "doge/usd", "Dogecoin", "data_DOGE_5m"),
    "BNB": CoinConfig("BNB", "bnb", "bnb/usd", "BNB", "data_BNB_5m"),
}
DEFAULT_SPOT_SYMBOL = COIN_CONFIGS[DEFAULT_COIN].spot_symbol


def parse_order_imbalance_taus(raw: str) -> tuple[float, ...]:
    values: list[float] = []
    for item in raw.split(","):
        try:
            value = float(item.strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return tuple(dict.fromkeys(values)) or (0.01, 0.02, 0.05, 0.10)


def tau_label(value: float) -> str:
    cents_value = value * 100.0
    if abs(cents_value - round(cents_value)) < 1e-9:
        return f"{int(round(cents_value))}c"
    return str(value).replace(".", "p").replace("-", "m")


ORDER_IMBALANCE_TAUS = parse_order_imbalance_taus(ORDER_IMBALANCE_TAUS_RAW)


def token_order_imbalance_csv_fields(prefix: str) -> list[str]:
    fields: list[str] = []
    for tau in ORDER_IMBALANCE_TAUS:
        label = tau_label(tau)
        fields.extend(
            [
                f"{prefix}_bid_depth_tau_{label}",
                f"{prefix}_ask_depth_tau_{label}",
                f"{prefix}_book_imbalance_tau_{label}",
            ]
        )
    return fields


def cross_order_imbalance_csv_fields() -> list[str]:
    fields: list[str] = []
    for tau in ORDER_IMBALANCE_TAUS:
        label = tau_label(tau)
        fields.extend(
            [
                f"up_down_bid_imbalance_tau_{label}",
                f"up_down_ask_imbalance_tau_{label}",
            ]
        )
    return fields

CSV_FIELDS = [
    "timestamp_utc",
    "sample_epoch_ms",
    "market_slug",
    "market_title",
    "market_id",
    "condition_id",
    "event_start_utc",
    "event_end_utc",
    "seconds_to_close",
    "up_token_id",
    "down_token_id",
    "spot_source",
    "spot_symbol",
    "spot_price",
    "spot_timestamp_utc",
    "spot_received_utc",
    "spot_age_seconds",
    "price_target",
    "price_target_source",
    "distance_to_target",
    "is_above_target",
    "up_best_bid",
    "up_best_bid_size",
    "up_best_ask",
    "up_best_ask_size",
    *token_order_imbalance_csv_fields("up"),
    "up_mid",
    "up_spread",
    "up_last_trade_price",
    "up_book_timestamp_utc",
    "up_book_age_seconds",
    "up_book_hash",
    "up_event_count",
    "down_best_bid",
    "down_best_bid_size",
    "down_best_ask",
    "down_best_ask_size",
    *token_order_imbalance_csv_fields("down"),
    "down_mid",
    "down_spread",
    "down_last_trade_price",
    "down_book_timestamp_utc",
    "down_book_age_seconds",
    "down_book_hash",
    "down_event_count",
    "book_timestamp_skew_seconds",
    *cross_order_imbalance_csv_fields(),
    "up_ask_plus_down_ask",
    "up_bid_plus_down_bid",
    "book_state_complete",
    "quality_flags",
]


@dataclass
class CurrentMarket:
    slug: str
    title: str
    market_id: str
    condition_id: str
    start_ts: int
    end_ts: int
    up_token_id: str
    down_token_id: str
    price_target: float | None = None
    price_target_source: str = ""


@dataclass
class SpotState:
    source: str = DEFAULT_SPOT_TOPIC
    symbol: str = DEFAULT_SPOT_SYMBOL
    price: float | None = None
    timestamp_ms: int | None = None
    received_ms: int | None = None


@dataclass
class TokenBook:
    token_id: str
    outcome: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    timestamp_ms: int | None = None
    book_hash: str = ""
    last_trade_price: float | None = None
    event_count: int = 0
    fallback_best_bid: float | None = None
    fallback_best_ask: float | None = None

    def replace_from_book(self, book: dict[str, Any]) -> None:
        self.bids = parse_levels(book.get("bids"))
        self.asks = parse_levels(book.get("asks"))
        self.timestamp_ms = parse_epoch_ms(book.get("timestamp")) or utc_now_ms()
        self.book_hash = str(book.get("hash") or "")
        self.last_trade_price = finite_float(book.get("last_trade_price"))
        self.fallback_best_bid = None
        self.fallback_best_ask = None
        self.event_count += 1

    def apply_price_change(self, change: dict[str, Any], timestamp_ms: int | None) -> None:
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
        self.book_hash = str(change.get("hash") or self.book_hash)
        self.timestamp_ms = timestamp_ms or utc_now_ms()
        self.event_count += 1

    def apply_best_bid_ask(self, msg: dict[str, Any]) -> None:
        self.fallback_best_bid = finite_float(msg.get("best_bid"))
        self.fallback_best_ask = finite_float(msg.get("best_ask"))
        self.timestamp_ms = parse_epoch_ms(msg.get("timestamp")) or utc_now_ms()
        self.event_count += 1

    def apply_trade(self, msg: dict[str, Any]) -> None:
        self.last_trade_price = finite_float(msg.get("price")) or self.last_trade_price
        self.timestamp_ms = parse_epoch_ms(msg.get("timestamp")) or utc_now_ms()
        self.event_count += 1

    def best_bid(self) -> tuple[float | None, float | None]:
        if self.bids:
            price = max(self.bids)
            return price, self.bids[price]
        return self.fallback_best_bid, None

    def best_ask(self) -> tuple[float | None, float | None]:
        if self.asks:
            price = min(self.asks)
            return price, self.asks[price]
        return self.fallback_best_ask, None

    def snapshot(self, now_ms: int) -> dict[str, Any]:
        bid, bid_size = self.best_bid()
        ask, ask_size = self.best_ask()
        mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        spread = ask - bid if bid is not None and ask is not None else None
        age = (now_ms - self.timestamp_ms) / 1000.0 if self.timestamp_ms else None
        return {
            "best_bid": bid,
            "best_bid_size": bid_size,
            "best_ask": ask,
            "best_ask_size": ask_size,
            **token_order_imbalance_metrics(self.bids, self.asks),
            "mid": mid,
            "spread": spread,
            "last_trade_price": self.last_trade_price,
            "timestamp_ms": self.timestamp_ms,
            "timestamp_utc": iso_from_ms(self.timestamp_ms),
            "age_seconds": age,
            "hash": self.book_hash,
            "event_count": self.event_count,
        }


class CollectorState:
    def __init__(self, spot_symbol: str, spot_source: str, max_spot_history: int = 2000) -> None:
        self.lock = asyncio.Lock()
        self.market: CurrentMarket | None = None
        self.spot = SpotState(source=spot_source, symbol=spot_symbol)
        self.spot_history: deque[SpotState] = deque(maxlen=max_spot_history)
        self.books: dict[str, TokenBook] = {}


def utc_now_ms() -> int:
    return int(time.time() * 1000)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_from_ts(ts: int | float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_from_ms(ms: int | float | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_epoch_ms(value: Any) -> int | None:
    number = finite_float(value)
    if number is None:
        return None
    if number > 10_000_000_000:
        return int(number)
    if number > 1_000_000_000:
        return int(number * 1000)
    return None


def finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def depth_within_tau(levels: dict[float, float], tau: float, *, side: str) -> float:
    if not levels:
        return 0.0
    if side == "ask":
        reference = min(levels)
        return sum(size for price, size in levels.items() if size > 0 and price <= reference + tau + 1e-12)
    reference = max(levels)
    return sum(size for price, size in levels.items() if size > 0 and price >= reference - tau - 1e-12)


def imbalance_value(left_depth: float | None, right_depth: float | None) -> float | None:
    if left_depth is None or right_depth is None:
        return None
    total = left_depth + right_depth
    if total <= 0:
        return None
    return (left_depth - right_depth) / total


def token_order_imbalance_metrics(bids: dict[float, float], asks: dict[float, float]) -> dict[str, float | None]:
    metrics: dict[str, float | None] = {}
    for tau in ORDER_IMBALANCE_TAUS:
        label = tau_label(tau)
        bid_depth = depth_within_tau(bids, tau, side="bid")
        ask_depth = depth_within_tau(asks, tau, side="ask")
        metrics.update(
            {
                f"bid_depth_tau_{label}": bid_depth,
                f"ask_depth_tau_{label}": ask_depth,
                f"book_imbalance_tau_{label}": imbalance_value(bid_depth, ask_depth),
            }
        )
    return metrics


def quote_imbalance_csv_values(prefix: str, quote: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for tau in ORDER_IMBALANCE_TAUS:
        label = tau_label(tau)
        values.update(
            {
                f"{prefix}_bid_depth_tau_{label}": quote.get(f"bid_depth_tau_{label}"),
                f"{prefix}_ask_depth_tau_{label}": quote.get(f"ask_depth_tau_{label}"),
                f"{prefix}_book_imbalance_tau_{label}": quote.get(f"book_imbalance_tau_{label}"),
            }
        )
    return values


def cross_imbalance_csv_values(up_quote: dict[str, Any], down_quote: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for tau in ORDER_IMBALANCE_TAUS:
        label = tau_label(tau)
        up_bid_depth = finite_float(up_quote.get(f"bid_depth_tau_{label}"))
        down_bid_depth = finite_float(down_quote.get(f"bid_depth_tau_{label}"))
        up_ask_depth = finite_float(up_quote.get(f"ask_depth_tau_{label}"))
        down_ask_depth = finite_float(down_quote.get(f"ask_depth_tau_{label}"))
        values.update(
            {
                f"up_down_bid_imbalance_tau_{label}": imbalance_value(up_bid_depth, down_bid_depth),
                f"up_down_ask_imbalance_tau_{label}": imbalance_value(down_ask_depth, up_ask_depth),
            }
        )
    return values


def spot_topic_matches(actual_topic: Any, requested_topic: str) -> bool:
    actual = str(actual_topic or "")
    if not actual:
        return True
    if actual == requested_topic:
        return True
    return actual in SPOT_TOPIC_ALIASES.get(requested_topic, set())


def normalize_spot_symbol(symbol: str) -> str:
    """Normalise symbol for comparison: lowercase, hyphens → slashes."""
    return symbol.lower().replace("-", "/")


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(value)
            except Exception:
                continue
            return parsed if isinstance(parsed, list) else []
    return []


def parse_levels(value: Any) -> dict[float, float]:
    levels: dict[float, float] = {}
    if not isinstance(value, list):
        return levels
    for item in value:
        if not isinstance(item, dict):
            continue
        price = finite_float(item.get("price") or item.get("px"))
        size = finite_float(item.get("size") or item.get("qty") or item.get("quantity"))
        if price is None or size is None:
            continue
        if size > 0:
            levels[price] = size
    return levels


def safe_filename(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return safe or "unknown"


def event_metadata_value(event: dict[str, Any] | None, market: dict[str, Any] | None) -> tuple[float | None, str]:
    keys = (
        "priceToBeat",
        "price_to_beat",
        "targetPrice",
        "target_price",
        "priceTarget",
        "initialPrice",
        "startPrice",
    )
    containers = []
    if isinstance(event, dict):
        containers.extend([event.get("eventMetadata"), event.get("metadata"), event])
    if isinstance(market, dict):
        containers.extend([market.get("eventMetadata"), market.get("metadata"), market])
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = finite_float(container.get(key))
            if value is not None:
                return value, f"gamma.{key}"
    return None, ""


def parse_market_from_event(event: dict[str, Any], *, allow_upcoming: bool = False) -> CurrentMarket | None:
    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    if market.get("closed") is True or market.get("active") is False:
        return None

    outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
    token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds"))]
    if len(outcomes) != len(token_ids) or len(token_ids) < 2:
        return None
    token_by_outcome = {outcome.lower(): token for outcome, token in zip(outcomes, token_ids)}
    up_token = token_by_outcome.get("up") or token_by_outcome.get("yes") or token_ids[0]
    down_token = token_by_outcome.get("down") or token_by_outcome.get("no") or token_ids[1]

    start = parse_dt(market.get("eventStartTime")) or parse_dt(market.get("startDate")) or parse_dt(event.get("startTime"))
    end = parse_dt(market.get("endDate")) or parse_dt(event.get("endDate"))
    if not start or not end:
        return None

    now = datetime.now(timezone.utc)
    if allow_upcoming:
        if now > end:
            return None
    elif not (start <= now <= end):
        return None

    target, target_source = event_metadata_value(event, market)
    return CurrentMarket(
        slug=event.get("slug") or market.get("slug") or "",
        title=event.get("title") or market.get("question") or "",
        market_id=str(market.get("id") or ""),
        condition_id=market.get("conditionId") or "",
        start_ts=int(start.timestamp()),
        end_ts=int(end.timestamp()),
        up_token_id=up_token,
        down_token_id=down_token,
        price_target=target,
        price_target_source=target_source,
    )


async def fetch_event(client: httpx.AsyncClient, slug: str) -> dict[str, Any] | None:
    response = await client.get(f"{GAMMA_BASE_URL}/events/slug/{slug}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


async def discover_current_market(
    client: httpx.AsyncClient,
    coin: CoinConfig,
    explicit_slug: str | None = None,
) -> CurrentMarket:
    if explicit_slug:
        event = await fetch_event(client, explicit_slug)
        if not event:
            raise RuntimeError(f"Polymarket event slug not found: {explicit_slug}")
        market = parse_market_from_event(event, allow_upcoming=True)
        if not market:
            raise RuntimeError(f"Polymarket event slug is not a usable {coin.symbol} 5m market: {explicit_slug}")
        return market

    current_slot = int(time.time()) // FIVE_MINUTE_SECONDS * FIVE_MINUTE_SECONDS
    candidate_slots = [current_slot + offset * FIVE_MINUTE_SECONDS for offset in (0, -1, 1, -2, 2, -3, 3)]
    for slot in candidate_slots:
        slug = f"{coin.slug_prefix}-updown-5m-{slot}"
        event = await fetch_event(client, slug)
        if not event:
            continue
        market = parse_market_from_event(event)
        if market:
            return market
    raise RuntimeError(f"No active {coin.symbol} 5-minute Up/Down market found")


async def fetch_books_simultaneous(client: httpx.AsyncClient, market: CurrentMarket) -> dict[str, dict[str, Any]]:
    body = [{"token_id": market.up_token_id}, {"token_id": market.down_token_id}]
    response = await client.post(f"{CLOB_BASE_URL}/books", json=body)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("/books response was not a list")
    by_asset: dict[str, dict[str, Any]] = {}
    for book in payload:
        if isinstance(book, dict):
            asset_id = str(book.get("asset_id") or book.get("token_id") or "")
            if asset_id:
                by_asset[asset_id] = book
    missing = [token for token in (market.up_token_id, market.down_token_id) if token not in by_asset]
    if missing:
        raise RuntimeError(f"/books response missing token ids: {missing}")
    return by_asset


async def load_initial_books(client: httpx.AsyncClient, state: CollectorState, market: CurrentMarket) -> None:
    books = await fetch_books_simultaneous(client, market)
    up_book = TokenBook(market.up_token_id, "Up")
    down_book = TokenBook(market.down_token_id, "Down")
    up_book.replace_from_book(books[market.up_token_id])
    down_book.replace_from_book(books[market.down_token_id])
    async with state.lock:
        state.books = {
            market.up_token_id: up_book,
            market.down_token_id: down_book,
        }


def target_from_spot_history(market: CurrentMarket, history: deque[SpotState], max_distance_seconds: float) -> tuple[float | None, str]:
    if market.price_target is not None:
        return market.price_target, market.price_target_source
    start_ms = market.start_ts * 1000
    best: tuple[int, float] | None = None
    for item in history:
        if item.price is None or item.received_ms is None:
            continue
        # Use wall-clock receipt time, not the on-chain Chainlink timestamp which can
        # lag by 2–3 minutes and causes distance checks against start_ms to always fail.
        distance_ms = abs(item.received_ms - start_ms)
        if distance_ms <= max_distance_seconds * 1000 and (best is None or distance_ms < best[0]):
            best = (distance_ms, item.price)
    if best is None:
        return None, ""
    return best[1], f"rtds_nearest_start_{best[0] / 1000.0:.1f}s"


async def set_current_market(state: CollectorState, market: CurrentMarket, target_max_distance_seconds: float) -> None:
    async with state.lock:
        target, target_source = target_from_spot_history(market, state.spot_history, target_max_distance_seconds)
        market.price_target = target
        market.price_target_source = target_source
        state.market = market
        state.books = {}


async def maybe_set_target_from_spot(state: CollectorState, spot: SpotState, target_max_distance_seconds: float) -> None:
    async with state.lock:
        market = state.market
        if not market or market.price_target is not None or spot.price is None or spot.received_ms is None:
            return
        start_ms = market.start_ts * 1000
        # Use wall-clock receipt time for the same reason as target_from_spot_history.
        distance = abs(spot.received_ms - start_ms) / 1000.0
        if distance <= target_max_distance_seconds:
            market.price_target = spot.price
            market.price_target_source = f"rtds_nearest_start_{distance:.1f}s"


async def rtds_ws_loop(state: CollectorState, stop: asyncio.Event, args: argparse.Namespace) -> None:
    # Subscribe to the requested topic AND all its aliases so that coins whose
    # Chainlink feed arrives under a sibling topic name (e.g. BTC comes via
    # "crypto_prices" rather than "crypto_prices_chainlink") are also captured.
    topics = sorted({args.spot_topic} | SPOT_TOPIC_ALIASES.get(args.spot_topic, set()))
    subscription = json.dumps(
        {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": topic,
                    "type": "*",
                    "filters": json.dumps({"symbol": args.spot_symbol}),
                }
                for topic in topics
            ],
        }
    )
    backoff = 1.0
    while not stop.is_set():
        ping_task: asyncio.Task[Any] | None = None
        try:
            async with websockets.connect(args.rtds_ws_url, ping_interval=None, open_timeout=args.ws_open_timeout) as ws:
                await ws.send(subscription)
                ping_task = asyncio.create_task(text_ping_loop(ws, 5.0))
                backoff = 1.0
                async for raw in ws:
                    if stop.is_set():
                        break
                    if isinstance(raw, str) and raw.strip().upper() in {"PING", "PONG"}:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    topic = msg.get("topic")
                    if not spot_topic_matches(topic, args.spot_topic):
                        continue
                    spot_source = str(topic or args.spot_topic)
                    for payload in spot_payloads(msg, args.spot_symbol):
                        symbol = str(payload.get("symbol") or "").lower()
                        # Normalise hyphens so "btc-usd" matches "btc/usd".
                        if normalize_spot_symbol(symbol) != normalize_spot_symbol(args.spot_symbol):
                            continue
                        price = finite_float(payload.get("value"))
                        timestamp_ms = parse_epoch_ms(payload.get("timestamp"))
                        if price is None:
                            continue
                        spot = SpotState(
                            source=spot_source,
                            symbol=args.spot_symbol,
                            price=price,
                            timestamp_ms=timestamp_ms or parse_epoch_ms(msg.get("timestamp")) or utc_now_ms(),
                            received_ms=utc_now_ms(),
                        )
                        async with state.lock:
                            state.spot = spot
                            state.spot_history.append(spot)
                        await maybe_set_target_from_spot(state, spot, args.target_max_distance_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"{iso_now()} RTDS websocket error: {type(exc).__name__}: {exc}", flush=True)
        finally:
            if ping_task:
                ping_task.cancel()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2.0, 30.0)


async def text_ping_loop(ws: Any, interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await ws.send("PING")


def spot_payloads(msg: dict[str, Any], default_symbol: str) -> list[dict[str, Any]]:
    payload = msg.get("payload")
    parent_symbol = ""
    if isinstance(payload, list):
        items = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        parent_symbol = str(payload.get("symbol") or "")
        data = payload.get("data")
        if isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
        else:
            items = [payload]
    else:
        return []

    out: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        row["symbol"] = str(row.get("symbol") or parent_symbol or default_symbol)
        out.append(row)
    return out


async def clob_ws_loop(state: CollectorState, market: CurrentMarket, stop: asyncio.Event, args: argparse.Namespace) -> None:
    subscription = json.dumps(
        {
            "assets_ids": [market.up_token_id, market.down_token_id],
            "type": "market",
            "custom_feature_enabled": True,
        }
    )
    backoff = 1.0
    while not stop.is_set() and time.time() <= market.end_ts + args.after_close_seconds:
        try:
            async with websockets.connect(args.clob_ws_url, ping_interval=20, ping_timeout=10, open_timeout=args.ws_open_timeout) as ws:
                await ws.send(subscription)
                backoff = 1.0
                async for raw in ws:
                    if stop.is_set() or time.time() > market.end_ts + args.after_close_seconds:
                        break
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    await apply_clob_message(state, msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"{iso_now()} CLOB websocket error for {market.slug}: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2.0, 20.0)


async def apply_clob_message(state: CollectorState, msg: Any) -> None:
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
                continue
            if event_type == "price_change":
                timestamp_ms = parse_epoch_ms(item.get("timestamp")) or utc_now_ms()
                changes = item.get("price_changes") or []
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    asset_id = str(change.get("asset_id") or "")
                    book = state.books.get(asset_id)
                    if book:
                        book.apply_price_change(change, timestamp_ms)
                continue
            if event_type == "best_bid_ask":
                asset_id = str(item.get("asset_id") or "")
                book = state.books.get(asset_id)
                if book:
                    book.apply_best_bid_ask(item)
                continue
            if event_type == "last_trade_price":
                asset_id = str(item.get("asset_id") or "")
                book = state.books.get(asset_id)
                if book:
                    book.apply_trade(item)


def quote_flags(prefix: str, quote: dict[str, Any], max_spread: float) -> list[str]:
    flags: list[str] = []
    bid = quote.get("best_bid")
    ask = quote.get("best_ask")
    spread = quote.get("spread")
    for name, price in (("bid", bid), ("ask", ask)):
        if price is None:
            flags.append(f"{prefix}_{name}_missing")
        elif not (0.0 <= price <= 1.0):
            flags.append(f"{prefix}_{name}_out_of_range")
    if bid is not None and ask is not None:
        if bid > ask:
            flags.append(f"{prefix}_crossed_book")
        if spread is not None and spread > max_spread:
            flags.append(f"{prefix}_wide_spread")
    return flags


def build_sample_row(
    market: CurrentMarket,
    spot: SpotState,
    up_quote: dict[str, Any],
    down_quote: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    now_ms = utc_now_ms()
    seconds_to_close = market.end_ts - now_ms / 1000.0
    target = market.price_target
    distance = spot.price - target if spot.price is not None and target is not None else None
    skew = None
    if up_quote.get("timestamp_ms") and down_quote.get("timestamp_ms"):
        skew = abs(up_quote["timestamp_ms"] - down_quote["timestamp_ms"]) / 1000.0
    spot_age = (now_ms - spot.timestamp_ms) / 1000.0 if spot.timestamp_ms else None

    flags: list[str] = []
    flags.extend(quote_flags("up", up_quote, args.max_spread))
    flags.extend(quote_flags("down", down_quote, args.max_spread))
    for prefix, quote in (("up", up_quote), ("down", down_quote)):
        age = quote.get("age_seconds")
        if age is None:
            flags.append(f"{prefix}_book_missing_timestamp")
        elif age > args.max_book_age_seconds:
            flags.append(f"{prefix}_book_stale")
    if skew is not None and skew > args.max_book_skew_seconds:
        flags.append("book_timestamp_skew")
    if spot.price is None:
        flags.append("spot_missing")
    elif spot_age is not None and spot_age > args.max_spot_age_seconds:
        flags.append("spot_stale")
    if target is None:
        flags.append("target_missing")
    if seconds_to_close < 0:
        flags.append("market_closed")

    up_ask_plus_down_ask = nullable_sum(up_quote.get("best_ask"), down_quote.get("best_ask"))
    up_bid_plus_down_bid = nullable_sum(up_quote.get("best_bid"), down_quote.get("best_bid"))
    if up_ask_plus_down_ask is not None and up_ask_plus_down_ask > 1.0 + args.max_complement_deviation:
        flags.append("ask_sum_high")
    if up_bid_plus_down_bid is not None and up_bid_plus_down_bid < 1.0 - args.max_complement_deviation:
        flags.append("bid_sum_low")

    return {
        "timestamp_utc": iso_from_ms(now_ms),
        "sample_epoch_ms": now_ms,
        "market_slug": market.slug,
        "market_title": market.title,
        "market_id": market.market_id,
        "condition_id": market.condition_id,
        "event_start_utc": iso_from_ts(market.start_ts),
        "event_end_utc": iso_from_ts(market.end_ts),
        "seconds_to_close": seconds_to_close,
        "up_token_id": market.up_token_id,
        "down_token_id": market.down_token_id,
        "spot_source": spot.source,
        "spot_symbol": spot.symbol,
        "spot_price": spot.price,
        "spot_timestamp_utc": iso_from_ms(spot.timestamp_ms),
        "spot_received_utc": iso_from_ms(spot.received_ms),
        "spot_age_seconds": spot_age,
        "price_target": target,
        "price_target_source": market.price_target_source,
        "distance_to_target": distance,
        "is_above_target": int(distance >= 0) if distance is not None else "",
        "up_best_bid": up_quote.get("best_bid"),
        "up_best_bid_size": up_quote.get("best_bid_size"),
        "up_best_ask": up_quote.get("best_ask"),
        "up_best_ask_size": up_quote.get("best_ask_size"),
        **quote_imbalance_csv_values("up", up_quote),
        "up_mid": up_quote.get("mid"),
        "up_spread": up_quote.get("spread"),
        "up_last_trade_price": up_quote.get("last_trade_price"),
        "up_book_timestamp_utc": up_quote.get("timestamp_utc"),
        "up_book_age_seconds": up_quote.get("age_seconds"),
        "up_book_hash": up_quote.get("hash"),
        "up_event_count": up_quote.get("event_count"),
        "down_best_bid": down_quote.get("best_bid"),
        "down_best_bid_size": down_quote.get("best_bid_size"),
        "down_best_ask": down_quote.get("best_ask"),
        "down_best_ask_size": down_quote.get("best_ask_size"),
        **quote_imbalance_csv_values("down", down_quote),
        "down_mid": down_quote.get("mid"),
        "down_spread": down_quote.get("spread"),
        "down_last_trade_price": down_quote.get("last_trade_price"),
        "down_book_timestamp_utc": down_quote.get("timestamp_utc"),
        "down_book_age_seconds": down_quote.get("age_seconds"),
        "down_book_hash": down_quote.get("hash"),
        "down_event_count": down_quote.get("event_count"),
        "book_timestamp_skew_seconds": skew,
        **cross_imbalance_csv_values(up_quote, down_quote),
        "up_ask_plus_down_ask": up_ask_plus_down_ask,
        "up_bid_plus_down_bid": up_bid_plus_down_bid,
        "book_state_complete": int(
            up_quote.get("best_bid") is not None
            and up_quote.get("best_ask") is not None
            and down_quote.get("best_bid") is not None
            and down_quote.get("best_ask") is not None
        ),
        "quality_flags": ";".join(sorted(set(flags))),
    }


def nullable_sum(left: Any, right: Any) -> float | None:
    left_float = finite_float(left)
    right_float = finite_float(right)
    if left_float is None or right_float is None:
        return None
    return left_float + right_float


def csv_path(data_dir: Path, coin_symbol: str, market_slug: str) -> Path:
    return data_dir / f"polymarket_data_{coin_symbol}_5m_{safe_filename(market_slug)}.csv"


def ensure_csv_schema(path: Path, fields: list[str]) -> bool:
    if not path.exists():
        return False
    try:
        with path.open(newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            old_fields = reader.fieldnames or []
            if old_fields == fields:
                return True
            rows = list(reader)
    except OSError:
        return True
    with path.open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        for old_row in rows:
            writer.writerow({field: old_row.get(field, "") for field in fields})
    return True


def append_csv_row(data_dir: Path, coin_symbol: str, row: dict[str, Any]) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = csv_path(data_dir, coin_symbol, str(row.get("market_slug") or "unknown"))
    exists = ensure_csv_schema(path, CSV_FIELDS)
    with path.open("a", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
    return path


async def sample_loop(state: CollectorState, market: CurrentMarket, stop: asyncio.Event, args: argparse.Namespace) -> None:
    wrote_once = False
    while not stop.is_set() and time.time() <= market.end_ts + args.after_close_seconds:
        if not wrote_once and args.initial_spot_wait_seconds > 0:
            await wait_for_initial_context(state, market, stop, args.initial_spot_wait_seconds)
        async with state.lock:
            active_market = state.market or market
            spot = SpotState(**state.spot.__dict__)
            up_book = state.books.get(market.up_token_id)
            down_book = state.books.get(market.down_token_id)
            if up_book:
                up_quote = up_book.snapshot(utc_now_ms())
            else:
                up_quote = {}
            if down_book:
                down_quote = down_book.snapshot(utc_now_ms())
            else:
                down_quote = {}
        row = build_sample_row(active_market, spot, up_quote, down_quote, args)
        path = append_csv_row(args.data_dir, args.coin, row)
        wrote_once = True
        print(
            f"{row['timestamp_utc']} {market.slug} "
            f"Up {row['up_best_bid']}/{row['up_best_ask']} "
            f"Down {row['down_best_bid']}/{row['down_best_ask']} "
            f"spot={row['spot_price']} target={row['price_target']} "
            f"flags={row['quality_flags'] or '-'} -> {path}",
            flush=True,
        )
        if args.once and wrote_once:
            stop.set()
            return
        await asyncio.sleep(args.sample_seconds)


async def wait_for_initial_context(
    state: CollectorState,
    market: CurrentMarket,
    stop: asyncio.Event,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not stop.is_set() and time.monotonic() < deadline:
        async with state.lock:
            active_market = state.market or market
            has_spot = state.spot.price is not None
            has_target = active_market.price_target is not None
        if has_spot and has_target:
            return
        await asyncio.sleep(0.1)


async def run_market(
    state: CollectorState,
    client: httpx.AsyncClient,
    market: CurrentMarket,
    stop: asyncio.Event,
    args: argparse.Namespace,
) -> None:
    await set_current_market(state, market, args.target_max_distance_seconds)
    await load_initial_books(client, state, market)
    print(
        f"{iso_now()} tracking {market.slug} "
        f"{iso_from_ts(market.start_ts)}..{iso_from_ts(market.end_ts)} "
        f"target={market.price_target} ({market.price_target_source or 'unknown'})",
        flush=True,
    )
    market_tasks = [
        asyncio.create_task(clob_ws_loop(state, market, stop, args)),
        asyncio.create_task(sample_loop(state, market, stop, args)),
    ]
    try:
        while not stop.is_set() and time.time() <= market.end_ts + args.after_close_seconds:
            for task in market_tasks:
                if task.done():
                    exc = task.exception()
                    if exc:
                        raise exc
                    return
            await asyncio.sleep(0.5)
    finally:
        for task in market_tasks:
            task.cancel()
        await asyncio.gather(*market_tasks, return_exceptions=True)


async def run(args: argparse.Namespace) -> None:
    stop = asyncio.Event()

    def handle_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_stop)
        except NotImplementedError:
            pass

    state = CollectorState(args.spot_symbol, args.spot_topic)
    spot_task = asyncio.create_task(rtds_ws_loop(state, stop, args))
    async with httpx.AsyncClient(timeout=args.http_timeout) as client:
        try:
            last_slug = ""
            while not stop.is_set():
                market = await discover_current_market(client, args.coin_config, args.slug)
                if market.slug == last_slug and not args.once:
                    await asyncio.sleep(args.market_refresh_seconds)
                    continue
                last_slug = market.slug
                await run_market(state, client, market, stop, args)
                if args.once or args.slug:
                    break
                await asyncio.sleep(args.market_refresh_seconds)
        finally:
            stop.set()
            spot_task.cancel()
            await asyncio.gather(spot_task, return_exceptions=True)


def parse_args(default_coin: str = DEFAULT_COIN) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coin", default=default_coin, help=f"Coin to collect: {', '.join(sorted(COIN_CONFIGS))}.")
    parser.add_argument("--data-dir", type=Path, default=None, help="Output directory. Defaults to polymarket/data_<COIN>_5m.")
    parser.add_argument("--slug", default="", help="Optional explicit market slug, e.g. eth-updown-5m-1778803200.")
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    parser.add_argument("--market-refresh-seconds", type=float, default=2.0)
    parser.add_argument("--after-close-seconds", type=float, default=10.0)
    parser.add_argument("--http-timeout", type=float, default=10.0)
    parser.add_argument("--ws-open-timeout", type=float, default=10.0)
    parser.add_argument("--clob-ws-url", default=CLOB_MARKET_WS_URL)
    parser.add_argument("--rtds-ws-url", default=RTDS_WS_URL)
    parser.add_argument("--spot-topic", default=DEFAULT_SPOT_TOPIC)
    parser.add_argument("--spot-symbol", default="", help="RTDS source symbol. Defaults to the selected coin, e.g. eth/usd.")
    parser.add_argument("--target-max-distance-seconds", type=float, default=300.0)
    parser.add_argument("--initial-spot-wait-seconds", type=float, default=5.0)
    parser.add_argument("--max-spread", type=float, default=0.20)
    parser.add_argument("--max-book-age-seconds", type=float, default=15.0)
    parser.add_argument("--max-book-skew-seconds", type=float, default=8.0)
    parser.add_argument("--max-spot-age-seconds", type=float, default=15.0)
    parser.add_argument("--max-complement-deviation", type=float, default=0.20)
    parser.add_argument("--once", action="store_true", help="Write one sample and exit.")
    args = parser.parse_args()
    args.coin = args.coin.strip().upper()
    if args.coin not in COIN_CONFIGS:
        parser.error(f"--coin must be one of: {', '.join(sorted(COIN_CONFIGS))}")
    args.coin_config = COIN_CONFIGS[args.coin]
    if args.data_dir is None:
        args.data_dir = APP_DIR / args.coin_config.data_dir_name
    args.spot_symbol = args.spot_symbol.strip().lower() or args.coin_config.spot_symbol
    args.slug = args.slug.strip() or None
    return args


def main(default_coin: str = DEFAULT_COIN) -> int:
    try:
        asyncio.run(run(parse_args(default_coin)))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"{iso_now()} fatal: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


def main_with_default_coin(default_coin: str) -> int:
    return main(default_coin)


if __name__ == "__main__":
    raise SystemExit(main())
