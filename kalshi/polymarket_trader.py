#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cli_trader_v2 as trader


APP_DIR = Path(__file__).resolve().parent
LOG_PATH = APP_DIR / "polymarket_trader.log"
TRADES_CSV_PATH = APP_DIR / "polymarket_trader_trades.csv"

DEFAULT_THRESHOLD = 0.68
DEFAULT_ENTRY_SECONDS = 2 * 60
DEFAULT_RETRY_ATTEMPTS = 10
DEFAULT_RETRY_DELAY_SECONDS = 0.5
DEFAULT_OUTCOME_DELAY_SECONDS = -2.0

TRADE_FIELDS = [
    "timestamp_utc",
    "event",
    "contract_id",
    "close_time",
    "polymarket_ticker",
    "remaining_seconds",
    "threshold",
    "yes_mid",
    "selected_side",
    "selected_probability",
    "selected_ask",
    "selected_ask_qty",
    "contracts",
    "dry_run",
    "order_status",
    "order_id",
    "fill_price",
    "filled_size",
    "actual_side",
    "actual_label",
    "correct",
    "polymarket_price",
    "polymarket_target",
    "polymarket_target_source",
    "successful_count",
    "unsuccessful_count",
    "skipped_count",
    "reason",
]


def iso_utc(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def append_log(message: str, *, prefix_timestamp: bool = True) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{iso_utc()} | {message}" if prefix_timestamp else message
    with LOG_PATH.open("a", encoding="utf-8") as file_obj:
        file_obj.write(line.rstrip() + "\n")
    print(line, flush=True)


def append_trade_row(row: dict[str, Any]) -> None:
    TRADES_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = TRADES_CSV_PATH.exists()
    with TRADES_CSV_PATH.open("a", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=TRADE_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in TRADE_FIELDS})


@dataclass
class Counts:
    successful: int = 0
    unsuccessful: int = 0
    skipped: int = 0


@dataclass
class TradeDecision:
    status: str = ""
    side: str = ""
    predicted_label: int | None = None
    selected_probability: float | None = None
    selected_ask: float | None = None
    selected_ask_qty: float | None = None
    contracts: int = 0
    order_id: str = ""
    fill_price: float | None = None
    filled_size: float = 0.0
    dry_run: bool = True
    reason: str = ""
    timestamp_utc: str = field(default_factory=iso_utc)
    outcome_recorded: bool = False

    @property
    def outcome_eligible(self) -> bool:
        return self.status in {"filled", "dry_run"} and self.predicted_label is not None


@dataclass
class ContractRuntime:
    ticker: str
    close_time: str
    polymarket_ticker: str
    history: list[dict[str, Any]] = field(default_factory=list)
    decision: TradeDecision | None = None
    decision_logged: bool = False
    outcome_logged: bool = False


class StopLossTriggered(RuntimeError):
    pass


def selected_side_from_mid(
    polymarket_snapshot: dict[str, Any],
    threshold: float,
) -> tuple[str | None, int | None, float | None, str]:
    yes_mid = finite_float(polymarket_snapshot.get("yes_mid"))
    if yes_mid is None:
        return None, None, None, "missing yes_mid"
    no_mid = 1.0 - yes_mid
    if yes_mid >= threshold and yes_mid >= no_mid:
        return "YES", 1, yes_mid, ""
    if no_mid >= threshold:
        return "NO", 0, no_mid, ""
    return None, None, max(yes_mid, no_mid), (
        f"more likely side {trader.fmt_price(max(yes_mid, no_mid), 4)} < "
        f"threshold {threshold:.2f}"
    )


def side_ask_snapshot(
    polymarket_snapshot: dict[str, Any],
    side: str,
) -> tuple[float | None, float | None]:
    if side == "YES":
        return (
            finite_float(polymarket_snapshot.get("yes_ask")),
            finite_float(polymarket_snapshot.get("best_yes_ask_qty")),
        )
    return (
        finite_float(polymarket_snapshot.get("no_ask")),
        finite_float(polymarket_snapshot.get("best_no_ask_qty")),
    )


