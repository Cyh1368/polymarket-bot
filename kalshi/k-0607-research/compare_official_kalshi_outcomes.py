#!/usr/bin/env python3
"""Compare trader-inferred outcomes with official Kalshi market results."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_URL = "https://external-api.kalshi.com/trade-api/v2/markets"
DEFAULT_CHUNK_SIZE = 50


def normalize_side(value: str | None) -> str:
    if value is None:
        return ""
    normalized = value.strip().lower()
    if normalized in {"", "missing", "none", "null", "nan"}:
        return ""
    if normalized in {"yes", "y", "1", "true"}:
        return "YES"
    if normalized in {"no", "n", "0", "false"}:
        return "NO"
    return value.strip().upper()


def official_result(market: dict[str, Any]) -> str:
    result = normalize_side(market.get("result"))
    if result in {"YES", "NO"}:
        return result

    value = str(market.get("settlement_value_dollars", "")).strip()
    if value:
        try:
            numeric = float(value)
        except ValueError:
            return ""
        if numeric == 1.0:
            return "YES"
        if numeric == 0.0:
            return "NO"
    return ""


def fetch_markets(tickers: list[str], chunk_size: int, pause_seconds: float) -> dict[str, dict[str, Any]]:
    markets: dict[str, dict[str, Any]] = {}
    for start in range(0, len(tickers), chunk_size):
        chunk = tickers[start : start + chunk_size]
        query = urllib.parse.urlencode({"tickers": ",".join(chunk), "limit": str(len(chunk))})
        request = urllib.request.Request(f"{BASE_URL}?{query}", headers={"User-Agent": "kalshi-outcome-audit/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for market in payload.get("markets", []):
            ticker = market.get("ticker")
            if ticker:
                markets[ticker] = market
        if pause_seconds and start + chunk_size < len(tickers):
            time.sleep(pause_seconds)
    return markets


def choose_outcome_row(rows: list[dict[str, str]]) -> dict[str, str] | None:
    outcome_rows = [row for row in rows if row.get("event") == "outcome"]
    if not outcome_rows:
        return None
    return outcome_rows[-1]


def compare(rows: list[dict[str, str]], markets: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["contract_id"]].append(row)

    comparisons: list[dict[str, str]] = []
    for contract_id in sorted(grouped):
        contract_rows = grouped[contract_id]
        first_row = contract_rows[0]
        outcome_row = choose_outcome_row(contract_rows)
        market = markets.get(contract_id, {})

        trader_side = ""
        if outcome_row:
            trader_side = normalize_side(outcome_row.get("actual_side")) or normalize_side(outcome_row.get("actual_label"))

        official_side = official_result(market)
        discrepancy = "NO"
        note = ""
        if not market:
            discrepancy = "UNKNOWN"
            note = "missing from Kalshi API response"
        elif not official_side:
            discrepancy = "UNKNOWN"
            note = "official result not available"
        elif not trader_side:
            discrepancy = "UNKNOWN"
            note = "no trader outcome row"
        elif trader_side != official_side:
            discrepancy = "YES"
            note = "trader outcome differs from official Kalshi result"

        comparisons.append(
            {
                "contract_id": contract_id,
                "close_time": first_row.get("close_time", ""),
                "row_count": str(len(contract_rows)),
                "has_outcome_row": "1" if outcome_row else "0",
                "trader_actual_side": trader_side,
                "trader_actual_label": outcome_row.get("actual_label", "") if outcome_row else "",
                "trader_kalshi_price": outcome_row.get("kalshi_price", "") if outcome_row else "",
                "trader_kalshi_target": outcome_row.get("kalshi_target", "") if outcome_row else "",
                "trader_kalshi_target_source": outcome_row.get("kalshi_target_source", "") if outcome_row else "",
                "official_result": official_side,
                "official_raw_result": str(market.get("result", "")),
                "official_status": str(market.get("status", "")),
                "official_settlement_ts": str(market.get("settlement_ts", "")),
                "official_settlement_value_dollars": str(market.get("settlement_value_dollars", "")),
                "official_expiration_value": str(market.get("expiration_value", "")),
                "official_floor_strike": str(market.get("floor_strike", "")),
                "official_title": str(market.get("title", "")),
                "discrepancy": discrepancy,
                "note": note,
            }
        )
    return comparisons


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: Path, source_csv: Path, comparisons: list[dict[str, str]], markets: dict[str, dict[str, Any]]) -> None:
    counts = Counter(row["discrepancy"] for row in comparisons)
    official_counts = Counter(row["official_result"] or "MISSING" for row in comparisons)
    trader_counts = Counter(row["trader_actual_side"] or "MISSING" for row in comparisons)
    discrepancies = [row for row in comparisons if row["discrepancy"] == "YES"]
    unknowns = [row for row in comparisons if row["discrepancy"] == "UNKNOWN"]
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    lines = [
        "# Official Kalshi Outcome Audit",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Source CSV: `{source_csv.name}`",
        f"- Official API: `{BASE_URL}?tickers=...`",
        f"- Unique contracts in CSV: `{len(comparisons)}`",
        f"- Markets returned by Kalshi API: `{len(markets)}`",
        f"- Discrepancies: `{counts.get('YES', 0)}`",
        f"- Matches: `{counts.get('NO', 0)}`",
        f"- Unknown comparisons: `{counts.get('UNKNOWN', 0)}`",
        "",
        "## Outcome Counts",
        "",
        "| Source | YES | NO | Missing |",
        "| --- | ---: | ---: | ---: |",
        f"| Trader-inferred | {trader_counts.get('YES', 0)} | {trader_counts.get('NO', 0)} | {trader_counts.get('MISSING', 0)} |",
        f"| Official Kalshi | {official_counts.get('YES', 0)} | {official_counts.get('NO', 0)} | {official_counts.get('MISSING', 0)} |",
        "",
        "## Discrepancies",
        "",
    ]

    if discrepancies:
        lines.extend(
            [
                "| Contract | Close Time | Trader Outcome | Official Outcome | Trader Price | Target | Official Expiration Value | Official Settlement Time |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for row in discrepancies:
            lines.append(
                "| {contract_id} | {close_time} | {trader_actual_side} | {official_result} | {trader_kalshi_price} | {trader_kalshi_target} | {official_expiration_value} | {official_settlement_ts} |".format(
                    **row
                )
            )
    else:
        lines.append("No trader-vs-official outcome discrepancies were found.")

    lines.extend(["", "## Unknown Comparisons", ""])
    if unknowns:
        lines.extend(["| Contract | Close Time | Trader Outcome | Official Outcome | Note |", "| --- | --- | ---: | ---: | --- |"])
        for row in unknowns:
            lines.append(
                "| {contract_id} | {close_time} | {trader_actual_side} | {official_result} | {note} |".format(
                    **row
                )
            )
    else:
        lines.append("No unknown comparisons.")

    path.write_text("\n".join(lines) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="kalshi_trader_trades.csv", help="Trade CSV to audit")
    parser.add_argument("--output-csv", default="kalshi_official_outcome_comparison.csv", help="Detailed comparison CSV")
    parser.add_argument("--output-report", default="kalshi_official_outcome_report.md", help="Markdown discrepancy report")
    parser.add_argument("--cache-json", default="kalshi_official_market_results.json", help="Raw official market response cache")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Tickers per Kalshi API request")
    parser.add_argument("--pause-seconds", type=float, default=0.1, help="Pause between batched API requests")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(__file__).resolve().parent
    input_path = (workdir / args.input).resolve()
    output_csv = (workdir / args.output_csv).resolve()
    output_report = (workdir / args.output_report).resolve()
    cache_json = (workdir / args.cache_json).resolve()

    with input_path.open(newline="") as input_file:
        rows = list(csv.DictReader(input_file))
    tickers = sorted({row["contract_id"] for row in rows if row.get("contract_id")})

    markets = fetch_markets(tickers, args.chunk_size, args.pause_seconds)
    missing = sorted(set(tickers) - set(markets))
    if missing:
        print(f"warning: {len(missing)} tickers missing from API response", file=sys.stderr)

    cache_json.write_text(json.dumps({"markets": markets, "missing_tickers": missing}, indent=2, sort_keys=True) + "\n")
    comparisons = compare(rows, markets)
    write_csv(output_csv, comparisons)
    write_report(output_report, input_path, comparisons, markets)

    counts = Counter(row["discrepancy"] for row in comparisons)
    print(
        json.dumps(
            {
                "contracts": len(comparisons),
                "markets_returned": len(markets),
                "matches": counts.get("NO", 0),
                "discrepancies": counts.get("YES", 0),
                "unknown": counts.get("UNKNOWN", 0),
                "output_csv": str(output_csv),
                "output_report": str(output_report),
                "cache_json": str(cache_json),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
