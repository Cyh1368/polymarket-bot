#!/usr/bin/env python3
import base64
import asyncio
import csv
import html
import json
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    pending_key: str | None = None
    pending_value: list[str] = []
    for raw_line in path.read_text().splitlines():
        if pending_key:
            pending_value.append(raw_line)
            if "END " in raw_line and "PRIVATE KEY" in raw_line:
                if pending_key not in os.environ:
                    os.environ[pending_key] = "\n".join(pending_value)
                pending_key = None
                pending_value = []
            continue
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if "BEGIN " in value and "PRIVATE KEY" in value and "END " not in value:
            pending_key = key
            pending_value = [value]
            continue
        if key and key not in os.environ:
            os.environ[key] = value.replace("\\n", "\n")


load_dotenv()

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8090"))
BASE_URL = os.getenv("KALSHI_API_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")
SERIES_TICKER = os.getenv("KALSHI_SERIES_TICKER", "KXBTC15M")
POLYMARKET_BASE_URL = os.getenv("POLYMARKET_API_BASE_URL", "https://gateway.polymarket.us/v1")
POLYMARKET_GAMMA_URL = os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
POLYMARKET_CLOB_URL = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
POLYMARKET_MARKET_SLUG = os.getenv("POLYMARKET_MARKET_SLUG", "").strip()
POLYMARKET_SEARCH_QUERY = os.getenv("POLYMARKET_SEARCH_QUERY", "Bitcoin Up or Down")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "2"))
ORDERBOOK_DEPTH = int(os.getenv("ORDERBOOK_DEPTH", "10"))
ORDER_IMBALANCE_TAUS_RAW = os.getenv("ORDER_IMBALANCE_TAUS", "0.01,0.02,0.05,0.10")
DATA_DIR = Path(os.getenv("KALSHI_DATA_DIR", "kalshi_btc15m_data"))
KRAKEN_API_URL = os.getenv("KRAKEN_API_URL", "https://api.kraken.com")
KRAKEN_PAIR = os.getenv("KRAKEN_PAIR", "XBTUSD")
POLYMARKET_RTDS_URL = os.getenv("POLYMARKET_RTDS_URL", "wss://ws-live-data.polymarket.com")
POLYMARKET_RTDS_SYMBOL = os.getenv("POLYMARKET_RTDS_SYMBOL", "btc/usd")
POLYMARKET_TARGET_MAX_DISTANCE_SECONDS = float(os.getenv("POLYMARKET_TARGET_MAX_DISTANCE_SECONDS", "1"))


