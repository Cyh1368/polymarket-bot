#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cli_trader_v2 as base


ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class CoinConfig:
    symbol: str
    kalshi_series: str
    polymarket_symbol: str
    polymarket_slug_prefix: str
    polymarket_searches: tuple[str, ...]
    aliases: tuple[str, ...]
    kraken_pair: str
    price_min: float
    price_max: float
    csv_dir: str
    kalshi_source_label: str


COINS: dict[str, CoinConfig] = {
    "BTC": CoinConfig(
        symbol="BTC",
        kalshi_series="KXBTC15M",
        polymarket_symbol="btc/usd",
        polymarket_slug_prefix="btc",
        polymarket_searches=("Bitcoin Up or Down 15m", "BTC Up or Down 15m", "Bitcoin 15 minutes"),
        aliases=("btc", "bitcoin"),
        kraken_pair="XBTUSD",
        price_min=1_000,
        price_max=1_000_000,
        csv_dir="data_BTC",
        kalshi_source_label="BRTI",
    ),
    "ETH": CoinConfig(
        symbol="ETH",
        kalshi_series="KXETH15M",
        polymarket_symbol="eth/usd",
        polymarket_slug_prefix="eth",
        polymarket_searches=("Ethereum Up or Down 15m", "ETH Up or Down 15m", "Ethereum 15 minutes"),
        aliases=("eth", "ethereum"),
        kraken_pair="ETHUSD",
        price_min=100,
        price_max=100_000,
        csv_dir="data_ETH",
        kalshi_source_label="ETH Kalshi source",
    ),
    "SOL": CoinConfig(
        symbol="SOL",
        kalshi_series="KXSOL15M",
        polymarket_symbol="sol/usd",
        polymarket_slug_prefix="sol",
        polymarket_searches=("Solana Up or Down 15m", "SOL Up or Down 15m", "Solana 15 minutes"),
        aliases=("sol", "solana"),
        kraken_pair="SOLUSD",
        price_min=1,
        price_max=10_000,
        csv_dir="data_SOL",
        kalshi_source_label="SOL Kalshi source",
    ),
    "XRP": CoinConfig(
        symbol="XRP",
        kalshi_series="KXXRP15M",
        polymarket_symbol="xrp/usd",
        polymarket_slug_prefix="xrp",
        polymarket_searches=("XRP Up or Down 15m", "Ripple Up or Down 15m", "XRP 15 minutes"),
        aliases=("xrp", "ripple"),
        kraken_pair="XRPUSD",
        price_min=0.01,
        price_max=100,
        csv_dir="data_XRP",
        kalshi_source_label="XRP Kalshi source",
    ),
    "HYPE": CoinConfig(
        symbol="HYPE",
        kalshi_series="KXHYPE15M",
        polymarket_symbol="hype/usd",
        polymarket_slug_prefix="hype",
        polymarket_searches=("HYPE Up or Down 15m", "Hyperliquid Up or Down 15m", "HYPE 15 minutes"),
        aliases=("hype", "hyperliquid"),
        kraken_pair="HYPEUSD",
        price_min=0.01,
        price_max=1_000,
        csv_dir="data_HYPE",
        kalshi_source_label="HYPE Kalshi source",
    ),
    "DOGE": CoinConfig(
        symbol="DOGE",
        kalshi_series="KXDOGE15M",
        polymarket_symbol="doge/usd",
        polymarket_slug_prefix="doge",
        polymarket_searches=("Dogecoin Up or Down 15m", "DOGE Up or Down 15m", "Dogecoin 15 minutes"),
        aliases=("doge", "dogecoin"),
        kraken_pair="DOGEUSD",
        price_min=0.001,
        price_max=10,
        csv_dir="data_DOGE",
        kalshi_source_label="DOGE Kalshi source",
    ),
    "BNB": CoinConfig(
        symbol="BNB",
        kalshi_series="KXBNB15M",
        polymarket_symbol="bnb/usd",
        polymarket_slug_prefix="bnb",
        polymarket_searches=("BNB Up or Down 15m", "Binance Coin Up or Down 15m", "BNB 15 minutes"),
        aliases=("bnb", "binance"),
        kraken_pair="BNBUSD",
        price_min=1,
        price_max=10_000,
        csv_dir="data_BNB",
        kalshi_source_label="BNB Kalshi source",
    ),
}


