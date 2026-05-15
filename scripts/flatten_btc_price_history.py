"""Flatten collected Polymarket BTC Up/Down token histories into CSV files."""

from __future__ import annotations

import csv
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path("data/btc_updown_15m")
HISTORIES_PATH = BASE_DIR / "polymarket_btc_15m_histories.jsonl"
LONG_OUTPUT_PATH = BASE_DIR / "polymarket_btc_15m_price_points_long.csv"
WIDE_OUTPUT_PATH = BASE_DIR / "polymarket_btc_15m_price_points_wide.csv"


def utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def flatten_long(histories_path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with histories_path.open(encoding="utf-8") as file:
        for line in file:
            item = json.loads(line)
            for outcome, token_field, history_field in (
                ("Up", "up_token_id", "up_history"),
                ("Down", "down_token_id", "down_history"),
            ):
                for point in item.get(history_field, []):
                    rows.append(
                        {
                            "slug": item["slug"],
                            "event_start_ts": item["event_start_ts"],
                            "event_start_utc": utc(item["event_start_ts"]),
                            "end_ts": item["end_ts"],
                            "end_utc": utc(item["end_ts"]),
                            "outcome": outcome,
                            "token_id": item[token_field],
                            "price_ts": int(point["t"]),
                            "price_utc": utc(int(point["t"])),
                            "price": float(point["p"]),
                            "seconds_from_start": int(point["t"]) - int(item["event_start_ts"]),
                            "seconds_to_end": int(item["end_ts"]) - int(point["t"]),
                        }
                    )
    rows.sort(key=lambda row: (row["event_start_ts"], row["price_ts"], row["outcome"]))
    return rows


def write_long(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "slug",
        "event_start_ts",
        "event_start_utc",
        "end_ts",
        "end_utc",
        "outcome",
        "token_id",
        "price_ts",
        "price_utc",
        "price",
        "seconds_from_start",
        "seconds_to_end",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_wide(histories_path: Path, output_path: Path) -> int:
    rows = []
    with histories_path.open(encoding="utf-8") as file:
        for line in file:
            item = json.loads(line)
            by_ts: dict[int, dict[str, object]] = {}
            for point in item.get("up_history", []):
                ts = int(point["t"])
                by_ts.setdefault(ts, {})["up_price"] = float(point["p"])
            for point in item.get("down_history", []):
                ts = int(point["t"])
                by_ts.setdefault(ts, {})["down_price"] = float(point["p"])

            last_up = None
            last_down = None
            first_up = None
            first_down = None
            for ts in sorted(by_ts):
                if "up_price" in by_ts[ts]:
                    last_up = by_ts[ts]["up_price"]
                    first_up = first_up if first_up is not None else last_up
                if "down_price" in by_ts[ts]:
                    last_down = by_ts[ts]["down_price"]
                    first_down = first_down if first_down is not None else last_down
                odds_ratio = None
                if last_up is not None and last_down not in (None, 0):
                    odds_ratio = float(last_up) / float(last_down)
                rows.append(
                    {
                        "slug": item["slug"],
                        "event_start_ts": item["event_start_ts"],
                        "event_start_utc": utc(item["event_start_ts"]),
                        "end_ts": item["end_ts"],
                        "end_utc": utc(item["end_ts"]),
                        "price_ts": ts,
                        "price_utc": utc(ts),
                        "up_price": last_up,
                        "down_price": last_down,
                        "up_change_from_first": None
                        if first_up is None or last_up is None
                        else float(last_up) - float(first_up),
                        "down_change_from_first": None
                        if first_down is None or last_down is None
                        else float(last_down) - float(first_down),
                        "up_down_odds_ratio": odds_ratio,
                        "seconds_from_start": ts - int(item["event_start_ts"]),
                        "seconds_to_end": int(item["end_ts"]) - ts,
                    }
                )

    rows.sort(key=lambda row: (row["event_start_ts"], row["price_ts"]))
    fieldnames = [
        "slug",
        "event_start_ts",
        "event_start_utc",
        "end_ts",
        "end_utc",
        "price_ts",
        "price_utc",
        "up_price",
        "down_price",
        "up_change_from_first",
        "down_change_from_first",
        "up_down_odds_ratio",
        "seconds_from_start",
        "seconds_to_end",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=HISTORIES_PATH)
    parser.add_argument("--long-output", type=Path, default=LONG_OUTPUT_PATH)
    parser.add_argument("--wide-output", type=Path, default=WIDE_OUTPUT_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    long_rows = flatten_long(args.input)
    write_long(args.long_output, long_rows)
    wide_count = write_wide(args.input, args.wide_output)
    print(f"long_price_points={len(long_rows)}")
    print(f"wide_price_points={wide_count}")
    print(f"long_output={args.long_output}")
    print(f"wide_output={args.wide_output}")


if __name__ == "__main__":
    main()