def parse_order_imbalance_taus(raw: str) -> tuple[float, ...]:
    values: list[float] = []
    for item in raw.split(","):
        try:
            value = float(item.strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return tuple(dict.fromkeys(values)) or (0.01, 0.02, 0.05, 0.10)


def tau_label(value: float) -> str:
    cents_value = value * 100.0
    if abs(cents_value - round(cents_value)) < 1e-9:
        return f"{int(round(cents_value))}c"
    return str(value).replace(".", "p").replace("-", "m")


ORDER_IMBALANCE_TAUS = parse_order_imbalance_taus(ORDER_IMBALANCE_TAUS_RAW)


def order_imbalance_csv_fields(prefix: str) -> list[str]:
    fields: list[str] = []
    stem = f"{prefix}_" if prefix else ""
    for tau in ORDER_IMBALANCE_TAUS:
        label = tau_label(tau)
        fields.extend(
            [
                f"{stem}yes_bid_depth_tau_{label}",
                f"{stem}yes_ask_depth_tau_{label}",
                f"{stem}yes_book_imbalance_tau_{label}",
                f"{stem}no_bid_depth_tau_{label}",
                f"{stem}no_ask_depth_tau_{label}",
                f"{stem}no_book_imbalance_tau_{label}",
                f"{stem}directional_bid_imbalance_tau_{label}",
                f"{stem}directional_ask_imbalance_tau_{label}",
            ]
        )
    return fields


STATE_LOCK = threading.Lock()
STATE: dict[str, Any] = {
    "series_ticker": SERIES_TICKER,
    "active_ticker": None,
    "active_market": None,
    "orderbook": None,
    "snapshot": None,
    "polymarket_slug": POLYMARKET_MARKET_SLUG or None,
    "polymarket_market": None,
    "polymarket_orderbook": None,
    "polymarket_snapshot": None,
    "history": [],
    "polymarket_history": [],
    "source_history": [],
    "last_fetch_at": None,
    "next_fetch_at": None,
    "error": None,
    "polymarket_error": None,
    "consecutive_errors": 0,
}
POLYMARKET_TARGET_CACHE: dict[str, float] = {}
POLYMARKET_TARGET_FETCH_AT: dict[str, float] = {}
SOURCE_PRICE_CACHE: dict[str, float] = {}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seconds_from_now(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def private_key_pem() -> str | None:
    for key in ("KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PEM"):
        value = os.getenv(key)
        if value:
            possible_path = Path(value).expanduser()
            if possible_path.exists():
                return possible_path.read_text()
            return value
    path_value = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if path_value:
        path = Path(path_value).expanduser()
        if path.exists():
            return path.read_text()
    return None


def auth_headers(method: str, path: str) -> dict[str, str]:
    key_id = (
        os.getenv("KALSHI_API_ID")
        or os.getenv("KALSHI_KEY_ID")
        or os.getenv("KALSHI_API_KEY_ID")
        or os.getenv("KALSHI_ACCESS_KEY")
    )
    pem = private_key_pem()
    if not key_id or not pem:
        return {}
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except Exception:
        return {}

    timestamp = str(int(time.time() * 1000))
    parsed = urlparse(BASE_URL)
    api_path = parsed.path.rstrip("/") + path
    signing_payload = f"{timestamp}{method.upper()}{api_path}".encode()
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    signature = key.sign(
        signing_payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


def request_json(path: str, params: dict[str, Any] | None = None, auth: bool = False) -> dict[str, Any]:
    query = f"?{urlencode(params, doseq=True)}" if params else ""
    url = f"{BASE_URL}{path}{query}"
    headers = {"Accept": "application/json", "User-Agent": "kalshi-btc15m-monitor/1.0"}
    if auth:
        headers.update(auth_headers("GET", path))
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def kalshi_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return request_json(path, params=params, auth=False)
    except HTTPError as exc:
        if exc.code != 401:
            raise
        return request_json(path, params=params, auth=True)


def public_get(
    base_url: str, path: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    query = f"?{urlencode(params, doseq=True)}" if params else ""
    url = f"{base_url.rstrip('/')}{path}{query}"
    headers = {"Accept": "application/json", "User-Agent": "btc15m-market-monitor/1.0"}
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def polymarket_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return public_get(POLYMARKET_BASE_URL, path, params=params)


def gamma_get(path: str, params: dict[str, Any] | None = None) -> Any:
    return public_get(POLYMARKET_GAMMA_URL, path, params=params)


def clob_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return public_get(POLYMARKET_CLOB_URL, path, params=params)


def public_text(url: str) -> str:
    headers = {"Accept": "text/html,application/json", "User-Agent": "btc15m-market-monitor/1.0"}
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=2) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_ts(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def normalize_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1:
        number /= 100.0
    return max(0.0, min(1.0, number))


def best_level(levels: Any, reverse: bool = True) -> tuple[float | None, float | None]:
    if not isinstance(levels, list) or not levels:
        return None, None
    parsed: list[tuple[float, float | None]] = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or not level:
            continue
        price = normalize_price(level[0])
        quantity = None
        if len(level) > 1:
            try:
                quantity = float(level[1])
            except (TypeError, ValueError):
                quantity = None
        if price is not None:
            parsed.append((price, quantity))
    if not parsed:
        return None, None
    parsed.sort(key=lambda item: item[0], reverse=reverse)
    return parsed[0]


def market_price(market: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = normalize_price(market.get(key))
        if value is not None:
            return value
    return None


def numeric_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def plausible_btc_price(value: float | None) -> float | None:
    if value is None:
        return None
    if 1_000 <= value <= 1_000_000:
        return value
    return None


def cached_source_price(key: str, value: float | None) -> float | None:
    valid_value = plausible_btc_price(value)
    if valid_value is not None:
        SOURCE_PRICE_CACHE[key] = valid_value
        return valid_value
    return SOURCE_PRICE_CACHE.get(key)


def parse_price_text(text: str) -> float | None:
    matches = re.findall(r"\$?\s*([0-9]{2,3}(?:,[0-9]{3})+(?:\.[0-9]+)?)", text)
    for match in matches:
        value = plausible_btc_price(numeric_value(match))
        if value is not None:
            return value
    if len(text) > 5000:
        return None
    matches = re.findall(r"\b([0-9]{4,6}(?:\.[0-9]+)?)\b", text)
    for match in matches:
        value = plausible_btc_price(numeric_value(match))
        if value is not None:
            return value
    return None


def fetch_brti_price() -> float | None:
    url = os.getenv("BRTI_PRICE_URL", "https://www.cfbenchmarks.com/data/indices/BRTI")
    try:
        return cached_source_price("brti", parse_price_text(public_text(url)))
    except Exception:
        return cached_source_price("brti", None)


def kraken_result(data: dict[str, Any]) -> Any:
    errors = data.get("error")
    if errors:
        raise RuntimeError(f"Kraken API error: {errors}")
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    for key, value in result.items():
        if key != "last":
            return value
    return None


async def polymarket_rtds_snapshot_async() -> list[dict[str, Any]]:
    import websockets

    subscribe_msg = json.dumps(
        {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": json.dumps({"symbol": POLYMARKET_RTDS_SYMBOL}),
                }
            ],
        }
    )
    async with websockets.connect(POLYMARKET_RTDS_URL, ping_interval=None, open_timeout=4) as ws:
        await ws.send(subscribe_msg)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.25, deadline - time.monotonic()))
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("topic") not in ("crypto_prices", "crypto_prices_chainlink"):
                continue
            payload = msg.get("payload")
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if not isinstance(payload, dict):
                continue
            symbol = str(payload.get("symbol") or POLYMARKET_RTDS_SYMBOL).lower()
            if symbol != POLYMARKET_RTDS_SYMBOL.lower():
                continue
            data = payload.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            if "value" in payload:
                return [payload]
    return []


def fetch_polymarket_rtds_snapshot() -> list[dict[str, Any]]:
    try:
        return asyncio.run(polymarket_rtds_snapshot_async())
    except Exception:
        return []


def polymarket_rtds_latest_price(snapshot: list[dict[str, Any]]) -> float | None:
    latest: tuple[float, float] | None = None
    for item in snapshot:
        price = plausible_btc_price(numeric_value(item.get("value")))
        timestamp = numeric_value(item.get("timestamp"))
        if price is None:
            continue
        ts = timestamp if timestamp is not None else 0.0
        if latest is None or ts >= latest[0]:
            latest = (ts, price)
    if latest is not None:
        return cached_source_price("polymarket_rtds", latest[1])
    return cached_source_price("polymarket_rtds", None)


def fetch_polymarket_rtds_price() -> float | None:
    return polymarket_rtds_latest_price(fetch_polymarket_rtds_snapshot())


def polymarket_rtds_price_at_from_snapshot(
    snapshot: list[dict[str, Any]],
    timestamp_seconds: float | None,
) -> float | None:
    if not timestamp_seconds:
        return None
    target_ms = int(timestamp_seconds * 1000)
    best: tuple[int, float] | None = None
    for item in snapshot:
        price = plausible_btc_price(numeric_value(item.get("value")))
        timestamp = numeric_value(item.get("timestamp"))
        if price is None or timestamp is None:
            continue
        ts_ms = int(timestamp)
        distance = abs(ts_ms - target_ms)
        if best is None or distance < best[0]:
            best = (distance, price)
    if best is None:
        return None
    if best[0] > int(POLYMARKET_TARGET_MAX_DISTANCE_SECONDS * 1000):
        return None
    return best[1]


def fetch_polymarket_rtds_price_at(timestamp_seconds: float | None) -> float | None:
    return polymarket_rtds_price_at_from_snapshot(fetch_polymarket_rtds_snapshot(), timestamp_seconds)


def fetch_kraken_price() -> float | None:
    try:
        data = public_get(KRAKEN_API_URL, "/0/public/Ticker", {"pair": KRAKEN_PAIR})
        ticker = kraken_result(data)
        if not isinstance(ticker, dict):
            return cached_source_price("kraken", None)
        bid = numeric_value((ticker.get("b") or [None])[0])
        ask = numeric_value((ticker.get("a") or [None])[0])
        last = numeric_value((ticker.get("c") or [None])[0])
        price = (bid + ask) / 2.0 if bid is not None and ask is not None else last
        return cached_source_price("kraken", price)
    except Exception:
        return cached_source_price("kraken", None)


def fetch_kraken_trade_price_at(timestamp_seconds: float | None) -> float | None:
    if not timestamp_seconds:
        return None
    since_ns = max(0, int((timestamp_seconds - 2) * 1_000_000_000))
    try:
        data = public_get(KRAKEN_API_URL, "/0/public/Trades", {"pair": KRAKEN_PAIR, "since": since_ns})
        trades = kraken_result(data)
        if not isinstance(trades, list):
            return None
        best_before: tuple[float, float] | None = None
        for trade in trades:
            if not isinstance(trade, list) or len(trade) < 3:
                continue
            price = plausible_btc_price(numeric_value(trade[0]))
            trade_ts = numeric_value(trade[2])
            if price is None or trade_ts is None:
                continue
            if trade_ts >= timestamp_seconds:
                return price
            best_before = (trade_ts, price)
        return best_before[1] if best_before else None
    except Exception:
        return None


def event_metadata_value(market: dict[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(market, dict):
        return None
    metadata = market.get("event_metadata") or market.get("eventMetadata") or {}
    if not isinstance(metadata, dict):
        return None
    for key in keys:
        value = numeric_value(metadata.get(key))
        if value is not None:
            return value
    return None


def polymarket_market_key(market: dict[str, Any] | None) -> str:
    if not isinstance(market, dict):
        return ""
    return str(
        market.get("slug")
        or market.get("event_slug")
        or market.get("ticker")
        or market.get("conditionId")
        or ""
    )


def polymarket_start_timestamp(market: dict[str, Any] | None) -> float | None:
    if isinstance(market, dict):
        for key in ("event_start_time", "eventStartTime", "startTime", "gameStartTime"):
            value = parse_ts(market.get(key))
            if value:
                return value
    slug = polymarket_market_key(market)
    try:
        return float(int(slug.rsplit("-", 1)[1]))
    except (IndexError, ValueError):
        return None


def walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(walk_dicts(child))
    return found


def next_data_json(text: str) -> dict[str, Any] | None:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if not match:
        return None
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return None


def fetch_polymarket_page_target(market: dict[str, Any] | None) -> float | None:
    slug = polymarket_market_key(market)
    if not slug:
        return None
    cached = POLYMARKET_TARGET_CACHE.get(slug)
    if cached is not None:
        return cached
    last_fetch = POLYMARKET_TARGET_FETCH_AT.get(slug, 0.0)
    if time.monotonic() - last_fetch < 30:
        return None
    POLYMARKET_TARGET_FETCH_AT[slug] = time.monotonic()
    try:
        data = next_data_json(public_text(f"https://polymarket.com/event/{slug}"))
    except Exception:
        return None
    if not data:
        return None
    events: dict[str, dict[str, Any]] = {}
    for item in walk_dicts(data):
        item_slug = item.get("slug") or item.get("ticker")
        if isinstance(item_slug, str) and item_slug.startswith("btc-updown-15m-"):
            events[item_slug] = item

    target = event_metadata_value(events.get(slug), "priceToBeat", "price_to_beat")
    target = plausible_btc_price(target)
    if target is not None:
        POLYMARKET_TARGET_CACHE[slug] = target
    return target


def load_polymarket_target_from_csv(slug: str) -> float | None:
    if not slug or not DATA_DIR.exists():
        return None
    paths = sorted(DATA_DIR.glob("combined_*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in paths:
        try:
            with path.open(newline="") as file_obj:
                reader = csv.DictReader(file_obj)
                if "polymarket_ticker" not in (reader.fieldnames or []):
                    continue
                if "polymarket_btc_target" not in (reader.fieldnames or []):
                    continue
                for row in reader:
                    if row.get("polymarket_ticker") != slug:
                        continue
                    target = plausible_btc_price(numeric_value(row.get("polymarket_btc_target")))
                    if target is not None:
                        POLYMARKET_TARGET_CACHE[slug] = target
                        return target
        except OSError:
            continue
    return None


def fetch_polymarket_target(market: dict[str, Any] | None) -> float | None:
    slug = polymarket_market_key(market)
    rtds_target = fetch_polymarket_rtds_price_at(polymarket_start_timestamp(market))
    if rtds_target is not None and slug:
        POLYMARKET_TARGET_CACHE[slug] = rtds_target
        return rtds_target
    if slug in POLYMARKET_TARGET_CACHE:
        return POLYMARKET_TARGET_CACHE[slug]
    return load_polymarket_target_from_csv(slug)


def kalshi_brti_60_sma(kalshi_price: float | None) -> tuple[float | None, int]:
    samples: list[float] = []
    with STATE_LOCK:
        history = list(STATE.get("source_history") or [])
    for item in history[-59:]:
        sample = plausible_btc_price(numeric_value(item.get("kalshi_price")))
        if sample is not None:
            samples.append(sample)
    if kalshi_price is not None:
        samples.append(kalshi_price)
    average = sum(samples) / len(samples) if samples else None
    return average, len(samples)


def source_price_snapshot(
    kalshi_market: dict[str, Any],
    polymarket_market: dict[str, Any] | None,
) -> dict[str, Any]:
    timestamp_utc = iso_utc()
    kalshi_current = plausible_btc_price(numeric_value(kalshi_market.get("expiration_value")))
    if kalshi_current is not None:
        cached_source_price("brti", kalshi_current)
    else:
        kalshi_current = fetch_brti_price()
    rtds_snapshot = fetch_polymarket_rtds_snapshot()
    polymarket_current = (
        plausible_btc_price(
            event_metadata_value(
                polymarket_market,
                "currentPrice",
                "current_price",
            )
        )
        or polymarket_rtds_latest_price(rtds_snapshot)
        or fetch_kraken_price()
        or plausible_btc_price(
            event_metadata_value(
                polymarket_market,
                "finalPrice",
                "final_price",
            )
        )
    )
    target_key = polymarket_market_key(polymarket_market)
    polymarket_target = POLYMARKET_TARGET_CACHE.get(target_key) if target_key else None
    if polymarket_target is None:
        polymarket_target = polymarket_rtds_price_at_from_snapshot(
            rtds_snapshot,
            polymarket_start_timestamp(polymarket_market),
        )
        if polymarket_target is not None and target_key:
            POLYMARKET_TARGET_CACHE[target_key] = polymarket_target
    if polymarket_target is None and target_key:
        polymarket_target = load_polymarket_target_from_csv(target_key)
    kalshi_60_sma, kalshi_60_sma_count = kalshi_brti_60_sma(kalshi_current)
    return {
        "timestamp_utc": timestamp_utc,
        "kalshi_price": kalshi_current,
        "kalshi_target": numeric_value(kalshi_market.get("floor_strike")),
        "kalshi_60_sma": kalshi_60_sma,
        "kalshi_60_sma_sample_count": kalshi_60_sma_count,
        "polymarket_price": polymarket_current,
        "polymarket_target": polymarket_target,
    }


def nested_price(value: Any) -> float | None:
    if isinstance(value, dict):
        if "value" in value:
            return normalize_price(value.get("value"))
        if "px" in value:
            return nested_price(value.get("px"))
        if "quote" in value:
            return nested_price(value.get("quote"))
    return normalize_price(value)


def invert_price(value: float | None) -> float | None:
    if value is None:
        return None
    return round(1.0 - value, 10)


def discover_active_market() -> dict[str, Any] | None:
    data = kalshi_get(
        "/markets",
        {
            "series_ticker": SERIES_TICKER,
            "status": "open",
            "limit": 200,
        },
    )
    markets = data.get("markets", [])
    if not markets:
        data = kalshi_get("/markets", {"event_ticker": SERIES_TICKER, "status": "open", "limit": 200})
        markets = data.get("markets", [])
    if not markets:
        data = kalshi_get("/markets", {"status": "open", "limit": 1000})
        markets = [
            market
            for market in data.get("markets", [])
            if str(market.get("ticker", "")).startswith(SERIES_TICKER)
            or str(market.get("event_ticker", "")).startswith(SERIES_TICKER)
        ]
    if not markets:
        return None
    markets.sort(
        key=lambda market: (
            parse_ts(market.get("close_time") or market.get("close_ts") or market.get("expiration_time")),
            str(market.get("ticker", "")),
        )
    )
    return markets[0]


def market_text(market: dict[str, Any]) -> str:
    fields = [
        market.get("question"),
        market.get("title"),
        market.get("subtitle"),
        market.get("description"),
        market.get("slug"),
    ]
    return " ".join(str(value or "") for value in fields).lower()


def interval_overlap_score(market: dict[str, Any], kalshi_market: dict[str, Any]) -> float:
    kalshi_open = parse_ts(kalshi_market.get("open_time"))
    kalshi_close = parse_ts(kalshi_market.get("close_time") or kalshi_market.get("close_ts"))
    poly_start = parse_ts(market.get("startDate") or market.get("startTime") or market.get("gameStartTime"))
    poly_end = parse_ts(market.get("endDate"))
    score = 0.0
    if kalshi_close and poly_end:
        score -= abs(poly_end - kalshi_close)
    if kalshi_open and poly_start:
        score -= 0.25 * abs(poly_start - kalshi_open)
    return score


def polymarket_event_market(event: dict[str, Any]) -> dict[str, Any] | None:
    markets = event.get("markets")
    if not isinstance(markets, list) or not markets:
        return None
    market = dict(markets[0])
    market.setdefault("event_ticker", event.get("ticker") or event.get("slug"))
    market.setdefault("event_title", event.get("title"))
    market.setdefault("event_slug", event.get("slug"))
    market.setdefault("event_start_time", event.get("startTime"))
    market.setdefault("event_end_time", event.get("endDate"))
    market.setdefault("event_metadata", event.get("eventMetadata"))
    return market


def gamma_event_by_slug(slug: str) -> dict[str, Any] | None:
    try:
        data = gamma_get(f"/events/slug/{slug}")
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if isinstance(data, dict) and data.get("slug"):
        return data
    return None


def discover_polymarket_market(kalshi_market: dict[str, Any]) -> dict[str, Any] | None:
    if POLYMARKET_MARKET_SLUG:
        event = gamma_event_by_slug(POLYMARKET_MARKET_SLUG)
        if event:
            return polymarket_event_market(event)
        data = gamma_get("/markets", {"slug": POLYMARKET_MARKET_SLUG})
        if isinstance(data, list) and data:
            return data[0]
        return None

    close_ts = parse_ts(kalshi_market.get("close_time") or kalshi_market.get("close_ts"))
    open_ts = parse_ts(kalshi_market.get("open_time"))
    slug_epochs = []
    for value in (open_ts, close_ts - 15 * 60 if close_ts else 0, close_ts):
        if value:
            slug_epochs.append(int(value))
    for epoch in dict.fromkeys(slug_epochs):
        event = gamma_event_by_slug(f"btc-updown-15m-{epoch}")
        if event:
            return polymarket_event_market(event)

    candidates: list[dict[str, Any]] = []
    searches = [
        POLYMARKET_SEARCH_QUERY,
        "BTC price up",
        "Bitcoin up down",
        "Bitcoin 15 minutes",
    ]
    for query in searches:
        data = polymarket_get("/search", {"query": query, "limit": 20})
        for event in data.get("events", []):
            for market in event.get("markets", []):
                if market.get("active") is False or market.get("closed") is True:
                    continue
                enriched = dict(market)
                enriched.setdefault("event_ticker", event.get("ticker") or event.get("slug"))
                enriched.setdefault("event_title", event.get("title"))
                candidates.append(enriched)

    if close_ts:
        window = 30 * 60
        data = polymarket_get(
            "/markets",
            {
                "active": "true",
                "closed": "false",
                "limit": 100,
                "endDateMin": datetime.fromtimestamp(close_ts - window, tz=timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "endDateMax": datetime.fromtimestamp(close_ts + window, tz=timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            },
        )
        candidates.extend(data.get("markets", []))

    unique = {str(market.get("slug")): market for market in candidates if market.get("slug")}
    filtered = [
        market
        for market in unique.values()
        if ("btc" in market_text(market) or "bitcoin" in market_text(market))
        and ("up" in market_text(market) or "above" in market_text(market) or "higher" in market_text(market))
    ]
    if not filtered:
        return None
    filtered.sort(key=lambda market: interval_overlap_score(market, kalshi_market), reverse=True)
    return filtered[0]


def orderbook_levels(orderbook: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    book = orderbook.get("orderbook_fp") or orderbook.get("orderbook") or {}
    yes = book.get("yes_dollars") or book.get("yes") or []
    no = book.get("no_dollars") or book.get("no") or []
    return yes, no


def polymarket_book_levels(orderbook: dict[str, Any]) -> tuple[list[list[Any]], list[list[Any]]]:
    data = orderbook.get("marketData") or orderbook

    def convert(levels: Any) -> list[list[Any]]:
        converted = []
        if not isinstance(levels, list):
            return converted
        for level in levels:
            if not isinstance(level, dict):
                continue
            price = nested_price(level.get("px") or level.get("price"))
            qty = level.get("qty") or level.get("quantity") or level.get("size")
            if price is not None:
                converted.append([price, qty])
        return converted

    return convert(data.get("bids")), convert(data.get("offers") or data.get("asks"))


def normalized_level_pairs(levels: Any) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    if not isinstance(levels, list):
        return pairs
    for level in levels:
        price = None
        quantity = None
        if isinstance(level, dict):
            price = nested_price(level.get("px") or level.get("price"))
            quantity = numeric_value(level.get("qty") or level.get("quantity") or level.get("size"))
        elif isinstance(level, (list, tuple)) and level:
            price = normalize_price(level[0])
            quantity = numeric_value(level[1]) if len(level) > 1 else None
        if price is not None and quantity is not None and quantity > 0:
            pairs.append((price, quantity))
    return pairs


def implied_ask_levels_from_bid_levels(bid_levels: Any) -> list[list[float]]:
    return [
        [round(1.0 - price, 10), quantity]
        for price, quantity in normalized_level_pairs(bid_levels)
    ]


def depth_within_tau(levels: Any, tau: float, *, side: str) -> float:
    pairs = normalized_level_pairs(levels)
    if not pairs:
        return 0.0
    if side == "ask":
        reference = min(price for price, _quantity in pairs)
        return sum(quantity for price, quantity in pairs if price <= reference + tau + 1e-12)
    reference = max(price for price, _quantity in pairs)
    return sum(quantity for price, quantity in pairs if price >= reference - tau - 1e-12)


def imbalance_value(left_depth: float, right_depth: float) -> float | None:
    total = left_depth + right_depth
    if total <= 0:
        return None
    return (left_depth - right_depth) / total


def order_imbalance_metrics(
    yes_bid_levels: Any,
    yes_ask_levels: Any,
    no_bid_levels: Any,
    no_ask_levels: Any,
) -> dict[str, float | None]:
    metrics: dict[str, float | None] = {}
    for tau in ORDER_IMBALANCE_TAUS:
        label = tau_label(tau)
        yes_bid_depth = depth_within_tau(yes_bid_levels, tau, side="bid")
        yes_ask_depth = depth_within_tau(yes_ask_levels, tau, side="ask")
        no_bid_depth = depth_within_tau(no_bid_levels, tau, side="bid")
        no_ask_depth = depth_within_tau(no_ask_levels, tau, side="ask")
        metrics.update(
            {
                f"yes_bid_depth_tau_{label}": yes_bid_depth,
                f"yes_ask_depth_tau_{label}": yes_ask_depth,
                f"yes_book_imbalance_tau_{label}": imbalance_value(yes_bid_depth, yes_ask_depth),
                f"no_bid_depth_tau_{label}": no_bid_depth,
                f"no_ask_depth_tau_{label}": no_ask_depth,
                f"no_book_imbalance_tau_{label}": imbalance_value(no_bid_depth, no_ask_depth),
                f"directional_bid_imbalance_tau_{label}": imbalance_value(yes_bid_depth, no_bid_depth),
                f"directional_ask_imbalance_tau_{label}": imbalance_value(no_ask_depth, yes_ask_depth),
            }
        )
    return metrics


def prefixed_order_imbalance_fields(prefix: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    if not any(snapshot.get(key) for key in ("yes_levels", "yes_ask_levels", "no_levels", "no_ask_levels")):
        return {field: "" for field in order_imbalance_csv_fields(prefix)}
    return {
        f"{prefix}_{key}": value
        for key, value in order_imbalance_metrics(
            snapshot.get("yes_levels", []),
            snapshot.get("yes_ask_levels", []),
            snapshot.get("no_levels", []),
            snapshot.get("no_ask_levels", []),
        ).items()
    }


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def polymarket_clob_orderbooks(market: dict[str, Any]) -> dict[str, Any]:
    token_ids = parse_json_list(market.get("clobTokenIds"))
    outcomes = [str(outcome).lower() for outcome in parse_json_list(market.get("outcomes"))]
    if len(token_ids) < 2:
        raise RuntimeError("Polymarket market has no CLOB token ids")

    up_index = outcomes.index("up") if "up" in outcomes else 0
    down_index = outcomes.index("down") if "down" in outcomes else 1
    up_token = token_ids[up_index]
    down_token = token_ids[down_index]
    with ThreadPoolExecutor(max_workers=2) as executor:
        up_future = executor.submit(clob_get, "/book", {"token_id": up_token})
        down_future = executor.submit(clob_get, "/book", {"token_id": down_token})
        up_book = up_future.result()
        down_book = down_future.result()
    return {
        "up": up_book,
        "down": down_book,
    }


def make_snapshot(market: dict[str, Any], orderbook: dict[str, Any]) -> dict[str, Any]:
    yes_levels, no_levels = orderbook_levels(orderbook)
    yes_ask_levels = implied_ask_levels_from_bid_levels(no_levels)
    no_ask_levels = implied_ask_levels_from_bid_levels(yes_levels)
    best_yes_bid, best_yes_bid_qty = best_level(yes_levels)
    best_no_bid, best_no_bid_qty = best_level(no_levels)
    yes_bid = best_yes_bid
    yes_ask = invert_price(best_no_bid)
    no_bid = best_no_bid
    no_ask = invert_price(best_yes_bid)
    midpoint = None
    if yes_bid is not None and yes_ask is not None:
        midpoint = (yes_bid + yes_ask) / 2.0
    elif yes_bid is not None:
        midpoint = yes_bid
    elif yes_ask is not None:
        midpoint = yes_ask
    return {
        "timestamp_utc": iso_utc(),
        "ticker": market.get("ticker"),
        "title": market.get("title") or market.get("subtitle") or "",
        "event_ticker": market.get("event_ticker") or "",
        "close_time": market.get("close_time") or market.get("close_ts") or market.get("expiration_time") or "",
        "status": market.get("status") or "",
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_mid": midpoint,
        "last_price": market_price(market, "last_price_dollars", "last_price"),
        "volume": market.get("volume") or market.get("volume_fp") or market.get("volume_24h") or market.get("volume_24h_fp") or "",
        "open_interest": market.get("open_interest") or market.get("open_interest_fp") or "",
        "best_yes_bid_qty": best_yes_bid_qty,
        "best_no_bid_qty": best_no_bid_qty,
        "yes_levels": yes_levels,
        "no_levels": no_levels,
        "yes_ask_levels": yes_ask_levels,
        "no_ask_levels": no_ask_levels,
        **order_imbalance_metrics(yes_levels, yes_ask_levels, no_levels, no_ask_levels),
    }


def make_polymarket_snapshot(market: dict[str, Any], orderbook: dict[str, Any]) -> dict[str, Any]:
    if "up" in orderbook or "down" in orderbook:
        yes_levels, yes_ask_levels = polymarket_book_levels(orderbook.get("up") or {})
        no_levels, no_ask_levels = polymarket_book_levels(orderbook.get("down") or {})
    else:
        yes_levels, yes_ask_levels = polymarket_book_levels(orderbook)
        no_levels = [
            [invert_price(level[0]), level[1]]
            for level in yes_ask_levels
            if level and level[0] is not None
        ]
        no_ask_levels = []
    best_yes_bid, best_yes_bid_qty = best_level(yes_levels)
    best_yes_ask, _best_yes_ask_qty = best_level(yes_ask_levels, reverse=False)
    best_no_bid, best_no_bid_qty = best_level(no_levels)
    best_no_ask, _best_no_ask_qty = best_level(no_ask_levels, reverse=False)
    stats = (orderbook.get("marketData") or orderbook).get("stats") or {}

    yes_bid = (
        best_yes_bid
        or nested_price(market.get("bestBidQuote"))
        or market_price(market, "bestBid", "best_bid")
    )
    yes_ask = (
        best_yes_ask
        or nested_price(market.get("bestAskQuote"))
        or market_price(market, "bestAsk", "best_ask")
    )
    no_bid = best_no_bid if best_no_bid is not None else invert_price(yes_ask)
    no_ask = best_no_ask if best_no_ask is not None else invert_price(yes_bid)
    midpoint = None
    if yes_bid is not None and yes_ask is not None:
        midpoint = (yes_bid + yes_ask) / 2.0
    elif yes_bid is not None:
        midpoint = yes_bid
    elif yes_ask is not None:
        midpoint = yes_ask

    return {
        "timestamp_utc": iso_utc(),
        "ticker": market.get("slug") or market.get("id"),
        "title": market.get("question") or market.get("title") or "",
        "event_ticker": market.get("event_ticker") or market.get("event_title") or "",
        "close_time": market.get("endDate") or market.get("event_end_time") or "",
        "status": market.get("ep3Status") or (orderbook.get("marketData") or {}).get("state") or "",
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_mid": midpoint,
        "last_price": nested_price(stats.get("lastTradePx") or market.get("lastTradePrice")),
        "volume": stats.get("sharesTraded") or market.get("volume") or market.get("volumeNum") or "",
        "open_interest": stats.get("openInterest") or market.get("openInterest") or "",
        "best_yes_bid_qty": best_yes_bid_qty,
        "best_no_bid_qty": best_no_bid_qty,
        "yes_levels": yes_levels,
        "no_levels": no_levels,
        "yes_ask_levels": yes_ask_levels,
        "no_ask_levels": no_ask_levels,
        "ask_levels": yes_ask_levels,
        **order_imbalance_metrics(yes_levels, yes_ask_levels, no_levels, no_ask_levels),
    }


def append_snapshot_csv(snapshot: dict[str, Any], prefix: str = "") -> None:
    ticker = snapshot.get("ticker") or "unknown"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_ticker = "".join(char if char.isalnum() or char in "._-" else "_" for char in str(ticker))
    path = DATA_DIR / f"{prefix}{safe_ticker}.csv"
    exists = path.exists()
    fields = [
        "timestamp_utc",
        "ticker",
        "title",
        "event_ticker",
        "close_time",
        "status",
        "yes_bid",
        "yes_ask",
        "no_bid",
        "no_ask",
        "yes_mid",
        "last_price",
        "volume",
        "open_interest",
        "best_yes_bid_qty",
        "best_no_bid_qty",
        *order_imbalance_csv_fields(""),
        "yes_levels_json",
        "no_levels_json",
        "yes_ask_levels_json",
        "no_ask_levels_json",
    ]
    row = {key: snapshot.get(key, "") for key in fields}
    row["yes_levels_json"] = json.dumps(snapshot.get("yes_levels", []), separators=(",", ":"))
    row["no_levels_json"] = json.dumps(snapshot.get("no_levels", []), separators=(",", ":"))
    row["yes_ask_levels_json"] = json.dumps(snapshot.get("yes_ask_levels", []), separators=(",", ":"))
    row["no_ask_levels_json"] = json.dumps(snapshot.get("no_ask_levels", []), separators=(",", ":"))
    exists = ensure_csv_schema(path, fields)
    with path.open("a", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def safe_filename(value: Any) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in str(value or "unknown"))


def combined_csv_path(snapshot: dict[str, Any]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"combined_{safe_filename(snapshot.get('ticker'))}.csv"


def nullable_sum(a: Any, b: Any) -> float | None:
    left = numeric_value(a)
    right = numeric_value(b)
    if left is None or right is None:
        return None
    return left + right


def ensure_csv_schema(path: Path, fields: list[str]) -> bool:
    if not path.exists():
        return False
    try:
        with path.open(newline="") as file_obj:
            reader = csv.DictReader(file_obj)
            old_fields = reader.fieldnames or []
            if old_fields == fields:
                return True
            rows = list(reader)
    except OSError:
        return True
    with path.open("w", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        for old_row in rows:
            writer.writerow({field: old_row.get(field, "") for field in fields})
    return True


def append_combined_snapshot_csv(
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any] | None,
    source_snapshot: dict[str, Any] | None,
    polymarket_error: str | None,
) -> None:
    fields = [
        "timestamp_utc",
        "kalshi_timestamp_utc",
        "kalshi_ticker",
        "kalshi_title",
        "kalshi_event_ticker",
        "kalshi_close_time",
        "kalshi_status",
        "kalshi_yes_bid",
        "kalshi_yes_ask",
        "kalshi_no_bid",
        "kalshi_no_ask",
        "kalshi_yes_mid",
        "kalshi_last_price",
        "kalshi_volume",
        "kalshi_open_interest",
        "kalshi_best_yes_bid_qty",
        "kalshi_best_no_bid_qty",
        *order_imbalance_csv_fields("kalshi"),
        "polymarket_timestamp_utc",
        "polymarket_ticker",
        "polymarket_title",
        "polymarket_event_ticker",
        "polymarket_close_time",
        "polymarket_status",
        "polymarket_yes_bid",
        "polymarket_yes_ask",
        "polymarket_no_bid",
        "polymarket_no_ask",
        "polymarket_yes_mid",
        "polymarket_last_price",
        "polymarket_volume",
        "polymarket_open_interest",
        "polymarket_best_yes_bid_qty",
        "polymarket_best_no_bid_qty",
        *order_imbalance_csv_fields("polymarket"),
        "source_timestamp_utc",
        "kalshi_btc_source",
        "kalshi_btc_price",
        "kalshi_btc_target",
        "kalshi_btc_60_sma",
        "kalshi_btc_60_sma_sample_count",
        "polymarket_btc_source",
        "polymarket_btc_price",
        "polymarket_btc_target",
        "k_plus_np",
        "nk_plus_p",
        "polymarket_error",
    ]
    poly = polymarket_snapshot or {}
    source = source_snapshot or {}
    row = {
        "timestamp_utc": iso_utc(),
        "kalshi_timestamp_utc": kalshi_snapshot.get("timestamp_utc", ""),
        "kalshi_ticker": kalshi_snapshot.get("ticker", ""),
        "kalshi_title": kalshi_snapshot.get("title", ""),
        "kalshi_event_ticker": kalshi_snapshot.get("event_ticker", ""),
        "kalshi_close_time": kalshi_snapshot.get("close_time", ""),
        "kalshi_status": kalshi_snapshot.get("status", ""),
        "kalshi_yes_bid": kalshi_snapshot.get("yes_bid", ""),
        "kalshi_yes_ask": kalshi_snapshot.get("yes_ask", ""),
        "kalshi_no_bid": kalshi_snapshot.get("no_bid", ""),
        "kalshi_no_ask": kalshi_snapshot.get("no_ask", ""),
        "kalshi_yes_mid": kalshi_snapshot.get("yes_mid", ""),
        "kalshi_last_price": kalshi_snapshot.get("last_price", ""),
        "kalshi_volume": kalshi_snapshot.get("volume", ""),
        "kalshi_open_interest": kalshi_snapshot.get("open_interest", ""),
        "kalshi_best_yes_bid_qty": kalshi_snapshot.get("best_yes_bid_qty", ""),
        "kalshi_best_no_bid_qty": kalshi_snapshot.get("best_no_bid_qty", ""),
        **prefixed_order_imbalance_fields("kalshi", kalshi_snapshot),
        "polymarket_timestamp_utc": poly.get("timestamp_utc", ""),
        "polymarket_ticker": poly.get("ticker", ""),
        "polymarket_title": poly.get("title", ""),
        "polymarket_event_ticker": poly.get("event_ticker", ""),
        "polymarket_close_time": poly.get("close_time", ""),
        "polymarket_status": poly.get("status", ""),
        "polymarket_yes_bid": poly.get("yes_bid", ""),
        "polymarket_yes_ask": poly.get("yes_ask", ""),
        "polymarket_no_bid": poly.get("no_bid", ""),
        "polymarket_no_ask": poly.get("no_ask", ""),
        "polymarket_yes_mid": poly.get("yes_mid", ""),
        "polymarket_last_price": poly.get("last_price", ""),
        "polymarket_volume": poly.get("volume", ""),
        "polymarket_open_interest": poly.get("open_interest", ""),
        "polymarket_best_yes_bid_qty": poly.get("best_yes_bid_qty", ""),
        "polymarket_best_no_bid_qty": poly.get("best_no_bid_qty", ""),
        **prefixed_order_imbalance_fields("polymarket", poly),
        "source_timestamp_utc": source.get("timestamp_utc", ""),
        "kalshi_btc_source": "BRTI",
        "kalshi_btc_price": source.get("kalshi_price", ""),
        "kalshi_btc_target": source.get("kalshi_target", ""),
        "kalshi_btc_60_sma": source.get("kalshi_60_sma", ""),
        "kalshi_btc_60_sma_sample_count": source.get("kalshi_60_sma_sample_count", ""),
        "polymarket_btc_source": "Polymarket RTDS",
        "polymarket_btc_price": source.get("polymarket_price", ""),
        "polymarket_btc_target": source.get("polymarket_target", ""),
        "k_plus_np": nullable_sum(kalshi_snapshot.get("yes_ask"), poly.get("no_ask")),
        "nk_plus_p": nullable_sum(kalshi_snapshot.get("no_ask"), poly.get("yes_ask")),
        "polymarket_error": polymarket_error or "",
    }
    path = combined_csv_path(kalshi_snapshot)
    exists = ensure_csv_schema(path, fields)
    with path.open("a", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def poll_once() -> None:
    market = discover_active_market()
    if not market:
        raise RuntimeError(f"No open market found for {SERIES_TICKER}")
    ticker = market["ticker"]
    orderbook = kalshi_get(f"/markets/{ticker}/orderbook", {"depth": ORDERBOOK_DEPTH})
    snapshot = make_snapshot(market, orderbook)
    polymarket_market = None
    polymarket_orderbook = None
    polymarket_snapshot = None
    polymarket_error = None
    source_snapshot = None
    try:
        polymarket_market = discover_polymarket_market(market)
        if not polymarket_market:
            raise RuntimeError("No matching open Polymarket market found")
        slug = polymarket_market.get("slug")
        if not slug:
            raise RuntimeError("Matching Polymarket market has no slug")
        polymarket_orderbook = polymarket_clob_orderbooks(polymarket_market)
        polymarket_snapshot = make_polymarket_snapshot(polymarket_market, polymarket_orderbook)
        source_snapshot = source_price_snapshot(market, polymarket_market)
    except Exception as exc:
        polymarket_error = f"{type(exc).__name__}: {exc}"
        source_snapshot = source_price_snapshot(market, polymarket_market)
    append_combined_snapshot_csv(snapshot, polymarket_snapshot, source_snapshot, polymarket_error)
    with STATE_LOCK:
        if STATE.get("active_ticker") != ticker:
            STATE["history"] = []
            STATE["polymarket_history"] = []
            STATE["source_history"] = []
        STATE.update(
            {
                "active_ticker": ticker,
                "active_market": market,
                "orderbook": orderbook,
                "snapshot": snapshot,
                "polymarket_slug": (polymarket_market or {}).get("slug") if polymarket_market else None,
                "polymarket_market": polymarket_market,
                "polymarket_orderbook": polymarket_orderbook,
                "polymarket_snapshot": polymarket_snapshot,
                "last_fetch_at": snapshot["timestamp_utc"],
                "next_fetch_at": seconds_from_now(POLL_SECONDS),
                "error": None,
                "polymarket_error": polymarket_error,
                "consecutive_errors": 0,
            }
        )
        STATE["history"].append(
            {
                "timestamp_utc": snapshot["timestamp_utc"],
                "ticker": ticker,
                "yes_mid": snapshot["yes_mid"],
                "yes_bid": snapshot["yes_bid"],
                "yes_ask": snapshot["yes_ask"],
                "tradable_yes_price": snapshot["yes_ask"],
            }
        )
        STATE["history"] = STATE["history"][-720:]
        if polymarket_snapshot:
            STATE["polymarket_history"].append(
                {
                    "timestamp_utc": polymarket_snapshot["timestamp_utc"],
                    "ticker": polymarket_snapshot["ticker"],
                    "yes_mid": polymarket_snapshot["yes_mid"],
                    "yes_bid": polymarket_snapshot["yes_bid"],
                    "yes_ask": polymarket_snapshot["yes_ask"],
                    "tradable_yes_price": polymarket_snapshot["yes_ask"],
                }
            )
            STATE["polymarket_history"] = STATE["polymarket_history"][-720:]
        if source_snapshot:
            STATE["source_history"].append(source_snapshot)
            STATE["source_history"] = STATE["source_history"][-720:]


def polling_loop() -> None:
    while True:
        started = time.monotonic()
        try:
            poll_once()
        except Exception as exc:
            with STATE_LOCK:
                STATE["error"] = f"{type(exc).__name__}: {exc}"
                STATE["consecutive_errors"] = int(STATE.get("consecutive_errors", 0)) + 1
                STATE["last_traceback"] = traceback.format_exc(limit=3)
        elapsed = time.monotonic() - started
        sleep_for = max(0.0, POLL_SECONDS - elapsed)
        with STATE_LOCK:
            STATE["next_fetch_at"] = seconds_from_now(sleep_for)
        time.sleep(sleep_for)


def state_payload() -> dict[str, Any]:
    with STATE_LOCK:
        payload = json.loads(json.dumps(STATE, default=str))
    payload["poll_seconds"] = POLL_SECONDS
    payload["orderbook_depth"] = ORDERBOOK_DEPTH
    payload["data_dir"] = str(DATA_DIR)
    return payload


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BTC 15m Market Monitor</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101418;
      --panel: #181f26;
      --panel-2: #202933;
      --text: #e9eef4;
      --muted: #9cacbb;
      --line: #34414e;
      --green: #31c48d;
      --red: #f98080;
      --blue: #7db7ff;
      --orange: #ffb86b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 20px 24px 12px;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
    }
    h1 { margin: 0; font-size: 22px; font-weight: 700; letter-spacing: 0; }
    .sub { color: var(--muted); font-size: 13px; margin-top: 5px; }
    .status { text-align: right; font-size: 13px; color: var(--muted); }
    main { padding: 18px 24px 24px; display: grid; gap: 18px; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 12px;
    }
    .metric, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .metric { padding: 14px; min-height: 86px; }
    .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
    .value { font-size: 28px; margin-top: 8px; font-weight: 750; }
    .small-value { font-size: 17px; overflow-wrap: anywhere; }
    .grid { display: grid; grid-template-columns: 1.4fr .9fr; gap: 18px; }
    .panel { padding: 16px; min-width: 0; }
    .panel h2 { margin: 0 0 12px; font-size: 15px; font-weight: 700; }
    .chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .chart-wrap { height: 360px; min-width: 0; }
    canvas { width: 100%; height: 100%; display: block; }
    table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
    th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: right; font-size: 13px; }
    th:first-child, td:first-child { text-align: left; }
    th { color: var(--muted); font-weight: 600; }
    .error { color: var(--red); white-space: pre-wrap; }
    @media (max-width: 1000px) {
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .grid, .chart-grid { grid-template-columns: 1fr; }
      header { align-items: start; flex-direction: column; }
      .status { text-align: left; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>BTC 15m Market Monitor</h1>
      <div class="sub" id="title">Waiting for first market snapshot</div>
    </div>
    <div class="status">
      <div id="clock">Now: --</div>
      <div id="ticker">Kalshi: --</div>
      <div id="polyTicker">Polymarket: --</div>
      <div id="timing">Last fetch: --</div>
    </div>
  </header>
  <main>
    <section class="metrics">
      <div class="metric"><div class="label">Kalshi YES tradable ask</div><div class="value" id="yesMid">--</div></div>
      <div class="metric"><div class="label">Polymarket YES tradable ask</div><div class="value" id="polyYesMid">--</div></div>
      <div class="metric"><div class="label">BRTI spot</div><div class="value small-value" id="brtiPrice">--</div></div>
      <div class="metric"><div class="label">BRTI 60-SMA</div><div class="value small-value" id="brtiAvg">--</div></div>
      <div class="metric"><div class="label">Polymarket BTC price</div><div class="value small-value" id="polySourcePrice">--</div></div>
      <div class="metric"><div class="label">K + NP</div><div class="value small-value" id="arbKnp">--</div></div>
      <div class="metric"><div class="label">NK + P</div><div class="value small-value" id="arbNkp">--</div></div>
      <div class="metric"><div class="label">Kalshi YES bid / ask</div><div class="value small-value" id="yesBidAsk">--</div></div>
      <div class="metric"><div class="label">Polymarket YES bid / ask</div><div class="value small-value" id="polyYesBidAsk">--</div></div>
      <div class="metric"><div class="label">Kalshi NO bid / ask</div><div class="value small-value" id="noBidAsk">--</div></div>
      <div class="metric"><div class="label">Polymarket NO bid / ask</div><div class="value small-value" id="polyNoBidAsk">--</div></div>
      <div class="metric"><div class="label">Kalshi Volume</div><div class="value small-value" id="volume">--</div></div>
      <div class="metric"><div class="label">Close time</div><div class="value small-value" id="closeTime">--</div></div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2>Tradable Prices and Source Prices</h2>
        <div class="chart-grid">
          <div class="chart-wrap"><canvas id="oddsChart"></canvas></div>
          <div class="chart-wrap"><canvas id="sourceChart"></canvas></div>
        </div>
      </div>
      <div class="panel">
        <h2>Current Snapshot</h2>
        <table>
          <tbody id="snapshotTable"></tbody>
        </table>
        <p class="error" id="error"></p>
      </div>
    </section>
  </main>
  <script>
    const fmtPct = value => value === null || value === undefined ? "--" : `${(value * 100).toFixed(1)}¢`;
    const fmtNum = value => value === null || value === undefined || value === "" ? "--" : Number(value).toLocaleString();
    const fmtUsd = value => value === null || value === undefined || value === "" ? "--" : `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    const fmtUsdWithSamples = (value, count) => {
      if (value === null || value === undefined || value === "") return "--";
      const sampleText = Number(count) > 0 ? ` (${Number(count)} samples)` : "";
      return `${fmtUsd(value)}${sampleText}`;
    };
    const priceSum = (a, b) => (
      a !== null && a !== undefined && a !== "" && b !== null && b !== undefined && b !== "" &&
      Number.isFinite(Number(a)) && Number.isFinite(Number(b))
    ) ? Number(a) + Number(b) : null;
    let latestHistory = [];
    let latestPolyHistory = [];
    let latestSourceHistory = [];

    function row(label, value) {
      return `<tr><td>${label}</td><td>${value}</td></tr>`;
    }
    function localTime(value) {
      if (!value) return "--";
      const date = new Date(value);
      return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
    }
    function drawChart() {
      const canvas = document.getElementById("oddsChart");
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      const ctx = canvas.getContext("2d");
      ctx.scale(dpr, dpr);
      const width = rect.width;
      const height = rect.height;
      const pad = { left: 48, right: 16, top: 18, bottom: 54 };
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#181f26";
      ctx.fillRect(0, 0, width, height);
      ctx.font = "12px Inter, system-ui, sans-serif";
      ctx.textBaseline = "middle";
      for (let y = 0; y <= 100; y += 20) {
        const py = pad.top + (100 - y) / 100 * (height - pad.top - pad.bottom);
        ctx.strokeStyle = "#26313c";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(pad.left, py);
        ctx.lineTo(width - pad.right, py);
        ctx.stroke();
        ctx.fillStyle = "#9cacbb";
        ctx.textAlign = "right";
        ctx.fillText(`${y}¢`, pad.left - 8, py);
      }
      const allTimes = [...latestHistory, ...latestPolyHistory]
        .map(item => new Date(item.timestamp_utc).valueOf())
        .filter(Number.isFinite);
      const minTime = allTimes.length ? Math.min(...allTimes) : 0;
      const maxTime = allTimes.length ? Math.max(...allTimes) : 0;
      const xForTime = ts => maxTime <= minTime ? pad.left : pad.left + (ts - minTime) / (maxTime - minTime) * (width - pad.left - pad.right);
      const yFor = value => pad.top + (100 - value * 100) / 100 * (height - pad.top - pad.bottom);

      function drawSeries(history, color) {
        ctx.strokeStyle = color;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        let started = false;
        history.forEach(item => {
          const value = item.tradable_yes_price ?? item.yes_ask;
          const ts = new Date(item.timestamp_utc).valueOf();
          if (value === null || value === undefined || !Number.isFinite(ts)) return;
          const x = xForTime(ts);
          const y = yFor(value);
          if (!started) {
            ctx.moveTo(x, y);
            started = true;
          } else {
            ctx.lineTo(x, y);
          }
        });
        if (started) ctx.stroke();
      }
      drawSeries(latestHistory, "#7db7ff");
      drawSeries(latestPolyHistory, "#ffb86b");

      const points = allTimes.length;
      const tickCount = Math.min(6, Math.max(2, points));
      if (points > 0) {
        ctx.fillStyle = "#9cacbb";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        for (let i = 0; i < tickCount; i++) {
          const ts = tickCount === 1 ? minTime : minTime + i * (maxTime - minTime) / (tickCount - 1);
          const x = xForTime(ts);
          const time = new Date(ts).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit"
          });
          ctx.strokeStyle = "#34414e";
          ctx.beginPath();
          ctx.moveTo(x, height - pad.bottom);
          ctx.lineTo(x, height - pad.bottom + 5);
          ctx.stroke();
          ctx.fillText(time, x, height - pad.bottom + 10);
        }
      }
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "#7db7ff";
      ctx.fillRect(pad.left, height - 14, 14, 3);
      ctx.fillStyle = "#e9eef4";
      ctx.fillText("Kalshi YES ask", pad.left + 20, height - 14);
      ctx.fillStyle = "#ffb86b";
      ctx.fillRect(pad.left + 132, height - 14, 14, 3);
      ctx.fillStyle = "#e9eef4";
      ctx.fillText("Polymarket YES ask", pad.left + 152, height - 14);
      if (!latestHistory.length && !latestPolyHistory.length) {
        ctx.fillStyle = "#9cacbb";
        ctx.textAlign = "center";
        ctx.fillText("Waiting for odds history", width / 2, height / 2);
      }
    }
    function drawSourceChart() {
      const canvas = document.getElementById("sourceChart");
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      const ctx = canvas.getContext("2d");
      ctx.scale(dpr, dpr);
      const width = rect.width;
      const height = rect.height;
      const pad = { left: 58, right: 18, top: 18, bottom: 54 };
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#181f26";
      ctx.fillRect(0, 0, width, height);
      ctx.font = "12px Inter, system-ui, sans-serif";
      const values = [];
      latestSourceHistory.forEach(item => {
        ["kalshi_price", "kalshi_60_sma", "polymarket_price", "kalshi_target", "polymarket_target"].forEach(key => {
          const value = Number(item[key]);
          if (Number.isFinite(value)) values.push(value);
        });
      });
      if (!values.length) {
        ctx.fillStyle = "#9cacbb";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("Waiting for source prices", width / 2, height / 2);
        return;
      }
      const minValue = Math.min(...values);
      const maxValue = Math.max(...values);
      const span = Math.max(10, maxValue - minValue);
      const yMin = minValue - span * 0.15;
      const yMax = maxValue + span * 0.15;
      const times = latestSourceHistory
        .map(item => new Date(item.timestamp_utc).valueOf())
        .filter(Number.isFinite);
      const minTime = Math.min(...times);
      const maxTime = Math.max(...times);
      const xForTime = ts => maxTime <= minTime ? pad.left : pad.left + (ts - minTime) / (maxTime - minTime) * (width - pad.left - pad.right);
      const yFor = value => pad.top + (yMax - value) / (yMax - yMin) * (height - pad.top - pad.bottom);
      for (let i = 0; i <= 4; i++) {
        const value = yMin + i * (yMax - yMin) / 4;
        const y = yFor(value);
        ctx.strokeStyle = "#26313c";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();
        ctx.fillStyle = "#9cacbb";
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        ctx.fillText(`$${Math.round(value).toLocaleString()}`, pad.left - 8, y);
      }
      function drawLine(key, color, dashed = false) {
        ctx.strokeStyle = color;
        ctx.lineWidth = dashed ? 1.6 : 2.4;
        ctx.setLineDash(dashed ? [6, 5] : []);
        ctx.beginPath();
        let started = false;
        latestSourceHistory.forEach(item => {
          const value = Number(item[key]);
          const ts = new Date(item.timestamp_utc).valueOf();
          if (!Number.isFinite(value) || !Number.isFinite(ts)) return;
          const x = xForTime(ts);
          const y = yFor(value);
          if (!started) {
            ctx.moveTo(x, y);
            started = true;
          } else {
            ctx.lineTo(x, y);
          }
        });
        if (started) ctx.stroke();
        ctx.setLineDash([]);
      }
      function drawHorizontal(value, color) {
        const target = Number(value);
        if (!Number.isFinite(target)) return;
        const y = yFor(target);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.8;
        ctx.setLineDash([6, 5]);
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      drawLine("kalshi_price", "#7db7ff");
      drawLine("kalshi_60_sma", "#31c48d");
      drawLine("polymarket_price", "#ffb86b");
      const latestSource = latestSourceHistory.at(-1) || {};
      drawHorizontal(latestSource.kalshi_target, "#7db7ff");
      drawHorizontal(latestSource.polymarket_target, "#ffb86b");
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "#7db7ff";
      ctx.fillRect(pad.left, height - 28, 14, 3);
      ctx.fillStyle = "#e9eef4";
      ctx.fillText("BRTI spot", pad.left + 20, height - 28);
      ctx.fillStyle = "#31c48d";
      ctx.fillRect(pad.left + 112, height - 28, 14, 3);
      ctx.fillStyle = "#e9eef4";
      ctx.fillText("BRTI 60-SMA", pad.left + 132, height - 28);
      ctx.fillStyle = "#ffb86b";
      ctx.fillRect(pad.left + 222, height - 28, 14, 3);
      ctx.fillStyle = "#e9eef4";
      ctx.fillText("Polymarket", pad.left + 242, height - 28);
      ctx.strokeStyle = "#9cacbb";
      ctx.setLineDash([6, 5]);
      ctx.beginPath();
      ctx.moveTo(pad.left, height - 12);
      ctx.lineTo(pad.left + 14, height - 12);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#e9eef4";
      ctx.fillText("target", pad.left + 20, height - 12);
    }
    function updateClock() {
      document.getElementById("clock").textContent = `Now: ${new Date().toLocaleString()}`;
    }
    async function refresh() {
      const response = await fetch("/api/state", { cache: "no-store" });
      const state = await response.json();
      const snap = state.snapshot || {};
      const poly = state.polymarket_snapshot || {};
      latestHistory = state.history || [];
      latestPolyHistory = state.polymarket_history || [];
      latestSourceHistory = state.source_history || [];
      document.getElementById("title").textContent = snap.title || "Waiting for first market snapshot";
      document.getElementById("ticker").textContent = `Kalshi: ${state.active_ticker || "--"}`;
      document.getElementById("polyTicker").textContent = `Polymarket: ${state.polymarket_slug || "--"}`;
      document.getElementById("timing").textContent = `Last fetch: ${localTime(state.last_fetch_at)} | every ${state.poll_seconds}s`;
      const latestSource = latestSourceHistory.at(-1) || {};
      document.getElementById("yesMid").textContent = fmtPct(snap.yes_ask);
      document.getElementById("polyYesMid").textContent = fmtPct(poly.yes_ask);
      document.getElementById("brtiPrice").textContent = fmtUsd(latestSource.kalshi_price);
      document.getElementById("brtiAvg").textContent = fmtUsdWithSamples(
        latestSource.kalshi_60_sma,
        latestSource.kalshi_60_sma_sample_count
      );
      document.getElementById("polySourcePrice").textContent = fmtUsd(latestSource.polymarket_price);
      document.getElementById("arbKnp").textContent = fmtPct(priceSum(snap.yes_ask, poly.no_ask));
      document.getElementById("arbNkp").textContent = fmtPct(priceSum(snap.no_ask, poly.yes_ask));
      document.getElementById("yesBidAsk").textContent = `${fmtPct(snap.yes_bid)} / ${fmtPct(snap.yes_ask)}`;
      document.getElementById("polyYesBidAsk").textContent = `${fmtPct(poly.yes_bid)} / ${fmtPct(poly.yes_ask)}`;
      document.getElementById("noBidAsk").textContent = `${fmtPct(snap.no_bid)} / ${fmtPct(snap.no_ask)}`;
      document.getElementById("polyNoBidAsk").textContent = `${fmtPct(poly.no_bid)} / ${fmtPct(poly.no_ask)}`;
      document.getElementById("volume").textContent = fmtNum(snap.volume);
      document.getElementById("closeTime").textContent = localTime(snap.close_time);
      document.getElementById("snapshotTable").innerHTML = [
        row("Kalshi event", snap.event_ticker || "--"),
        row("Kalshi open interest", fmtNum(snap.open_interest)),
        row("Kalshi last price", fmtPct(snap.last_price)),
        row("Polymarket title", poly.title || "--"),
        row("Polymarket open interest", fmtNum(poly.open_interest)),
        row("Polymarket last price", fmtPct(poly.last_price)),
        row("Kalshi BRTI target", fmtUsd(latestSource.kalshi_target)),
        row("BRTI 60-SMA", fmtUsdWithSamples(latestSource.kalshi_60_sma, latestSource.kalshi_60_sma_sample_count)),
        row("Polymarket target", fmtUsd(latestSource.polymarket_target)),
        row("CSV directory", state.data_dir),
        row("Depth", state.orderbook_depth)
      ].join("");
      document.getElementById("error").textContent = [
        state.error ? `Kalshi fetch issue: ${state.error}` : "",
        state.polymarket_error ? `Polymarket fetch issue: ${state.polymarket_error}` : ""
      ].filter(Boolean).join("\n");
      drawChart();
      drawSourceChart();
    }
    updateClock();
    refresh().catch(console.error);
    setInterval(updateClock, 1000);
    setInterval(() => refresh().catch(console.error), 2000);
    window.addEventListener("resize", () => {
      drawChart();
      drawSourceChart();
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(INDEX_HTML.encode("utf-8"))
            return
        if parsed.path == "/api/state":
            body = json.dumps(state_payload()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{iso_utc()} {self.address_string()} {fmt % args}")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    thread = threading.Thread(target=polling_loop, daemon=True)
    thread.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Kalshi BTC 15m monitor listening on http://{HOST}:{PORT}")
    print(f"Polling {SERIES_TICKER} every {POLL_SECONDS:g}s; writing CSV files to {DATA_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