def current_outcome(
    runtime: ContractRuntime,
    source_snapshot: dict[str, Any],
) -> tuple[int | None, str, float | None, float | None, str]:
    price = finite_float(source_snapshot.get("polymarket_price"))
    target = finite_float(source_snapshot.get("polymarket_target"))
    target_source = "observed_source_snapshot"
    if target is None:
        for row in reversed(runtime.history):
            target = finite_float(row.get("polymarket_target"))
            if target is not None:
                target_source = "observed_history"
                break
    if target is None:
        for row in runtime.history:
            target = finite_float(row.get("polymarket_price"))
            if target is not None:
                target_source = "inferred_from_opening_rtds"
                break
    if price is None or target is None:
        return None, "MISSING", price, target, "missing"
    actual_label = int(price > target)
    return actual_label, "YES" if actual_label else "NO", price, target, target_source


def trade_row_base(
    event: str,
    runtime: ContractRuntime,
    remaining: float | None,
    threshold: float,
    polymarket_snapshot: dict[str, Any],
    counts: Counts,
) -> dict[str, Any]:
    return {
        "timestamp_utc": iso_utc(),
        "event": event,
        "contract_id": runtime.ticker,
        "close_time": runtime.close_time,
        "polymarket_ticker": runtime.polymarket_ticker,
        "remaining_seconds": "" if remaining is None else f"{remaining:.3f}",
        "threshold": threshold,
        "yes_mid": finite_float(polymarket_snapshot.get("yes_mid")),
        "successful_count": counts.successful,
        "unsuccessful_count": counts.unsuccessful,
        "skipped_count": counts.skipped,
    }


async def polymarket_balance() -> tuple[float | None, float | None, str]:
    try:
        balance, allowance = await asyncio.to_thread(trader.polymarket_balance_amounts)
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    return balance, allowance, ""


async def log_balance(
    event: str,
    remaining: float | None,
    initial_balance: float | None,
    stop_loss: float,
) -> float | None:
    balance, allowance, error = await polymarket_balance()
    remaining_text = f" T={remaining:.1f}s" if remaining is not None else ""
    if error:
        append_log(f"BALANCE{remaining_text} {event} | Polymarket ERROR {error}")
        return None
    drawdown = "" if initial_balance is None else f" drawdown={initial_balance - balance:.4f}"
    append_log(
        f"BALANCE{remaining_text} {event} | Polymarket {trader.fmt_money(balance)} "
        f"allowance={trader.fmt_money(allowance)}{drawdown}"
    )
    if initial_balance is not None and stop_loss > 0 and balance < initial_balance - stop_loss:
        raise StopLossTriggered(
            f"Polymarket balance {trader.fmt_money(balance)} is below initial "
            f"{trader.fmt_money(initial_balance)} by at least {trader.fmt_money(stop_loss)}"
        )
    return balance


async def log_balance_with_stop_loss_baseline(
    event: str,
    remaining: float | None,
    args: argparse.Namespace,
) -> float | None:
    balance = await log_balance(event, remaining, args.initial_balance, args.stop_loss)
    if args.initial_balance is None and balance is not None:
        args.initial_balance = balance
        if args.stop_loss > 0:
            append_log(f"STOP_LOSS baseline set to {trader.fmt_money(balance)}")
    return balance


