#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("America/New_York")

STATS_FIELDS = [
    "timestamp_utc",
    "event",
    "source",
    "contract_id",
    "portfolio_value",
    "portfolio_available",
    "portfolio_pnl_today_dollars",
    "portfolio_pnl_today_percent",
    "successful_count",
    "unsuccessful_count",
    "skipped_count",
    "traded_count",
    "avg_contract_price",
    "success_percentage",
    "breakeven_success_rate",
    "p_value",
    "estimated_realized_pnl_dollars",
    "estimated_realized_pnl_percent",
]

BALANCE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T[0-9:.]+Z) \| "
    r"BALANCE(?P<event>.*?) \| Kalshi \$(?P<balance>-?\d+(?:\.\d+)?)"
    r"(?: available=\$(?P<available>-?\d+(?:\.\d+)?))?"
)


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def finite_int(value: Any) -> int | None:
    number = finite_float(value)
    if number is None:
        return None
    return int(number)


def csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.10g}"
    return value


def binomial_right_tail(successes: int, trials: int, probability: float | None) -> float | None:
    if trials <= 0 or successes < 0 or successes > trials or probability is None:
        return None
    p = min(1.0, max(0.0, probability))
    if successes <= 0:
        return 1.0
    if p == 0.0:
        return 0.0
    if p == 1.0:
        return 1.0 if successes <= trials else 0.0
    logs = []
    log_p = math.log(p)
    log_q = math.log1p(-p)
    base = math.lgamma(trials + 1)
    for k in range(successes, trials + 1):
        logs.append(base - math.lgamma(k + 1) - math.lgamma(trials - k + 1) + k * log_p + (trials - k) * log_q)
    peak = max(logs)
    return min(1.0, max(0.0, math.exp(peak) * sum(math.exp(item - peak) for item in logs)))


@dataclass(order=True)
class TimelineEvent:
    timestamp: datetime
    sequence: int
    kind: str
    row: dict[str, Any]


@dataclass
class PortfolioState:
    value: float | None = None
    available: float | None = None
    day_start: dict[str, float] | None = None
    pnl_today_dollars: float | None = None
    pnl_today_percent: float | None = None

    def __post_init__(self) -> None:
        if self.day_start is None:
            self.day_start = {}

    def update_balance(self, timestamp: datetime, balance: float | None, available: float | None) -> None:
        if balance is None:
            return
        self.value = balance
        self.available = available
        local_day = timestamp.astimezone(LOCAL_TZ).date().isoformat()
        assert self.day_start is not None
        self.day_start.setdefault(local_day, balance)
        start = self.day_start[local_day]
        self.pnl_today_dollars = balance - start
        self.pnl_today_percent = (self.pnl_today_dollars / start) if start else None


@dataclass
class StrategyState:
    successful: int = 0
    unsuccessful: int = 0
    skipped: int = 0
    price_quantity: float = 0.0
    quantity: float = 0.0
    realized_pnl: float = 0.0
    total_stake: float = 0.0

    def maybe_reset(self, row: dict[str, Any]) -> None:
        s = finite_int(row.get("successful_count"))
        u = finite_int(row.get("unsuccessful_count"))
        k = finite_int(row.get("skipped_count"))
        if s is None or u is None or k is None:
            return
        if s < self.successful or u < self.unsuccessful or k < self.skipped:
            self.successful = 0
            self.unsuccessful = 0
            self.skipped = 0
            self.price_quantity = 0.0
            self.quantity = 0.0
            self.realized_pnl = 0.0
            self.total_stake = 0.0

    def update_trade(self, row: dict[str, Any]) -> None:
        self.maybe_reset(row)
        if row.get("event") == "outcome" and str(row.get("order_status", "")).lower() in {"filled", "dry_run"}:
            correct = finite_int(row.get("correct"))
            price = finite_float(row.get("fill_price")) or finite_float(row.get("selected_ask"))
            quantity = finite_float(row.get("filled_size")) or finite_float(row.get("contracts"))
            if correct is not None and price is not None and quantity is not None and quantity > 0:
                self.price_quantity += price * quantity
                self.quantity += quantity
                self.total_stake += price * quantity
                self.realized_pnl += (1.0 - price) * quantity if correct else -price * quantity

        self.successful = finite_int(row.get("successful_count")) or self.successful
        self.unsuccessful = finite_int(row.get("unsuccessful_count")) or self.unsuccessful
        self.skipped = finite_int(row.get("skipped_count")) or self.skipped

    @property
    def traded(self) -> int:
        return self.successful + self.unsuccessful

    @property
    def avg_contract_price(self) -> float | None:
        return self.price_quantity / self.quantity if self.quantity > 0 else None

    @property
    def success_percentage(self) -> float | None:
        return self.successful / self.traded if self.traded else None

    @property
    def p_value(self) -> float | None:
        return binomial_right_tail(self.successful, self.traded, self.avg_contract_price)

    @property
    def realized_pnl_percent(self) -> float | None:
        return self.realized_pnl / self.total_stake if self.total_stake else None


