#!/usr/bin/env python3
import argparse
import csv
import time
import traceback
from pathlib import Path
from typing import Any

import kalshi_btc15_server as btc


CSV_FIELDS = [
    "timestamp_utc",
    "kalshi_ticker",
    "polymarket_ticker",
    "kalshi_price",
    "polymarket_price",
    "kalshi_yes_bid",
    "kalshi_yes_ask",
    "kalshi_no_bid",
    "kalshi_no_ask",
    "polymarket_yes_bid",
    "polymarket_yes_ask",
    "polymarket_no_bid",
    "polymarket_no_ask",
    "arbitrage_direction",
    "arbitrage_kalshi_price",
    "arbitrage_polymarket_price",
    "kalshi_spend",
    "polymarket_spend",
    "expected_profit",
    "guaranteed_payout",
]
TRADER_LOG_PATH = Path(__file__).resolve().parent / "trader_log.txt"
CONCISE_TRADER_LOG_PATH = Path(__file__).resolve().parent / "concise_trader_log.txt"
TRADER_LOG_MAX_LINES = 200


def trim_log_file(path: Path, max_lines: int = TRADER_LOG_MAX_LINES) -> None:
    if path.resolve() == CONCISE_TRADER_LOG_PATH.resolve():
        return
    if max_lines < 1 or not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= max_lines:
        return
    path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")


def is_concise_log_line(line: str) -> bool:
    return (
        "BALANCE " in line
        or "CLI TRADER INITIATED" in line
        or "CONTRACT " in line
        or "POSITION REVIEW" in line
        or "POSITION CLEAR" in line
        or "TRADED " in line
        or "PARTIAL ENTRY" in line
        or "SKIP Kalshi hedge" in line
        or "SKIP Polymarket" in line
        or "DRY RUN would place" in line
        or "SETTLED " in line
        or "EXITED " in line
        or "EXIT PARTIAL" in line
        or "EXIT WAIT" in line
        or "EXIT FAILED" in line
        or "EXIT_REVIEW" in line
        or "FATAL " in line
    )


def yellow_text(text: str) -> str:
    return text


def append_terminal_log(line: str, force_concise: bool = False) -> None:
    dated_line = "\n" if line == "" else f"{btc.iso_utc()[:10]} {line}\n"
    with TRADER_LOG_PATH.open("a", encoding="utf-8") as file_obj:
        file_obj.write(dated_line)
    if "CHECK SKIP" in line:
        return
    if force_concise or is_concise_log_line(line):
        with CONCISE_TRADER_LOG_PATH.open("a", encoding="utf-8") as file_obj:
            file_obj.write(dated_line)


def print_line(line: str, force_concise: bool = False) -> None:
    print(line, flush=True)
    append_terminal_log(line, force_concise=force_concise)


def fmt_price(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value * 100:.2f}c"
    return "--"


