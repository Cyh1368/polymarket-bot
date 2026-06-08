"""Live BTC 15-minute Up/Down Polymarket order book collector.

Polls the current market once per second and stores order book snapshots in SQLite.

Example:
  python3 scripts/live_btc_orderbook_collector.py
  python3 scripts/live_btc_orderbook_collector.py --once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"
BTC_15M_SECONDS = 15 * 60
DEFAULT_ORDER_IMBALANCE_TAUS = (0.01, 0.02, 0.05, 0.10)


def parse_order_imbalance_taus(raw: str) -> tuple[float, ...]:
    values: list[float] = []
    for item in raw.split(","):
        try:
            value = float(item.strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return tuple(dict.fromkeys(values)) or DEFAULT_ORDER_IMBALANCE_TAUS


def tau_label(value: float) -> str:
    cents_value = value * 100.0
    if abs(cents_value - round(cents_value)) < 1e-9:
        return f"{int(round(cents_value))}c"
    return str(value).replace(".", "p").replace("-", "m")


@dataclass
class CurrentMarket:
    slug: str
    title: str
    market_id: str
    condition_id: str
    event_start_ts: int
    end_ts: int
    up_token_id: str
    down_token_id: str


def utc_now_ts() -> int:
    return int(time.time())


def utc_iso(ts: int | None = None) -> str:
    value = utc_now_ts() if ts is None else ts
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    return []


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS markets (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            market_id TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            event_start_ts INTEGER NOT NULL,
            event_start_utc TEXT NOT NULL,
            end_ts INTEGER NOT NULL,
            end_utc TEXT NOT NULL,
            up_token_id TEXT NOT NULL,
            down_token_id TEXT NOT NULL,
            first_seen_ts INTEGER NOT NULL,
            last_seen_ts INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            token_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            collected_ts INTEGER NOT NULL,
            collected_utc TEXT NOT NULL,
            book_timestamp TEXT,
            book_hash TEXT,
            best_bid REAL,
            best_ask REAL,
            mid_price REAL,
            spread REAL,
            last_trade_price REAL,
            raw_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_slug_time
            ON orderbook_snapshots(slug, collected_ts);
        CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_token_time
            ON orderbook_snapshots(token_id, collected_ts);

        CREATE TABLE IF NOT EXISTS orderbook_levels (
            snapshot_id INTEGER NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            size REAL NOT NULL,
            level_index INTEGER NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES orderbook_snapshots(id)
        );

        CREATE INDEX IF NOT EXISTS idx_orderbook_levels_snapshot
            ON orderbook_levels(snapshot_id);

        CREATE TABLE IF NOT EXISTS orderbook_imbalance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            collected_ts INTEGER NOT NULL,
            collected_utc TEXT NOT NULL,
            tau REAL NOT NULL,
            tau_label TEXT NOT NULL,
            up_snapshot_id INTEGER NOT NULL,
            down_snapshot_id INTEGER NOT NULL,
            up_bid_depth REAL NOT NULL,
            up_ask_depth REAL NOT NULL,
            up_book_imbalance REAL,
            down_bid_depth REAL NOT NULL,
            down_ask_depth REAL NOT NULL,
            down_book_imbalance REAL,
            up_down_bid_imbalance REAL,
            up_down_ask_imbalance REAL,
            FOREIGN KEY(up_snapshot_id) REFERENCES orderbook_snapshots(id),
            FOREIGN KEY(down_snapshot_id) REFERENCES orderbook_snapshots(id)
        );

        CREATE INDEX IF NOT EXISTS idx_orderbook_imbalance_slug_time
            ON orderbook_imbalance_snapshots(slug, collected_ts);
        CREATE INDEX IF NOT EXISTS idx_orderbook_imbalance_tau
            ON orderbook_imbalance_snapshots(tau_label, collected_ts);
        """
    )
    return conn


def upsert_market(conn: sqlite3.Connection, market: CurrentMarket) -> None:
    now = utc_now_ts()
    conn.execute(
        """
        INSERT INTO markets (
            slug, title, market_id, condition_id, event_start_ts, event_start_utc,
            end_ts, end_utc, up_token_id, down_token_id, first_seen_ts, last_seen_ts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            title=excluded.title,
            market_id=excluded.market_id,
            condition_id=excluded.condition_id,
            event_start_ts=excluded.event_start_ts,
            event_start_utc=excluded.event_start_utc,
            end_ts=excluded.end_ts,
            end_utc=excluded.end_utc,
            up_token_id=excluded.up_token_id,
            down_token_id=excluded.down_token_id,
            last_seen_ts=excluded.last_seen_ts
        """,
        (
            market.slug,
            market.title,
            market.market_id,
            market.condition_id,
            market.event_start_ts,
            utc_iso(market.event_start_ts),
            market.end_ts,
            utc_iso(market.end_ts),
            market.up_token_id,
            market.down_token_id,
            now,
            now,
        ),
    )
    conn.commit()


