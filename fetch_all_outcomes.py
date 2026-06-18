#!/usr/bin/env python3
"""Fetch settlement outcomes for Kalshi and Polymarket contracts,
and backfill missing price_target values in Polymarket 5m CSVs via Kraken 1m OHLCV.

Outputs
-------
  kalshi/<coin>_official_market_results.json         (one per coin)
  polymarket/polymarket_<coin>_5m_official_outcomes.csv  (one per coin)
  polymarket/data_<COIN>_5m/*.csv                   (price_target column filled in-place)

Usage
-----
  python fetch_all_outcomes.py
  python fetch_all_outcomes.py --coins BTC,ETH
  python fetch_all_outcomes.py --skip-kalshi --skip-pm-outcomes
  python fetch_all_outcomes.py --skip-target-fill
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

ALL_COINS = ["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"]

KALSHI_API_BASE = "https://external-api.kalshi.com/trade-api/v2/markets"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"

KRAKEN_PAIRS = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "XRP": "XRPUSD",
    "HYPE": "HYPEUSD",
    "DOGE": "DOGEUSD",
    "BNB": "BNBUSD",
}

KALSHI_CHUNK = 50

POLYMARKET_CSV_FIELDS = [
    "fetched_at_utc", "market_slug", "market_id", "condition_id", "question",
    "event_start_utc", "event_end_utc", "event_closed", "market_closed", "market_active",
    "uma_resolution_status", "resolution_source", "outcomes", "outcome_prices",
    "winning_outcome", "up_won", "down_won", "official_outcome_source",
    "up_token_id", "down_token_id", "volume", "liquidity", "gamma_updated_at", "error",
]


# ── helpers ────────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def http_get(url: str, timeout: float = 30) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": "fetch-outcomes/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            p = json.loads(value)
            return p if isinstance(p, list) else []
        except json.JSONDecodeError:
            return []
    return []


def ts_to_5m_ts(dt_str: str) -> int | None:
    """Parse ISO8601 UTC string to floor-5-minute unix timestamp (Polymarket contract bucket)."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        ts = int(dt.timestamp())
        return (ts // 300) * 300
    except Exception:
        return None


# ── Kalshi outcomes ────────────────────────────────────────────────────────

def _kalshi_result(market: dict) -> str:
    r = str(market.get("result") or "").strip().lower()
    if r in {"yes", "y", "1", "true"}:
        return "YES"
    if r in {"no", "n", "0", "false"}:
        return "NO"
    sv = str(market.get("settlement_value_dollars") or "")
    try:
        v = float(sv)
        if v == 1.0:
            return "YES"
        if v == 0.0:
            return "NO"
    except ValueError:
        pass
    return ""


def fetch_kalshi_outcomes(coin: str) -> None:
    data_dir = ROOT / "kalshi" / f"data_{coin}"
    if not data_dir.exists():
        print(f"  {coin}: kalshi data dir not found, skipping")
        return

    tickers: set[str] = set()
    for path in sorted(data_dir.glob("*.csv")):
        try:
            with path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    t = (row.get("kalshi_ticker") or "").strip()
                    if t:
                        tickers.add(t)
        except Exception:
            pass

    tickers_sorted = sorted(tickers)
    if not tickers_sorted:
        print(f"  {coin}: no kalshi tickers found")
        return

    results: dict[str, Any] = {}
    for i in range(0, len(tickers_sorted), KALSHI_CHUNK):
        chunk = tickers_sorted[i : i + KALSHI_CHUNK]
        query = urllib.parse.urlencode({"tickers": ",".join(chunk), "limit": len(chunk)})
        try:
            data = http_get(f"{KALSHI_API_BASE}?{query}")
            for m in data.get("markets", []):
                ticker = m.get("ticker", "")
                if ticker:
                    results[ticker] = {
                        "result": _kalshi_result(m),
                        "status": m.get("status", ""),
                        "settlement_value_dollars": m.get("settlement_value_dollars", ""),
                        "expiration_value": m.get("expiration_value", ""),
                        "floor_strike": m.get("floor_strike", ""),
                        "settlement_ts": m.get("settlement_ts", ""),
                    }
        except Exception as exc:
            print(f"  {coin} kalshi chunk {i}: ERROR {exc}")
        if i + KALSHI_CHUNK < len(tickers_sorted):
            time.sleep(0.5)

    out_path = ROOT / "kalshi" / f"{coin.lower()}_official_market_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    settled = sum(1 for v in results.values() if v["result"] in {"YES", "NO"})
    unfetched = len(tickers_sorted) - len(results)
    unfetched_note = f"  ({unfetched} tickers not returned by API)" if unfetched else ""
    print(f"  {coin}: {len(results)} fetched, {settled} settled → {out_path}{unfetched_note}")


# ── Polymarket outcomes ────────────────────────────────────────────────────

def _infer_winner(market: dict) -> tuple[str, str, str, str]:
    outcomes = [str(o) for o in parse_json_list(market.get("outcomes"))]
    prices = [finite_float(p) for p in parse_json_list(market.get("outcomePrices"))]
    resolution = str(market.get("resolution") or market.get("resolved_to") or "").strip()
    winner = str(market.get("winner") or "").strip()

    if resolution:
        return (
            resolution,
            str(int(resolution.lower() == "up")),
            str(int(resolution.lower() == "down")),
            "gamma.resolution",
        )
    if winner:
        if winner.isdigit() and outcomes:
            idx = int(winner)
            if 0 <= idx < len(outcomes):
                wo = outcomes[idx]
                return wo, str(int(wo.lower() == "up")), str(int(wo.lower() == "down")), "gamma.winner_index"
        return winner, str(int(winner.lower() == "up")), str(int(winner.lower() == "down")), "gamma.winner"

    if outcomes and len(prices) >= len(outcomes):
        resolved = [(o, p) for o, p in zip(outcomes, prices) if p is not None]
        ones = [(o, p) for o, p in resolved if p >= 0.999]
        zeros = [(o, p) for o, p in resolved if p <= 0.001]
        if len(ones) == 1 and len(zeros) >= len(outcomes) - 1:
            wo = ones[0][0]
            return wo, str(int(wo.lower() == "up")), str(int(wo.lower() == "down")), "gamma.outcomePrices_final"
        highs = [(o, p) for o, p in resolved if p > 0.9]
        lows = [(o, p) for o, p in resolved if p < 0.1]
        if len(highs) == 1 and len(lows) >= len(outcomes) - 1:
            wo = highs[0][0]
            return (
                wo,
                str(int(wo.lower() == "up")),
                str(int(wo.lower() == "down")),
                "gamma.outcomePrices_near_final",
            )
    return "", "", "", ""


def _row_for_slug(slug: str, timeout: float = 20.0, retries: int = 2) -> dict:
    fetched_at = utc_now()
    last_error = ""
    for attempt in range(retries + 1):
        try:
            event = http_get(f"{GAMMA_BASE_URL}/events/slug/{slug}", timeout=timeout)
            market = (event.get("markets") or [{}])[0]
            outcomes = parse_json_list(market.get("outcomes"))
            prices = parse_json_list(market.get("outcomePrices"))
            token_ids = [str(t) for t in parse_json_list(market.get("clobTokenIds"))]
            wo, up_won, down_won, source = _infer_winner(market)
            return {
                "fetched_at_utc": fetched_at,
                "market_slug": slug,
                "market_id": str(market.get("id") or ""),
                "condition_id": str(market.get("conditionId") or ""),
                "question": str(event.get("title") or market.get("question") or ""),
                "event_start_utc": str(
                    market.get("eventStartTime") or event.get("startTime") or event.get("startDate") or ""
                ),
                "event_end_utc": str(market.get("endDate") or event.get("endDate") or ""),
                "event_closed": str(event.get("closed")),
                "market_closed": str(market.get("closed")),
                "market_active": str(market.get("active")),
                "uma_resolution_status": str(market.get("umaResolutionStatus") or ""),
                "resolution_source": str(
                    market.get("resolutionSource") or event.get("resolutionSource") or ""
                ),
                "outcomes": json.dumps(outcomes, separators=(",", ":")),
                "outcome_prices": json.dumps(prices, separators=(",", ":")),
                "winning_outcome": wo,
                "up_won": up_won,
                "down_won": down_won,
                "official_outcome_source": source,
                "up_token_id": token_ids[0] if len(token_ids) >= 1 else "",
                "down_token_id": token_ids[1] if len(token_ids) >= 2 else "",
                "volume": str(
                    market.get("volumeNum") or market.get("volume") or event.get("volume") or ""
                ),
                "liquidity": str(market.get("liquidityNum") or market.get("liquidity") or ""),
                "gamma_updated_at": str(market.get("updatedAt") or event.get("updatedAt") or ""),
                "error": "",
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(1.0)

    blank = {f: "" for f in POLYMARKET_CSV_FIELDS}
    blank.update({"fetched_at_utc": fetched_at, "market_slug": slug, "error": last_error})
    return blank


def _discover_pm_slugs(data_dir: Path, coin: str) -> list[str]:
    slugs: set[str] = set()
    for path in sorted(data_dir.glob(f"polymarket_data_{coin}_5m_*.csv")):
        try:
            with path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    slug = (row.get("market_slug") or "").strip()
                    if slug:
                        slugs.add(slug)
                        break
        except Exception:
            pass
    return sorted(
        slugs,
        key=lambda s: int(s.rsplit("-", 1)[-1]) if s.rsplit("-", 1)[-1].isdigit() else s,
    )


def fetch_polymarket_outcomes(coin: str) -> None:
    data_dir = ROOT / "polymarket" / f"data_{coin}_5m"
    if not data_dir.exists():
        print(f"  {coin}: polymarket data dir not found, skipping")
        return

    slugs = _discover_pm_slugs(data_dir, coin)
    if not slugs:
        print(f"  {coin}: no slugs found")
        return

    rows = []
    for i, slug in enumerate(slugs):
        rows.append(_row_for_slug(slug))
        if (i + 1) % 50 == 0:
            print(f"  {coin}: {i + 1}/{len(slugs)} slugs fetched...")
        time.sleep(0.2)

    out_path = ROOT / "polymarket" / f"polymarket_{coin.lower()}_5m_official_outcomes.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=POLYMARKET_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    resolved = sum(1 for r in rows if r.get("winning_outcome"))
    errors = sum(1 for r in rows if r.get("error"))
    up_wins = sum(1 for r in rows if r.get("up_won") == "1")
    print(
        f"  {coin}: {len(rows)} slugs, {resolved} resolved (up={up_wins}), "
        f"{errors} errors → {out_path}"
    )


# ── Kraken OHLCV price-target backfill ────────────────────────────────────

def _fetch_kraken_range(pair: str, start_ts: int, end_ts: int) -> dict[int, float]:
    """Returns 5m_bucket_ts → open_price for the full [start_ts, end_ts] range.

    Uses interval=5 (5-minute candles) so bucket timestamps align exactly with
    Polymarket 5m contract start times (always on 5-minute UTC boundaries).
    720 candles × 5 minutes = 60 hours of history per API call.
    """
    candle_map: dict[int, float] = {}
    since = max(0, (start_ts // 300) * 300 - 300)
    while since <= end_ts:
        try:
            url = f"{KRAKEN_OHLC_URL}?pair={pair}&interval=5&since={since}"
            data = http_get(url)
            errors = data.get("error") or []
            if errors:
                print(f"    Kraken API error for {pair}: {errors}")
                break
            result = data.get("result", {})
            pair_key = next((k for k in result if k != "last"), None)
            if not pair_key:
                break
            candles = result[pair_key]
            if not candles:
                break
            for candle in candles:
                bucket_ts = int(candle[0])
                open_price = finite_float(candle[1])
                if open_price is not None:
                    candle_map[bucket_ts] = open_price
            last_ts = int(result.get("last", candles[-1][0]))
            if last_ts <= since:
                break
            since = last_ts
            if since > end_ts:
                break
            time.sleep(0.5)
        except Exception as exc:
            print(f"    Kraken OHLCV error for {pair} since={since}: {exc}")
            break
    return candle_map


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return fieldnames, rows


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def backfill_pm_price_target(coin: str) -> None:
    data_dir = ROOT / "polymarket" / f"data_{coin}_5m"
    if not data_dir.exists():
        return

    files = sorted(data_dir.glob(f"polymarket_data_{coin}_5m_*.csv"))
    if not files:
        return

    # Quick scan: collect all event_start_utc values and check if fill is needed
    start_times: dict[Path, str] = {}  # file → event_start_utc
    needs_fill = False
    for path in files:
        try:
            fieldnames, rows = _read_csv(path)
        except Exception:
            continue
        if "price_target" not in fieldnames:
            continue
        for row in rows:
            s = (row.get("event_start_utc") or "").strip()
            if s:
                start_times[path] = s
                break
        for row in rows:
            if not (row.get("price_target") or "").strip():
                needs_fill = True
                break

    if not needs_fill:
        print(f"  {coin}: price_target already complete, skipping")
        return

    # Compute time range and fetch Kraken candles
    valid_ts = [ts_to_5m_ts(s) for s in start_times.values()]
    valid_ts = [ts for ts in valid_ts if ts is not None]
    kraken_map: dict[int, float] = {}
    pair = KRAKEN_PAIRS.get(coin)
    if pair and valid_ts:
        min_ts, max_ts = min(valid_ts), max(valid_ts)
        span_hours = (max_ts - min_ts) / 3600
        print(
            f"  {coin}: fetching Kraken {pair} OHLCV 5m "
            f"({datetime.fromtimestamp(min_ts, timezone.utc).strftime('%Y-%m-%dT%H:%M')} "
            f"→ {datetime.fromtimestamp(max_ts, timezone.utc).strftime('%Y-%m-%dT%H:%M')}, "
            f"{span_hours:.1f}h)"
        )
        kraken_map = _fetch_kraken_range(pair, min_ts, max_ts + 300)
        print(f"  {coin}: {len(kraken_map)} 1m candles fetched")

    filled_files = 0
    filled_rows = 0
    no_source_files = 0

    for path in files:
        try:
            fieldnames, rows = _read_csv(path)
        except Exception:
            continue
        if "price_target" not in fieldnames:
            continue

        # Determine fill price for this contract
        event_start = start_times.get(path)
        minute_ts = ts_to_5m_ts(event_start) if event_start else None
        kraken_price = kraken_map.get(minute_ts) if minute_ts else None

        # Fallback: earliest spot_price in the file (closest to contract start)
        fallback_price: float | None = None
        fallback_source = ""
        if "spot_price" in fieldnames:
            best_stc = -1.0
            for row in rows:
                sp = finite_float(row.get("spot_price"))
                stc = finite_float(row.get("seconds_to_close"))
                if sp is not None and stc is not None and stc > best_stc:
                    best_stc = stc
                    fallback_price = sp
                    fallback_source = f"csv_first_spot_stc_{stc:.0f}s"

        fill_price = kraken_price if kraken_price else fallback_price
        if fill_price is None:
            no_source_files += 1
            continue
        fill_source = "kraken_ohlcv_5m_open" if kraken_price else fallback_source

        changed = 0
        for row in rows:
            if not (row.get("price_target") or "").strip():
                row["price_target"] = str(fill_price)
                row["price_target_source"] = fill_source
                qf = row.get("quality_flags") or ""
                flags = [f for f in qf.split(";") if f and f != "target_missing"]
                row["quality_flags"] = ";".join(flags)
                changed += 1

        if changed:
            _write_csv(path, fieldnames, rows)
            filled_files += 1
            filled_rows += changed

    print(
        f"  {coin}: price_target filled in {filled_files} files "
        f"({filled_rows} rows), {no_source_files} files had no source"
    )


# ── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch outcomes and backfill price_target for all coins.")
    parser.add_argument("--coins", default=",".join(ALL_COINS), help="Comma-separated coin list")
    parser.add_argument("--skip-kalshi", action="store_true", help="Skip Kalshi outcome fetch")
    parser.add_argument("--skip-pm-outcomes", action="store_true", help="Skip Polymarket outcome fetch")
    parser.add_argument("--skip-target-fill", action="store_true", help="Skip Polymarket price_target backfill")
    args = parser.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]

    if not args.skip_kalshi:
        print("\n=== Kalshi official outcomes ===")
        for coin in coins:
            fetch_kalshi_outcomes(coin)

    if not args.skip_pm_outcomes:
        print("\n=== Polymarket official outcomes ===")
        for coin in coins:
            print(f"  {coin}: discovering slugs...")
            fetch_polymarket_outcomes(coin)

    if not args.skip_target_fill:
        print("\n=== Polymarket price_target backfill (Kraken OHLCV) ===")
        for coin in coins:
            backfill_pm_price_target(coin)

    print("\nDone.")


if __name__ == "__main__":
    main()
