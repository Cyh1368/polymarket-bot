"""Collect BTC 15-minute Polymarket labels and Binance validation candles.

Outputs:
  data/btc_updown_15m/polymarket_btc_15m.csv
  data/btc_updown_15m/polymarket_btc_15m_histories.jsonl
  data/btc_updown_15m/binance_btcusdt_15m.csv
  data/btc_updown_15m/btc_15m_joined_validation.csv

The Polymarket event slug format is:
  btc-updown-15m-{event_start_epoch_seconds}
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"
BINANCE_BASE_URL = "https://api.binance.com"
BTC_15M_SECONDS = 15 * 60


@dataclass
class PolymarketRow:
    slug: str
    title: str
    condition_id: str
    market_id: str
    event_start_ts: int
    event_start_utc: str
    end_ts: int
    end_utc: str
    closed: bool
    winner: str
    up_final_price: float | None
    down_final_price: float | None
    up_token_id: str
    down_token_id: str
    volume: float | None
    liquidity: float | None
    last_trade_price: float | None
    best_bid: float | None
    best_ask: float | None


def parse_list(value: Any) -> list[Any]:
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


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def winner_from_prices(outcomes: list[Any], prices: list[float]) -> str:
    if len(outcomes) != 2 or len(prices) != 2:
        return ""
    return str(outcomes[max(range(len(prices)), key=lambda i: prices[i])])


def fetch_event(client: httpx.Client, slug: str) -> dict[str, Any] | None:
    response = client.get(f"{GAMMA_BASE_URL}/events/slug/{slug}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


async def fetch_event_async(
    client: httpx.AsyncClient,
    slug: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, dict[str, Any] | None]:
    async with semaphore:
        response = await client.get(f"{GAMMA_BASE_URL}/events/slug/{slug}")
    if response.status_code == 404:
        return slug, None
    response.raise_for_status()
    return slug, response.json()


def fetch_price_history(
    client: httpx.Client,
    token_id: str,
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    response = client.get(
        f"{CLOB_BASE_URL}/prices-history",
        params={
            "market": token_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "interval": "1m",
            "fidelity": 10,
        },
    )
    if response.status_code == 400:
        return []
    response.raise_for_status()
    return response.json().get("history", [])


async def fetch_price_history_async(
    client: httpx.AsyncClient,
    token_id: str,
    start_ts: int,
    end_ts: int,
    semaphore: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    async with semaphore:
        response = await client.get(
            f"{CLOB_BASE_URL}/prices-history",
            params={
                "market": token_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "interval": "1m",
                "fidelity": 10,
            },
        )
    if response.status_code == 400:
        return []
    response.raise_for_status()
    return response.json().get("history", [])


def parse_polymarket_event(event: dict[str, Any], slug: str) -> PolymarketRow | None:
    markets = event.get("markets") or []
    if not markets:
        return None

    market = markets[0]
    if not market.get("closed"):
        return None

    outcomes = [str(item) for item in parse_list(market.get("outcomes"))]
    prices = [float(item) for item in parse_list(market.get("outcomePrices"))]
    tokens = [str(item) for item in parse_list(market.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(tokens) != 2:
        return None

    token_by_outcome = dict(zip(outcomes, tokens))
    price_by_outcome = dict(zip(outcomes, prices))
    start = parse_dt(market.get("eventStartTime")) or parse_dt(market.get("startDate"))
    end = parse_dt(market.get("endDate"))
    if not start or not end:
        return None

    return PolymarketRow(
        slug=slug,
        title=event.get("title") or market.get("question") or "",
        condition_id=market.get("conditionId") or "",
        market_id=str(market.get("id") or ""),
        event_start_ts=int(start.timestamp()),
        event_start_utc=start.isoformat(),
        end_ts=int(end.timestamp()),
        end_utc=end.isoformat(),
        closed=bool(market.get("closed")),
        winner=winner_from_prices(outcomes, prices),
        up_final_price=as_float(price_by_outcome.get("Up")),
        down_final_price=as_float(price_by_outcome.get("Down")),
        up_token_id=token_by_outcome.get("Up", ""),
        down_token_id=token_by_outcome.get("Down", ""),
        volume=as_float(market.get("volume") or market.get("volumeNum")),
        liquidity=as_float(market.get("liquidity") or market.get("liquidityNum")),
        last_trade_price=as_float(market.get("lastTradePrice")),
        best_bid=as_float(market.get("bestBid")),
        best_ask=as_float(market.get("bestAsk")),
    )


def collect_polymarket(
    output_dir: Path,
    days: int | None,
    max_consecutive_misses: int,
    sleep_seconds: float,
) -> list[PolymarketRow]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[PolymarketRow] = []
    histories_path = output_dir / "polymarket_btc_15m_histories.jsonl"

    now_ts = int(datetime.now(timezone.utc).timestamp())
    current_slot = now_ts // BTC_15M_SECONDS * BTC_15M_SECONDS
    oldest_slot = 0 if days is None else current_slot - days * 24 * 60 * 60
    misses = 0

    with httpx.Client(timeout=30.0) as client, histories_path.open("w", encoding="utf-8") as histories_file:
        slot = current_slot
        while slot >= oldest_slot and misses < max_consecutive_misses:
            slug = f"btc-updown-15m-{slot}"
            event = fetch_event(client, slug)
            if event is None:
                misses += 1
                slot -= BTC_15M_SECONDS
                continue

            misses = 0
            row = parse_polymarket_event(event, slug)
            if row:
                rows.append(row)
                up_history = fetch_price_history(
                    client, row.up_token_id, row.event_start_ts, row.end_ts
                )
                down_history = fetch_price_history(
                    client, row.down_token_id, row.event_start_ts, row.end_ts
                )
                histories_file.write(
                    json.dumps(
                        {
                            "slug": row.slug,
                            "event_start_ts": row.event_start_ts,
                            "end_ts": row.end_ts,
                            "up_token_id": row.up_token_id,
                            "down_token_id": row.down_token_id,
                            "up_history": up_history,
                            "down_history": down_history,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )

            if len(rows) % 100 == 0 and rows:
                write_polymarket_csv(output_dir / "polymarket_btc_15m.csv", rows)
                print(f"collected_polymarket_rows={len(rows)} latest_slot={slot}")

            slot -= BTC_15M_SECONDS
            if sleep_seconds:
                time.sleep(sleep_seconds)

    write_polymarket_csv(output_dir / "polymarket_btc_15m.csv", rows)
    return rows


async def collect_polymarket_async(
    output_dir: Path,
    days: int | None,
    max_consecutive_misses: int,
    concurrency: int,
) -> list[PolymarketRow]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[PolymarketRow] = []
    histories: list[dict[str, Any]] = []

    now_ts = int(datetime.now(timezone.utc).timestamp())
    current_slot = now_ts // BTC_15M_SECONDS * BTC_15M_SECONDS
    oldest_slot = 0 if days is None else current_slot - days * 24 * 60 * 60
    batch_size = max(max_consecutive_misses, concurrency)
    event_semaphore = asyncio.Semaphore(concurrency)
    history_semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=30.0) as client:
        slot = current_slot
        found_any = False
        empty_batches_after_data = 0

        while slot >= oldest_slot:
            slots = [
                candidate
                for candidate in range(
                    slot,
                    max(slot - batch_size * BTC_15M_SECONDS, oldest_slot - BTC_15M_SECONDS),
                    -BTC_15M_SECONDS,
                )
            ]
            event_tasks = [
                fetch_event_async(client, f"btc-updown-15m-{candidate}", event_semaphore)
                for candidate in slots
            ]
            event_results = await asyncio.gather(*event_tasks)
            parsed_rows: list[PolymarketRow] = []
            for slug, event in event_results:
                if not event:
                    continue
                row = parse_polymarket_event(event, slug)
                if row:
                    parsed_rows.append(row)

            if parsed_rows:
                found_any = True
                empty_batches_after_data = 0
            elif found_any:
                empty_batches_after_data += 1
                if empty_batches_after_data * batch_size >= max_consecutive_misses:
                    break

            parsed_rows.sort(key=lambda item: item.event_start_ts)
            rows.extend(parsed_rows)

            history_tasks = []
            for row in parsed_rows:
                history_tasks.append(
                    fetch_price_history_async(
                        client,
                        row.up_token_id,
                        row.event_start_ts,
                        row.end_ts,
                        history_semaphore,
                    )
                )
                history_tasks.append(
                    fetch_price_history_async(
                        client,
                        row.down_token_id,
                        row.event_start_ts,
                        row.end_ts,
                        history_semaphore,
                    )
                )
            history_results = await asyncio.gather(*history_tasks) if history_tasks else []
            for index, row in enumerate(parsed_rows):
                histories.append(
                    {
                        "slug": row.slug,
                        "event_start_ts": row.event_start_ts,
                        "end_ts": row.end_ts,
                        "up_token_id": row.up_token_id,
                        "down_token_id": row.down_token_id,
                        "up_history": history_results[index * 2],
                        "down_history": history_results[index * 2 + 1],
                    }
                )

            if parsed_rows:
                write_polymarket_csv(output_dir / "polymarket_btc_15m.csv", rows)
                write_histories_jsonl(
                    output_dir / "polymarket_btc_15m_histories.jsonl", histories
                )
                oldest = min(item.event_start_utc for item in parsed_rows)
                newest = max(item.event_start_utc for item in parsed_rows)
                print(
                    f"collected_polymarket_rows={len(rows)} "
                    f"batch_rows={len(parsed_rows)} batch_range={oldest}..{newest}",
                    flush=True,
                )

            slot = slots[-1] - BTC_15M_SECONDS

    rows.sort(key=lambda item: item.event_start_ts)
    histories.sort(key=lambda item: item["event_start_ts"])
    write_polymarket_csv(output_dir / "polymarket_btc_15m.csv", rows)
    write_histories_jsonl(output_dir / "polymarket_btc_15m_histories.jsonl", histories)
    return rows


def write_histories_jsonl(path: Path, histories: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for item in histories:
            file.write(json.dumps(item, separators=(",", ":")) + "\n")


def write_polymarket_csv(path: Path, rows: list[PolymarketRow]) -> None:
    fieldnames = list(PolymarketRow.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted(rows, key=lambda item: item.event_start_ts):
            writer.writerow(row.__dict__)


def fetch_binance_klines(
    client: httpx.Client,
    start_ts: int,
    end_ts: int,
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    start_ms = start_ts * 1000
    end_ms = end_ts * 1000
    while start_ms <= end_ms:
        response = client.get(
            f"{BINANCE_BASE_URL}/api/v3/klines",
            params={
                "symbol": "BTCUSDT",
                "interval": "15m",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        rows.extend(batch)
        next_start = int(batch[-1][0]) + BTC_15M_SECONDS * 1000
        if next_start <= start_ms:
            break
        start_ms = next_start
    return rows


def write_binance_csv(path: Path, klines: list[list[Any]]) -> None:
    fieldnames = [
        "open_time_ts",
        "open_time_utc",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time_ts",
        "close_time_utc",
        "quote_asset_volume",
        "number_of_trades",
        "taker_buy_base_asset_volume",
        "taker_buy_quote_asset_volume",
        "binance_direction",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for item in klines:
            open_ts = int(item[0]) // 1000
            close_ts = int(item[6]) // 1000
            open_price = float(item[1])
            close_price = float(item[4])
            writer.writerow(
                {
                    "open_time_ts": open_ts,
                    "open_time_utc": datetime.fromtimestamp(open_ts, timezone.utc).isoformat(),
                    "open": open_price,
                    "high": item[2],
                    "low": item[3],
                    "close": close_price,
                    "volume": item[5],
                    "close_time_ts": close_ts,
                    "close_time_utc": datetime.fromtimestamp(close_ts, timezone.utc).isoformat(),
                    "quote_asset_volume": item[7],
                    "number_of_trades": item[8],
                    "taker_buy_base_asset_volume": item[9],
                    "taker_buy_quote_asset_volume": item[10],
                    "binance_direction": "Up" if close_price >= open_price else "Down",
                }
            )


def collect_binance(output_dir: Path, rows: list[PolymarketRow]) -> list[list[Any]]:
    if not rows:
        return []
    start_ts = min(row.event_start_ts for row in rows)
    end_ts = max(row.end_ts for row in rows)
    with httpx.Client(timeout=30.0) as client:
        klines = fetch_binance_klines(client, start_ts, end_ts)
    write_binance_csv(output_dir / "binance_btcusdt_15m.csv", klines)
    return klines


def write_joined_validation(
    output_dir: Path,
    rows: list[PolymarketRow],
    klines: list[list[Any]],
) -> None:
    binance_by_open_ts = {int(item[0]) // 1000: item for item in klines}
    path = output_dir / "btc_15m_joined_validation.csv"
    fieldnames = [
        "slug",
        "event_start_ts",
        "event_start_utc",
        "end_utc",
        "polymarket_winner",
        "binance_open",
        "binance_close",
        "binance_direction",
        "matches_binance",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted(rows, key=lambda item: item.event_start_ts):
            kline = binance_by_open_ts.get(row.event_start_ts)
            if not kline:
                continue
            open_price = float(kline[1])
            close_price = float(kline[4])
            direction = "Up" if close_price >= open_price else "Down"
            writer.writerow(
                {
                    "slug": row.slug,
                    "event_start_ts": row.event_start_ts,
                    "event_start_utc": row.event_start_utc,
                    "end_utc": row.end_utc,
                    "polymarket_winner": row.winner,
                    "binance_open": open_price,
                    "binance_close": close_price,
                    "binance_direction": direction,
                    "matches_binance": row.winner == direction,
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default="data/btc_updown_15m",
        help="Directory for collected CSV/JSONL files.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Collect this many days back. Omit to continue until missing slugs stop the scan.",
    )
    parser.add_argument(
        "--max-consecutive-misses",
        type=int,
        default=384,
        help="Stop after this many missing 15-minute slugs.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.02,
        help="Small delay between Gamma requests for the legacy sequential collector.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=20,
        help="Max concurrent Polymarket API requests.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    rows = asyncio.run(
        collect_polymarket_async(
            output_dir=output_dir,
            days=args.days,
            max_consecutive_misses=args.max_consecutive_misses,
            concurrency=args.concurrency,
        )
    )
    klines = collect_binance(output_dir, rows)
    write_joined_validation(output_dir, rows, klines)

    print(f"polymarket_rows={len(rows)}")
    print(f"binance_klines={len(klines)}")
    print(f"output_dir={output_dir}")


if __name__ == "__main__":
    main()