def fmt_money(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"${value:.4f}"
    return "--"


def fmt_display_time(value: Any) -> str:
    if not isinstance(value, str):
        return "--"
    time_part = value.split("T", 1)[-1].rstrip("Z")
    main_time, _, fractional = time_part.partition(".")
    if fractional:
        return f"{main_time}.{fractional[0]}"
    return main_time


def fmt_display_cents(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value * 100:.1f}"
    return "--"


def price_sum(*values: float | None) -> float | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def market_price(snapshot: dict[str, Any], side: str, outcome: str) -> float | None:
    value = snapshot.get(f"{outcome}_{side}")
    if isinstance(value, (int, float)):
        return value
    return None


def fetch_snapshots() -> tuple[dict[str, Any], dict[str, Any]]:
    market = btc.discover_active_market()
    if not market:
        raise RuntimeError(f"No open market found for {btc.SERIES_TICKER}")

    ticker = market["ticker"]
    orderbook = btc.kalshi_get(f"/markets/{ticker}/orderbook", {"depth": btc.ORDERBOOK_DEPTH})
    kalshi_snapshot = btc.make_snapshot(market, orderbook)

    polymarket_market = btc.discover_polymarket_market(market)
    if not polymarket_market:
        raise RuntimeError("No matching open Polymarket market found")

    polymarket_orderbook = btc.polymarket_clob_orderbooks(polymarket_market)
    polymarket_snapshot = btc.make_polymarket_snapshot(polymarket_market, polymarket_orderbook)
    return kalshi_snapshot, polymarket_snapshot


def arbitrage_candidate(
    direction: str,
    kalshi_price: float | None,
    polymarket_price: float | None,
    kalshi_contract: str,
    polymarket_contract: str,
) -> dict[str, Any] | None:
    if kalshi_price is None or polymarket_price is None:
        return None
    total_cost = kalshi_price + polymarket_price
    if total_cost <= 0 or total_cost >= 1:
        return None

    guaranteed_payout = 1.0 / total_cost
    return {
        "direction": direction,
        "kalshi_contract": kalshi_contract,
        "polymarket_contract": polymarket_contract,
        "kalshi_price": kalshi_price,
        "polymarket_price": polymarket_price,
        "total_cost": total_cost,
        "kalshi_spend": kalshi_price / total_cost,
        "polymarket_spend": polymarket_price / total_cost,
        "expected_profit": guaranteed_payout - 1.0,
        "guaranteed_payout": guaranteed_payout,
    }


def best_arbitrage(
    kalshi_snapshot: dict[str, Any], polymarket_snapshot: dict[str, Any]
) -> dict[str, Any] | None:
    candidates = [
        arbitrage_candidate(
            "buy_kalshi_yes_buy_polymarket_no",
            market_price(kalshi_snapshot, "ask", "yes"),
            market_price(polymarket_snapshot, "ask", "no"),
            "YES",
            "NO",
        ),
        arbitrage_candidate(
            "buy_kalshi_no_buy_polymarket_yes",
            market_price(kalshi_snapshot, "ask", "no"),
            market_price(polymarket_snapshot, "ask", "yes"),
            "NO",
            "YES",
        ),
    ]
    valid = [candidate for candidate in candidates if candidate is not None]
    if not valid:
        return None
    return max(valid, key=lambda candidate: candidate["expected_profit"])


def csv_row(
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    arbitrage: dict[str, Any] | None,
) -> dict[str, Any]:
    row = {
        "timestamp_utc": kalshi_snapshot.get("timestamp_utc"),
        "kalshi_ticker": kalshi_snapshot.get("ticker"),
        "polymarket_ticker": polymarket_snapshot.get("ticker"),
        "kalshi_price": kalshi_snapshot.get("yes_ask"),
        "polymarket_price": polymarket_snapshot.get("yes_ask"),
        "kalshi_yes_bid": kalshi_snapshot.get("yes_bid"),
        "kalshi_yes_ask": kalshi_snapshot.get("yes_ask"),
        "kalshi_no_bid": kalshi_snapshot.get("no_bid"),
        "kalshi_no_ask": kalshi_snapshot.get("no_ask"),
        "polymarket_yes_bid": polymarket_snapshot.get("yes_bid"),
        "polymarket_yes_ask": polymarket_snapshot.get("yes_ask"),
        "polymarket_no_bid": polymarket_snapshot.get("no_bid"),
        "polymarket_no_ask": polymarket_snapshot.get("no_ask"),
        "arbitrage_direction": "",
        "arbitrage_kalshi_price": "",
        "arbitrage_polymarket_price": "",
        "kalshi_spend": "",
        "polymarket_spend": "",
        "expected_profit": 0.0,
        "guaranteed_payout": "",
    }
    if arbitrage:
        row.update(
            {
                "arbitrage_direction": arbitrage["direction"],
                "arbitrage_kalshi_price": arbitrage["kalshi_price"],
                "arbitrage_polymarket_price": arbitrage["polymarket_price"],
                "kalshi_spend": arbitrage["kalshi_spend"],
                "polymarket_spend": arbitrage["polymarket_spend"],
                "expected_profit": arbitrage["expected_profit"],
                "guaranteed_payout": arbitrage["guaranteed_payout"],
            }
        )
    return row


def format_snapshot_line(
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    arbitrage: dict[str, Any] | None,
    suffix: str = "",
) -> str:
    kalshi_yes_ask = market_price(kalshi_snapshot, "ask", "yes")
    kalshi_no_ask = market_price(kalshi_snapshot, "ask", "no")
    polymarket_yes_ask = market_price(polymarket_snapshot, "ask", "yes")
    polymarket_no_ask = market_price(polymarket_snapshot, "ask", "no")
    yes_no_cost = price_sum(kalshi_yes_ask, polymarket_no_ask)
    no_yes_cost = price_sum(kalshi_no_ask, polymarket_yes_ask)
    yes_no_column = (
        f"K + NP = {fmt_display_cents(kalshi_yes_ask)} + "
        f"{fmt_display_cents(polymarket_no_ask)} = {fmt_display_cents(yes_no_cost)}"
    )
    no_yes_column = (
        f"NK + P = {fmt_display_cents(kalshi_no_ask)} + "
        f"{fmt_display_cents(polymarket_yes_ask)} = {fmt_display_cents(no_yes_cost)}"
    )
    if arbitrage:
        kalshi_label = "K" if arbitrage["kalshi_contract"] == "YES" else "NK"
        polymarket_label = "P" if arbitrage["polymarket_contract"] == "YES" else "NP"
        status = (
            f"ARB {kalshi_label} = {fmt_money(arbitrage['kalshi_spend'])},"
            f" {polymarket_label} = {fmt_money(arbitrage['polymarket_spend'])},"
            f" +{fmt_money(arbitrage['expected_profit'])}"
        )
    else:
        status = "NO ARB"
    line = (
        f"{fmt_display_time(kalshi_snapshot.get('timestamp_utc')):<10}"
        f" | {yes_no_column:<32}"
        f" | {no_yes_column:<32}"
        f" | {status}"
    )
    if suffix:
        line += f" | {suffix}"
    return line


def print_snapshot(
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    arbitrage: dict[str, Any] | None,
    suffix: str = "",
) -> None:
    print_line(format_snapshot_line(kalshi_snapshot, polymarket_snapshot, arbitrage, suffix))


def safe_filename(value: Any) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in str(value))