def price_size(item: dict[str, Any]) -> tuple[float, float]:
    return float(item["price"]), float(item["size"])


def best_bid_ask(book: dict[str, Any]) -> tuple[float | None, float | None]:
    bids = [price_size(item)[0] for item in book.get("bids", [])]
    asks = [price_size(item)[0] for item in book.get("asks", [])]
    best_bid = max(bids) if bids else None
    best_ask = min(asks) if asks else None
    return best_bid, best_ask


def levels_to_depth_map(levels: Any) -> dict[float, float]:
    depth: dict[float, float] = {}
    if not isinstance(levels, list):
        return depth
    for item in levels:
        if not isinstance(item, dict):
            continue
        try:
            price = float(item["price"])
            size = float(item["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if size > 0:
            depth[price] = depth.get(price, 0.0) + size
    return depth


def depth_within_tau(levels: Any, tau: float, *, side: str) -> float:
    depth = levels_to_depth_map(levels)
    if not depth:
        return 0.0
    if side == "ask":
        reference = min(depth)
        return sum(size for price, size in depth.items() if price <= reference + tau + 1e-12)
    reference = max(depth)
    return sum(size for price, size in depth.items() if price >= reference - tau - 1e-12)


def imbalance_value(left_depth: float, right_depth: float) -> float | None:
    total = left_depth + right_depth
    if total <= 0:
        return None
    return (left_depth - right_depth) / total


def insert_order_imbalance_snapshots(
    conn: sqlite3.Connection,
    market: CurrentMarket,
    up_snapshot_id: int,
    down_snapshot_id: int,
    up_book: dict[str, Any],
    down_book: dict[str, Any],
    tau_values: tuple[float, ...] = DEFAULT_ORDER_IMBALANCE_TAUS,
) -> None:
    collected_ts = utc_now_ts()
    rows = []
    for tau in tau_values:
        up_bid_depth = depth_within_tau(up_book.get("bids", []), tau, side="bid")
        up_ask_depth = depth_within_tau(up_book.get("asks", []), tau, side="ask")
        down_bid_depth = depth_within_tau(down_book.get("bids", []), tau, side="bid")
        down_ask_depth = depth_within_tau(down_book.get("asks", []), tau, side="ask")
        rows.append(
            (
                market.slug,
                collected_ts,
                utc_iso(collected_ts),
                tau,
                tau_label(tau),
                up_snapshot_id,
                down_snapshot_id,
                up_bid_depth,
                up_ask_depth,
                imbalance_value(up_bid_depth, up_ask_depth),
                down_bid_depth,
                down_ask_depth,
                imbalance_value(down_bid_depth, down_ask_depth),
                imbalance_value(up_bid_depth, down_bid_depth),
                imbalance_value(down_ask_depth, up_ask_depth),
            )
        )
    conn.executemany(
        """
        INSERT INTO orderbook_imbalance_snapshots (
            slug, collected_ts, collected_utc, tau, tau_label,
            up_snapshot_id, down_snapshot_id,
            up_bid_depth, up_ask_depth, up_book_imbalance,
            down_bid_depth, down_ask_depth, down_book_imbalance,
            up_down_bid_imbalance, up_down_ask_imbalance
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def insert_book(
    conn: sqlite3.Connection,
    market: CurrentMarket,
    token_id: str,
    outcome: str,
    book: dict[str, Any],
) -> int:
    collected_ts = utc_now_ts()
    best_bid, best_ask = best_bid_ask(book)
    mid_price = None
    spread = None
    if best_bid is not None and best_ask is not None:
        mid_price = (best_bid + best_ask) / 2
        spread = best_ask - best_bid

    cursor = conn.execute(
        """
        INSERT INTO orderbook_snapshots (
            slug, token_id, outcome, collected_ts, collected_utc, book_timestamp,
            book_hash, best_bid, best_ask, mid_price, spread, last_trade_price, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market.slug,
            token_id,
            outcome,
            collected_ts,
            utc_iso(collected_ts),
            str(book.get("timestamp") or ""),
            str(book.get("hash") or ""),
            best_bid,
            best_ask,
            mid_price,
            spread,
            float(book["last_trade_price"]) if book.get("last_trade_price") else None,
            json.dumps(book, separators=(",", ":")),
        ),
    )
    snapshot_id = int(cursor.lastrowid)

    level_rows = []
    for side in ("bids", "asks"):
        levels = sorted(
            book.get(side, []),
            key=lambda item: float(item["price"]),
            reverse=side == "bids",
        )
        for index, level in enumerate(levels):
            price, size = price_size(level)
            level_rows.append((snapshot_id, side[:-1], price, size, index))

    conn.executemany(
        """
        INSERT INTO orderbook_levels (snapshot_id, side, price, size, level_index)
        VALUES (?, ?, ?, ?, ?)
        """,
        level_rows,
    )
    conn.commit()
    return snapshot_id


async def fetch_event(client: httpx.AsyncClient, slug: str) -> dict[str, Any] | None:
    response = await client.get(f"{GAMMA_BASE_URL}/events/slug/{slug}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def parse_current_market(event: dict[str, Any]) -> CurrentMarket | None:
    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    if market.get("closed"):
        return None

    outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
    token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds"))]
    if len(outcomes) != len(token_ids):
        return None
    token_by_outcome = dict(zip(outcomes, token_ids))
    if not token_by_outcome.get("Up") or not token_by_outcome.get("Down"):
        return None

    start = parse_dt(market.get("eventStartTime")) or parse_dt(market.get("startDate"))
    end = parse_dt(market.get("endDate"))
    if not start or not end:
        return None

    now = datetime.now(timezone.utc)
    if not (start <= now <= end):
        return None

    return CurrentMarket(
        slug=event.get("slug") or market.get("slug") or "",
        title=event.get("title") or market.get("question") or "",
        market_id=str(market.get("id") or ""),
        condition_id=market.get("conditionId") or "",
        event_start_ts=int(start.timestamp()),
        end_ts=int(end.timestamp()),
        up_token_id=token_by_outcome["Up"],
        down_token_id=token_by_outcome["Down"],
    )


async def discover_current_market(client: httpx.AsyncClient) -> CurrentMarket:
    current_slot = utc_now_ts() // BTC_15M_SECONDS * BTC_15M_SECONDS
    candidate_slots = [
        current_slot + offset * BTC_15M_SECONDS
        for offset in (0, -1, 1, -2, 2, -3, 3)
    ]
    for slot in candidate_slots:
        slug = f"btc-updown-15m-{slot}"
        event = await fetch_event(client, slug)
        if not event:
            continue
        market = parse_current_market(event)
        if market:
            return market
    raise RuntimeError("No active BTC 15-minute Up/Down market found")


async def fetch_book(client: httpx.AsyncClient, token_id: str) -> dict[str, Any]:
    response = await client.get(f"{CLOB_BASE_URL}/book", params={"token_id": token_id})
    response.raise_for_status()
    return response.json()


async def poll_once(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    market: CurrentMarket,
    tau_values: tuple[float, ...] = DEFAULT_ORDER_IMBALANCE_TAUS,
) -> None:
    up_book, down_book = await asyncio.gather(
        fetch_book(client, market.up_token_id),
        fetch_book(client, market.down_token_id),
    )
    upsert_market(conn, market)
    up_snapshot_id = insert_book(conn, market, market.up_token_id, "Up", up_book)
    down_snapshot_id = insert_book(conn, market, market.down_token_id, "Down", down_book)
    insert_order_imbalance_snapshots(
        conn,
        market,
        up_snapshot_id,
        down_snapshot_id,
        up_book,
        down_book,
        tau_values,
    )

    up_bid, up_ask = best_bid_ask(up_book)
    down_bid, down_ask = best_bid_ask(down_book)
    print(
        f"{utc_iso()} {market.slug} "
        f"Up bid/ask={up_bid}/{up_ask} Down bid/ask={down_bid}/{down_ask}",
        flush=True,
    )


async def run(args: argparse.Namespace) -> None:
    conn = init_db(args.db)
    stop = asyncio.Event()

    def handle_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_stop)

    current_market: CurrentMarket | None = None
    async with httpx.AsyncClient(timeout=10.0) as client:
        while not stop.is_set():
            now = utc_now_ts()
            if (
                current_market is None
                or now >= current_market.end_ts
                or now < current_market.event_start_ts
            ):
                current_market = await discover_current_market(client)
                print(
                    f"tracking {current_market.slug} "
                    f"{utc_iso(current_market.event_start_ts)}..{utc_iso(current_market.end_ts)}",
                    flush=True,
                )

            started = time.monotonic()
            await poll_once(client, conn, current_market, args.imbalance_taus)
            if args.once:
                break
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, args.interval_seconds - elapsed))

    conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/live_orderbooks/btc_updown_orderbooks.sqlite"),
    )
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument(
        "--imbalance-taus",
        default=",".join(str(value) for value in DEFAULT_ORDER_IMBALANCE_TAUS),
        help="Comma-separated price-depth bands for order imbalance, e.g. 0.01,0.02,0.05,0.10.",
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    args.imbalance_taus = parse_order_imbalance_taus(args.imbalance_taus)
    return args


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