CRITICAL_FIELDS = [
    "timestamp_utc",
    "kalshi_ticker",
    "kalshi_close_time",
    "kalshi_yes_bid",
    "kalshi_yes_ask",
    "kalshi_no_bid",
    "kalshi_no_ask",
    "polymarket_ticker",
    "polymarket_close_time",
    "polymarket_yes_bid",
    "polymarket_yes_ask",
    "polymarket_no_bid",
    "polymarket_no_ask",
    "kalshi_btc_price",
    "kalshi_btc_target",
    "polymarket_btc_price",
    "k_plus_np",
    "nk_plus_p",
]


def blank(value: Any) -> bool:
    if value in (None, ""):
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def configure_base(config: CoinConfig) -> None:
    base.SERIES_TICKER = config.kalshi_series
    base.POLYMARKET_RTDS_SYMBOL = config.polymarket_symbol
    base.POLYMARKET_SEARCH_QUERY = config.polymarket_searches[0]
    base.KRAKEN_PAIR = config.kraken_pair
    base.DEFAULT_CSV_DIR = ROOT / config.csv_dir

    def plausible_price(value: float | None) -> float | None:
        if value is None:
            return None
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        if config.price_min <= price <= config.price_max:
            return price
        return None

    base.plausible_btc_price = plausible_price
    base.discover_polymarket_market = lambda kalshi_market: discover_polymarket_market(config, kalshi_market)
    base.source_price_snapshot = lambda kalshi_market, polymarket_market, rtds_snapshot=None: source_price_snapshot(
        config,
        kalshi_market,
        polymarket_market,
        rtds_snapshot,
    )

    base.KALSHI_MARKET_CACHE.clear()
    base.POLYMARKET_MARKET_CACHE.clear()
    base.POLYMARKET_TARGET_CACHE.clear()
    base.SOURCE_PRICE_CACHE.clear()
    base.SOURCE_HISTORY.clear()


def coin_text_matches(config: CoinConfig, market: dict[str, Any]) -> bool:
    text = base.market_text(market)
    return any(alias in text for alias in config.aliases)