def csv_path_for_contract(csv_dir: Path, kalshi_snapshot: dict[str, Any]) -> Path:
    ticker = safe_filename(kalshi_snapshot.get("ticker") or "unknown")
    return csv_dir / f"cli_prices_{ticker}.csv"


def append_rows(csv_path: Path, rows: list[dict[str, Any]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    exists = csv_path.exists()
    with csv_path.open("a", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def flush_pending(pending_rows: dict[Path, list[dict[str, Any]]]) -> None:
    for csv_path, rows in list(pending_rows.items()):
        if rows:
            append_rows(csv_path, rows)
            rows.clear()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print Kalshi and Polymarket BTC 15m prices and log arbitrage data."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=btc.POLL_SECONDS,
        help=f"Seconds between rows. Default: {btc.POLL_SECONDS:g}",
    )
    parser.add_argument(
        "--csv-dir",
        type=Path,
        default=btc.DATA_DIR,
        help=f"CSV output directory. Default: {btc.DATA_DIR}",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=1,
        help="Rows to buffer before writing. Default: 1 for live tailing.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    interval = max(0.1, args.interval)
    flush_every = max(1, args.flush_every)
    pending_rows: dict[Path, list[dict[str, Any]]] = {}

    print_line(
        f"Polling every {interval:g}s; writing one CSV per contract in {args.csv_dir}",
    )
    while True:
        started = time.monotonic()
        try:
            kalshi_snapshot, polymarket_snapshot = fetch_snapshots()
            arbitrage = best_arbitrage(kalshi_snapshot, polymarket_snapshot)
            csv_path = csv_path_for_contract(args.csv_dir, kalshi_snapshot)
            rows = pending_rows.setdefault(csv_path, [])
            rows.append(csv_row(kalshi_snapshot, polymarket_snapshot, arbitrage))
            print_snapshot(kalshi_snapshot, polymarket_snapshot, arbitrage)
            if len(rows) >= flush_every:
                append_rows(csv_path, rows)
                rows.clear()
        except KeyboardInterrupt:
            if pending_rows:
                flush_pending(pending_rows)
            raise
        except Exception as exc:
            print_line(f"{btc.iso_utc()} | ERROR {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=2)

        elapsed = time.monotonic() - started
        time.sleep(max(0.0, interval - elapsed))


if __name__ == "__main__":
    main()