async def state_for_retry(
    context: trader.AsyncMarketContext | None,
    poll_only: bool,
    poll_interval: float,
) -> trader.MarketState | None:
    if poll_only or context is None:
        try:
            return await asyncio.to_thread(trader.fetch_market_state)
        except Exception as exc:
            append_log(f"RETRY FETCH ERROR {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(0.1, poll_interval))
            return None
    return await context.wait_for_update(timeout=max(0.1, poll_interval))


async def place_order_with_retries(
    *,
    context: trader.AsyncMarketContext | None,
    poll_only: bool,
    side: str,
    contracts: int,
    dry_run: bool,
    order_type: str,
    retry_attempts: int,
    retry_delay: float,
) -> tuple[TradeDecision, dict[str, Any] | None]:
    last_state: dict[str, Any] | None = None
    attempts = max(1, retry_attempts)
    for attempt in range(1, attempts + 1):
        state = await state_for_retry(context, poll_only, retry_delay)
        if state is None:
            continue
        _kalshi_market, _kalshi_snapshot, polymarket_market, polymarket_snapshot, _source = state
        last_state = polymarket_snapshot
        price, qty = side_ask_snapshot(polymarket_snapshot, side)
        if price is None or not 0.0 < price < 1.0:
            append_log(f"ORDER RETRY {attempt}/{attempts} {side} invalid best ask {price}")
            await asyncio.sleep(max(0.0, retry_delay))
            continue
        if qty is None or qty < contracts:
            qty_text = "--" if qty is None else f"{qty:g}"
            append_log(
                f"ORDER RETRY {attempt}/{attempts} {side} insufficient best-ask liquidity "
                f"{qty_text} < {contracts:g} at {trader.fmt_cents(price)}"
            )
            await asyncio.sleep(max(0.0, retry_delay))
            continue
        if dry_run:
            return (
                TradeDecision(
                    status="dry_run",
                    side=side,
                    selected_ask=price,
                    selected_ask_qty=qty,
                    contracts=contracts,
                    dry_run=True,
                    reason=f"would buy {contracts:g} {side} at best ask {trader.fmt_cents(price)}",
                ),
                polymarket_snapshot,
            )
        try:
            response = await asyncio.to_thread(
                trader.polymarket_post_order,
                polymarket_market,
                side,
                price,
                contracts,
                None,
                order_type,
            )
        except Exception as exc:
            append_log(f"ORDER RETRY {attempt}/{attempts} {side} post error {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(0.0, retry_delay))
            continue
        order_id = trader.response_order_id(response)
        filled, fill_price, filled_size = trader.polymarket_fill_summary(response, price, contracts)
        if filled:
            return (
                TradeDecision(
                    status="filled",
                    side=side,
                    selected_ask=price,
                    selected_ask_qty=qty,
                    contracts=contracts,
                    dry_run=False,
                    order_id=order_id,
                    fill_price=fill_price,
                    filled_size=filled_size,
                    reason=f"filled {filled_size:g} at {trader.fmt_cents(fill_price)}",
                ),
                polymarket_snapshot,
            )
        append_log(f"ORDER RETRY {attempt}/{attempts} {side} unfilled at {trader.fmt_cents(price)} id={order_id}")
        await asyncio.sleep(max(0.0, retry_delay))

    price, qty = side_ask_snapshot(last_state or {}, side)
    return (
        TradeDecision(
            status="skip",
            side=side,
            selected_ask=price,
            selected_ask_qty=qty,
            contracts=contracts,
            dry_run=dry_run,
            reason=f"no fill/liquidity after {attempts:g} attempts",
        ),
        last_state,
    )


async def evaluate_entry(
    runtime: ContractRuntime,
    context: trader.AsyncMarketContext | None,
    polymarket_snapshot: dict[str, Any],
    counts: Counts,
    args: argparse.Namespace,
    remaining: float | None,
) -> None:
    side, label, probability, reason = selected_side_from_mid(polymarket_snapshot, args.threshold)
    await log_balance_with_stop_loss_baseline("T2M", remaining, args)

    row = trade_row_base("decision", runtime, remaining, args.threshold, polymarket_snapshot, counts)
    if side is None or label is None:
        counts.skipped += 1
        decision = TradeDecision(
            status="skip",
            side="",
            predicted_label=None,
            selected_probability=probability,
            contracts=args.contracts,
            dry_run=not args.live,
            reason=reason,
        )
        runtime.decision = decision
        append_log(
            f"STATUS T=2m {runtime.ticker} | yes_mid={trader.fmt_price(polymarket_snapshot.get('yes_mid'), 4)} "
            f"decision=SKIP reason={reason} | counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        row.update(
            {
                "selected_side": "",
                "selected_probability": probability,
                "contracts": args.contracts,
                "dry_run": int(not args.live),
                "order_status": "skip",
                "reason": reason,
                "skipped_count": counts.skipped,
            }
        )
        append_trade_row(row)
        return

    append_log(
        f"STATUS T=2m {runtime.ticker} | yes_mid={trader.fmt_price(polymarket_snapshot.get('yes_mid'), 4)} "
        f"selected={side} selected_prob={trader.fmt_price(probability, 4)} threshold={args.threshold:.2f}"
        f" contracts={args.contracts}",
        prefix_timestamp=False,
    )
    decision, latest_snapshot = await place_order_with_retries(
        context=context,
        poll_only=args.poll_only,
        side=side,
        contracts=args.contracts,
        dry_run=not args.live,
        order_type=args.order_type,
        retry_attempts=args.retry_attempts,
        retry_delay=args.retry_delay,
    )
    decision.predicted_label = label
    decision.selected_probability = probability
    runtime.decision = decision

    if latest_snapshot is not None:
        row["yes_mid"] = finite_float(latest_snapshot.get("yes_mid"))
    if decision.status == "skip":
        counts.skipped += 1
    append_log(
        f"ORDER {decision.status.upper()} {runtime.ticker} {side} | "
        f"ask={trader.fmt_cents(decision.selected_ask)} qty={decision.selected_ask_qty if decision.selected_ask_qty is not None else '--'} "
        f"order_id={decision.order_id or '--'} {decision.reason} | "
        f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
        prefix_timestamp=False,
    )
    row.update(
        {
            "selected_side": side,
            "selected_probability": probability,
            "selected_ask": decision.selected_ask,
            "selected_ask_qty": decision.selected_ask_qty,
            "contracts": args.contracts,
            "dry_run": int(not args.live),
            "order_status": decision.status,
            "order_id": decision.order_id,
            "fill_price": decision.fill_price,
            "filled_size": decision.filled_size,
            "reason": decision.reason,
            "skipped_count": counts.skipped,
        }
    )
    append_trade_row(row)


def outcome_event(
    runtime: ContractRuntime,
    source_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    counts: Counts,
    threshold: float,
    remaining: float | None,
) -> None:
    actual_label, actual_side, price, target, target_source = current_outcome(runtime, source_snapshot)
    decision = runtime.decision
    latest = "skipped"
    correct: int | str = ""
    if decision is not None and decision.outcome_eligible and actual_label is not None:
        correct = int(decision.predicted_label == actual_label)
        if correct:
            counts.successful += 1
            latest = "successful"
        else:
            counts.unsuccessful += 1
            latest = "unsuccessful"
        decision.outcome_recorded = True

    append_log(
        f"OUTCOME {runtime.ticker} | P price={trader.fmt_price(price, 2)} "
        f"target={trader.fmt_price(target, 2)} target_source={target_source} actual={actual_side} "
        f"trade={decision.side if decision else '--'} status={decision.status if decision else 'none'} "
        f"result={latest} correct={correct if correct != '' else '--'} | "
        f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
        prefix_timestamp=False,
    )
    row = trade_row_base("outcome", runtime, remaining, threshold, polymarket_snapshot, counts)
    row.update(
        {
            "selected_side": decision.side if decision else "",
            "selected_probability": decision.selected_probability if decision else "",
            "selected_ask": decision.selected_ask if decision else "",
            "selected_ask_qty": decision.selected_ask_qty if decision else "",
            "contracts": decision.contracts if decision else "",
            "dry_run": int(decision.dry_run) if decision else "",
            "order_status": decision.status if decision else "none",
            "order_id": decision.order_id if decision else "",
            "fill_price": decision.fill_price if decision else "",
            "filled_size": decision.filled_size if decision else "",
            "actual_side": actual_side,
            "actual_label": actual_label if actual_label is not None else "",
            "correct": correct,
            "polymarket_price": price if price is not None else "",
            "polymarket_target": target if target is not None else "",
            "polymarket_target_source": target_source,
            "reason": latest,
        }
    )
    append_trade_row(row)
    runtime.outcome_logged = True


async def start_context_with_backoff(startup_timeout: float) -> trader.AsyncMarketContext:
    while True:
        context = trader.AsyncMarketContext(trader.fetch_market_state, logger=lambda line: append_log(line))
        try:
            await asyncio.wait_for(context.start(), timeout=startup_timeout)
            return context
        except TimeoutError:
            await context.stop()
            append_log(f"MARKET REFRESH WAIT startup timeout after {startup_timeout:g}s; retrying")
        except Exception as exc:
            await context.stop()
            if not (
                trader.is_active_market_rate_limit_error(exc)
                or "No open market found" in str(exc)
                or "No matching open Polymarket market found" in str(exc)
            ):
                raise
            append_log(f"MARKET REFRESH WAIT startup {type(exc).__name__}: {exc}; retrying")
        await asyncio.sleep(trader.ACTIVE_MARKET_REFRESH_MIN_INTERVAL_SECONDS)


async def fetch_state_polling() -> trader.MarketState | None:
    try:
        return await asyncio.to_thread(trader.fetch_market_state)
    except Exception as exc:
        append_log(f"POLL FETCH ERROR {type(exc).__name__}: {exc}")
        return None


def contract_key(ticker: str, close_time: str) -> tuple[str, str]:
    return ticker, close_time


async def run() -> None:
    args = parse_args()
    args.initial_balance = None
    counts = Counts()
    started_at = time.monotonic()
    completed_contracts = 0
    completed_seen: set[tuple[str, str]] = set()
    expired_seen: set[tuple[str, str]] = set()

    append_log(
        f"START polymarket_trader live={args.live} contracts={args.contracts} "
        f"threshold={args.threshold:.2f} entry_seconds={args.entry_seconds:g} "
        f"retry_attempts={args.retry_attempts} stop_loss={trader.fmt_money(args.stop_loss)}"
    )
    if await log_balance_with_stop_loss_baseline("START", None, args) is None and args.stop_loss > 0:
        append_log("STOP_LOSS baseline unavailable until first successful balance read")

    context = None if args.poll_only else await start_context_with_backoff(args.startup_timeout)
    runtime: ContractRuntime | None = None

    try:
        while True:
            if args.max_seconds and time.monotonic() - started_at >= args.max_seconds:
                append_log(f"STOP max_seconds={args.max_seconds:g} reached")
                return

            if args.poll_only:
                state = await fetch_state_polling()
                if state is None:
                    await asyncio.sleep(args.poll_interval)
                    continue
                await asyncio.sleep(max(0.1, args.poll_interval))
            else:
                if context is None:
                    context = await start_context_with_backoff(args.startup_timeout)
                state = await context.wait_for_update(timeout=0.5)

            kalshi_market, kalshi_snapshot, _polymarket_market, polymarket_snapshot, source_snapshot = state
            ticker = str(kalshi_snapshot.get("ticker") or kalshi_market.get("ticker") or "")
            close_time = str(kalshi_snapshot.get("close_time") or kalshi_market.get("close_time") or "")
            polymarket_ticker = str(polymarket_snapshot.get("ticker") or "")
            remaining = trader.seconds_to_expiry(kalshi_snapshot)
            key = contract_key(ticker, close_time)

            if key in completed_seen:
                continue

            if (runtime is None or runtime.ticker != ticker) and remaining is not None and remaining < 0:
                if key not in expired_seen:
                    expired_seen.add(key)
                    append_log(f"SKIP expired contract {ticker} close={close_time} T={remaining:.1f}s")
                trader.KALSHI_MARKET_CACHE.pop(trader.SERIES_TICKER, None)
                continue

            if runtime is None or runtime.ticker != ticker:
                runtime = ContractRuntime(ticker=ticker, close_time=close_time, polymarket_ticker=polymarket_ticker)
                append_log("", prefix_timestamp=False)
                append_log(
                    f"CONTRACT {ticker} | close {close_time} | Polymarket {polymarket_ticker} | "
                    f"P target {trader.fmt_price(source_snapshot.get('polymarket_target'), 2)}",
                    prefix_timestamp=False,
                )

            runtime.history.append(
                {
                    "timestamp_utc": trader.iso_utc(),
                    "polymarket_price": source_snapshot.get("polymarket_price"),
                    "polymarket_target": source_snapshot.get("polymarket_target"),
                }
            )

            if (
                remaining is not None
                and 0 <= remaining <= args.entry_seconds
                and not runtime.decision_logged
            ):
                runtime.decision_logged = True
                await evaluate_entry(runtime, context, polymarket_snapshot, counts, args, remaining)

            if remaining is not None and remaining <= args.outcome_delay_seconds and not runtime.outcome_logged:
                await log_balance_with_stop_loss_baseline("OUTCOME", remaining, args)
                outcome_event(runtime, source_snapshot, polymarket_snapshot, counts, args.threshold, remaining)
                completed_seen.add(key)
                completed_contracts += 1
                if args.max_contracts and completed_contracts >= args.max_contracts:
                    append_log(f"STOP max_contracts={args.max_contracts:g} reached")
                    return
                trader.KALSHI_MARKET_CACHE.pop(trader.SERIES_TICKER, None)
                if not args.poll_only:
                    old_context = context
                    context = await start_context_with_backoff(args.startup_timeout)
                    if old_context is not None:
                        await old_context.stop()
                runtime = None
    except StopLossTriggered as exc:
        append_log(f"STOP_LOSS {exc}")
        return
    finally:
        if context is not None:
            await context.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple Polymarket BTC 15m 2m-threshold trader.")
    parser.add_argument("--live", action="store_true", help="Submit real Polymarket orders. Omit for dry-run logging.")
    parser.add_argument("--contracts", type=int, default=2, help="Contracts/shares to buy. Default: 2.")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="More-likely-side midpoint threshold. Default: 0.68.")
    parser.add_argument("--entry-seconds", type=float, default=DEFAULT_ENTRY_SECONDS, help="Evaluate once when T <= this many seconds. Default: 120.")
    parser.add_argument("--retry-attempts", type=int, default=DEFAULT_RETRY_ATTEMPTS, help="Max best-price liquidity/order retries. Default: 10.")
    parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY_SECONDS, help="Seconds between retries. Default: 0.5.")
    parser.add_argument("--order-type", default="FOK", help="Polymarket order type. Default: FOK.")
    parser.add_argument("--stop-loss", type=float, default=10.0, help="Stop if Polymarket balance drops this many dollars below initial balance. Default: 10.")
    parser.add_argument("--poll-only", action="store_true", help="Use HTTP polling instead of websocket market context.")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between HTTP polls in --poll-only mode. Default: 2.")
    parser.add_argument("--startup-timeout", type=float, default=25.0, help="Seconds before websocket startup is retried. Default: 25.")
    parser.add_argument("--outcome-delay-seconds", type=float, default=DEFAULT_OUTCOME_DELAY_SECONDS, help="Evaluate outcome this many seconds relative to close. Default: -2.")
    parser.add_argument("--max-contracts", type=int, default=0, help="Stop after this many contract outcomes. 0 means run forever.")
    parser.add_argument("--max-seconds", type=float, default=0.0, help="Stop after this many wall-clock seconds. 0 means no limit.")
    args = parser.parse_args()
    args.contracts = max(1, args.contracts)
    args.retry_attempts = max(1, args.retry_attempts)
    return args


if __name__ == "__main__":
    asyncio.run(run())