def read_balance_events(log_path: Path) -> list[TimelineEvent]:
    if not log_path.exists():
        return []
    events: list[TimelineEvent] = []
    for sequence, line in enumerate(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
        match = BALANCE_RE.match(line.strip())
        if not match:
            continue
        timestamp = parse_timestamp(match.group("timestamp"))
        if timestamp is None:
            continue
        events.append(
            TimelineEvent(
                timestamp=timestamp,
                sequence=sequence,
                kind="balance",
                row={
                    "event": "balance",
                    "balance_event": " ".join((match.group("event") or "").split()) or "BALANCE",
                    "portfolio_value": finite_float(match.group("balance")),
                    "portfolio_available": finite_float(match.group("available")),
                },
            )
        )
    return events


def read_trade_events(trades_csv_path: Path) -> list[TimelineEvent]:
    if not trades_csv_path.exists():
        return []
    events: list[TimelineEvent] = []
    with trades_csv_path.open(newline="", encoding="utf-8") as file_obj:
        reader = csv.DictReader(file_obj)
        for sequence, row in enumerate(reader):
            timestamp = parse_timestamp(row.get("timestamp_utc"))
            if timestamp is None:
                continue
            events.append(TimelineEvent(timestamp=timestamp, sequence=sequence, kind="trade", row=row))
    return events


def build_stats_rows(log_path: Path, trades_csv_path: Path) -> list[dict[str, Any]]:
    events = read_balance_events(log_path) + read_trade_events(trades_csv_path)
    events.sort()
    portfolio = PortfolioState()
    strategy = StrategyState()
    rows: list[dict[str, Any]] = []
    for event in events:
        source = event.kind
        contract_id = event.row.get("contract_id", "")
        event_name = str(event.row.get("event") or source)
        if event.kind == "balance":
            portfolio.update_balance(
                event.timestamp,
                finite_float(event.row.get("portfolio_value")),
                finite_float(event.row.get("portfolio_available")),
            )
            event_name = event.row.get("balance_event") or "BALANCE"
        elif event.kind == "trade":
            strategy.update_trade(event.row)

        avg_price = strategy.avg_contract_price
        row = {
            "timestamp_utc": iso_utc(event.timestamp),
            "event": event_name,
            "source": source,
            "contract_id": contract_id,
            "portfolio_value": portfolio.value,
            "portfolio_available": portfolio.available,
            "portfolio_pnl_today_dollars": portfolio.pnl_today_dollars,
            "portfolio_pnl_today_percent": portfolio.pnl_today_percent,
            "successful_count": strategy.successful,
            "unsuccessful_count": strategy.unsuccessful,
            "skipped_count": strategy.skipped,
            "traded_count": strategy.traded,
            "avg_contract_price": avg_price,
            "success_percentage": strategy.success_percentage,
            "breakeven_success_rate": avg_price,
            "p_value": strategy.p_value,
            "estimated_realized_pnl_dollars": strategy.realized_pnl,
            "estimated_realized_pnl_percent": strategy.realized_pnl_percent,
        }
        rows.append({field: csv_value(row.get(field)) for field in STATS_FIELDS})
    return rows


def write_stats_csv(log_path: Path, trades_csv_path: Path, stats_csv_path: Path) -> list[dict[str, Any]]:
    rows = build_stats_rows(log_path, trades_csv_path)
    stats_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with stats_csv_path.open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=STATS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def refresh_stats_csv(log_path: Path, trades_csv_path: Path, stats_csv_path: Path) -> None:
    write_stats_csv(log_path, trades_csv_path, stats_csv_path)


def read_stats_csv(stats_csv_path: Path) -> list[dict[str, Any]]:
    if not stats_csv_path.exists():
        return []
    with stats_csv_path.open(newline="", encoding="utf-8") as file_obj:
        return list(csv.DictReader(file_obj))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Kalshi trader stats CSV from log and trade CSV files.")
    parser.add_argument("--log", type=Path, default=Path("kalshi_trader.log"))
    parser.add_argument("--trades", type=Path, default=Path("kalshi_trader_trades.csv"))
    parser.add_argument("--stats", type=Path, default=Path("kalshi_trader_stats.csv"))
    args = parser.parse_args()
    rows = write_stats_csv(args.log, args.trades, args.stats)
    print(f"wrote {args.stats} rows={len(rows)}")


if __name__ == "__main__":
    main()