def discover_polymarket_market(config: CoinConfig, kalshi_market: dict[str, Any]) -> dict[str, Any] | None:
    if base.POLYMARKET_MARKET_SLUG:
        event = base.gamma_event_by_slug(base.POLYMARKET_MARKET_SLUG)
        if event:
            return base.polymarket_event_market(event)
        data = base.gamma_get("/markets", {"slug": base.POLYMARKET_MARKET_SLUG})
        if isinstance(data, list) and data:
            return data[0]
        return None

    close_ts = base.parse_ts(kalshi_market.get("close_time") or kalshi_market.get("close_ts"))
    open_ts = base.parse_ts(kalshi_market.get("open_time"))
    slug_epochs: list[int] = []
    for value in (open_ts, close_ts - base.CONTRACT_SECONDS if close_ts else 0, close_ts):
        if value:
            slug_epochs.append(int(value))
    for epoch in dict.fromkeys(slug_epochs):
        event = base.gamma_event_by_slug(f"{config.polymarket_slug_prefix}-updown-15m-{epoch}")
        if event:
            return base.polymarket_event_market(event)

    candidates: list[dict[str, Any]] = []
    for query in config.polymarket_searches:
        data = base.polymarket_get("/search", {"query": query, "limit": 30})
        for event in data.get("events", []):
            for market in event.get("markets", []):
                if market.get("active") is False or market.get("closed") is True:
                    continue
                enriched = dict(market)
                enriched.setdefault("event_ticker", event.get("ticker") or event.get("slug"))
                enriched.setdefault("event_title", event.get("title"))
                enriched.setdefault("event_slug", event.get("slug"))
                enriched.setdefault("event_start_time", event.get("startTime"))
                enriched.setdefault("event_end_time", event.get("endDate"))
                enriched.setdefault("event_metadata", event.get("eventMetadata"))
                candidates.append(enriched)

    if close_ts:
        window = 30 * 60
        data = base.polymarket_get(
            "/markets",
            {
                "active": "true",
                "closed": "false",
                "limit": 100,
                "endDateMin": base.datetime.fromtimestamp(close_ts - window, tz=base.timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "endDateMax": base.datetime.fromtimestamp(close_ts + window, tz=base.timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            },
        )
        candidates.extend(data.get("markets", []))

    unique = {str(market.get("slug") or market.get("id")): market for market in candidates if market.get("slug") or market.get("id")}
    filtered = [
        market
        for market in unique.values()
        if coin_text_matches(config, market)
        and ("up" in base.market_text(market) or "above" in base.market_text(market) or "higher" in base.market_text(market))
    ]
    if not filtered:
        return None
    filtered.sort(key=lambda market: base.interval_overlap_score(market, kalshi_market), reverse=True)
    return filtered[0]


def source_price_snapshot(
    config: CoinConfig,
    kalshi_market: dict[str, Any],
    polymarket_market: dict[str, Any] | None,
    rtds_snapshot: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    timestamp_utc = base.iso_utc()
    if config.symbol == "BTC":
        market_price = base.fetch_brti_price() or base.plausible_btc_price(
            base.numeric_value(kalshi_market.get("expiration_value"))
        )
    else:
        market_price = base.plausible_btc_price(base.numeric_value(kalshi_market.get("expiration_value")))
    kraken_price = base.fetch_kraken_price()
    kalshi_current = market_price or kraken_price
    if kalshi_current is not None:
        base.cached_source_price(f"{config.symbol.lower()}_kalshi_source", kalshi_current)

    snapshot = rtds_snapshot if rtds_snapshot is not None else base.fetch_polymarket_rtds_snapshot()
    polymarket_current = (
        base.polymarket_rtds_latest_price(snapshot)
        or base.plausible_btc_price(base.event_metadata_value(polymarket_market, "currentPrice", "current_price"))
        or kraken_price
        or base.plausible_btc_price(base.event_metadata_value(polymarket_market, "finalPrice", "final_price"))
    )

    target_key = base.polymarket_market_key(polymarket_market)
    polymarket_target = base.POLYMARKET_TARGET_CACHE.get(target_key) if target_key else None
    if polymarket_target is None:
        polymarket_target = base.polymarket_rtds_price_at_from_snapshot(
            snapshot,
            base.polymarket_start_timestamp(polymarket_market),
        )
        if polymarket_target is not None and target_key:
            base.POLYMARKET_TARGET_CACHE[target_key] = polymarket_target

    kalshi_60_sma, kalshi_60_sma_count = base.kalshi_brti_60_sma(kalshi_current)
    out = {
        "timestamp_utc": timestamp_utc,
        "kalshi_price": kalshi_current,
        "kalshi_target": base.numeric_value(kalshi_market.get("floor_strike")),
        "kalshi_60_sma": kalshi_60_sma,
        "kalshi_60_sma_sample_count": kalshi_60_sma_count,
        "polymarket_price": polymarket_current,
        "polymarket_target": polymarket_target,
    }
    base.SOURCE_HISTORY.append(out)
    return out


def build_data_row(config: CoinConfig, state: base.MarketState) -> dict[str, Any]:
    kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot = state
    runtime = base.ContractRuntime(
        ticker=str(kalshi_market.get("ticker") or ""),
        close_time=str(kalshi_snapshot.get("close_time") or kalshi_market.get("close_time") or ""),
    )
    row = base.build_csv_row(
        kalshi_snapshot=kalshi_snapshot,
        polymarket_snapshot=polymarket_snapshot,
        source_snapshot=source_snapshot,
        runtime=runtime,
        profit_margin=0.0,
        contracts=1,
        active_position=None,
        polymarket_error="",
    )
    row["kalshi_btc_source"] = config.kalshi_source_label
    row["polymarket_btc_source"] = f"{config.symbol} Polymarket RTDS"
    row["tradable"] = 0
    row["active_trade"] = 0
    return row


def validation_summary(row: dict[str, Any]) -> dict[str, Any]:
    missing_columns = [field for field in base.CSV_FIELDS if field not in row]
    missing_critical_values = [field for field in CRITICAL_FIELDS if blank(row.get(field))]
    return {
        "column_count": len(base.CSV_FIELDS),
        "missing_columns": missing_columns,
        "missing_critical_values": missing_critical_values,
    }


def collect_one(config: CoinConfig, csv_dir: Path, dry_run: bool) -> dict[str, Any]:
    configure_base(config)
    state = base.fetch_market_state()
    row = build_data_row(config, state)
    summary = validation_summary(row)
    summary.update(
        {
            "coin": config.symbol,
            "kalshi_ticker": row.get("kalshi_ticker"),
            "polymarket_ticker": row.get("polymarket_ticker"),
            "kalshi_price": row.get("kalshi_btc_price"),
            "polymarket_price": row.get("polymarket_btc_price"),
            "kalshi_target": row.get("kalshi_btc_target"),
            "polymarket_target": row.get("polymarket_btc_target"),
            "k_plus_np": row.get("k_plus_np"),
            "nk_plus_p": row.get("nk_plus_p"),
            "dry_run": dry_run,
        }
    )
    if not dry_run:
        csv_path = base.csv_path_for_contract(csv_dir, row.get("kalshi_ticker"))
        base.append_csv_row(csv_path, row)
        summary["csv_path"] = str(csv_path)
    return summary


async def _fetch_and_write_settlement(ticker: str, csv_dir: Path, delay_seconds: float) -> None:
    """Wait for settlement then append a final row with the official status."""
    await asyncio.sleep(delay_seconds)
    try:
        data = base.kalshi_get(f"/markets/{ticker}")
        market = data.get("market") if isinstance(data, dict) else None
        if not isinstance(market, dict):
            market = data if isinstance(data, dict) else {}
        status = str(market.get("status") or "")
        result = str(market.get("result") or "")
        settlement_value = market.get("settlement_value_dollars")
        expiration_value = market.get("expiration_value")
        floor_strike = market.get("floor_strike")
        settlement_ts = market.get("settlement_ts") or ""
        # Build a minimal settlement row — all trading/book fields are blank.
        row: dict[str, Any] = {field: "" for field in base.CSV_FIELDS}
        row.update(
            {
                "timestamp_utc": base.iso_utc(),
                "kalshi_timestamp_utc": base.iso_utc(),
                "kalshi_ticker": ticker,
                "kalshi_status": status,
                "kalshi_btc_target": floor_strike if floor_strike is not None else "",
                "kalshi_btc_price": expiration_value if expiration_value is not None else "",
            }
        )
        csv_path = base.csv_path_for_contract(csv_dir, ticker)
        base.append_csv_row(csv_path, row)
        print(
            f"{base.iso_utc()} | SETTLEMENT {ticker} status={status} result={result} "
            f"settlement_value={settlement_value} expiration_value={expiration_value} "
            f"floor_strike={floor_strike} settlement_ts={settlement_ts}"
        )
    except Exception as exc:
        print(f"{base.iso_utc()} | SETTLEMENT FETCH ERROR {ticker}: {type(exc).__name__}: {exc}")


async def run_collector(config: CoinConfig, csv_dir: Path, csv_save_interval: float, max_seconds: float | None, settlement_delay_seconds: float = 300.0) -> None:
    configure_base(config)
    csv_dir.mkdir(parents=True, exist_ok=True)
    print(f"{base.iso_utc()} | START cli_data_{config.symbol} series={config.kalshi_series} csv_dir={csv_dir} interval={csv_save_interval}s settlement_delay={settlement_delay_seconds}s")
    ctx = base.AsyncMarketContext(base.fetch_market_state, logger=print)
    await ctx.start()
    started = time.monotonic()
    current_runtime: base.ContractRuntime | None = None
    settlement_tasks: list[asyncio.Task[None]] = []
    try:
        while True:
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                print(f"{base.iso_utc()} | STOP cli_data_{config.symbol} max_seconds={max_seconds}")
                return
            kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot = await ctx.wait_for_update(
                timeout=min(0.5, max(0.1, csv_save_interval))
            )
            ticker = str(kalshi_market.get("ticker") or kalshi_snapshot.get("ticker") or "")
            close_time = str(kalshi_snapshot.get("close_time") or kalshi_market.get("close_time") or "")
            remaining = base.seconds_to_expiry(kalshi_snapshot)
            if remaining is not None and remaining <= -2:
                print(f"{base.iso_utc()} | CONTRACT ROLLOVER {ticker} expired; refreshing active market")
                if ticker:
                    task = asyncio.create_task(
                        _fetch_and_write_settlement(ticker, csv_dir, settlement_delay_seconds)
                    )
                    settlement_tasks.append(task)
                base.KALSHI_MARKET_CACHE.pop(base.SERIES_TICKER, None)
                base.POLYMARKET_MARKET_CACHE.clear()
                await ctx.stop()
                ctx = base.AsyncMarketContext(base.fetch_market_state, logger=print)
                await ctx.start()
                current_runtime = None
                continue
            if current_runtime is None or current_runtime.ticker != ticker:
                current_runtime = base.ContractRuntime(ticker=ticker, close_time=close_time)
                print(
                    f"{base.iso_utc()} | CONTRACT {ticker} | close {close_time} | "
                    f"Polymarket {polymarket_snapshot.get('ticker')} | "
                    f"K target {base.fmt_money(source_snapshot.get('kalshi_target'))} | "
                    f"P target {base.fmt_money(source_snapshot.get('polymarket_target'))}"
                )
            now = time.monotonic()
            if now - current_runtime.last_csv_save_at >= csv_save_interval:
                state = (kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot)
                row = build_data_row(config, state)
                csv_path = base.csv_path_for_contract(csv_dir, ticker)
                base.append_csv_row(csv_path, row)
                current_runtime.last_csv_save_at = now
                current_runtime.history.append(row)
            # Prune completed settlement tasks.
            settlement_tasks = [t for t in settlement_tasks if not t.done()]
            await asyncio.sleep(0.1)
    finally:
        await ctx.stop()
        # Wait for any in-flight settlement fetches so they aren't dropped on clean exit.
        if settlement_tasks:
            await asyncio.gather(*settlement_tasks, return_exceptions=True)


def parser_for(config: CoinConfig) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Collect {config.symbol} 15-minute Kalshi/Polymarket data without trading.")
    parser.add_argument("--csv-save-interval", type=float, default=2.0, help="Seconds between CSV rows. Default: 2.")
    parser.add_argument("--csv-dir", type=Path, default=ROOT / config.csv_dir, help=f"Output directory. Default: {config.csv_dir}.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch one row, validate it, print a JSON summary, and do not write CSV.")
    parser.add_argument("--once", action="store_true", help="Fetch one row, write it unless --dry-run is set, then exit.")
    parser.add_argument("--max-seconds", type=float, default=None, help="Stop after this many seconds.")
    return parser


def main_for_coin(symbol: str) -> None:
    config = COINS[symbol.upper()]
    parser = parser_for(config)
    args = parser.parse_args()
    if args.dry_run or args.once:
        summary = collect_one(config, args.csv_dir, dry_run=bool(args.dry_run))
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    asyncio.run(run_collector(config, args.csv_dir, args.csv_save_interval, args.max_seconds))
