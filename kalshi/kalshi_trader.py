#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cli_trader_v2 as trader
import kalshi_trader_stats


APP_DIR = Path(__file__).resolve().parent
LOG_PATH = Path(os.getenv("KALSHI_TRADER_LOG_PATH", str(APP_DIR / "kalshi_trader.log"))).expanduser()
TRADES_CSV_PATH = Path(os.getenv("KALSHI_TRADER_TRADES_CSV", str(APP_DIR / "kalshi_trader_trades.csv"))).expanduser()
STATS_CSV_PATH = Path(
    os.getenv("KALSHI_TRADER_STATS_CSV", str(LOG_PATH.with_name("kalshi_trader_stats.csv")))
).expanduser()

DEFAULT_BAND_LOW = 0.50
DEFAULT_BAND_HIGH = 0.78
DEFAULT_CONTRACTS = 2
DEFAULT_ENTRY_SECONDS = 630
DEFAULT_RETRY_ATTEMPTS = 10
DEFAULT_RETRY_DELAY_SECONDS = 0.5
DEFAULT_OUTCOME_DELAY_SECONDS = -2.0
PRICE_EPSILON = 1e-9

TRADE_FIELDS = [
    "timestamp_utc",
    "event",
    "contract_id",
    "close_time",
    "remaining_seconds",
    "entry_seconds",
    "band_low",
    "band_high",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
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
    "kalshi_price",
    "kalshi_target",
    "kalshi_target_source",
    "successful_count",
    "unsuccessful_count",
    "skipped_count",
    "valid_orderbook",
    "invalid_orderbook_reasons",
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
    refresh_stats_csv()


def refresh_stats_csv() -> None:
    try:
        kalshi_trader_stats.refresh_stats_csv(LOG_PATH, TRADES_CSV_PATH, STATS_CSV_PATH)
    except Exception as exc:
        append_log(f"STATS_CSV ERROR {type(exc).__name__}: {exc}", prefix_timestamp=True)


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
    valid_orderbook: bool = True
    invalid_orderbook_reasons: list[str] = field(default_factory=list)
    reason: str = ""
    timestamp_utc: str = field(default_factory=iso_utc)
    outcome_recorded: bool = False

    @property
    def outcome_eligible(self) -> bool:
        return self.status in {"filled", "dry_run"} and self.predicted_label is not None


@dataclass
class EntryCandidate:
    side: str | None = None
    predicted_label: int | None = None
    selected_probability: float | None = None
    selected_ask: float | None = None
    selected_ask_qty: float | None = None
    spot_agrees: bool | None = None
    entry_spot: float | None = None
    entry_target: float | None = None
    entry_delta: float | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.side is not None and self.predicted_label is not None and not self.reason


@dataclass
class ContractRuntime:
    ticker: str
    close_time: str
    market: dict[str, Any]
    history: list[dict[str, Any]] = field(default_factory=list)
    decision: TradeDecision | None = None
    decision_logged: bool = False
    outcome_logged: bool = False
    last_status_log_at: float = 0.0


class StopLossTriggered(RuntimeError):
    pass


def entry_label(entry_seconds: float) -> str:
    if entry_seconds % 60 == 0:
        return f"T={entry_seconds / 60:g}m"
    return f"T={entry_seconds:g}s"


def side_display(side: str | None) -> str:
    return str(side or "").upper()


def kalshi_source_price_snapshot(kalshi_market: dict[str, Any]) -> dict[str, Any]:
    kalshi_current = trader.fetch_brti_price() or trader.plausible_btc_price(
        trader.numeric_value(kalshi_market.get("expiration_value"))
    )
    if kalshi_current is not None:
        trader.cached_source_price("brti", kalshi_current)
    kalshi_60_sma, kalshi_60_sma_count = trader.kalshi_brti_60_sma(kalshi_current)
    out = {
        "timestamp_utc": trader.iso_utc(),
        "kalshi_price": kalshi_current,
        "kalshi_target": trader.numeric_value(kalshi_market.get("floor_strike")),
        "kalshi_60_sma": kalshi_60_sma,
        "kalshi_60_sma_sample_count": kalshi_60_sma_count,
    }
    trader.SOURCE_HISTORY.append(out)
    return out


def minimal_snapshot(kalshi_market: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp_utc": trader.iso_utc(),
        "ticker": kalshi_market.get("ticker"),
        "title": kalshi_market.get("title") or kalshi_market.get("subtitle") or "",
        "event_ticker": kalshi_market.get("event_ticker") or "",
        "close_time": (
            kalshi_market.get("close_time")
            or kalshi_market.get("close_ts")
            or kalshi_market.get("expiration_time")
            or ""
        ),
        "status": kalshi_market.get("status") or "",
    }


def fetch_kalshi_state(
    kalshi_market: dict[str, Any] | None = None,
    *,
    allow_missing_orderbook: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    market = kalshi_market or trader.cached_active_kalshi_market()
    if not market:
        raise RuntimeError(f"No open market found for {trader.SERIES_TICKER}")
    source_snapshot = kalshi_source_price_snapshot(market)
    try:
        orderbook = trader.kalshi_get(
            f"/markets/{market['ticker']}/orderbook",
            {"depth": trader.ORDERBOOK_DEPTH},
        )
        snapshot = trader.make_snapshot(market, orderbook)
    except Exception:
        if not allow_missing_orderbook:
            raise
        orderbook = {}
        snapshot = minimal_snapshot(market)
    return market, snapshot, source_snapshot, orderbook


async def fetch_state_polling(
    kalshi_market: dict[str, Any] | None = None,
    *,
    allow_missing_orderbook: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    try:
        return await asyncio.to_thread(fetch_kalshi_state, kalshi_market, allow_missing_orderbook=allow_missing_orderbook)
    except Exception as exc:
        append_log(f"POLL FETCH ERROR {type(exc).__name__}: {exc}")
        return None


def best_raw_quotes(orderbook: dict[str, Any]) -> dict[str, float | None]:
    yes_levels, no_levels = trader.orderbook_levels(orderbook)
    yes_bid, yes_bid_qty = trader.best_level(yes_levels)
    no_bid, no_bid_qty = trader.best_level(no_levels)
    return {
        "yes_bid": finite_float(yes_bid),
        "yes_bid_qty": finite_float(yes_bid_qty),
        "no_bid": finite_float(no_bid),
        "no_bid_qty": finite_float(no_bid_qty),
        "yes_ask": trader.invert_price(finite_float(no_bid)),
        "yes_ask_qty": finite_float(no_bid_qty),
        "no_ask": trader.invert_price(finite_float(yes_bid)),
        "no_ask_qty": finite_float(yes_bid_qty),
    }


def side_ask_from_orderbook(orderbook: dict[str, Any], side: str) -> tuple[float | None, float | None]:
    quotes = best_raw_quotes(orderbook)
    if side == "yes":
        return finite_float(quotes.get("yes_ask")), finite_float(quotes.get("yes_ask_qty"))
    return finite_float(quotes.get("no_ask")), finite_float(quotes.get("no_ask_qty"))


def orderbook_invalid_reasons(
    orderbook: dict[str, Any],
    *,
    side: str | None = None,
    contracts: int | None = None,
) -> list[str]:
    reasons: list[str] = []
    if not orderbook:
        return ["missing_orderbook"]

    quotes = best_raw_quotes(orderbook)
    for name in ("yes_bid", "no_bid"):
        price = finite_float(quotes.get(name))
        if price is not None and not (0.0 <= price <= 1.0):
            reasons.append(f"{name}:price_out_of_range:{price:g}")
    for name in ("yes_ask", "no_ask"):
        price = finite_float(quotes.get(name))
        if price is None or not (0.0 < price < 1.0):
            reasons.append(f"{name}:price_missing_or_out_of_range:{price}")
    for name in ("yes_bid_qty", "no_bid_qty", "yes_ask_qty", "no_ask_qty"):
        qty = finite_float(quotes.get(name))
        if qty is not None and qty < 0:
            reasons.append(f"{name}:quantity_negative:{qty:g}")

    yes_bid = finite_float(quotes.get("yes_bid"))
    no_bid = finite_float(quotes.get("no_bid"))
    if yes_bid is not None and no_bid is not None and yes_bid + no_bid > 1.0 + PRICE_EPSILON:
        reasons.append(f"crossed_book:yes_bid+no_bid={yes_bid + no_bid:.4f}>1")

    if side:
        ask, qty = side_ask_from_orderbook(orderbook, side)
        if ask is None or not 0.0 < ask < 1.0:
            reasons.append(f"{side}_ask:price_missing_or_out_of_range:{ask}")
        if contracts is not None and (qty is None or qty < contracts):
            qty_text = "--" if qty is None else f"{qty:g}"
            reasons.append(f"{side}_ask:liquidity_{qty_text}<{contracts:g}")
    return reasons


def selected_side_from_mid(kalshi_snapshot: dict[str, Any]) -> tuple[str | None, int | None, float | None, str]:
    yes_mid = finite_float(kalshi_snapshot.get("yes_mid"))
    if yes_mid is None:
        return None, None, None, "missing yes_mid"
    no_mid = 1.0 - yes_mid
    if yes_mid >= no_mid:
        side = "yes"
        label = 1
        probability = yes_mid
    else:
        side = "no"
        label = 0
        probability = no_mid
    return side, label, probability, ""


def spot_agreement(source_snapshot: dict[str, Any], side: str) -> tuple[bool | None, float | None, float | None, float | None, str]:
    spot = finite_float(source_snapshot.get("kalshi_price"))
    target = finite_float(source_snapshot.get("kalshi_target"))
    if spot is None or target is None:
        return None, spot, target, None, "missing spot or target for spot-agreement filter"
    delta = spot - target
    agrees = delta > 0 if side == "yes" else delta < 0
    if agrees:
        return True, spot, target, delta, ""
    return (
        False,
        spot,
        target,
        delta,
        f"spot disagrees with {side_display(side)}: K {trader.fmt_price(spot, 2)} "
        f"{trader.fmt_price_delta(spot, target, 2)}",
    )


def entry_candidate(
    kalshi_snapshot: dict[str, Any],
    orderbook: dict[str, Any],
    source_snapshot: dict[str, Any],
    band_low: float,
    band_high: float,
) -> EntryCandidate:
    side, label, probability, side_reason = selected_side_from_mid(kalshi_snapshot)
    if side is None or label is None:
        return EntryCandidate(selected_probability=probability, reason=side_reason)

    selected_ask, selected_ask_qty = side_ask_from_orderbook(orderbook, side)
    agrees, spot, target, delta, spot_reason = spot_agreement(source_snapshot, side)
    candidate = EntryCandidate(
        side=side,
        predicted_label=label,
        selected_probability=probability,
        selected_ask=selected_ask,
        selected_ask_qty=selected_ask_qty,
        spot_agrees=agrees,
        entry_spot=spot,
        entry_target=target,
        entry_delta=delta,
    )

    if selected_ask is None or not 0.0 < selected_ask < 1.0:
        candidate.reason = f"{side}_ask missing or out of range: {selected_ask}"
    elif not (band_low < selected_ask < band_high):
        candidate.reason = (
            f"selected ask {trader.fmt_price(selected_ask, 4)} outside ask band "
            f"{band_low:.2f}<p<{band_high:.2f}"
        )
    elif agrees is not True:
        candidate.reason = spot_reason
    return candidate


def current_outcome(
    runtime: ContractRuntime,
    source_snapshot: dict[str, Any],
) -> tuple[int | None, str, float | None, float | None, str]:
    price = finite_float(source_snapshot.get("kalshi_price"))
    target = finite_float(source_snapshot.get("kalshi_target"))
    target_source = "observed_source_snapshot"
    if target is None:
        for row in reversed(runtime.history):
            target = finite_float(row.get("kalshi_target"))
            if target is not None:
                target_source = "observed_history"
                break
    if price is None:
        for row in reversed(runtime.history):
            price = finite_float(row.get("kalshi_price"))
            if price is not None:
                break
    if price is None or target is None:
        return None, "MISSING", price, target, "missing"
    actual_label = int(price > target)
    return actual_label, "YES" if actual_label else "NO", price, target, target_source


def trade_row_base(
    event: str,
    runtime: ContractRuntime,
    remaining: float | None,
    args: argparse.Namespace,
    kalshi_snapshot: dict[str, Any],
    counts: Counts,
    orderbook_reasons: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "timestamp_utc": iso_utc(),
        "event": event,
        "contract_id": runtime.ticker,
        "close_time": runtime.close_time,
        "remaining_seconds": "" if remaining is None else f"{remaining:.3f}",
        "entry_seconds": args.entry_seconds,
        "band_low": args.band_low,
        "band_high": args.band_high,
        "yes_bid": finite_float(kalshi_snapshot.get("yes_bid")),
        "yes_ask": finite_float(kalshi_snapshot.get("yes_ask")),
        "no_bid": finite_float(kalshi_snapshot.get("no_bid")),
        "no_ask": finite_float(kalshi_snapshot.get("no_ask")),
        "yes_mid": finite_float(kalshi_snapshot.get("yes_mid")),
        "successful_count": counts.successful,
        "unsuccessful_count": counts.unsuccessful,
        "skipped_count": counts.skipped,
        "valid_orderbook": int(not orderbook_reasons),
        "invalid_orderbook_reasons": "; ".join(orderbook_reasons or []),
    }


async def kalshi_balance() -> tuple[float | None, float | None, str]:
    try:
        balance, available = await asyncio.to_thread(trader.kalshi_balance_amounts)
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    return balance, available, ""


async def log_balance(
    event: str,
    remaining: float | None,
    initial_balance: float | None,
    stop_loss: float,
) -> float | None:
    balance, available, error = await kalshi_balance()
    remaining_text = f" T={remaining:.1f}s" if remaining is not None else ""
    if error:
        append_log(f"BALANCE{remaining_text} {event} | Kalshi ERROR {error}")
        return None
    available_text = "" if available is None else f" available={trader.fmt_money(available)}"
    drawdown = "" if initial_balance is None else f" drawdown={initial_balance - balance:.4f}"
    append_log(f"BALANCE{remaining_text} {event} | Kalshi {trader.fmt_money(balance)}{available_text}{drawdown}")
    refresh_stats_csv()
    if initial_balance is not None and stop_loss > 0 and balance < initial_balance - stop_loss:
        raise StopLossTriggered(
            f"Kalshi balance {trader.fmt_money(balance)} is below initial "
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


async def place_order_with_retries(
    *,
    expected_ticker: str,
    side: str,
    contracts: int,
    dry_run: bool,
    time_in_force: str,
    band_low: float,
    band_high: float,
    retry_attempts: int,
    retry_delay: float,
) -> tuple[TradeDecision, dict[str, Any] | None, list[str]]:
    last_snapshot: dict[str, Any] | None = None
    last_reasons: list[str] = []
    last_failure_reason = ""
    attempts = max(1, retry_attempts)
    for attempt in range(1, attempts + 1):
        state = await fetch_state_polling()
        if state is None:
            await asyncio.sleep(max(0.0, retry_delay))
            continue
        kalshi_market, kalshi_snapshot, source_snapshot, orderbook = state
        ticker = str(kalshi_snapshot.get("ticker") or kalshi_market.get("ticker") or "")
        if ticker != expected_ticker:
            reason = f"contract changed during retry: {ticker or '--'} != {expected_ticker}"
            append_log(f"ORDER RETRY {attempt}/{attempts} {side_display(side)} {reason}")
            last_failure_reason = reason
            await asyncio.sleep(max(0.0, retry_delay))
            continue

        last_snapshot = kalshi_snapshot
        candidate = entry_candidate(kalshi_snapshot, orderbook, source_snapshot, band_low, band_high)
        if candidate.side != side or candidate.predicted_label is None:
            reason = candidate.reason or f"current side changed to {side_display(candidate.side)}"
            append_log(f"ORDER RETRY {attempt}/{attempts} {side_display(side)} {reason}")
            last_failure_reason = reason
            await asyncio.sleep(max(0.0, retry_delay))
            continue
        if not candidate.ok:
            append_log(f"ORDER RETRY {attempt}/{attempts} {side_display(side)} {candidate.reason}")
            last_failure_reason = candidate.reason
            await asyncio.sleep(max(0.0, retry_delay))
            continue

        reasons = orderbook_invalid_reasons(orderbook, side=side, contracts=contracts)
        last_reasons = reasons
        if reasons:
            last_failure_reason = f"invalid Kalshi orderbook: {'; '.join(reasons)}"
            append_log(
                f"ORDER RETRY {attempt}/{attempts} {side_display(side)} invalid Kalshi orderbook: "
                f"{'; '.join(reasons)}"
            )
            await asyncio.sleep(max(0.0, retry_delay))
            continue

        price = candidate.selected_ask
        qty = candidate.selected_ask_qty
        if dry_run:
            return (
                TradeDecision(
                    status="dry_run",
                    side=side,
                    predicted_label=candidate.predicted_label,
                    selected_probability=candidate.selected_probability,
                    selected_ask=price,
                    selected_ask_qty=qty,
                    contracts=contracts,
                    dry_run=True,
                    filled_size=float(contracts),
                    reason=f"would buy {contracts:g} {side_display(side)} at best ask {trader.fmt_cents(price)}",
                ),
                kalshi_snapshot,
                reasons,
            )

        client_order_id = f"kalshi-trader-{uuid.uuid4().hex[:16]}"
        try:
            response = await asyncio.to_thread(
                trader.kalshi_post_order,
                expected_ticker,
                side,
                price,
                contracts,
                client_order_id,
                "buy",
                time_in_force,
            )
        except Exception as exc:
            last_failure_reason = f"post error {type(exc).__name__}: {exc}"
            append_log(f"ORDER RETRY {attempt}/{attempts} {side_display(side)} {last_failure_reason}")
            await asyncio.sleep(max(0.0, retry_delay))
            continue

        order_id = trader.response_order_id(response)
        filled_size = trader.fill_count(response)
        fill_price = trader.filled_price(response, side)
        if filled_size >= contracts:
            return (
                TradeDecision(
                    status="filled",
                    side=side,
                    predicted_label=candidate.predicted_label,
                    selected_probability=candidate.selected_probability,
                    selected_ask=price,
                    selected_ask_qty=qty,
                    contracts=contracts,
                    dry_run=False,
                    order_id=order_id,
                    fill_price=fill_price,
                    filled_size=filled_size,
                    reason=f"filled {filled_size:g} at {trader.fmt_cents(fill_price)}",
                ),
                kalshi_snapshot,
                reasons,
            )

        append_log(
            f"ORDER RETRY {attempt}/{attempts} {side_display(side)} unfilled at "
            f"{trader.fmt_cents(price)} id={order_id}"
        )
        last_failure_reason = f"unfilled at {trader.fmt_cents(price)} id={order_id}"
        await asyncio.sleep(max(0.0, retry_delay))

    price, qty = side_ask_from_orderbook({}, side)
    if last_snapshot is not None:
        price = finite_float(last_snapshot.get("yes_ask" if side == "yes" else "no_ask"))
        qty = finite_float(last_snapshot.get("best_yes_ask_qty" if side == "yes" else "best_no_ask_qty"))
    retry_reason = f"no fill/valid liquidity after {attempts:g} attempts"
    if last_failure_reason:
        retry_reason = f"{retry_reason}; last={last_failure_reason}"
    return (
        TradeDecision(
            status="skip",
            side=side,
            selected_ask=price,
            selected_ask_qty=qty,
            contracts=contracts,
            dry_run=dry_run,
            valid_orderbook=not last_reasons,
            invalid_orderbook_reasons=last_reasons,
            reason=retry_reason,
        ),
        last_snapshot,
        last_reasons,
    )


async def evaluate_entry(
    runtime: ContractRuntime,
    counts: Counts,
    args: argparse.Namespace,
    remaining: float | None,
) -> None:
    state = await fetch_state_polling(runtime.market)
    if state is None:
        counts.skipped += 1
        decision = TradeDecision(status="skip", contracts=args.contracts, dry_run=not args.live, reason="missing entry state")
        runtime.decision = decision
        append_log(
            f"STATUS {entry_label(args.entry_seconds)} {runtime.ticker} | decision=SKIP reason=missing entry state | "
            f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        return

    _market, kalshi_snapshot, source_snapshot, orderbook = state
    await log_balance_with_stop_loss_baseline(entry_label(args.entry_seconds).replace("=", ""), remaining, args)
    base_reasons = orderbook_invalid_reasons(orderbook)
    row = trade_row_base("decision", runtime, remaining, args, kalshi_snapshot, counts, base_reasons)

    if base_reasons:
        counts.skipped += 1
        decision = TradeDecision(
            status="skip",
            contracts=args.contracts,
            dry_run=not args.live,
            valid_orderbook=False,
            invalid_orderbook_reasons=base_reasons,
            reason=f"invalid Kalshi orderbook: {'; '.join(base_reasons)}",
        )
        runtime.decision = decision
        append_log(
            f"STATUS {entry_label(args.entry_seconds)} {runtime.ticker} | "
            f"yes_mid={trader.fmt_price(kalshi_snapshot.get('yes_mid'), 4)} decision=SKIP "
            f"reason={decision.reason} | counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        row.update(
            {
                "contracts": args.contracts,
                "dry_run": int(not args.live),
                "order_status": "skip",
                "reason": decision.reason,
                "skipped_count": counts.skipped,
            }
        )
        append_trade_row(row)
        return

    candidate = entry_candidate(kalshi_snapshot, orderbook, source_snapshot, args.band_low, args.band_high)
    if not candidate.ok:
        counts.skipped += 1
        decision = TradeDecision(
            status="skip",
            side=candidate.side or "",
            predicted_label=candidate.predicted_label,
            selected_probability=candidate.selected_probability,
            selected_ask=candidate.selected_ask,
            selected_ask_qty=candidate.selected_ask_qty,
            contracts=args.contracts,
            dry_run=not args.live,
            reason=candidate.reason,
        )
        runtime.decision = decision
        append_log(
            f"STATUS {entry_label(args.entry_seconds)} {runtime.ticker} | "
            f"yes_mid={trader.fmt_price(kalshi_snapshot.get('yes_mid'), 4)} decision=SKIP "
            f"reason={candidate.reason} | counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
            prefix_timestamp=False,
        )
        row.update(
            {
                "selected_side": side_display(candidate.side),
                "selected_probability": candidate.selected_probability,
                "selected_ask": candidate.selected_ask,
                "selected_ask_qty": candidate.selected_ask_qty,
                "contracts": args.contracts,
                "dry_run": int(not args.live),
                "order_status": "skip",
                "reason": candidate.reason,
                "skipped_count": counts.skipped,
            }
        )
        append_trade_row(row)
        return

    append_log(
        f"STATUS {entry_label(args.entry_seconds)} {runtime.ticker} | "
        f"yes_mid={trader.fmt_price(kalshi_snapshot.get('yes_mid'), 4)} selected={side_display(candidate.side)} "
        f"selected_prob={trader.fmt_price(candidate.selected_probability, 4)} "
        f"ask={trader.fmt_cents(candidate.selected_ask)} ask_band={args.band_low:.2f}<p<{args.band_high:.2f} "
        f"spot_agrees=1 "
        f"contracts={args.contracts}",
        prefix_timestamp=False,
    )
    decision, latest_snapshot, latest_reasons = await place_order_with_retries(
        expected_ticker=runtime.ticker,
        side=str(candidate.side),
        contracts=args.contracts,
        dry_run=not args.live,
        time_in_force=args.time_in_force,
        band_low=args.band_low,
        band_high=args.band_high,
        retry_attempts=args.retry_attempts,
        retry_delay=args.retry_delay,
    )
    if decision.predicted_label is None:
        decision.predicted_label = candidate.predicted_label
    if decision.selected_probability is None:
        decision.selected_probability = candidate.selected_probability
    if decision.selected_ask is None:
        decision.selected_ask = candidate.selected_ask
    if decision.selected_ask_qty is None:
        decision.selected_ask_qty = candidate.selected_ask_qty
    runtime.decision = decision

    if latest_snapshot is not None:
        row["yes_mid"] = finite_float(latest_snapshot.get("yes_mid"))
    if decision.status == "skip":
        counts.skipped += 1
    append_log(
        f"ORDER {decision.status.upper()} {runtime.ticker} {side_display(candidate.side)} | "
        f"ask={trader.fmt_cents(decision.selected_ask)} "
        f"qty={decision.selected_ask_qty if decision.selected_ask_qty is not None else '--'} "
        f"order_id={decision.order_id or '--'} {decision.reason} | "
        f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
        prefix_timestamp=False,
    )
    row.update(
        {
            "selected_side": side_display(candidate.side),
            "selected_probability": decision.selected_probability,
            "selected_ask": decision.selected_ask,
            "selected_ask_qty": decision.selected_ask_qty,
            "contracts": args.contracts,
            "dry_run": int(not args.live),
            "order_status": decision.status,
            "order_id": decision.order_id,
            "fill_price": decision.fill_price,
            "filled_size": decision.filled_size,
            "valid_orderbook": int(not latest_reasons),
            "invalid_orderbook_reasons": "; ".join(latest_reasons),
            "reason": decision.reason,
            "skipped_count": counts.skipped,
        }
    )
    append_trade_row(row)


def outcome_event(
    runtime: ContractRuntime,
    source_snapshot: dict[str, Any],
    kalshi_snapshot: dict[str, Any],
    counts: Counts,
    args: argparse.Namespace,
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
        f"OUTCOME {runtime.ticker} | K price={trader.fmt_price(price, 2)} "
        f"target={trader.fmt_price(target, 2)} target_source={target_source} actual={actual_side} "
        f"trade={side_display(decision.side) if decision else '--'} status={decision.status if decision else 'none'} "
        f"result={latest} correct={correct if correct != '' else '--'} | "
        f"counts S={counts.successful} U={counts.unsuccessful} K={counts.skipped}",
        prefix_timestamp=False,
    )
    row = trade_row_base("outcome", runtime, remaining, args, kalshi_snapshot, counts)
    row.update(
        {
            "selected_side": side_display(decision.side) if decision else "",
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
            "kalshi_price": price if price is not None else "",
            "kalshi_target": target if target is not None else "",
            "kalshi_target_source": target_source,
            "reason": latest,
        }
    )
    append_trade_row(row)
    runtime.outcome_logged = True


def contract_key(ticker: str, close_time: str) -> tuple[str, str]:
    return ticker, close_time


async def maybe_record_runtime_outcome(
    runtime: ContractRuntime | None,
    counts: Counts,
    args: argparse.Namespace,
    completed_seen: set[tuple[str, str]],
) -> tuple[bool, ContractRuntime | None]:
    if runtime is None or runtime.outcome_logged:
        return False, runtime
    close_ts = trader.parse_ts(runtime.close_time)
    if not close_ts:
        return False, runtime
    remaining = close_ts - time.time()
    if remaining > args.outcome_delay_seconds:
        return False, runtime

    state = await fetch_state_polling(runtime.market, allow_missing_orderbook=True)
    if state is None:
        source_snapshot = kalshi_source_price_snapshot(runtime.market)
        kalshi_snapshot = minimal_snapshot(runtime.market)
    else:
        _market, kalshi_snapshot, source_snapshot, _orderbook = state
    await log_balance_with_stop_loss_baseline("OUTCOME", remaining, args)
    outcome_event(runtime, source_snapshot, kalshi_snapshot, counts, args, remaining)
    completed_seen.add(contract_key(runtime.ticker, runtime.close_time))
    trader.KALSHI_MARKET_CACHE.pop(trader.SERIES_TICKER, None)
    return True, None


async def run() -> None:
    args = parse_args()
    args.initial_balance = None
    counts = Counts()
    started_at = time.monotonic()
    completed_contracts = 0
    completed_seen: set[tuple[str, str]] = set()
    expired_seen: set[tuple[str, str]] = set()

    append_log(
        f"START kalshi_trader live={args.live} contracts={args.contracts} "
        f"ask_band={args.band_low:.2f}<p<{args.band_high:.2f} spot_agreement=required "
        f"entry_seconds={args.entry_seconds:g} "
        f"retry_attempts={args.retry_attempts} stop_loss={trader.fmt_money(args.stop_loss)}"
    )
    if await log_balance_with_stop_loss_baseline("START", None, args) is None and args.stop_loss > 0:
        append_log("STOP_LOSS baseline unavailable until first successful balance read")

    runtime: ContractRuntime | None = None

    try:
        while True:
            if args.max_seconds and time.monotonic() - started_at >= args.max_seconds:
                append_log(f"STOP max_seconds={args.max_seconds:g} reached")
                return

            outcome_done, runtime = await maybe_record_runtime_outcome(runtime, counts, args, completed_seen)
            if outcome_done:
                completed_contracts += 1
                if args.max_contracts and completed_contracts >= args.max_contracts:
                    append_log(f"STOP max_contracts={args.max_contracts:g} reached")
                    return
                await asyncio.sleep(max(0.1, args.poll_interval))
                continue

            state = await fetch_state_polling()
            if state is None:
                await asyncio.sleep(max(0.1, args.poll_interval))
                continue

            kalshi_market, kalshi_snapshot, source_snapshot, orderbook = state
            ticker = str(kalshi_snapshot.get("ticker") or kalshi_market.get("ticker") or "")
            close_time = str(kalshi_snapshot.get("close_time") or kalshi_market.get("close_time") or "")
            remaining = trader.seconds_to_expiry(kalshi_snapshot)
            key = contract_key(ticker, close_time)

            if key in completed_seen:
                await asyncio.sleep(max(0.1, args.poll_interval))
                continue

            if (runtime is None or runtime.ticker != ticker) and remaining is not None and remaining < 0:
                if key not in expired_seen:
                    expired_seen.add(key)
                    append_log(f"SKIP expired contract {ticker} close={close_time} T={remaining:.1f}s")
                trader.KALSHI_MARKET_CACHE.pop(trader.SERIES_TICKER, None)
                await asyncio.sleep(max(0.1, args.poll_interval))
                continue

            if runtime is None or runtime.ticker != ticker:
                runtime = ContractRuntime(ticker=ticker, close_time=close_time, market=kalshi_market)
                append_log("", prefix_timestamp=False)
                append_log(
                    f"CONTRACT {ticker} | close {close_time} | K target "
                    f"{trader.fmt_price(source_snapshot.get('kalshi_target'), 2)}",
                    prefix_timestamp=False,
                )

            runtime.history.append(
                {
                    "timestamp_utc": trader.iso_utc(),
                    "kalshi_price": source_snapshot.get("kalshi_price"),
                    "kalshi_target": source_snapshot.get("kalshi_target"),
                }
            )

            now_monotonic = time.monotonic()
            should_log_status = (
                remaining is not None
                and (
                    args.log_interval <= 0
                    or runtime.last_status_log_at <= 0
                    or now_monotonic - runtime.last_status_log_at >= args.log_interval
                )
            )
            if remaining is not None and should_log_status:
                book_reasons = orderbook_invalid_reasons(orderbook)
                status_suffix = ""
                if book_reasons:
                    status_suffix = f" orderbook=INVALID {'; '.join(book_reasons)}"
                append_log(
                    f"STATUS T={remaining:.1f}s | K {trader.fmt_price(source_snapshot.get('kalshi_price'), 2)} "
                    f"{trader.fmt_price_delta(source_snapshot.get('kalshi_price'), source_snapshot.get('kalshi_target'), 2)} "
                    f"| yes_mid={trader.fmt_price(kalshi_snapshot.get('yes_mid'), 4)} "
                    f"trade={runtime.decision.status if runtime.decision else '--'}{status_suffix}",
                    prefix_timestamp=False,
                )
                runtime.last_status_log_at = now_monotonic

            if (
                remaining is not None
                and 0 <= remaining <= args.entry_seconds
                and not runtime.decision_logged
            ):
                runtime.decision_logged = True
                await evaluate_entry(runtime, counts, args, remaining)

            await asyncio.sleep(max(0.1, args.poll_interval))
    except StopLossTriggered as exc:
        append_log(f"STOP_LOSS {exc}")
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kalshi BTC 15m ask-band trader with spot-agreement filter.")
    parser.add_argument("--live", action="store_true", help="Submit real Kalshi orders. Omit for dry-run logging.")
    parser.add_argument("--contracts", type=int, default=DEFAULT_CONTRACTS, help="Contracts to buy. Default: 2.")
    parser.add_argument("--band-low", type=float, default=DEFAULT_BAND_LOW, help="Exclusive selected-ask lower bound. Default: 0.50.")
    parser.add_argument("--band-high", type=float, default=DEFAULT_BAND_HIGH, help="Exclusive selected-ask upper bound. Default: 0.78.")
    parser.add_argument("--entry-seconds", type=float, default=DEFAULT_ENTRY_SECONDS, help="Evaluate once when T <= this many seconds. Default: 630.")
    parser.add_argument("--retry-attempts", type=int, default=DEFAULT_RETRY_ATTEMPTS, help="Max best-price liquidity/order retries. Default: 10.")
    parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY_SECONDS, help="Seconds between retries. Default: 0.5.")
    parser.add_argument("--time-in-force", default="fill_or_kill", help="Kalshi order time_in_force. Default: fill_or_kill.")
    parser.add_argument("--stop-loss", type=float, default=10.0, help="Stop if Kalshi balance drops this many dollars below initial balance. Default: 10.")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between HTTP polls. Default: 2.")
    parser.add_argument("--log-interval", type=float, default=60.0, help="Seconds between recurring STATUS log lines. Use 0 to log every poll. Default: 60.")
    parser.add_argument("--outcome-delay-seconds", type=float, default=DEFAULT_OUTCOME_DELAY_SECONDS, help="Evaluate outcome this many seconds relative to close. Default: -2.")
    parser.add_argument("--max-contracts", type=int, default=0, help="Stop after this many contract outcomes. 0 means run forever.")
    parser.add_argument("--max-seconds", type=float, default=0.0, help="Stop after this many wall-clock seconds. 0 means no limit.")
    args = parser.parse_args()
    args.contracts = max(1, args.contracts)
    args.retry_attempts = max(1, args.retry_attempts)
    args.poll_interval = max(0.1, args.poll_interval)
    args.log_interval = max(0.0, args.log_interval)
    if args.band_high <= args.band_low:
        parser.error("--band-high must be greater than --band-low")
    return args


if __name__ == "__main__":
    asyncio.run(run())
