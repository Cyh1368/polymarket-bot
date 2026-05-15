"""Refetch BTC Up/Down Polymarket price histories at finer available fidelity.

Uses POST /batch-prices-history with:
  interval=all
  fidelity=2

This returns materially more points than interval=1m,fidelity=10 for the
15-minute BTC Up/Down markets.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
from pathlib import Path
from typing import Any

import httpx


CLOB_BASE_URL = "https://clob.polymarket.com"
DEFAULT_INPUT = Path("data/btc_updown_15m/polymarket_btc_15m.csv")
DEFAULT_OUTPUT = Path("data/btc_updown_15m/polymarket_btc_15m_histories_all_fidelity2.jsonl")


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as file:
        return list(csv.DictReader(file))


async def fetch_market_history(
    client: httpx.AsyncClient,
    row: dict[str, str],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    body = {
        "markets": [row["up_token_id"], row["down_token_id"]],
        "start_ts": int(row["event_start_ts"]),
        "end_ts": int(row["end_ts"]),
        "interval": "all",
        "fidelity": 2,
    }
    response = None
    for attempt in range(5):
        try:
            async with semaphore:
                response = await client.post(
                    f"{CLOB_BASE_URL}/batch-prices-history", json=body
                )
            response.raise_for_status()
            break
        except (httpx.HTTPError, httpx.TimeoutException):
            if attempt == 4:
                raise
            await asyncio.sleep((2**attempt) + random.random())
    if response is None:
        raise RuntimeError("request did not complete")
    history = response.json().get("history", {})
    return {
        "slug": row["slug"],
        "event_start_ts": int(row["event_start_ts"]),
        "end_ts": int(row["end_ts"]),
        "up_token_id": row["up_token_id"],
        "down_token_id": row["down_token_id"],
        "interval": "all",
        "fidelity": 2,
        "up_history": history.get(row["up_token_id"], []),
        "down_history": history.get(row["down_token_id"], []),
    }


async def collect(input_path: Path, output_path: Path, concurrency: int, batch_size: int) -> int:
    rows = load_rows(input_path)
    done_slugs = set()
    if output_path.exists():
        with output_path.open(encoding="utf-8") as existing:
            for line in existing:
                if line.strip():
                    done_slugs.add(json.loads(line)["slug"])
    rows = [row for row in rows if row["slug"] not in done_slugs]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)
    total = 0

    with output_path.open("a", encoding="utf-8") as output:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for start in range(0, len(rows), batch_size):
                chunk = rows[start : start + batch_size]
                results = await asyncio.gather(
                    *[fetch_market_history(client, row, semaphore) for row in chunk]
                )
                results.sort(key=lambda item: item["event_start_ts"])
                for item in results:
                    output.write(json.dumps(item, separators=(",", ":")) + "\n")
                output.flush()
                total += len(results)
                print(
                    f"refetched_histories={len(done_slugs) + total}/"
                    f"{len(done_slugs) + len(rows)}",
                    flush=True,
                )
    return total


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument("--batch-size", type=int, default=384)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    total = asyncio.run(
        collect(
            input_path=args.input,
            output_path=args.output,
            concurrency=args.concurrency,
            batch_size=args.batch_size,
        )
    )
    print(f"done={total}")
    print(f"output={args.output}")


if __name__ == "__main__":
    main()
