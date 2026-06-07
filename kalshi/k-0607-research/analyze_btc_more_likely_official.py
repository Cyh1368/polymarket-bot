#!/usr/bin/env python3
"""Find Kalshi BTC more-likely-side mispricing using official outcomes."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BASE_URL = "https://external-api.kalshi.com/trade-api/v2/markets"
ET = ZoneInfo("America/New_York")
HORIZONS_SECONDS = list(range(60, 901, 60))
PRICE_BINS = [
    (0.50, 0.60),
    (0.60, 0.70),
    (0.70, 0.80),
    (0.80, 0.90),
    (0.90, 1.00),
]
AGGREGATE_BANDS = [
    (0.50, 1.00),
    (0.50, 0.80),
    (0.60, 0.80),
    (0.70, 0.90),
]


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


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


def bin_label(low: float, high: float) -> str:
    return f"{low:.2f}-{high:.2f}"


def price_bin(value: float, bins: list[tuple[float, float]]) -> tuple[float, float] | None:
    for low, high in bins:
        if low <= value < high or (high == 1.0 and low <= value <= high):
            return (low, high)
    return None


def official_result(market: dict[str, Any]) -> str:
    result = normalize_side(market.get("result"))
    if result in {"YES", "NO"}:
        return result

    settlement_value = parse_float(str(market.get("settlement_value_dollars", "")))
    if settlement_value == 1.0:
        return "YES"
    if settlement_value == 0.0:
        return "NO"
    return ""


def fetch_markets(tickers: list[str], chunk_size: int, pause_seconds: float) -> dict[str, dict[str, Any]]:
    markets: dict[str, dict[str, Any]] = {}
    for start in range(0, len(tickers), chunk_size):
        chunk = tickers[start : start + chunk_size]
        query = urllib.parse.urlencode({"tickers": ",".join(chunk), "limit": str(len(chunk))})
        request = urllib.request.Request(f"{BASE_URL}?{query}", headers={"User-Agent": "kalshi-btc-official-audit/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for market in payload.get("markets", []):
            ticker = market.get("ticker")
            if ticker:
                markets[ticker] = market
        if pause_seconds and start + chunk_size < len(tickers):
            time.sleep(pause_seconds)
    return markets


def load_contracts(data_dir: Path) -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    csv_paths = sorted(path for path in data_dir.glob("*.csv") if not path.name.endswith(":Zone.Identifier"))

    for path in csv_paths:
        with path.open(newline="") as input_file:
            raw_rows = list(csv.DictReader(input_file))
        if not raw_rows:
            continue

        ticker = raw_rows[0].get("kalshi_ticker") or path.stem.removeprefix("cli_predictor_polymarket_")
        close_time_raw = raw_rows[0].get("kalshi_close_time", "")
        if not ticker or not close_time_raw:
            continue
        close_time = parse_iso(close_time_raw)

        valid_rows: list[dict[str, Any]] = []
        invalid_quote_rows = 0
        for row in raw_rows:
            timestamp_raw = row.get("timestamp_utc") or row.get("kalshi_timestamp_utc")
            if not timestamp_raw:
                continue
            timestamp = parse_iso(timestamp_raw)
            yes_bid = parse_float(row.get("kalshi_yes_bid"))
            yes_ask = parse_float(row.get("kalshi_yes_ask"))
            no_bid = parse_float(row.get("kalshi_no_bid"))
            no_ask = parse_float(row.get("kalshi_no_ask"))
            yes_mid = parse_float(row.get("kalshi_yes_mid"))
            if None in {yes_bid, yes_ask, no_bid, no_ask, yes_mid}:
                continue
            assert yes_bid is not None
            assert yes_ask is not None
            assert no_bid is not None
            assert no_ask is not None
            assert yes_mid is not None
            if not (0 <= yes_bid <= yes_ask <= 1 and 0 <= no_bid <= no_ask <= 1 and 0 <= yes_mid <= 1):
                invalid_quote_rows += 1
                continue
            if yes_bid + no_bid > 1.000001:
                invalid_quote_rows += 1
                continue

            remaining_seconds = (close_time - timestamp).total_seconds()
            more_likely_side = "YES" if yes_mid >= 0.5 else "NO"
            more_likely_mid = yes_mid if more_likely_side == "YES" else 1.0 - yes_mid
            cost = yes_ask if more_likely_side == "YES" else no_ask
            valid_rows.append(
                {
                    "timestamp_utc": timestamp,
                    "remaining_seconds": remaining_seconds,
                    "yes_mid": yes_mid,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "more_likely_side": more_likely_side,
                    "more_likely_mid": more_likely_mid,
                    "cost": cost,
                    "source_row": row,
                }
            )

        contracts[ticker] = {
            "path": str(path),
            "ticker": ticker,
            "close_time": close_time,
            "close_time_raw": close_time_raw,
            "raw_row_count": len(raw_rows),
            "valid_rows": valid_rows,
            "invalid_quote_rows": invalid_quote_rows,
        }

    return contracts


def select_entry(valid_rows: list[dict[str, Any]], horizon_seconds: int, tolerance_seconds: float) -> dict[str, Any] | None:
    if not valid_rows:
        return None
    selected = min(valid_rows, key=lambda row: abs(row["remaining_seconds"] - horizon_seconds))
    if abs(selected["remaining_seconds"] - horizon_seconds) > tolerance_seconds:
        return None
    return selected


def build_entries(
    contracts: dict[str, dict[str, Any]],
    markets: dict[str, dict[str, Any]],
    horizons: list[int],
    tolerance_seconds: float,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for ticker in sorted(contracts):
        contract = contracts[ticker]
        market = markets.get(ticker, {})
        outcome = official_result(market)
        close_time = contract["close_time"]
        close_et = close_time.astimezone(ET)
        for horizon in horizons:
            selected = select_entry(contract["valid_rows"], horizon, tolerance_seconds)
            if not selected:
                continue
            single_bin = price_bin(selected["more_likely_mid"], PRICE_BINS)
            if not single_bin:
                continue
            success = int(bool(outcome) and selected["more_likely_side"] == outcome)
            pnl = (1.0 - selected["cost"]) if success else -selected["cost"]
            entries.append(
                {
                    "ticker": ticker,
                    "close_time_utc": close_time.isoformat().replace("+00:00", "Z"),
                    "close_date_utc": close_time.date().isoformat(),
                    "close_time_et": close_et.isoformat(),
                    "close_date_et": close_et.date().isoformat(),
                    "close_hour_et": str(close_et.hour),
                    "close_hour_bucket_et": f"{(close_et.hour // 4) * 4:02d}-{(close_et.hour // 4) * 4 + 3:02d}",
                    "horizon_seconds": str(horizon),
                    "selected_timestamp_utc": selected["timestamp_utc"].isoformat().replace("+00:00", "Z"),
                    "selected_remaining_seconds": f"{selected['remaining_seconds']:.3f}",
                    "horizon_abs_error_seconds": f"{abs(selected['remaining_seconds'] - horizon):.3f}",
                    "yes_mid": f"{selected['yes_mid']:.6f}",
                    "more_likely_side": selected["more_likely_side"],
                    "more_likely_mid": f"{selected['more_likely_mid']:.6f}",
                    "cost": f"{selected['cost']:.6f}",
                    "price_bin": bin_label(*single_bin),
                    "official_outcome": outcome,
                    "official_raw_result": str(market.get("result", "")),
                    "official_status": str(market.get("status", "")),
                    "official_settlement_ts": str(market.get("settlement_ts", "")),
                    "official_settlement_value_dollars": str(market.get("settlement_value_dollars", "")),
                    "official_expiration_value": str(market.get("expiration_value", "")),
                    "official_floor_strike": str(market.get("floor_strike", "")),
                    "resolved": "1" if outcome else "0",
                    "success": str(success) if outcome else "",
                    "gross_pnl": f"{pnl:.6f}" if outcome else "",
                }
            )
    return entries


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    phat = wins / n
    denominator = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denominator
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def summarize_entries(entries: list[dict[str, str]], band: tuple[float, float] | None = None) -> dict[str, Any]:
    resolved_entries = [entry for entry in entries if entry["resolved"] == "1"]
    if band is not None:
        low, high = band
        resolved_entries = [
            entry
            for entry in resolved_entries
            if low <= float(entry["more_likely_mid"]) < high or (high == 1.0 and low <= float(entry["more_likely_mid"]) <= high)
        ]
    n = len(resolved_entries)
    wins = sum(int(entry["success"]) for entry in resolved_entries)
    losses = n - wins
    avg_more_likely_mid = statistics.fmean(float(entry["more_likely_mid"]) for entry in resolved_entries) if n else 0.0
    avg_cost = statistics.fmean(float(entry["cost"]) for entry in resolved_entries) if n else 0.0
    gross_pnl = sum(float(entry["gross_pnl"]) for entry in resolved_entries) if n else 0.0
    p_success = wins / n if n else 0.0
    ev = p_success - avg_cost if n else 0.0
    wilson_low, wilson_high = wilson_interval(wins, n)
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "p_success": p_success,
        "p_wrong": losses / n if n else 0.0,
        "avg_more_likely_mid": avg_more_likely_mid,
        "avg_cost": avg_cost,
        "gross_pnl": gross_pnl,
        "ev": ev,
        "wilson_low": wilson_low,
        "wilson_high": wilson_high,
        "wilson_ev_low": wilson_low - avg_cost if n else 0.0,
        "wilson_ev_high": wilson_high - avg_cost if n else 0.0,
    }


def build_parameter_grid(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    by_horizon: dict[int, list[dict[str, str]]] = defaultdict(list)
    for entry in entries:
        by_horizon[int(entry["horizon_seconds"])].append(entry)

    rows: list[dict[str, str]] = []
    bands = [(low, high, "single_decile") for low, high in PRICE_BINS] + [
        (low, high, "aggregate") for low, high in AGGREGATE_BANDS
    ]
    for horizon in sorted(by_horizon):
        for low, high, band_type in bands:
            summary = summarize_entries(by_horizon[horizon], (low, high))
            rows.append(
                {
                    "horizon_seconds": str(horizon),
                    "price_bin": bin_label(low, high),
                    "band_low": f"{low:.2f}",
                    "band_high": f"{high:.2f}",
                    "band_type": band_type,
                    "n": str(summary["n"]),
                    "wins": str(summary["wins"]),
                    "losses": str(summary["losses"]),
                    "p_success": f"{summary['p_success']:.6f}",
                    "p_wrong": f"{summary['p_wrong']:.6f}",
                    "avg_more_likely_mid": f"{summary['avg_more_likely_mid']:.6f}",
                    "avg_cost": f"{summary['avg_cost']:.6f}",
                    "ev_p_minus_c": f"{summary['ev']:.6f}",
                    "gross_pnl": f"{summary['gross_pnl']:.6f}",
                    "wilson_p_success_low": f"{summary['wilson_low']:.6f}",
                    "wilson_p_success_high": f"{summary['wilson_high']:.6f}",
                    "wilson_ev_low": f"{summary['wilson_ev_low']:.6f}",
                    "wilson_ev_high": f"{summary['wilson_ev_high']:.6f}",
                }
            )
    return rows


def sort_grid(rows: list[dict[str, str]], min_n: int = 0, band_type: str | None = None) -> list[dict[str, str]]:
    filtered = [row for row in rows if int(row["n"]) >= min_n and (band_type is None or row["band_type"] == band_type)]
    return sorted(filtered, key=lambda row: (float(row["ev_p_minus_c"]), int(row["n"])), reverse=True)


def neighbor_rows(grid: list[dict[str, str]], chosen: dict[str, str], min_n: int) -> list[dict[str, str]]:
    horizon = int(chosen["horizon_seconds"])
    low = float(chosen["band_low"])
    high = float(chosen["band_high"])
    band_type = chosen["band_type"]
    rows: list[dict[str, str]] = []
    for row in grid:
        if row["band_type"] != band_type:
            continue
        if int(row["n"]) < min_n:
            continue
        row_horizon = int(row["horizon_seconds"])
        row_low = float(row["band_low"])
        row_high = float(row["band_high"])
        horizon_distance = abs(row_horizon - horizon)
        bin_distance = round(abs(row_low - low), 2)
        is_same = row_horizon == horizon and row_low == low and row_high == high
        is_neighbor = horizon_distance <= 60 and bin_distance <= 0.10 and not is_same
        if is_neighbor:
            rows.append(row)
    return sorted(rows, key=lambda row: (int(row["horizon_seconds"]), float(row["band_low"])))


def positive_component(grid: list[dict[str, str]], chosen: dict[str, str], min_n: int) -> list[dict[str, str]]:
    candidates = {
        (int(row["horizon_seconds"]), float(row["band_low"]), float(row["band_high"])): row
        for row in grid
        if row["band_type"] == chosen["band_type"] and int(row["n"]) >= min_n and float(row["ev_p_minus_c"]) > 0
    }
    start = (int(chosen["horizon_seconds"]), float(chosen["band_low"]), float(chosen["band_high"]))
    if start not in candidates:
        return []

    seen = {start}
    queue = [start]
    while queue:
        horizon, low, high = queue.pop(0)
        for key in candidates:
            if key in seen:
                continue
            key_horizon, key_low, _ = key
            if (abs(key_horizon - horizon) == 60 and key_low == low) or (key_horizon == horizon and round(abs(key_low - low), 2) == 0.10):
                seen.add(key)
                queue.append(key)
    return [candidates[key] for key in sorted(seen)]


def grouped_summaries(
    entries: list[dict[str, str]],
    group_field: str,
    chosen: dict[str, str] | None = None,
    min_n: int = 0,
) -> list[dict[str, str]]:
    if chosen is not None:
        horizon = chosen["horizon_seconds"]
        low = float(chosen["band_low"])
        high = float(chosen["band_high"])
        entries = [
            entry
            for entry in entries
            if entry["horizon_seconds"] == horizon
            and (low <= float(entry["more_likely_mid"]) < high or (high == 1.0 and low <= float(entry["more_likely_mid"]) <= high))
        ]

    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for entry in entries:
        groups[entry[group_field]].append(entry)

    rows: list[dict[str, str]] = []
    for group_value in sorted(groups):
        summary = summarize_entries(groups[group_value])
        if summary["n"] < min_n:
            continue
        rows.append(
            {
                group_field: group_value,
                "n": str(summary["n"]),
                "wins": str(summary["wins"]),
                "losses": str(summary["losses"]),
                "p_success": f"{summary['p_success']:.6f}",
                "avg_cost": f"{summary['avg_cost']:.6f}",
                "ev_p_minus_c": f"{summary['ev']:.6f}",
                "gross_pnl": f"{summary['gross_pnl']:.6f}",
            }
        )
    return rows


def best_by_group(
    entries: list[dict[str, str]],
    group_field: str,
    min_n: int,
    band_type: str = "single_decile",
) -> list[dict[str, str]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for entry in entries:
        groups[entry[group_field]].append(entry)

    rows: list[dict[str, str]] = []
    for group_value in sorted(groups):
        grid = build_parameter_grid(groups[group_value])
        sorted_rows = sort_grid(grid, min_n=min_n, band_type=band_type)
        if not sorted_rows:
            continue
        best = sorted_rows[0]
        rows.append(
            {
                group_field: group_value,
                "horizon_seconds": best["horizon_seconds"],
                "price_bin": best["price_bin"],
                "n": best["n"],
                "p_success": best["p_success"],
                "avg_cost": best["avg_cost"],
                "ev_p_minus_c": best["ev_p_minus_c"],
                "gross_pnl": best["gross_pnl"],
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def pct(value: str | float) -> str:
    number = float(value)
    return f"{number * 100:.2f}%"


def fmt(value: str | float) -> str:
    return f"{float(value):.4f}"


def markdown_grid_table(rows: list[dict[str, str]], limit: int | None = None) -> list[str]:
    selected = rows[:limit] if limit else rows
    lines = [
        "| T seconds | Price bin | N | P(success) | Avg cost c | EV p-c | Gross P&L | Wilson EV low |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in selected:
        lines.append(
            f"| {row['horizon_seconds']} | {row['price_bin']} | {row['n']} | {pct(row['p_success'])} | {fmt(row['avg_cost'])} | {fmt(row['ev_p_minus_c'])} | {fmt(row['gross_pnl'])} | {fmt(row['wilson_ev_low'])} |"
        )
    return lines


def markdown_group_table(rows: list[dict[str, str]], first_col: str, first_label: str) -> list[str]:
    lines = [
        f"| {first_label} | N | Wins | Losses | P(success) | Avg cost c | EV p-c | Gross P&L |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row[first_col]} | {row['n']} | {row['wins']} | {row['losses']} | {pct(row['p_success'])} | {fmt(row['avg_cost'])} | {fmt(row['ev_p_minus_c'])} | {fmt(row['gross_pnl'])} |"
        )
    return lines


def markdown_best_group_table(rows: list[dict[str, str]], first_col: str, first_label: str) -> list[str]:
    lines = [
        f"| {first_label} | Best T | Best price bin | N | P(success) | Avg cost c | EV p-c | Gross P&L |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row[first_col]} | {row['horizon_seconds']} | {row['price_bin']} | {row['n']} | {pct(row['p_success'])} | {fmt(row['avg_cost'])} | {fmt(row['ev_p_minus_c'])} | {fmt(row['gross_pnl'])} |"
        )
    return lines


def write_report(
    path: Path,
    contracts: dict[str, dict[str, Any]],
    markets: dict[str, dict[str, Any]],
    entries: list[dict[str, str]],
    grid: list[dict[str, str]],
    min_n: int,
    tolerance_seconds: float,
    artifacts: dict[str, str],
) -> dict[str, Any]:
    raw_best_single = sort_grid(grid, min_n=1, band_type="single_decile")[0]
    best_single = sort_grid(grid, min_n=min_n, band_type="single_decile")[0]
    best_any = sort_grid(grid, min_n=min_n, band_type=None)[0]
    best_horizon_rows = []
    for horizon in HORIZONS_SECONDS:
        rows = sort_grid([row for row in grid if row["horizon_seconds"] == str(horizon)], min_n=min_n, band_type="single_decile")
        if rows:
            best_horizon_rows.append(rows[0])

    chosen = best_single
    neighbors = neighbor_rows(grid, chosen, min_n)
    component = positive_component(grid, chosen, min_n)
    neighbor_evs = [float(row["ev_p_minus_c"]) for row in neighbors]
    stable = bool(neighbors) and sum(ev > 0 for ev in neighbor_evs) >= max(1, math.ceil(0.6 * len(neighbor_evs)))
    stable = stable and (statistics.fmean(neighbor_evs) if neighbor_evs else -1) > 0
    stable = stable and len(component) >= 4

    chosen_by_date = grouped_summaries(entries, "close_date_et", chosen, min_n=1)
    chosen_by_bucket = grouped_summaries(entries, "close_hour_bucket_et", chosen, min_n=1)
    best_by_date = best_by_group(entries, "close_date_et", min_n=max(5, min_n // 2))
    best_by_bucket = best_by_group(entries, "close_hour_bucket_et", min_n=max(5, min_n // 2))

    all_rows = sum(contract["raw_row_count"] for contract in contracts.values())
    valid_rows = sum(len(contract["valid_rows"]) for contract in contracts.values())
    invalid_quote_rows = sum(contract["invalid_quote_rows"] for contract in contracts.values())
    resolved_contracts = sum(1 for ticker in contracts if official_result(markets.get(ticker, {})))
    unresolved = sorted(ticker for ticker in contracts if not official_result(markets.get(ticker, {})))
    statuses = Counter(str(market.get("status", "")) for market in markets.values())
    outcome_counts = Counter(official_result(market) or "MISSING" for market in markets.values())
    usable_by_horizon = Counter(entry["horizon_seconds"] for entry in entries if entry["resolved"] == "1")

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    close_times = [contract["close_time"] for contract in contracts.values()]

    main_result = (
        f"The best single-decile parameter with N >= {min_n} is T={chosen['horizon_seconds']}s and bin {chosen['price_bin']}: "
        f"P(success)={pct(chosen['p_success'])}, avg cost c={fmt(chosen['avg_cost'])}, EV p-c={fmt(chosen['ev_p_minus_c'])}, N={chosen['n']}."
    )
    if best_any != best_single:
        main_result += (
            f" The best aggregate band with the same N filter is T={best_any['horizon_seconds']}s and {best_any['price_bin']} "
            f"with EV {fmt(best_any['ev_p_minus_c'])}, but the requested decile-bin analysis is the primary selection."
        )

    lines = [
        "# Kalshi BTC More-Likely Side Official-Outcome Backtest",
        "",
        f"Generated: `{generated_at}`",
        "",
        "## Summary",
        "",
        "This report uses only Kalshi quote columns from `data_BTC/*.csv` and official Kalshi market outcomes from the public `GET /markets?tickers=...` API. Spot-price-derived outcomes are not used.",
        "",
        main_result,
        "",
        f"The unrestricted raw single-decile argmax is T={raw_best_single['horizon_seconds']}s and bin {raw_best_single['price_bin']} with EV {fmt(raw_best_single['ev_p_minus_c'])}, N={raw_best_single['n']}.",
        "",
        "Gross EV is calculated per 1 contract before fees:",
        "",
        "```text",
        "EV = P(success) - average buy cost",
        "```",
        "",
        "where success means the official Kalshi result resolves to the same side as the more-likely Kalshi midpoint side at the selected horizon.",
        "",
        "## Artifacts",
        "",
    ]
    for label, artifact in artifacts.items():
        lines.append(f"- {label}: `{artifact}`")

    lines.extend(
        [
            "",
            "## Data And Method",
            "",
            f"- Contract CSV files: `{len(contracts)}`",
            f"- Raw rows: `{all_rows}`",
            f"- Valid Kalshi quote rows: `{valid_rows}`",
            f"- Invalid quote rows rejected: `{invalid_quote_rows}`",
            f"- Close-time range: `{min(close_times).isoformat().replace('+00:00', 'Z')}` to `{max(close_times).isoformat().replace('+00:00', 'Z')}`",
            f"- Markets returned by Kalshi API: `{len(markets)}`",
            f"- Resolved official outcomes: `{resolved_contracts}`",
            f"- Official outcome counts: YES `{outcome_counts.get('YES', 0)}`, NO `{outcome_counts.get('NO', 0)}`, missing `{outcome_counts.get('MISSING', 0)}`",
            f"- Official market statuses: {', '.join(f'{key} `{value}`' for key, value in sorted(statuses.items()))}",
            f"- Horizon grid: `{HORIZONS_SECONDS[0]}`s to `{HORIZONS_SECONDS[-1]}`s in 60s steps",
            f"- Horizon row selection tolerance: `{tolerance_seconds:.0f}` seconds",
            f"- Primary result minimum sample size: `N >= {min_n}`",
            "",
        ]
    )
    if unresolved:
        lines.append(f"Unresolved official markets excluded from success-rate scoring: `{', '.join(unresolved)}`")
        lines.append("")

    lines.extend(
        [
            "For each contract and horizon, I selected the valid row closest to T seconds before expiry. The more-likely side is YES if `kalshi_yes_mid >= 0.5`, otherwise NO. The tested decile bins are based on `max(kalshi_yes_mid, 1 - kalshi_yes_mid)`. The cost `c` is the best ask to buy that selected side: `kalshi_yes_ask` for YES and `kalshi_no_ask` for NO.",
            "",
            "## Usable Entry Counts",
            "",
            "| T seconds | Usable resolved entries |",
            "|---:|---:|",
        ]
    )
    for horizon in HORIZONS_SECONDS:
        lines.append(f"| {horizon} | {usable_by_horizon.get(str(horizon), 0)} |")

    lines.extend(
        [
            "",
            "## Best Parameter Cells",
            "",
            f"Top single-decile cells with `N >= {min_n}`:",
            "",
            *markdown_grid_table(sort_grid(grid, min_n=min_n, band_type="single_decile"), limit=15),
            "",
            f"Best single-decile cell at each horizon with `N >= {min_n}`:",
            "",
            *markdown_grid_table(best_horizon_rows),
            "",
            "## Selected Horizon Detail",
            "",
            f"All single-decile bins at T={chosen['horizon_seconds']}s:",
            "",
            *markdown_grid_table(
                sorted(
                    [
                        row
                        for row in grid
                        if row["band_type"] == "single_decile" and row["horizon_seconds"] == chosen["horizon_seconds"]
                    ],
                    key=lambda row: float(row["band_low"]),
                )
            ),
            "",
            "## Stability In Parameter Space",
            "",
            f"Chosen parameter: T={chosen['horizon_seconds']}s, bin {chosen['price_bin']}, EV={fmt(chosen['ev_p_minus_c'])}, N={chosen['n']}.",
            "",
            f"Stability assessment: `{'stable' if stable else 'not stable'}`.",
            "",
            f"Positive connected component size around the chosen cell: `{len(component)}` cells using 4-neighbor adjacency over T and price bin.",
            "",
        ]
    )
    if neighbors:
        lines.extend(
            [
                f"Immediate neighbors with `N >= {min_n}`:",
                "",
                *markdown_grid_table(neighbors),
                "",
            ]
        )
        lines.append(
            f"Neighbor EV mean is `{statistics.fmean(neighbor_evs):.4f}`, with `{sum(ev > 0 for ev in neighbor_evs)}/{len(neighbor_evs)}` positive neighbors."
        )
        lines.append("")
    else:
        lines.extend(["No immediate neighbors met the minimum sample filter.", ""])

    if stable:
        lines.append("The optimum is not an isolated spike: nearby horizon/bin cells remain mostly positive, so the selected parameter sits on a local profitable plateau.")
    else:
        lines.append("The optimum is fragile: nearby cells do not form a broad positive plateau under the minimum sample filter. Treat it as a hypothesis rather than a stable equilibrium.")

    lines.extend(
        [
            "",
            "## Time And Day Dependence",
            "",
            f"Selected parameter performance by ET date:",
            "",
            *markdown_group_table(chosen_by_date, "close_date_et", "ET date"),
            "",
            f"Best single-decile parameter by ET date with `N >= {max(5, min_n // 2)}`:",
            "",
            *markdown_best_group_table(best_by_date, "close_date_et", "ET date"),
            "",
            "Selected parameter performance by ET 4-hour close-time bucket:",
            "",
            *markdown_group_table(chosen_by_bucket, "close_hour_bucket_et", "ET close-hour bucket"),
            "",
            f"Best single-decile parameter by ET 4-hour close-time bucket with `N >= {max(5, min_n // 2)}`:",
            "",
            *markdown_best_group_table(best_by_bucket, "close_hour_bucket_et", "ET close-hour bucket"),
            "",
            "Interpretation: if the best parameter moves materially across dates or time buckets, the apparent edge may be regime-dependent rather than a persistent market mispricing.",
            "",
            "## Conclusion",
            "",
        ]
    )
    if float(chosen["ev_p_minus_c"]) > 0:
        lines.append(
            f"The main mispriced opportunity in this sample is buying the Kalshi more-likely side in the `{chosen['price_bin']}` more-likely midpoint bin at about `{int(chosen['horizon_seconds']) // 60}` minutes before expiry, subject to the stability and time/day caveats above."
        )
    else:
        lines.append("No positive-EV single-decile opportunity passed the minimum sample filter in this sample.")

    lines.append("")
    lines.append("Fees are not included. Applying Kalshi fees will reduce every EV estimate, especially near 0.50 where fees are largest.")
    lines.append("")

    path.write_text("\n".join(lines))

    return {
        "raw_best_single_decile": raw_best_single,
        "best_single_decile_min_n": best_single,
        "best_any_band_min_n": best_any,
        "stable": stable,
        "neighbor_positive_count": sum(ev > 0 for ev in neighbor_evs),
        "neighbor_count": len(neighbor_evs),
        "neighbor_ev_mean": statistics.fmean(neighbor_evs) if neighbor_evs else None,
        "positive_component_size": len(component),
        "resolved_contracts": resolved_contracts,
        "unresolved_contracts": unresolved,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data_BTC", help="Directory of per-contract BTC CSV files")
    parser.add_argument("--chunk-size", type=int, default=50, help="Tickers per Kalshi API request")
    parser.add_argument("--pause-seconds", type=float, default=0.1, help="Pause between Kalshi API requests")
    parser.add_argument("--tolerance-seconds", type=float, default=45.0, help="Max absolute distance from requested horizon")
    parser.add_argument("--min-n", type=int, default=20, help="Minimum N for primary parameter selection")
    parser.add_argument("--entries-csv", default="btc_more_likely_entries_official.csv")
    parser.add_argument("--grid-csv", default="btc_more_likely_parameter_grid_official.csv")
    parser.add_argument("--summary-json", default="btc_more_likely_summary_official.json")
    parser.add_argument("--market-cache-json", default="btc_official_market_results.json")
    parser.add_argument("--report-md", default="btc_more_likely_official_report.md")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(__file__).resolve().parent
    data_dir = (workdir / args.data_dir).resolve()
    contracts = load_contracts(data_dir)
    if not contracts:
        print(f"no contracts loaded from {data_dir}", file=sys.stderr)
        return 1

    tickers = sorted(contracts)
    markets = fetch_markets(tickers, args.chunk_size, args.pause_seconds)
    missing = sorted(set(tickers) - set(markets))
    if missing:
        print(f"warning: {len(missing)} tickers missing from Kalshi API response", file=sys.stderr)

    entries = build_entries(contracts, markets, HORIZONS_SECONDS, args.tolerance_seconds)
    grid = build_parameter_grid(entries)

    entries_csv = workdir / args.entries_csv
    grid_csv = workdir / args.grid_csv
    summary_json = workdir / args.summary_json
    market_cache_json = workdir / args.market_cache_json
    report_md = workdir / args.report_md

    write_csv(entries_csv, entries)
    write_csv(grid_csv, grid)
    market_cache_json.write_text(json.dumps({"markets": markets, "missing_tickers": missing}, indent=2, sort_keys=True) + "\n")

    artifacts = {
        "Entry ledger": entries_csv.name,
        "Parameter grid": grid_csv.name,
        "Machine-readable summary": summary_json.name,
        "Official market cache": market_cache_json.name,
    }
    summary = write_report(
        report_md,
        contracts,
        markets,
        entries,
        grid,
        args.min_n,
        args.tolerance_seconds,
        artifacts,
    )
    summary["artifacts"] = {**artifacts, "Markdown report": report_md.name}
    summary["contracts"] = len(contracts)
    summary["entries"] = len(entries)
    summary["grid_rows"] = len(grid)
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
