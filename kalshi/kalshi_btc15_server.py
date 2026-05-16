#!/usr/bin/env python3
import base64
import csv
import html
import json
import os
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
DATA_DIR = Path(os.getenv("KALSHI_DATA_DIR", "kalshi_btc15m_data"))


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
    "last_fetch_at": None,
    "next_fetch_at": None,
    "error": None,
    "polymarket_error": None,
    "consecutive_errors": 0,
}


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
        "ask_levels": yes_ask_levels,
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
        "yes_levels_json",
        "no_levels_json",
    ]
    row = {key: snapshot.get(key, "") for key in fields}
    row["yes_levels_json"] = json.dumps(snapshot.get("yes_levels", []), separators=(",", ":"))
    row["no_levels_json"] = json.dumps(snapshot.get("no_levels", []), separators=(",", ":"))
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
    append_snapshot_csv(snapshot)
    polymarket_market = None
    polymarket_orderbook = None
    polymarket_snapshot = None
    polymarket_error = None
    try:
        polymarket_market = discover_polymarket_market(market)
        if not polymarket_market:
            raise RuntimeError("No matching open Polymarket market found")
        slug = polymarket_market.get("slug")
        if not slug:
            raise RuntimeError("Matching Polymarket market has no slug")
        polymarket_orderbook = polymarket_clob_orderbooks(polymarket_market)
        polymarket_snapshot = make_polymarket_snapshot(polymarket_market, polymarket_orderbook)
        append_snapshot_csv(polymarket_snapshot, prefix="polymarket_")
    except Exception as exc:
        polymarket_error = f"{type(exc).__name__}: {exc}"
    with STATE_LOCK:
        if STATE.get("active_ticker") != ticker:
            STATE["history"] = []
            STATE["polymarket_history"] = []
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
                }
            )
            STATE["polymarket_history"] = STATE["polymarket_history"][-720:]


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
    .chart-wrap { height: 360px; }
    canvas { width: 100%; height: 100%; display: block; }
    table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
    th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: right; font-size: 13px; }
    th:first-child, td:first-child { text-align: left; }
    th { color: var(--muted); font-weight: 600; }
    .books { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .exchange-book { min-width: 0; }
    .book-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .error { color: var(--red); white-space: pre-wrap; }
    @media (max-width: 1000px) {
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .grid, .books, .book-pair { grid-template-columns: 1fr; }
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
      <div class="metric"><div class="label">Kalshi YES midpoint</div><div class="value" id="yesMid">--</div></div>
      <div class="metric"><div class="label">Polymarket YES midpoint</div><div class="value" id="polyYesMid">--</div></div>
      <div class="metric"><div class="label">Kalshi YES bid / ask</div><div class="value small-value" id="yesBidAsk">--</div></div>
      <div class="metric"><div class="label">Polymarket YES bid / ask</div><div class="value small-value" id="polyYesBidAsk">--</div></div>
      <div class="metric"><div class="label">Kalshi NO bid / ask</div><div class="value small-value" id="noBidAsk">--</div></div>
      <div class="metric"><div class="label">Polymarket NO bid / ask</div><div class="value small-value" id="polyNoBidAsk">--</div></div>
      <div class="metric"><div class="label">Kalshi Volume</div><div class="value small-value" id="volume">--</div></div>
      <div class="metric"><div class="label">Close time</div><div class="value small-value" id="closeTime">--</div></div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2>Odds Over Time</h2>
        <div class="chart-wrap"><canvas id="oddsChart"></canvas></div>
      </div>
      <div class="panel">
        <h2>Current Snapshot</h2>
        <table>
          <tbody id="snapshotTable"></tbody>
        </table>
        <p class="error" id="error"></p>
      </div>
    </section>
    <section class="panel">
        <h2>Order Books</h2>
      <div class="books">
        <div class="exchange-book">
          <h2>Kalshi</h2>
          <div class="book-pair">
            <div>
              <h2>YES bids</h2>
              <table><thead><tr><th>Price</th><th>Contracts</th></tr></thead><tbody id="yesBook"></tbody></table>
            </div>
            <div>
              <h2>NO bids</h2>
              <table><thead><tr><th>Price</th><th>Contracts</th></tr></thead><tbody id="noBook"></tbody></table>
            </div>
          </div>
        </div>
        <div class="exchange-book">
          <h2>Polymarket</h2>
          <div class="book-pair">
            <div>
              <h2>YES bids</h2>
              <table><thead><tr><th>Price</th><th>Contracts</th></tr></thead><tbody id="polyYesBook"></tbody></table>
            </div>
            <div>
              <h2>NO bids</h2>
              <table><thead><tr><th>Price</th><th>Contracts</th></tr></thead><tbody id="polyNoBook"></tbody></table>
            </div>
          </div>
        </div>
      </div>
    </section>
  </main>
  <script>
    const fmtPct = value => value === null || value === undefined ? "--" : `${(value * 100).toFixed(1)}¢`;
    const fmtNum = value => value === null || value === undefined || value === "" ? "--" : Number(value).toLocaleString();
    let latestHistory = [];
    let latestPolyHistory = [];

    function row(label, value) {
      return `<tr><td>${label}</td><td>${value}</td></tr>`;
    }
    function bookRows(levels) {
      if (!Array.isArray(levels) || levels.length === 0) return `<tr><td colspan="2">No levels</td></tr>`;
      return levels.slice(0, 10).map(level => {
        const price = Number(level[0]);
        const qty = Number(level[1]);
        return `<tr><td>${Number.isFinite(price) ? (price * 100).toFixed(1) + "¢" : "--"}</td><td>${Number.isFinite(qty) ? qty.toLocaleString() : "--"}</td></tr>`;
      }).join("");
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
          const value = item.yes_mid;
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
      ctx.fillText("Kalshi YES mid", pad.left + 20, height - 14);
      ctx.fillStyle = "#ffb86b";
      ctx.fillRect(pad.left + 132, height - 14, 14, 3);
      ctx.fillStyle = "#e9eef4";
      ctx.fillText("Polymarket YES mid", pad.left + 152, height - 14);
      if (!latestHistory.length && !latestPolyHistory.length) {
        ctx.fillStyle = "#9cacbb";
        ctx.textAlign = "center";
        ctx.fillText("Waiting for odds history", width / 2, height / 2);
      }
    }
    function updateClock() {
      document.getElementById("clock").textContent = `Now: ${new Date().toLocaleString()}`;
    }
    async function refresh() {
      const response = await fetch("/api/state", { cache: "no-store" });
      const state = await response.json();
      const snap = state.snapshot || {};
      const poly = state.polymarket_snapshot || {};
      document.getElementById("title").textContent = snap.title || "Waiting for first market snapshot";
      document.getElementById("ticker").textContent = `Kalshi: ${state.active_ticker || "--"}`;
      document.getElementById("polyTicker").textContent = `Polymarket: ${state.polymarket_slug || "--"}`;
      document.getElementById("timing").textContent = `Last fetch: ${localTime(state.last_fetch_at)} | every ${state.poll_seconds}s`;
      document.getElementById("yesMid").textContent = fmtPct(snap.yes_mid);
      document.getElementById("polyYesMid").textContent = fmtPct(poly.yes_mid);
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
        row("CSV directory", state.data_dir),
        row("Depth", state.orderbook_depth)
      ].join("");
      document.getElementById("error").textContent = [
        state.error ? `Kalshi fetch issue: ${state.error}` : "",
        state.polymarket_error ? `Polymarket fetch issue: ${state.polymarket_error}` : ""
      ].filter(Boolean).join("\n");
      const yesLevels = Array.isArray(snap.yes_levels) ? [...snap.yes_levels].sort((a, b) => Number(b[0]) - Number(a[0])) : [];
      document.getElementById("yesBook").innerHTML = bookRows(yesLevels);
      document.getElementById("noBook").innerHTML = bookRows(snap.no_levels);
      const polyYesLevels = Array.isArray(poly.yes_levels) ? [...poly.yes_levels].sort((a, b) => Number(b[0]) - Number(a[0])) : [];
      const polyNoLevels = Array.isArray(poly.no_levels) ? [...poly.no_levels].sort((a, b) => Number(b[0]) - Number(a[0])) : [];
      document.getElementById("polyYesBook").innerHTML = bookRows(polyYesLevels);
      document.getElementById("polyNoBook").innerHTML = bookRows(polyNoLevels);
      latestHistory = state.history || [];
      latestPolyHistory = state.polymarket_history || [];
      drawChart();
    }
    updateClock();
    refresh().catch(console.error);
    setInterval(updateClock, 1000);
    setInterval(() => refresh().catch(console.error), 2000);
    window.addEventListener("resize", drawChart);
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
