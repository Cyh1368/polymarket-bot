#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import concurrent.futures
import csv
import html
import json
import math
import os
import re
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import joblib
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent

BASE_URL = os.getenv("KALSHI_API_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")
KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "wss://external-api-ws.kalshi.com/trade-api/ws/v2")
SERIES_TICKER = os.getenv("KALSHI_SERIES_TICKER", "KXBTC15M")
POLYMARKET_BASE_URL = os.getenv("POLYMARKET_API_BASE_URL", "https://gateway.polymarket.us/v1")
POLYMARKET_GAMMA_URL = os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
POLYMARKET_CLOB_URL = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
POLYMARKET_MARKET_WS_URL = os.getenv(
    "POLYMARKET_MARKET_WS_URL",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
)
POLYMARKET_RTDS_URL = os.getenv("POLYMARKET_RTDS_URL", "wss://ws-live-data.polymarket.com")
POLYMARKET_RTDS_SYMBOL = os.getenv("POLYMARKET_RTDS_SYMBOL", "btc/usd")
POLYMARKET_MARKET_SLUG = os.getenv("POLYMARKET_MARKET_SLUG", "").strip()
POLYMARKET_SEARCH_QUERY = os.getenv("POLYMARKET_SEARCH_QUERY", "Bitcoin Up or Down")
POLYMARKET_TARGET_MAX_DISTANCE_SECONDS = float(os.getenv("POLYMARKET_TARGET_MAX_DISTANCE_SECONDS", "1"))
KRAKEN_API_URL = os.getenv("KRAKEN_API_URL", "https://api.kraken.com")
KRAKEN_PAIR = os.getenv("KRAKEN_PAIR", "XBTUSD")
BRTI_PRICE_URL = os.getenv("BRTI_PRICE_URL", "https://www.cfbenchmarks.com/data/indices/BRTI")

DEFAULT_MODEL_DIR = ROOT / "kp-0529-research" / "horizon_models"
DEFAULT_CSV_DIR = ROOT / "kalshi_btc15m_data"
TRADER_LOG_PATH = ROOT / "trader_log.txt"
CONCISE_LOG_PATH = ROOT / "concise_trader_log.txt"

CONTRACT_SECONDS = 15 * 60
BALANCE_MIDPOINT_SECONDS_TO_EXPIRY = CONTRACT_SECONDS / 2
WINDOW_SECONDS = 60
MODEL_SAMPLE_INTERVAL_SECONDS = 2
MODEL_WINDOW_SAMPLE_COUNT = WINDOW_SECONDS // MODEL_SAMPLE_INTERVAL_SECONDS
MODEL_WARMUP_SAMPLE_COUNT = 30
MIN_WINDOW_ROWS = 10
MAX_ASOF_GAP_SECONDS = 10.0
ENTRY_STRATEGY_NAME = "any_2_1_latch_hold"
LATCH_HOLD_ENTRY_HORIZONS = {"2m", "1m"}
KALSHI_MIN_QUOTE_SPREAD = 0.001
ORDERBOOK_DEPTH = int(os.getenv("ORDERBOOK_DEPTH", "10"))
KALSHI_LOCAL_BOOK_RESYNC_SECONDS = 30.0
WEBSOCKET_REPORT_INTERVAL = 0.5
WEBSOCKET_STALE_SECONDS = 5.0
STATUS_LOG_INTERVAL_SECONDS = 10.0

KALSHI_FEE_RATE = 0.07
POLYMARKET_FEE_RATE = 0.05
POLYMARKET_MIN_ORDER_NOTIONAL = 1.0
KALSHI_ORDER_VERIFY_ATTEMPTS = 5
KALSHI_ORDER_VERIFY_DELAY_SECONDS = 0.25
POLYMARKET_SELL_BALANCE_ATTEMPTS = 8
POLYMARKET_SELL_BALANCE_DELAY_SECONDS = 0.75
MIN_ENTRY_SECONDS_TO_EXPIRY = float(os.getenv("MIN_ENTRY_SECONDS_TO_EXPIRY", "30"))
ENTRY_MISSING_LEG_RETRY_ATTEMPTS = int(os.getenv("ENTRY_MISSING_LEG_RETRY_ATTEMPTS", "10"))
ENTRY_MISSING_LEG_RETRY_DELAY_SECONDS = float(os.getenv("ENTRY_MISSING_LEG_RETRY_DELAY_SECONDS", "0.5"))
EMERGENCY_EXIT_MAX_CHUNK_CONTRACTS = max(1, int(os.getenv("EMERGENCY_EXIT_MAX_CHUNK_CONTRACTS", "1")))
KALSHI_MIN_ORDER_CONTRACTS = max(1, int(os.getenv("KALSHI_MIN_ORDER_CONTRACTS", "1")))
POLYMARKET_MIN_ORDER_CONTRACTS = max(1, int(os.getenv("POLYMARKET_MIN_ORDER_CONTRACTS", "1")))
POLYMARKET_MIN_EXIT_CONTRACTS = max(1, int(os.getenv("POLYMARKET_MIN_EXIT_CONTRACTS", str(POLYMARKET_MIN_ORDER_CONTRACTS))))

HORIZONS: dict[str, int] = {
    "5m": 5 * 60,
    "3m": 3 * 60,
    "2m": 2 * 60,
    "1m": 1 * 60,
}

AGG_STATS = ("last", "mean", "std", "min", "max", "range", "change")
FEATURE_NAMES = [
    "price_spread",
    "price_spread_abs",
    "kalshi_distance_to_target",
    "polymarket_distance_to_target",
    "spread_vs_distance_ratio",
    "feeds_on_same_side",
    "elapsed_fraction",
    "time_to_close_seconds",
    "kalshi_bid_ask_spread_yes",
    "kalshi_order_book_imbalance",
    "kalshi_yes_mid",
    "kalshi_last_price",
    "polymarket_bid_ask_spread_yes",
    "polymarket_order_book_imbalance",
    "polymarket_yes_mid",
    "implied_prob_spread",
    "k_plus_np",
    "nk_plus_p",
    "arb_available",
    "price_spread_roll10_std",
    "kalshi_btc_price_roll10_mean",
    "kalshi_btc_price_roll10_std",
    "kalshi_btc_price_lag5",
    "kalshi_btc_price_lag10",
    "kalshi_btc_price_momentum_5",
    "kalshi_btc_price_momentum_10",
    "implied_prob_spread_roll10_std",
    "polymarket_error_flag",
    "price_spread_abs_x_elapsed_fraction",
    "spread_vs_distance_ratio_x_elapsed_fraction",
    "feeds_on_same_side_x_elapsed_fraction",
]
ENTRY_COST_FEATURES = [
    "k_yes_p_no_entry_cost",
    "k_yes_p_no_kalshi_fee",
    "k_yes_p_no_polymarket_fee",
    "k_yes_p_no_total_fee",
    "k_yes_p_no_all_in_cost",
    "k_yes_p_no_fee_adjusted_edge",
    "k_no_p_yes_entry_cost",
    "k_no_p_yes_kalshi_fee",
    "k_no_p_yes_polymarket_fee",
    "k_no_p_yes_total_fee",
    "k_no_p_yes_all_in_cost",
    "k_no_p_yes_fee_adjusted_edge",
    "best_raw_entry_cost",
    "best_total_fee",
    "best_all_in_cost",
    "fee_adjusted_edge",
    "best_entry_cost",
    "entry_edge",
]

CSV_FIELDS = [
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
    "k_plus_np_kalshi_fee",
    "k_plus_np_polymarket_fee",
    "k_plus_np_total_fee",
    "k_plus_np_all_in_cost",
    "k_plus_np_fee_adjusted_edge",
    "nk_plus_p_kalshi_fee",
    "nk_plus_p_polymarket_fee",
    "nk_plus_p_total_fee",
    "nk_plus_p_all_in_cost",
    "nk_plus_p_fee_adjusted_edge",
    "best_arb_direction",
    "best_arb_raw_cost",
    "best_arb_total_fee",
    "best_arb_all_in_cost",
    "best_arb_fee_adjusted_edge",
    "best_arb_profitable",
    "profit_margin",
    "tradable",
    "strategy_name",
    "latch_horizon",
    "latch_action",
    "last_model_horizon",
    "last_diverge_prob",
    "last_diverge_threshold",
    "active_trade",
    "polymarket_error",
]

BALANCE_CSV_FILENAME = "cli_trader_v2_balances.csv"
BALANCE_CSV_FIELDS = [
    "timestamp_utc",
    "balance_event",
    "seconds_to_expiry",
    "kalshi_ticker",
    "kalshi_close_time",
    "polymarket_ticker",
    "polymarket_close_time",
    "kalshi_target",
    "polymarket_target",
    "kalshi_balance",
    "kalshi_available_balance",
    "kalshi_error",
    "polymarket_balance",
    "polymarket_allowance",
    "polymarket_error",
    "total_balance",
    "balance_complete",
]

MarketState = tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]

KALSHI_MARKET_CACHE: dict[str, dict[str, Any]] = {}
POLYMARKET_MARKET_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
POLYMARKET_TARGET_CACHE: dict[str, float] = {}
SOURCE_PRICE_CACHE: dict[str, float] = {}
SOURCE_HISTORY: deque[dict[str, Any]] = deque(maxlen=720)


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    pending_key: str | None = None
    pending_value: list[str] = []
    for raw_line in path.read_text().splitlines():
        if pending_key:
            pending_value.append(raw_line)
            if "END " in raw_line and "PRIVATE KEY" in raw_line:
                os.environ.setdefault(pending_key, "\n".join(pending_value))
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
        if key:
            os.environ.setdefault(key, value.replace("\\n", "\n"))


load_dotenv()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append_log(
    message: str,
    *,
    concise: bool = False,
    print_stdout: bool = True,
    prefix_timestamp: bool = True,
) -> None:
    line = f"{iso_utc()} | {message}" if prefix_timestamp else message
    TRADER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRADER_LOG_PATH.open("a") as file_obj:
        file_obj.write(line + "\n")
    if concise:
        with CONCISE_LOG_PATH.open("a") as file_obj:
            file_obj.write(line + "\n")
    if print_stdout:
        print(line, flush=True)


def fmt_price(value: Any, places: int = 4) -> str:
    number = finite_float(value)
    if number is None:
        return "--"
    return f"{number:.{places}f}"


def fmt_price_delta(value: Any, target: Any, places: int = 2) -> str:
    number = finite_float(value)
    target_number = finite_float(target)
    if number is None or target_number is None:
        return "--"
    delta = number - target_number
    sign = "+" if delta >= 0 else "-"
    return f"{sign} {abs(delta):.{places}f}"


def fmt_cents(value: Any) -> str:
    number = finite_float(value)
    if number is None:
        return "--"
    return f"{number * 100:.1f}c"


def fmt_money(value: Any) -> str:
    number = finite_float(value)
    if number is None:
        return "--"
    return f"${number:.4f}"


def json_safe_value(value: Any) -> Any:
    number = finite_float(value)
    if number is not None:
        return number
    if value in (None, ""):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def as_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def finite_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


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
    valid = plausible_btc_price(value)
    if valid is not None:
        SOURCE_PRICE_CACHE[key] = valid
        return valid
    return SOURCE_PRICE_CACHE.get(key)


def cents(value: float) -> int:
    return max(1, min(99, int(round(value * 100))))


def safe_filename(value: Any) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in str(value or "unknown"))


def parse_ts(value: Any) -> float:
    if value in (None, ""):
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


def invert_price(value: float | None) -> float | None:
    if value is None:
        return None
    return round(1.0 - value, 10)


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


def kalshi_ws_headers() -> dict[str, str]:
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
    signing_payload = f"{timestamp}GET/trade-api/ws/v2".encode()
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


def http_json(
    method: str,
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
    auth: bool = False,
    params: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    query = f"?{urlencode(params, doseq=True)}" if params else ""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "User-Agent": "btc15m-cli-trader-v2/1.0"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if auth:
        headers.update(auth_headers(method, path))
    req = Request(f"{base_url.rstrip('/')}{path}{query}", data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code} {detail}") from exc


def public_get(base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
    return http_json("GET", base_url, path, params=params, auth=False)


def kalshi_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return http_json("GET", BASE_URL, path, params=params, auth=False)
    except RuntimeError as exc:
        if "HTTP 401" not in str(exc):
            raise
        return http_json("GET", BASE_URL, path, params=params, auth=True)


def gamma_get(path: str, params: dict[str, Any] | None = None) -> Any:
    return public_get(POLYMARKET_GAMMA_URL, path, params=params)


def polymarket_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return public_get(POLYMARKET_BASE_URL, path, params=params)


def clob_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return public_get(POLYMARKET_CLOB_URL, path, params=params)


def public_text(url: str, timeout: float = 2.0) -> str:
    headers = {"Accept": "text/html,application/json", "User-Agent": "btc15m-cli-trader-v2/1.0"}
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


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
    try:
        return cached_source_price("brti", parse_price_text(public_text(BRTI_PRICE_URL)))
    except Exception:
        return cached_source_price("brti", None)


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
            snapshot = rtds_snapshot_from_message(raw)
            if snapshot:
                return snapshot
    return []


def fetch_polymarket_rtds_snapshot() -> list[dict[str, Any]]:
    try:
        return asyncio.run(polymarket_rtds_snapshot_async())
    except RuntimeError:
        return []
    except Exception:
        return []


def rtds_snapshot_from_message(raw: str | bytes | dict[str, Any]) -> list[dict[str, Any]]:
    try:
        message = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(message, dict):
        return []
    topic = str(message.get("topic") or "")
    if topic and topic not in ("crypto_prices", "crypto_prices_chainlink"):
        return []
    payload = message.get("payload")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    symbol = str(payload.get("symbol") or POLYMARKET_RTDS_SYMBOL).lower()
    if symbol != POLYMARKET_RTDS_SYMBOL.lower():
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if "value" in payload:
        return [payload]
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
        distance = abs(int(timestamp) - target_ms)
        if best is None or distance < best[0]:
            best = (distance, price)
    if best is None or best[0] > int(POLYMARKET_TARGET_MAX_DISTANCE_SECONDS * 1000):
        return None
    return best[1]


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


def kalshi_brti_60_sma(kalshi_price: float | None) -> tuple[float | None, int]:
    samples: list[float] = []
    for item in list(SOURCE_HISTORY)[-59:]:
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
    rtds_snapshot: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    timestamp_utc = iso_utc()
    kalshi_current = fetch_brti_price() or plausible_btc_price(
        numeric_value(kalshi_market.get("expiration_value"))
    )
    if kalshi_current is not None:
        cached_source_price("brti", kalshi_current)

    snapshot = rtds_snapshot if rtds_snapshot is not None else fetch_polymarket_rtds_snapshot()
    polymarket_current = (
        polymarket_rtds_latest_price(snapshot)
        or plausible_btc_price(event_metadata_value(polymarket_market, "currentPrice", "current_price"))
        or fetch_kraken_price()
        or plausible_btc_price(event_metadata_value(polymarket_market, "finalPrice", "final_price"))
    )

    target_key = polymarket_market_key(polymarket_market)
    polymarket_target = POLYMARKET_TARGET_CACHE.get(target_key) if target_key else None
    if polymarket_target is None:
        polymarket_target = polymarket_rtds_price_at_from_snapshot(
            snapshot,
            polymarket_start_timestamp(polymarket_market),
        )
        if polymarket_target is not None and target_key:
            POLYMARKET_TARGET_CACHE[target_key] = polymarket_target

    kalshi_60_sma, kalshi_60_sma_count = kalshi_brti_60_sma(kalshi_current)
    out = {
        "timestamp_utc": timestamp_utc,
        "kalshi_price": kalshi_current,
        "kalshi_target": numeric_value(kalshi_market.get("floor_strike")),
        "kalshi_60_sma": kalshi_60_sma,
        "kalshi_60_sma_sample_count": kalshi_60_sma_count,
        "polymarket_price": polymarket_current,
        "polymarket_target": polymarket_target,
    }
    SOURCE_HISTORY.append(out)
    return out


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


def discover_active_market() -> dict[str, Any] | None:
    data = kalshi_get(
        "/markets",
        {"series_ticker": SERIES_TICKER, "status": "open", "limit": 200},
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
    now = time.time()
    live_markets = [
        market
        for market in markets
        if parse_ts(market.get("close_time") or market.get("close_ts") or market.get("expiration_time")) > now
    ]
    selected = live_markets or markets
    selected.sort(
        key=lambda market: (
            parse_ts(market.get("close_time") or market.get("close_ts") or market.get("expiration_time")),
            str(market.get("ticker", "")),
        )
    )
    return selected[0]


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
    except Exception as exc:
        if "HTTP 404" in str(exc):
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
    for value in (open_ts, close_ts - CONTRACT_SECONDS if close_ts else 0, close_ts):
        if value:
            slug_epochs.append(int(value))
    for epoch in dict.fromkeys(slug_epochs):
        event = gamma_event_by_slug(f"btc-updown-15m-{epoch}")
        if event:
            return polymarket_event_market(event)

    candidates: list[dict[str, Any]] = []
    searches = [POLYMARKET_SEARCH_QUERY, "BTC price up", "Bitcoin up down", "Bitcoin 15 minutes"]
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
    return normalize_kalshi_snapshot_quotes({
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
        "best_yes_ask_qty": best_no_bid_qty,
        "best_no_ask_qty": best_yes_bid_qty,
        "yes_levels": yes_levels,
        "no_levels": no_levels,
    })


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
    best_yes_ask, best_yes_ask_qty = best_level(yes_ask_levels, reverse=False)
    best_no_bid, best_no_bid_qty = best_level(no_levels)
    best_no_ask, best_no_ask_qty = best_level(no_ask_levels, reverse=False)
    stats = (orderbook.get("marketData") or orderbook).get("stats") or {}

    yes_bid = best_yes_bid or nested_price(market.get("bestBidQuote")) or market_price(market, "bestBid", "best_bid")
    yes_ask = best_yes_ask or nested_price(market.get("bestAskQuote")) or market_price(market, "bestAsk", "best_ask")
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
        "best_yes_ask_qty": best_yes_ask_qty,
        "best_no_ask_qty": best_no_ask_qty,
        "yes_levels": yes_levels,
        "no_levels": no_levels,
        "yes_ask_levels": yes_ask_levels,
        "no_ask_levels": no_ask_levels,
    }


def token_ids_by_contract(market: dict[str, Any]) -> dict[str, str]:
    token_ids = parse_json_list(market.get("clobTokenIds"))
    outcomes = [str(outcome).lower() for outcome in parse_json_list(market.get("outcomes"))]
    if len(token_ids) < 2:
        raise RuntimeError("Polymarket market has no CLOB token ids")
    up_index = outcomes.index("up") if "up" in outcomes else 0
    down_index = outcomes.index("down") if "down" in outcomes else 1
    return {"YES": str(token_ids[up_index]), "NO": str(token_ids[down_index])}


def polymarket_clob_orderbooks(market: dict[str, Any]) -> dict[str, Any]:
    ids = token_ids_by_contract(market)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        up_future = executor.submit(clob_get, "/book", {"token_id": ids["YES"]})
        down_future = executor.submit(clob_get, "/book", {"token_id": ids["NO"]})
        return {"up": up_future.result(), "down": down_future.result()}


def cached_active_kalshi_market() -> dict[str, Any] | None:
    cached = KALSHI_MARKET_CACHE.get(SERIES_TICKER)
    if cached:
        close_ts = parse_ts(cached.get("close_time") or cached.get("close_ts"))
        if not close_ts or close_ts > time.time() + 5:
            return cached
    market = discover_active_market()
    if market:
        KALSHI_MARKET_CACHE[SERIES_TICKER] = market
    return market


def fetch_market_state() -> MarketState:
    kalshi_market = cached_active_kalshi_market()
    if not kalshi_market:
        raise RuntimeError(f"No open market found for {SERIES_TICKER}")
    cache_key = (
        str(kalshi_market.get("ticker") or ""),
        str(kalshi_market.get("close_time") or kalshi_market.get("close_ts") or ""),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        kalshi_orderbook_future = executor.submit(
            kalshi_get,
            f"/markets/{kalshi_market['ticker']}/orderbook",
            {"depth": ORDERBOOK_DEPTH},
        )
        polymarket_market = POLYMARKET_MARKET_CACHE.get(cache_key)
        poly_market_future = None if polymarket_market is not None else executor.submit(discover_polymarket_market, kalshi_market)
        kalshi_orderbook = kalshi_orderbook_future.result()
        if poly_market_future is not None:
            polymarket_market = poly_market_future.result()
            if polymarket_market is not None:
                POLYMARKET_MARKET_CACHE[cache_key] = polymarket_market
    if not polymarket_market:
        raise RuntimeError("No matching open Polymarket market found")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        poly_orderbook_future = executor.submit(polymarket_clob_orderbooks, polymarket_market)
        source_future = executor.submit(source_price_snapshot, kalshi_market, polymarket_market)
        poly_orderbook = poly_orderbook_future.result()
        source_snapshot = source_future.result()
    return (
        kalshi_market,
        make_snapshot(kalshi_market, kalshi_orderbook),
        polymarket_market,
        make_polymarket_snapshot(polymarket_market, poly_orderbook),
        source_snapshot,
    )


def level_price(level: Any) -> float | None:
    if not isinstance(level, (list, tuple)) or not level:
        return None
    return normalize_price(level[0])


def level_quantity(level: Any) -> float:
    if not isinstance(level, (list, tuple)) or len(level) < 2:
        return 0.0
    try:
        return float(level[1])
    except (TypeError, ValueError):
        return 0.0


def levels_to_book(levels: Any) -> dict[float, float]:
    book: dict[float, float] = {}
    if not isinstance(levels, list):
        return book
    for level in levels:
        price = level_price(level)
        quantity = level_quantity(level)
        if price is not None and quantity > 0:
            book[round(price, 10)] = quantity
    return book


def book_to_levels(book: dict[float, float]) -> list[list[float]]:
    return [
        [price, quantity]
        for price, quantity in sorted(book.items(), key=lambda item: item[0], reverse=True)
        if quantity > 0
    ]


def snapshot_with_kalshi_book(
    snapshot: dict[str, Any],
    yes_book: dict[float, float],
    no_book: dict[float, float],
) -> dict[str, Any]:
    yes_levels = book_to_levels(yes_book)
    no_levels = book_to_levels(no_book)
    best_yes_bid, best_yes_bid_qty = best_level(yes_levels)
    best_no_bid, best_no_bid_qty = best_level(no_levels)
    updated = dict(snapshot)
    updated["yes_levels"] = yes_levels
    updated["no_levels"] = no_levels
    updated["yes_bid"] = best_yes_bid
    updated["yes_ask"] = invert_price(best_no_bid)
    updated["no_bid"] = best_no_bid
    updated["no_ask"] = invert_price(best_yes_bid)
    updated["best_yes_bid_qty"] = best_yes_bid_qty
    updated["best_no_bid_qty"] = best_no_bid_qty
    updated["best_yes_ask_qty"] = best_no_bid_qty
    updated["best_no_ask_qty"] = best_yes_bid_qty
    if updated["yes_bid"] is not None and updated["yes_ask"] is not None:
        updated["yes_mid"] = (updated["yes_bid"] + updated["yes_ask"]) / 2.0
    elif updated["yes_bid"] is not None:
        updated["yes_mid"] = updated["yes_bid"]
    elif updated["yes_ask"] is not None:
        updated["yes_mid"] = updated["yes_ask"]
    else:
        updated["yes_mid"] = None
    return normalize_kalshi_snapshot_quotes(updated)


def non_crossed_ask(bid: Any, ask: Any, min_spread: float = KALSHI_MIN_QUOTE_SPREAD) -> float | None:
    bid_value = finite_float(bid)
    ask_value = finite_float(ask)
    if ask_value is None:
        return None
    if bid_value is None:
        return ask_value
    if ask_value < bid_value + min_spread:
        return min(1.0, bid_value + min_spread)
    return ask_value


def normalize_kalshi_snapshot_quotes(snapshot: dict[str, Any]) -> dict[str, Any]:
    updated = dict(snapshot)
    updated["yes_ask"] = non_crossed_ask(updated.get("yes_bid"), updated.get("yes_ask"))
    updated["no_ask"] = non_crossed_ask(updated.get("no_bid"), updated.get("no_ask"))
    yes_bid = finite_float(updated.get("yes_bid"))
    yes_ask = finite_float(updated.get("yes_ask"))
    if yes_bid is not None and yes_ask is not None:
        updated["yes_mid"] = (yes_bid + yes_ask) / 2.0
    elif yes_bid is not None:
        updated["yes_mid"] = yes_bid
    elif yes_ask is not None:
        updated["yes_mid"] = yes_ask
    else:
        updated["yes_mid"] = None
    return updated


def book_signature(kalshi_snapshot: dict[str, Any], polymarket_snapshot: dict[str, Any]) -> tuple[Any, ...]:
    return (
        kalshi_snapshot.get("yes_bid"),
        kalshi_snapshot.get("yes_ask"),
        kalshi_snapshot.get("no_bid"),
        kalshi_snapshot.get("no_ask"),
        polymarket_snapshot.get("yes_bid"),
        polymarket_snapshot.get("yes_ask"),
        polymarket_snapshot.get("no_bid"),
        polymarket_snapshot.get("no_ask"),
    )


def as_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def positive_float(value: Any) -> float | None:
    number = as_optional_float(value)
    if number is None or number <= 0:
        return None
    return number


def int_value(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def best_bid_ask_from_message(message: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    bid = positive_float(message.get("best_bid"))
    ask = positive_float(message.get("best_ask"))
    bid_qty = None
    ask_qty = None
    bids = message.get("bids")
    if isinstance(bids, list):
        parsed = [
            (price, as_float(item.get("size")))
            for item in bids
            if isinstance(item, dict)
            and as_optional_float(item.get("size")) not in (None, 0.0)
            and (price := positive_float(item.get("price"))) is not None
        ]
        if parsed:
            bid, bid_qty = max(parsed, key=lambda item: item[0])
    asks = message.get("asks")
    if isinstance(asks, list):
        parsed = [
            (price, as_float(item.get("size")))
            for item in asks
            if isinstance(item, dict)
            and as_optional_float(item.get("size")) not in (None, 0.0)
            and (price := positive_float(item.get("price"))) is not None
        ]
        if parsed:
            ask, ask_qty = min(parsed, key=lambda item: item[0])
    return bid, ask, bid_qty, ask_qty


def websocket_connect(websockets_module: Any, url: str, **kwargs: Any) -> Any:
    try:
        return websockets_module.connect(url, **kwargs)
    except TypeError:
        headers = kwargs.pop("additional_headers", None)
        if headers is not None:
            kwargs["extra_headers"] = headers
        return websockets_module.connect(url, **kwargs)


class AsyncMarketContext:
    def __init__(
        self,
        fetcher: Any,
        *,
        logger: Any = None,
        report_interval: float = WEBSOCKET_REPORT_INTERVAL,
        stale_seconds: float = WEBSOCKET_STALE_SECONDS,
    ) -> None:
        self.fetcher = fetcher
        self.logger = logger or (lambda _line: None)
        self.report_interval = max(0.25, report_interval)
        self.stale_seconds = max(1.0, stale_seconds)
        self.lock = asyncio.Lock()
        self.update_event = asyncio.Event()
        self._state: MarketState | None = None
        self._last_signature: tuple[Any, ...] | None = None
        self._last_source: tuple[Any, Any] | None = None
        self._kalshi_local_book: dict[str, Any] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        self.kalshi_connected = False
        self.polymarket_connected = False
        self.source_connected = False
        self.last_kalshi_update = 0.0
        self.last_polymarket_update = 0.0
        self.last_source_update = 0.0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.bootstrap("startup")
        self._tasks = [
            asyncio.create_task(self._kalshi_ws_loop(), name="kalshi-ws-v2"),
            asyncio.create_task(self._polymarket_ws_loop(), name="polymarket-ws-v2"),
            asyncio.create_task(self._source_loop(), name="source-ws-v2"),
            asyncio.create_task(self._kalshi_resync_loop(), name="kalshi-resync-v2"),
        ]

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def bootstrap(self, reason: str) -> MarketState:
        state = await asyncio.to_thread(self.fetcher)
        async with self.lock:
            self._state = state
            self._last_signature = book_signature(state[1], state[3])
            self._last_source = (state[4].get("kalshi_price"), state[4].get("polymarket_price"))
            self._reset_kalshi_local_book_from_snapshot(state[1], reason)
        self.update_event.set()
        self.logger(f"WEBSOCKET bootstrap via HTTP ({reason})")
        return state

    async def snapshot(self) -> MarketState:
        async with self.lock:
            state = self._state
        if state is None:
            return await self.bootstrap("missing snapshot")
        return state

    async def wait_for_update(self, timeout: float | None = None) -> MarketState:
        timeout = self.report_interval if timeout is None else max(0.1, timeout)
        try:
            await asyncio.wait_for(self.update_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self.update_event.clear()
        return await self.snapshot()

    def failsafe_required(self) -> bool:
        now = time.monotonic()
        return (
            not self.kalshi_connected
            or not self.polymarket_connected
            or now - self.last_kalshi_update > self.stale_seconds
            or now - self.last_polymarket_update > self.stale_seconds
        )

    async def refresh_after_event(self, reason: str) -> None:
        try:
            state = await asyncio.to_thread(self.fetcher)
        except Exception as exc:
            self.logger(f"WEBSOCKET refresh failed ({reason}): {type(exc).__name__}: {exc}")
            return
        async with self.lock:
            old_signature = self._last_signature
            old_source = self._last_source
            self._state = state
            self._last_signature = book_signature(state[1], state[3])
            self._last_source = (state[4].get("kalshi_price"), state[4].get("polymarket_price"))
            self._reset_kalshi_local_book_from_snapshot(state[1], reason)
            changed = old_signature != self._last_signature or old_source != self._last_source
        if changed:
            self.update_event.set()

    def _reset_kalshi_local_book_from_snapshot(self, snapshot: dict[str, Any], reason: str) -> None:
        ticker = str(snapshot.get("ticker") or "")
        if not ticker:
            self._kalshi_local_book = None
            return
        self._kalshi_local_book = {
            "ticker": ticker,
            "yes": levels_to_book(snapshot.get("yes_levels")),
            "no": levels_to_book(snapshot.get("no_levels")),
            "seq": None,
            "complete": True,
            "updated_at": time.monotonic(),
            "reason": reason,
        }

    async def _kalshi_ws_loop(self) -> None:
        import websockets

        backoff = 1.0
        while self._running:
            try:
                state = await self.snapshot()
                ticker = str(state[0].get("ticker") or state[1].get("ticker") or "")
                if not ticker:
                    await asyncio.sleep(backoff)
                    continue
                async with websocket_connect(
                    websockets,
                    KALSHI_WS_URL,
                    additional_headers=kalshi_ws_headers(),
                    ping_interval=20,
                    ping_timeout=10,
                    open_timeout=10,
                ) as ws:
                    self.kalshi_connected = True
                    self.last_kalshi_update = time.monotonic()
                    backoff = 1.0
                    await ws.send(
                        json.dumps(
                            {
                                "id": int(time.time()),
                                "cmd": "subscribe",
                                "params": {
                                    "channels": ["ticker", "orderbook_delta", "fill"],
                                    "market_tickers": [ticker],
                                    "use_yes_price": False,
                                },
                            }
                        )
                    )
                    async for raw in ws:
                        self.last_kalshi_update = time.monotonic()
                        if await self._handle_kalshi_message(raw):
                            self.update_event.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.kalshi_connected:
                    self.logger(f"WEBSOCKET Kalshi disconnected: {type(exc).__name__}: {exc}")
                else:
                    self.logger(f"WEBSOCKET Kalshi connect failed: {type(exc).__name__}: {exc}")
                self.kalshi_connected = False
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    async def _handle_kalshi_message(self, raw: str | bytes) -> bool:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return False
        msg_type = str(data.get("type") or data.get("event_type") or "")
        if msg_type == "error":
            self.logger(f"WEBSOCKET Kalshi error {data.get('msg')}")
            return False
        if msg_type == "fill":
            return True
        if msg_type == "ticker" and await self._apply_kalshi_ticker(data.get("msg") or data):
            return True
        if msg_type == "orderbook_snapshot":
            return await self._apply_kalshi_orderbook_snapshot(data)
        if msg_type == "orderbook_delta":
            changed = await self._apply_kalshi_orderbook_delta(data)
            if changed is None:
                await self.refresh_after_event("kalshi orderbook_delta fallback")
                return True
            return changed
        return False

    async def _apply_kalshi_ticker(self, message: dict[str, Any]) -> bool:
        yes_bid = positive_float(message.get("yes_bid_dollars") or message.get("yes_bid"))
        yes_ask = positive_float(message.get("yes_ask_dollars") or message.get("yes_ask"))
        last_price = positive_float(message.get("price_dollars") or message.get("last_price"))
        if yes_bid is None and yes_ask is None and last_price is None:
            return False
        async with self.lock:
            current = self._state
            if current is None:
                return False
            kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot = current
            updated = dict(kalshi_snapshot)
            if yes_bid is not None:
                updated["yes_bid"] = yes_bid
                updated["no_ask"] = round(1.0 - yes_bid, 10)
            if yes_ask is not None:
                updated["yes_ask"] = yes_ask
                updated["no_bid"] = round(1.0 - yes_ask, 10)
            if last_price is not None:
                updated["last_price"] = last_price
            updated = normalize_kalshi_snapshot_quotes(updated)
            old_signature = self._last_signature
            self._state = (kalshi_market, updated, polymarket_market, polymarket_snapshot, source_snapshot)
            self._last_signature = book_signature(updated, polymarket_snapshot)
            return old_signature != self._last_signature

    async def _apply_kalshi_orderbook_snapshot(self, data: dict[str, Any]) -> bool:
        message = data.get("msg") if isinstance(data.get("msg"), dict) else data
        ticker = str(message.get("market_ticker") or message.get("ticker") or "")
        yes_levels = message.get("yes_dollars_fp") or message.get("yes_dollars") or message.get("yes") or []
        no_levels = message.get("no_dollars_fp") or message.get("no_dollars") or message.get("no") or []
        async with self.lock:
            current = self._state
            if current is None:
                return False
            kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot = current
            if ticker and ticker != str(kalshi_snapshot.get("ticker") or ""):
                return False
            yes_book = levels_to_book(yes_levels)
            no_book = levels_to_book(no_levels)
            updated = snapshot_with_kalshi_book(kalshi_snapshot, yes_book, no_book)
            old_signature = self._last_signature
            self._kalshi_local_book = {
                "ticker": ticker or kalshi_snapshot.get("ticker"),
                "yes": yes_book,
                "no": no_book,
                "seq": data.get("seq"),
                "complete": True,
                "updated_at": time.monotonic(),
                "reason": "ws_snapshot",
            }
            self._state = (kalshi_market, updated, polymarket_market, polymarket_snapshot, source_snapshot)
            self._last_signature = book_signature(updated, polymarket_snapshot)
            return old_signature != self._last_signature

    async def _apply_kalshi_orderbook_delta(self, data: dict[str, Any]) -> bool | None:
        message = data.get("msg") if isinstance(data.get("msg"), dict) else data
        side = str(message.get("side") or "").lower()
        if side not in ("yes", "no"):
            return None
        price = normalize_price(message.get("price_dollars") or message.get("price_dollars_fp") or message.get("price"))
        delta = as_optional_float(message.get("delta_fp") or message.get("delta") or message.get("contracts_fp"))
        ticker = str(message.get("market_ticker") or message.get("ticker") or "")
        if price is None or delta is None:
            return None
        async with self.lock:
            current = self._state
            book = self._kalshi_local_book
            if current is None or book is None or not book.get("complete"):
                return None
            kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot = current
            current_ticker = str(kalshi_snapshot.get("ticker") or "")
            if ticker and ticker != current_ticker:
                return False
            expected_seq = int_value(book.get("seq"))
            seq = int_value(data.get("seq"))
            if expected_seq is not None and seq is not None and seq != expected_seq + 1:
                book["complete"] = False
                self.logger(f"WEBSOCKET Kalshi sequence gap expected {expected_seq + 1}, got {seq}; resyncing")
                return None
            side_book = book[side]
            price = round(price, 10)
            next_quantity = float(side_book.get(price, 0.0)) + delta
            if next_quantity <= 0:
                side_book.pop(price, None)
            else:
                side_book[price] = next_quantity
            book["seq"] = seq if seq is not None else expected_seq
            book["updated_at"] = time.monotonic()
            updated = snapshot_with_kalshi_book(kalshi_snapshot, book["yes"], book["no"])
            old_signature = self._last_signature
            self._state = (kalshi_market, updated, polymarket_market, polymarket_snapshot, source_snapshot)
            self._last_signature = book_signature(updated, polymarket_snapshot)
            return old_signature != self._last_signature

    async def _kalshi_resync_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(KALSHI_LOCAL_BOOK_RESYNC_SECONDS)
                if self.kalshi_connected:
                    await self.refresh_after_event("kalshi periodic local book resync")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger(f"WEBSOCKET Kalshi periodic resync failed: {type(exc).__name__}: {exc}")

    async def _polymarket_ws_loop(self) -> None:
        import websockets

        backoff = 1.0
        while self._running:
            try:
                state = await self.snapshot()
                token_ids = [str(token_id) for token_id in parse_json_list(state[2].get("clobTokenIds")) if token_id not in (None, "")]
                if not token_ids:
                    await asyncio.sleep(backoff)
                    continue
                async with websockets.connect(
                    POLYMARKET_MARKET_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    open_timeout=10,
                ) as ws:
                    self.polymarket_connected = True
                    self.last_polymarket_update = time.monotonic()
                    backoff = 1.0
                    await ws.send(
                        json.dumps(
                            {
                                "assets_ids": token_ids,
                                "type": "market",
                                "custom_feature_enabled": True,
                            }
                        )
                    )
                    async for raw in ws:
                        self.last_polymarket_update = time.monotonic()
                        if await self._handle_polymarket_message(raw):
                            self.update_event.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.polymarket_connected:
                    self.logger(f"WEBSOCKET Polymarket disconnected: {type(exc).__name__}: {exc}")
                else:
                    self.logger(f"WEBSOCKET Polymarket connect failed: {type(exc).__name__}: {exc}")
                self.polymarket_connected = False
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    async def _handle_polymarket_message(self, raw: str | bytes) -> bool:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return False
        messages = data if isinstance(data, list) else [data]
        changed = False
        for message in messages:
            if not isinstance(message, dict):
                continue
            event_type = str(message.get("event_type") or message.get("type") or "")
            if event_type in ("book", "price_change", "best_bid_ask"):
                changed = await self._apply_polymarket_book_message(message) or changed
            elif event_type == "last_trade_price":
                changed = await self._apply_polymarket_last_trade(message) or changed
        if changed and self._state is None:
            await self.refresh_after_event("polymarket book")
        return changed

    async def _apply_polymarket_last_trade(self, message: dict[str, Any]) -> bool:
        async with self.lock:
            current = self._state
            if current is None:
                return False
            kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot = current
            updated = dict(polymarket_snapshot)
            price = nested_price(message.get("price") or message.get("last_trade_price"))
            if price is None:
                return False
            updated["last_price"] = price
            self._state = (kalshi_market, kalshi_snapshot, polymarket_market, updated, source_snapshot)
            return True

    async def _apply_polymarket_book_message(self, message: dict[str, Any]) -> bool:
        async with self.lock:
            current = self._state
            if current is None:
                return False
            kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot = current
            token_map = self._polymarket_contract_by_token(polymarket_market)
            updated = dict(polymarket_snapshot)
            changed = False
            event_type = str(message.get("event_type") or message.get("type") or "")
            payloads = message.get("price_changes") if event_type == "price_change" else [message]
            if not isinstance(payloads, list):
                payloads = [message]
            for payload in payloads:
                if not isinstance(payload, dict):
                    continue
                contract = token_map.get(str(payload.get("asset_id") or ""))
                if contract not in ("YES", "NO"):
                    continue
                bid, ask, bid_qty, ask_qty = best_bid_ask_from_message(payload)
                prefix = contract.lower()
                for key, value in (
                    (f"{prefix}_bid", bid),
                    (f"{prefix}_ask", ask),
                    (f"best_{prefix}_bid_qty", bid_qty),
                    (f"best_{prefix}_ask_qty", ask_qty),
                ):
                    if value is not None and value != updated.get(key):
                        updated[key] = value
                        changed = True
            if not changed:
                return False
            old_signature = self._last_signature
            self._state = (kalshi_market, kalshi_snapshot, polymarket_market, updated, source_snapshot)
            self._last_signature = book_signature(kalshi_snapshot, updated)
            return old_signature != self._last_signature

    def _polymarket_contract_by_token(self, market: dict[str, Any]) -> dict[str, str]:
        token_ids = parse_json_list(market.get("clobTokenIds"))
        outcomes = [str(outcome).upper() for outcome in parse_json_list(market.get("outcomes"))]
        if not outcomes:
            outcomes = ["YES", "NO"]
        mapping: dict[str, str] = {}
        for index, token_id in enumerate(token_ids):
            outcome = outcomes[index] if index < len(outcomes) else ("YES" if index == 0 else "NO")
            if outcome == "UP":
                outcome = "YES"
            elif outcome == "DOWN":
                outcome = "NO"
            mapping[str(token_id)] = outcome
        return mapping

    async def _source_loop(self) -> None:
        import websockets

        backoff = 1.0
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
        while self._running:
            try:
                async with websockets.connect(
                    POLYMARKET_RTDS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    open_timeout=10,
                ) as ws:
                    await ws.send(subscribe_msg)
                    self.source_connected = True
                    backoff = 1.0
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.report_interval)
                        except asyncio.TimeoutError:
                            await self._refresh_source_snapshot("source heartbeat", None)
                            continue
                        self.last_source_update = time.monotonic()
                        snapshot = rtds_snapshot_from_message(raw)
                        if snapshot:
                            await self._refresh_source_snapshot("source websocket", snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.source_connected = False
                self.logger(f"WEBSOCKET source refresh failed: {type(exc).__name__}: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    async def _refresh_source_snapshot(self, reason: str, rtds_snapshot: list[dict[str, Any]] | None) -> None:
        state = await self.snapshot()
        try:
            source = await asyncio.to_thread(source_price_snapshot, state[0], state[2], rtds_snapshot)
        except Exception as exc:
            self.logger(f"WEBSOCKET source snapshot failed ({reason}): {type(exc).__name__}: {exc}")
            return
        async with self.lock:
            current = self._state
            old_source = self._last_source
            if current is None:
                return
            self._state = (current[0], current[1], current[2], current[3], source)
            self._last_source = (source.get("kalshi_price"), source.get("polymarket_price"))
            changed = old_source != self._last_source
        if changed:
            self.update_event.set()


def kalshi_fee(price: Any, contracts: float = 1.0) -> float | None:
    p = finite_float(price)
    if p is None:
        return None
    return KALSHI_FEE_RATE * contracts * p * (1.0 - p)


def polymarket_fee(price: Any, contracts: float = 1.0) -> float | None:
    p = finite_float(price)
    if p is None:
        return None
    return POLYMARKET_FEE_RATE * contracts * p * (1.0 - p)


def ask_liquidity(snapshot: dict[str, Any], contract: str) -> float:
    key = "best_yes_ask_qty" if contract.upper() == "YES" else "best_no_ask_qty"
    return as_float(snapshot.get(key))


def arbitrage_candidate(
    name: str,
    kalshi_contract: str,
    polymarket_contract: str,
    kalshi_price: Any,
    polymarket_price: Any,
    profit_margin: float,
    contracts: float = 1.0,
    kalshi_liquidity: float | None = None,
    polymarket_liquidity: float | None = None,
    min_liquidity: float | None = None,
    require_liquidity: bool = True,
) -> dict[str, Any] | None:
    k_price = finite_float(kalshi_price)
    p_price = finite_float(polymarket_price)
    if k_price is None or p_price is None:
        return None
    k_fee = kalshi_fee(k_price, contracts)
    p_fee = polymarket_fee(p_price, contracts)
    if k_fee is None or p_fee is None:
        return None
    raw_cost = k_price + p_price
    total_fee = k_fee + p_fee
    all_in_cost = raw_cost + total_fee
    edge = 1.0 - all_in_cost
    min_size = float(min_liquidity if min_liquidity is not None else contracts)
    k_liquidity = as_float(kalshi_liquidity)
    p_liquidity = as_float(polymarket_liquidity)
    liquid = k_liquidity >= min_size and p_liquidity >= min_size
    if require_liquidity and not liquid:
        return None
    return {
        "name": name,
        "kalshi_contract": kalshi_contract,
        "polymarket_contract": polymarket_contract,
        "kalshi_price": k_price,
        "polymarket_price": p_price,
        "kalshi_liquidity": k_liquidity,
        "polymarket_liquidity": p_liquidity,
        "min_liquidity": min_size,
        "liquid": liquid,
        "raw_cost": raw_cost,
        "kalshi_fee": k_fee,
        "polymarket_fee": p_fee,
        "total_fee": total_fee,
        "all_in_cost": all_in_cost,
        "fee_adjusted_edge": edge,
        "profit_margin": profit_margin,
        "profitable": edge > profit_margin,
    }


def arbitrage_candidates(
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    profit_margin: float,
    contracts: float = 1.0,
    *,
    require_liquidity: bool = True,
) -> list[dict[str, Any]]:
    candidates = [
        arbitrage_candidate(
            "K+NP",
            "YES",
            "NO",
            kalshi_snapshot.get("yes_ask"),
            polymarket_snapshot.get("no_ask"),
            profit_margin,
            contracts,
            ask_liquidity(kalshi_snapshot, "YES"),
            ask_liquidity(polymarket_snapshot, "NO"),
            contracts,
            require_liquidity,
        ),
        arbitrage_candidate(
            "NK+P",
            "NO",
            "YES",
            kalshi_snapshot.get("no_ask"),
            polymarket_snapshot.get("yes_ask"),
            profit_margin,
            contracts,
            ask_liquidity(kalshi_snapshot, "NO"),
            ask_liquidity(polymarket_snapshot, "YES"),
            contracts,
            require_liquidity,
        ),
    ]
    return [candidate for candidate in candidates if candidate is not None]


def add_pair(a: Any, b: Any) -> float | None:
    left = finite_float(a)
    right = finite_float(b)
    if left is None or right is None:
        return None
    return left + right


def bid_sum_arbitrage_features(
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
) -> dict[str, float | None]:
    return {
        "K+NP": add_pair(kalshi_snapshot.get("yes_bid"), polymarket_snapshot.get("no_bid")),
        "NK+P": add_pair(kalshi_snapshot.get("no_bid"), polymarket_snapshot.get("yes_bid")),
    }


def best_arbitrage_candidate(
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    profit_margin: float,
    contracts: float = 1.0,
    *,
    require_liquidity: bool = True,
) -> dict[str, Any] | None:
    candidates = arbitrage_candidates(
        kalshi_snapshot,
        polymarket_snapshot,
        profit_margin,
        contracts,
        require_liquidity=require_liquidity,
    )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item["all_in_cost"])
    return candidates[0]


def csv_path_for_contract(csv_dir: Path, ticker: Any) -> Path:
    csv_dir.mkdir(parents=True, exist_ok=True)
    return csv_dir / f"cli_trader_v2_{safe_filename(ticker)}.csv"


def balance_csv_path(csv_dir: Path) -> Path:
    csv_dir.mkdir(parents=True, exist_ok=True)
    return csv_dir / BALANCE_CSV_FILENAME


def append_csv_row(csv_path: Path, row: dict[str, Any]) -> None:
    exists = csv_path.exists()
    with csv_path.open("a", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})


def append_balance_csv_row(csv_path: Path, row: dict[str, Any]) -> None:
    exists = csv_path.exists()
    with csv_path.open("a", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=BALANCE_CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in BALANCE_CSV_FIELDS})


def build_csv_row(
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    source_snapshot: dict[str, Any],
    runtime: "ContractRuntime",
    profit_margin: float,
    contracts: int,
    active_position: dict[str, Any] | None,
    polymarket_error: str = "",
) -> dict[str, Any]:
    candidates = {
        item["name"]: item
        for item in arbitrage_candidates(kalshi_snapshot, polymarket_snapshot, profit_margin, contracts)
    }
    k_plus_np = candidates.get("K+NP")
    nk_plus_p = candidates.get("NK+P")
    bid_sum_features = bid_sum_arbitrage_features(kalshi_snapshot, polymarket_snapshot)
    best = best_arbitrage_candidate(kalshi_snapshot, polymarket_snapshot, profit_margin, contracts)
    last_decision = runtime.last_model_decision()
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
        "polymarket_timestamp_utc": polymarket_snapshot.get("timestamp_utc", ""),
        "polymarket_ticker": polymarket_snapshot.get("ticker", ""),
        "polymarket_title": polymarket_snapshot.get("title", ""),
        "polymarket_event_ticker": polymarket_snapshot.get("event_ticker", ""),
        "polymarket_close_time": polymarket_snapshot.get("close_time", ""),
        "polymarket_status": polymarket_snapshot.get("status", ""),
        "polymarket_yes_bid": polymarket_snapshot.get("yes_bid", ""),
        "polymarket_yes_ask": polymarket_snapshot.get("yes_ask", ""),
        "polymarket_no_bid": polymarket_snapshot.get("no_bid", ""),
        "polymarket_no_ask": polymarket_snapshot.get("no_ask", ""),
        "polymarket_yes_mid": polymarket_snapshot.get("yes_mid", ""),
        "polymarket_last_price": polymarket_snapshot.get("last_price", ""),
        "polymarket_volume": polymarket_snapshot.get("volume", ""),
        "polymarket_open_interest": polymarket_snapshot.get("open_interest", ""),
        "polymarket_best_yes_bid_qty": polymarket_snapshot.get("best_yes_bid_qty", ""),
        "polymarket_best_no_bid_qty": polymarket_snapshot.get("best_no_bid_qty", ""),
        "source_timestamp_utc": source_snapshot.get("timestamp_utc", ""),
        "kalshi_btc_source": "BRTI",
        "kalshi_btc_price": source_snapshot.get("kalshi_price", ""),
        "kalshi_btc_target": source_snapshot.get("kalshi_target", ""),
        "kalshi_btc_60_sma": source_snapshot.get("kalshi_60_sma", ""),
        "kalshi_btc_60_sma_sample_count": source_snapshot.get("kalshi_60_sma_sample_count", ""),
        "polymarket_btc_source": "Polymarket RTDS",
        "polymarket_btc_price": source_snapshot.get("polymarket_price", ""),
        "polymarket_btc_target": source_snapshot.get("polymarket_target", ""),
        "k_plus_np": bid_sum_features.get("K+NP") if bid_sum_features.get("K+NP") is not None else "",
        "nk_plus_p": bid_sum_features.get("NK+P") if bid_sum_features.get("NK+P") is not None else "",
        "k_plus_np_kalshi_fee": k_plus_np.get("kalshi_fee") if k_plus_np else "",
        "k_plus_np_polymarket_fee": k_plus_np.get("polymarket_fee") if k_plus_np else "",
        "k_plus_np_total_fee": k_plus_np.get("total_fee") if k_plus_np else "",
        "k_plus_np_all_in_cost": k_plus_np.get("all_in_cost") if k_plus_np else "",
        "k_plus_np_fee_adjusted_edge": k_plus_np.get("fee_adjusted_edge") if k_plus_np else "",
        "nk_plus_p_kalshi_fee": nk_plus_p.get("kalshi_fee") if nk_plus_p else "",
        "nk_plus_p_polymarket_fee": nk_plus_p.get("polymarket_fee") if nk_plus_p else "",
        "nk_plus_p_total_fee": nk_plus_p.get("total_fee") if nk_plus_p else "",
        "nk_plus_p_all_in_cost": nk_plus_p.get("all_in_cost") if nk_plus_p else "",
        "nk_plus_p_fee_adjusted_edge": nk_plus_p.get("fee_adjusted_edge") if nk_plus_p else "",
        "best_arb_direction": best.get("name") if best else "",
        "best_arb_raw_cost": best.get("raw_cost") if best else "",
        "best_arb_total_fee": best.get("total_fee") if best else "",
        "best_arb_all_in_cost": best.get("all_in_cost") if best else "",
        "best_arb_fee_adjusted_edge": best.get("fee_adjusted_edge") if best else "",
        "best_arb_profitable": int(bool(best and best.get("profitable"))),
        "profit_margin": profit_margin,
        "tradable": int(runtime.tradable),
        "strategy_name": ENTRY_STRATEGY_NAME,
        "latch_horizon": runtime.latch_horizon,
        "latch_action": runtime.latch_action,
        "last_model_horizon": last_decision.get("horizon", ""),
        "last_diverge_prob": last_decision.get("diverge_prob", ""),
        "last_diverge_threshold": last_decision.get("threshold", ""),
        "active_trade": int(active_position is not None),
        "polymarket_error": polymarket_error,
    }
    return row


def history_dataframe(rows: deque[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(list(rows))
    for col in CSV_FIELDS:
        if col not in df.columns:
            df[col] = np.nan
    timestamp_cols = [
        "timestamp_utc",
        "kalshi_timestamp_utc",
        "kalshi_close_time",
        "polymarket_timestamp_utc",
        "polymarket_close_time",
        "source_timestamp_utc",
    ]
    for col in timestamp_cols:
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce", format="mixed")
    numeric_cols = [
        col
        for col in CSV_FIELDS
        if col not in timestamp_cols
        and not col.endswith("ticker")
        and col
        not in {
            "kalshi_title",
            "kalshi_event_ticker",
            "kalshi_status",
            "polymarket_title",
            "polymarket_event_ticker",
            "polymarket_status",
            "kalshi_btc_source",
            "polymarket_btc_source",
            "best_arb_direction",
            "last_model_horizon",
            "polymarket_error",
        }
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    fallback = str(df["kalshi_ticker"].dropna().iloc[-1]) if df["kalshi_ticker"].notna().any() else "unknown"
    df["contract_id"] = df["kalshi_ticker"].ffill().bfill().fillna(fallback)
    df = df.dropna(subset=["timestamp_utc", "kalshi_close_time"]).sort_values("timestamp_utc")
    df = df.drop_duplicates("timestamp_utc", keep="last").reset_index(drop=True)
    return df


def model_sampled_history(raw: pd.DataFrame, asof_time: pd.Timestamp) -> pd.DataFrame:
    eligible = raw[raw["timestamp_utc"] <= asof_time].sort_values("timestamp_utc").copy()
    if eligible.empty:
        return eligible

    periods = MODEL_WINDOW_SAMPLE_COUNT + MODEL_WARMUP_SAMPLE_COUNT
    sample_times = pd.date_range(
        end=asof_time,
        periods=periods,
        freq=pd.Timedelta(seconds=MODEL_SAMPLE_INTERVAL_SECONDS),
    )
    grid = pd.DataFrame({"model_sample_timestamp_utc": sample_times})
    source = eligible.rename(columns={"timestamp_utc": "model_sample_source_timestamp_utc"})
    sampled = pd.merge_asof(
        grid,
        source,
        left_on="model_sample_timestamp_utc",
        right_on="model_sample_source_timestamp_utc",
        direction="backward",
    )
    sampled = sampled.dropna(subset=["model_sample_source_timestamp_utc", "kalshi_close_time"]).copy()
    sampled["timestamp_utc"] = sampled["model_sample_timestamp_utc"]
    sampled = sampled.drop(columns=["model_sample_timestamp_utc"])
    return sampled.reset_index(drop=True)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().sort_values(["contract_id", "timestamp_utc"]).reset_index(drop=True)
    out["time_to_close_seconds"] = (out["kalshi_close_time"] - out["timestamp_utc"]).dt.total_seconds()
    out["contract_start_time"] = out["kalshi_close_time"] - pd.to_timedelta(CONTRACT_SECONDS, unit="s")
    out["elapsed_fraction"] = (
        (out["timestamp_utc"] - out["contract_start_time"]).dt.total_seconds() / CONTRACT_SECONDS
    )
    out["elapsed_fraction"] = out["elapsed_fraction"].clip(0.0, 1.0)

    rolling_sma = (
        out.groupby("contract_id", sort=False)["kalshi_btc_price"]
        .rolling(30, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )
    out["kalshi_btc_sma_for_distance"] = out["kalshi_btc_60_sma"].where(out["kalshi_btc_60_sma"].notna(), rolling_sma)

    out["price_spread"] = out["kalshi_btc_price"] - out["polymarket_btc_price"]
    out["price_spread_abs"] = out["price_spread"].abs()
    out["kalshi_distance_to_target"] = out["kalshi_btc_sma_for_distance"] - out["kalshi_btc_target"]
    out["polymarket_distance_to_target"] = out["polymarket_btc_price"] - out["kalshi_btc_target"]
    out["spread_vs_distance_ratio"] = out["price_spread_abs"] / (out["kalshi_distance_to_target"].abs() + 1e-6)
    out["spread_vs_distance_ratio"] = out["spread_vs_distance_ratio"].clip(0, 1_000_000)

    same_positive = (out["kalshi_distance_to_target"] > 0) & (out["polymarket_distance_to_target"] > 0)
    same_negative = (out["kalshi_distance_to_target"] < 0) & (out["polymarket_distance_to_target"] < 0)
    known_sides = out["kalshi_distance_to_target"].notna() & out["polymarket_distance_to_target"].notna()
    out["feeds_on_same_side"] = np.where(known_sides, (same_positive | same_negative).astype(float), np.nan)

    out["kalshi_bid_ask_spread_yes"] = out["kalshi_yes_ask"] - out["kalshi_yes_bid"]
    out["kalshi_order_book_imbalance"] = (
        (out["kalshi_best_yes_bid_qty"] - out["kalshi_best_no_bid_qty"])
        / (out["kalshi_best_yes_bid_qty"] + out["kalshi_best_no_bid_qty"] + 1e-6)
    )
    out["polymarket_bid_ask_spread_yes"] = out["polymarket_yes_ask"] - out["polymarket_yes_bid"]
    out["polymarket_order_book_imbalance"] = (
        (out["polymarket_best_yes_bid_qty"] - out["polymarket_best_no_bid_qty"])
        / (out["polymarket_best_yes_bid_qty"] + out["polymarket_best_no_bid_qty"] + 1e-6)
    )

    out["implied_prob_spread"] = out["kalshi_yes_mid"] - out["polymarket_yes_mid"]
    out["arb_available"] = ((out["k_plus_np"] > 1.0) | (out["nk_plus_p"] > 1.0)).astype(float)
    out["polymarket_error_flag"] = out["polymarket_error"].notna() & (out["polymarket_error"].astype(str) != "")
    out["polymarket_error_flag"] = out["polymarket_error_flag"].astype(float)

    group = out.groupby("contract_id", sort=False, group_keys=False)
    out["price_spread_roll10_std"] = group["price_spread"].rolling(10, min_periods=2).std().reset_index(level=0, drop=True)
    out["kalshi_btc_price_roll10_mean"] = group["kalshi_btc_price"].rolling(10, min_periods=1).mean().reset_index(level=0, drop=True)
    out["kalshi_btc_price_roll10_std"] = group["kalshi_btc_price"].rolling(10, min_periods=2).std().reset_index(level=0, drop=True)
    out["kalshi_btc_price_lag5"] = group["kalshi_btc_price"].shift(5)
    out["kalshi_btc_price_lag10"] = group["kalshi_btc_price"].shift(10)
    out["kalshi_btc_price_momentum_5"] = out["kalshi_btc_price"] - out["kalshi_btc_price_lag5"]
    out["kalshi_btc_price_momentum_10"] = out["kalshi_btc_price"] - out["kalshi_btc_price_lag10"]
    out["implied_prob_spread_roll10_std"] = group["implied_prob_spread"].rolling(10, min_periods=2).std().reset_index(level=0, drop=True)
    out["price_spread_abs_x_elapsed_fraction"] = out["price_spread_abs"] * out["elapsed_fraction"]
    out["spread_vs_distance_ratio_x_elapsed_fraction"] = out["spread_vs_distance_ratio"] * out["elapsed_fraction"]
    out["feeds_on_same_side_x_elapsed_fraction"] = out["feeds_on_same_side"] * out["elapsed_fraction"]

    out[FEATURE_NAMES] = out[FEATURE_NAMES].replace([np.inf, -np.inf], np.nan)
    return out


def aggregate_series(series: pd.Series) -> dict[str, float]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return {stat: math.nan for stat in AGG_STATS}
    first = float(values.iloc[0])
    last = float(values.iloc[-1])
    min_value = float(values.min())
    max_value = float(values.max())
    return {
        "last": last,
        "mean": float(values.mean()),
        "std": float(values.std(ddof=1)) if len(values) > 1 else 0.0,
        "min": min_value,
        "max": max_value,
        "range": max_value - min_value,
        "change": last - first,
    }


def add_entry_cost_features(window: pd.DataFrame) -> pd.DataFrame:
    out = window.copy()
    out["k_yes_p_no_entry_cost"] = out["kalshi_yes_ask"] + out["polymarket_no_ask"]
    out["k_yes_p_no_kalshi_fee"] = KALSHI_FEE_RATE * out["kalshi_yes_ask"] * (1.0 - out["kalshi_yes_ask"])
    out["k_yes_p_no_polymarket_fee"] = POLYMARKET_FEE_RATE * out["polymarket_no_ask"] * (1.0 - out["polymarket_no_ask"])
    out["k_yes_p_no_total_fee"] = out["k_yes_p_no_kalshi_fee"] + out["k_yes_p_no_polymarket_fee"]
    out["k_yes_p_no_all_in_cost"] = out["k_yes_p_no_entry_cost"] + out["k_yes_p_no_total_fee"]
    out["k_yes_p_no_fee_adjusted_edge"] = 1.0 - out["k_yes_p_no_all_in_cost"]

    out["k_no_p_yes_entry_cost"] = out["kalshi_no_ask"] + out["polymarket_yes_ask"]
    out["k_no_p_yes_kalshi_fee"] = KALSHI_FEE_RATE * out["kalshi_no_ask"] * (1.0 - out["kalshi_no_ask"])
    out["k_no_p_yes_polymarket_fee"] = POLYMARKET_FEE_RATE * out["polymarket_yes_ask"] * (1.0 - out["polymarket_yes_ask"])
    out["k_no_p_yes_total_fee"] = out["k_no_p_yes_kalshi_fee"] + out["k_no_p_yes_polymarket_fee"]
    out["k_no_p_yes_all_in_cost"] = out["k_no_p_yes_entry_cost"] + out["k_no_p_yes_total_fee"]
    out["k_no_p_yes_fee_adjusted_edge"] = 1.0 - out["k_no_p_yes_all_in_cost"]

    yes_no_better = out["k_yes_p_no_all_in_cost"] <= out["k_no_p_yes_all_in_cost"]
    out["best_raw_entry_cost"] = np.where(yes_no_better, out["k_yes_p_no_entry_cost"], out["k_no_p_yes_entry_cost"])
    out["best_total_fee"] = np.where(yes_no_better, out["k_yes_p_no_total_fee"], out["k_no_p_yes_total_fee"])
    out["best_all_in_cost"] = np.where(yes_no_better, out["k_yes_p_no_all_in_cost"], out["k_no_p_yes_all_in_cost"])
    out["fee_adjusted_edge"] = 1.0 - out["best_all_in_cost"]
    out["best_entry_cost"] = out["best_all_in_cost"]
    out["entry_edge"] = out["fee_adjusted_edge"]
    return out


def aggregate_horizon_features(rows: deque[dict[str, Any]], horizon_name: str, horizon_seconds: int) -> tuple[dict[str, Any], str]:
    raw = history_dataframe(rows)
    if raw.empty:
        return {}, "missing_history"
    close_time = raw["kalshi_close_time"].dropna().iloc[-1]
    asof_time = close_time - pd.Timedelta(seconds=horizon_seconds)
    sampled_raw = model_sampled_history(raw, asof_time)
    if sampled_raw.empty:
        return {}, "missing_history"
    features = add_features(sampled_raw)
    window_start = asof_time - pd.Timedelta(seconds=WINDOW_SECONDS)
    eligible_rows = features[features["timestamp_utc"] <= asof_time]
    window = eligible_rows[
        (eligible_rows["timestamp_utc"] > window_start)
        & (eligible_rows["timestamp_utc"] <= asof_time)
    ].copy()

    if window.empty:
        last_seen_ts = eligible_rows["timestamp_utc"].max() if not eligible_rows.empty else pd.NaT
        asof_gap = float((asof_time - last_seen_ts).total_seconds()) if pd.notna(last_seen_ts) else math.nan
        status = "missing_window"
    else:
        source_ts = (
            window["model_sample_source_timestamp_utc"]
            if "model_sample_source_timestamp_utc" in window
            else window["timestamp_utc"]
        )
        last_seen_ts = source_ts.max()
        asof_gap = float((asof_time - last_seen_ts).total_seconds())
        if len(window) < MIN_WINDOW_ROWS:
            status = "too_few_window_rows"
        elif asof_gap > MAX_ASOF_GAP_SECONDS:
            status = "stale_asof_snapshot"
        else:
            status = "ok"

    window = add_entry_cost_features(window)
    row: dict[str, Any] = {
        "horizon_seconds": horizon_seconds,
        "model_sampling_mode": "in_memory_previous_tick_2s_grid",
        "model_sample_interval_seconds": MODEL_SAMPLE_INTERVAL_SECONDS,
        "model_expected_window_rows": MODEL_WINDOW_SAMPLE_COUNT,
        "model_warmup_sample_rows": MODEL_WARMUP_SAMPLE_COUNT,
        "model_raw_history_rows": int(len(raw)),
        "model_sampled_history_rows": int(len(features)),
        "window_rows": int(len(window)),
        "window_actual_seconds": float((window["timestamp_utc"].max() - window["timestamp_utc"].min()).total_seconds()) if len(window) > 1 else 0.0,
        "asof_gap_seconds": asof_gap,
    }
    for feature in [*FEATURE_NAMES, *ENTRY_COST_FEATURES]:
        stats = aggregate_series(window[feature]) if feature in window.columns else {stat: math.nan for stat in AGG_STATS}
        for stat, value in stats.items():
            row[f"{feature}_{stat}"] = value
    if "polymarket_error_flag" in window:
        row["polymarket_error_rate_window"] = float(window["polymarket_error_flag"].mean()) if len(window) else math.nan
    else:
        row["polymarket_error_rate_window"] = math.nan
    return row, status


@dataclass
class HorizonModel:
    name: str
    seconds: int
    threshold: float
    feature_names: list[str]
    model: Any
    collection_started: bool = False
    evaluated: bool = False
    last_prob: float | None = None
    last_status: str = ""


@dataclass
class ContractRuntime:
    ticker: str
    close_time: str
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=900))
    tradable: bool = False
    latch_horizon: str = ""
    latch_action: str = ""
    horizon_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    entry_in_progress: bool = False
    entry_blocked_reason: str = ""
    entry_attempt_count: int = 0
    last_csv_save_at: float = 0.0
    last_status_log_at: float = 0.0

    def last_model_decision(self) -> dict[str, Any]:
        if not self.horizon_decisions:
            return {}
        ordered = [self.horizon_decisions[name] for name in HORIZONS if name in self.horizon_decisions]
        return ordered[-1] if ordered else {}


@dataclass
class TraderSharedState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    runtime: ContractRuntime | None = None
    active_position: dict[str, Any] | None = None
    last_sample_bucket: int | None = None


def load_horizon_models(model_dir: Path) -> dict[str, HorizonModel]:
    loaded: dict[str, HorizonModel] = {}
    for name, seconds in HORIZONS.items():
        stem = f"divergence_horizon_{name}"
        model_path = model_dir / f"{stem}_model.pkl"
        metadata_path = model_dir / f"{stem}_metadata.json"
        if not model_path.exists() or not metadata_path.exists():
            raise RuntimeError(f"Missing horizon artifacts for {name}: {model_path} / {metadata_path}")
        metadata = json.loads(metadata_path.read_text())
        threshold = float(metadata["metrics"]["recommended_trade_threshold"])
        feature_names = list(metadata["feature_names"])
        loaded[name] = HorizonModel(
            name=name,
            seconds=int(metadata.get("horizon_seconds") or seconds),
            threshold=threshold,
            feature_names=feature_names,
            model=joblib.load(model_path),
        )
    return loaded


def model_feature_debug_message(
    runtime: ContractRuntime,
    horizon: HorizonModel,
    feature_row: dict[str, Any],
    status: str,
) -> str:
    features = {name: json_safe_value(feature_row.get(name, math.nan)) for name in horizon.feature_names}
    null_features = [name for name, value in features.items() if value is None]
    payload = {
        "ticker": runtime.ticker,
        "horizon": horizon.name,
        "status": status,
        "threshold": horizon.threshold,
        "sampling": {
            "mode": feature_row.get("model_sampling_mode", "in_memory_previous_tick_2s_grid"),
            "sample_interval_seconds": json_safe_value(feature_row.get("model_sample_interval_seconds")),
            "expected_window_rows": json_safe_value(feature_row.get("model_expected_window_rows")),
            "warmup_sample_rows": json_safe_value(feature_row.get("model_warmup_sample_rows")),
            "raw_history_rows": json_safe_value(feature_row.get("model_raw_history_rows")),
            "sampled_history_rows": json_safe_value(feature_row.get("model_sampled_history_rows")),
            "actual_window_rows": json_safe_value(feature_row.get("window_rows")),
            "window_actual_seconds": json_safe_value(feature_row.get("window_actual_seconds")),
            "asof_gap_seconds": json_safe_value(feature_row.get("asof_gap_seconds")),
        },
        "feature_count": len(horizon.feature_names),
        "null_feature_count": len(null_features),
        "null_features": null_features,
        "features": features,
    }
    return f"MODEL_FEATURES {horizon.name} {runtime.ticker} | {json.dumps(payload, separators=(',', ':'), sort_keys=False)}"


def evaluate_horizon_model(
    runtime: ContractRuntime,
    horizon: HorizonModel,
    *,
    debug_features: bool = False,
    logger: Any = None,
) -> dict[str, Any]:
    feature_row, status = aggregate_horizon_features(runtime.history, horizon.name, horizon.seconds)
    decision: dict[str, Any] = {
        "horizon": horizon.name,
        "status": status,
        "threshold": horizon.threshold,
        "diverge_prob": math.nan,
        "tradable": False,
        "window_rows": feature_row.get("window_rows", math.nan),
        "expected_window_rows": feature_row.get("model_expected_window_rows", math.nan),
        "min_window_rows": MIN_WINDOW_ROWS,
        "sampled_history_rows": feature_row.get("model_sampled_history_rows", math.nan),
        "raw_history_rows": feature_row.get("model_raw_history_rows", math.nan),
        "asof_gap_seconds": feature_row.get("asof_gap_seconds", math.nan),
        "partial_window": (
            (finite_float(feature_row.get("window_rows")) or 0.0) < MODEL_WINDOW_SAMPLE_COUNT
            if feature_row
            else False
        ),
    }
    if status != "ok":
        return decision
    for feature in horizon.feature_names:
        feature_row.setdefault(feature, math.nan)
    x = pd.DataFrame([{feature: feature_row.get(feature, math.nan) for feature in horizon.feature_names}])
    if debug_features and logger is not None:
        logger(model_feature_debug_message(runtime, horizon, feature_row, status))
    prob = float(horizon.model.predict_proba(x)[0, 1])
    decision["diverge_prob"] = prob
    decision["tradable"] = bool(prob < horizon.threshold)
    return decision


def apply_latch_hold_decision(runtime: ContractRuntime, decision: dict[str, Any]) -> dict[str, Any]:
    horizon_name = str(decision.get("horizon") or "")
    model_pass = bool(decision.get("status") == "ok" and decision.get("tradable"))
    old_tradable = runtime.tradable
    role = "latch_candidate" if horizon_name in LATCH_HOLD_ENTRY_HORIZONS else "observe_only"

    if role == "observe_only":
        action = "observe_only"
    elif model_pass:
        runtime.tradable = True
        if old_tradable:
            action = "already_latched_true"
        else:
            runtime.latch_horizon = horizon_name
            action = "latched_true"
    elif old_tradable:
        action = "hold_existing_true"
    else:
        action = "no_latch"

    runtime.latch_action = action
    decision["model_pass"] = model_pass
    decision["strategy"] = ENTRY_STRATEGY_NAME
    decision["strategy_role"] = role
    decision["latch_action"] = action
    decision["latch_tradable"] = runtime.tradable
    decision["latch_horizon"] = runtime.latch_horizon
    decision["old_tradable"] = old_tradable
    decision["tradable"] = runtime.tradable
    return decision


def seconds_to_expiry(kalshi_snapshot: dict[str, Any]) -> float | None:
    close_ts = parse_ts(kalshi_snapshot.get("close_time"))
    if not close_ts:
        return None
    return close_ts - time.time()


def fill_count(order: dict[str, Any]) -> float:
    return as_float(order.get("fill_count_fp") or order.get("fill_count") or order.get("filled_count"))


def filled_price(order: dict[str, Any], side: str, action: str = "buy") -> float:
    filled = fill_count(order)
    cost = as_float(order.get("taker_fill_cost_dollars") or order.get("maker_fill_cost_dollars") or order.get("cost_dollars"))
    reference = finite_float(order.get("limit_price") or order.get("best_bid") or order.get("best_ask"))
    if filled and cost:
        unit_cost = cost / filled
        if action == "sell":
            complement = round(1.0 - unit_cost, 10)
            if reference is not None:
                return min((unit_cost, complement), key=lambda price: abs(price - reference))
            return complement
        return unit_cost
    return as_float(order.get(f"{side}_price_dollars")) or as_float(order.get(f"{side}_price")) / 100.0


def kalshi_post_order(
    ticker: str,
    side: str,
    price: float,
    contracts: int,
    client_order_id: str,
    action: str = "buy",
    time_in_force: str = "fill_or_kill",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ticker": ticker,
        "side": side,
        "action": action,
        "client_order_id": client_order_id,
        "count": contracts,
        "time_in_force": time_in_force,
    }
    payload[f"{side}_price"] = cents(price)
    response = http_json("POST", BASE_URL, "/portfolio/orders", payload=payload, auth=True, timeout=20)
    order = response.get("order") or response
    order_id = order.get("order_id")
    if order_id:
        last_error: Exception | None = None
        for attempt in range(KALSHI_ORDER_VERIFY_ATTEMPTS):
            try:
                verified = http_json("GET", BASE_URL, f"/portfolio/orders/{order_id}", auth=True, timeout=20)
                return verified.get("order") or verified
            except Exception as exc:
                last_error = exc
                if "HTTP 404" not in str(exc) or attempt == KALSHI_ORDER_VERIFY_ATTEMPTS - 1:
                    break
                time.sleep(KALSHI_ORDER_VERIFY_DELAY_SECONDS * (attempt + 1))
        order["verification_error"] = f"{type(last_error).__name__}: {last_error}"
        if fill_count(order) > 0:
            return order
        if last_error:
            raise last_error
    return order


def kalshi_exit_plan(ticker: str, side: str, contracts: int) -> tuple[float | None, float, dict[str, Any]]:
    orderbook = kalshi_get(f"/markets/{ticker}/orderbook", {"depth": ORDERBOOK_DEPTH})
    yes_levels, no_levels = orderbook_levels(orderbook)
    bid_levels = yes_levels if side == "yes" else no_levels
    priced_levels = [(price, level_quantity(level)) for level in bid_levels if (price := level_price(level)) is not None]
    priced_levels.sort(key=lambda item: item[0], reverse=True)
    cumulative = 0.0
    levels = []
    selected_price: float | None = None
    for price, quantity in priced_levels:
        cumulative += quantity
        selected = selected_price is None and cumulative >= contracts
        if selected:
            selected_price = price
        levels.append({"price": price, "quantity": quantity, "cumulative": cumulative, "selected": selected})
        if selected:
            break
    return selected_price, cumulative, {"side": side, "liquidity": cumulative, "levels": levels}


def kalshi_exit_position(ticker: str, side: str, contracts: int) -> dict[str, Any]:
    bid, liquidity, _plan = kalshi_exit_plan(ticker, side, contracts)
    if bid is None:
        raise RuntimeError(f"Kalshi {side.upper()} exit liquidity {liquidity:g} < {contracts:g} for {ticker}")
    order = kalshi_post_order(
        ticker,
        side,
        bid,
        contracts,
        f"v2-exit-k-{uuid.uuid4().hex[:16]}",
        action="sell",
    )
    order["best_bid"] = bid
    order["limit_price"] = bid
    return order


def polymarket_client_v2() -> Any:
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2
    except ImportError as exc:
        raise RuntimeError(
            "Polymarket trading requires py-clob-client-v2. Install with: python3 -m pip install py-clob-client-v2"
        ) from exc
    key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PK")
    if not key:
        raise RuntimeError("Missing POLYMARKET_PRIVATE_KEY in .env")
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
    kwargs: dict[str, Any] = {"host": POLYMARKET_CLOB_URL, "chain_id": chain_id, "key": key}
    signature_type = os.getenv("POLYMARKET_SIGNATURE_TYPE")
    funder = (
        os.getenv("POLYMARET_ADDRESS")
        or os.getenv("POLYMARKET_ADDRESS")
        or os.getenv("POLYMARKET_FUNDER")
        or os.getenv("POLYMARKET_PROXY_FUNDER")
    )
    if funder and not signature_type:
        signature_type = str(int(SignatureTypeV2.POLY_1271))
    if signature_type:
        kwargs["signature_type"] = int(signature_type)
    if funder:
        kwargs["funder"] = funder

    if os.getenv("CLOB_API_KEY") and os.getenv("CLOB_SECRET") and os.getenv("CLOB_PASS_PHRASE"):
        creds = ApiCreds(
            api_key=os.environ["CLOB_API_KEY"],
            api_secret=os.environ["CLOB_SECRET"],
            api_passphrase=os.environ["CLOB_PASS_PHRASE"],
        )
    else:
        auth_client = ClobClient(**kwargs)
        try:
            creds = auth_client.derive_api_key()
        except Exception:
            creds = auth_client.create_api_key()
    return ClobClient(**kwargs, creds=creds)


def kalshi_balance_dollars(value: Any, key: str) -> float:
    number = as_float(value)
    if "dollars" in key:
        return number
    if number >= 100:
        return number / 100.0
    return number


def kalshi_balance_amounts() -> tuple[float, float | None]:
    data = http_json("GET", BASE_URL, "/portfolio/balance", auth=True, timeout=20)
    balance = data.get("balance") if isinstance(data, dict) else None
    if isinstance(balance, dict):
        cash_key = next(
            (
                key
                for key in ("cash_balance_dollars", "cash_balance", "balance_dollars", "balance")
                if balance.get(key) not in (None, "")
            ),
            "balance",
        )
        cash = (
            balance.get("cash_balance_dollars")
            or balance.get("cash_balance")
            or balance.get("balance_dollars")
            or balance.get("balance")
        )
        available_key = next(
            (
                key
                for key in ("available_balance_dollars", "available_balance", "cash_available_dollars", "cash_available")
                if balance.get(key) not in (None, "")
            ),
            "",
        )
        available = (
            balance.get("available_balance_dollars")
            or balance.get("available_balance")
            or balance.get("cash_available_dollars")
            or balance.get("cash_available")
        )
    else:
        cash_key = "balance"
        cash = data.get("balance") if isinstance(data, dict) else None
        available_key = "available_balance"
        available = data.get("available_balance") if isinstance(data, dict) else None
    cash_dollars = kalshi_balance_dollars(cash, cash_key)
    available_dollars = kalshi_balance_dollars(available, available_key) if available not in (None, "") else None
    return cash_dollars, available_dollars


def polymarket_balance_amounts() -> tuple[float, float]:
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    client = polymarket_client_v2()
    data = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    if not isinstance(data, dict):
        raise RuntimeError(f"Polymarket balance response {data}")
    balance = data.get("balance") or data.get("usdc_balance") or data.get("collateral")
    allowances = data.get("allowances")
    if isinstance(allowances, dict) and allowances:
        allowance = max(as_float(value) for value in allowances.values())
    else:
        allowance = data.get("allowance") or data.get("usdc_allowance")
    return as_float(balance) / 1_000_000.0, as_float(allowance) / 1_000_000.0


def balance_snapshot_row(
    *,
    balance_event: str = "",
    seconds_to_expiry: Any = "",
    kalshi_ticker: str = "",
    kalshi_close_time: str = "",
    polymarket_ticker: str = "",
    polymarket_close_time: str = "",
    kalshi_target: Any = "",
    polymarket_target: Any = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp_utc": iso_utc(),
        "balance_event": balance_event,
        "seconds_to_expiry": json_safe_value(seconds_to_expiry),
        "kalshi_ticker": kalshi_ticker,
        "kalshi_close_time": kalshi_close_time,
        "polymarket_ticker": polymarket_ticker,
        "polymarket_close_time": polymarket_close_time,
        "kalshi_target": json_safe_value(kalshi_target),
        "polymarket_target": json_safe_value(polymarket_target),
        "kalshi_balance": "",
        "kalshi_available_balance": "",
        "kalshi_error": "",
        "polymarket_balance": "",
        "polymarket_allowance": "",
        "polymarket_error": "",
        "total_balance": 0.0,
        "balance_complete": True,
    }
    total_balance = 0.0
    try:
        kalshi_cash, kalshi_available = kalshi_balance_amounts()
        total_balance += kalshi_cash
        row["kalshi_balance"] = kalshi_cash
        row["kalshi_available_balance"] = "" if kalshi_available is None else kalshi_available
    except Exception as exc:
        row["kalshi_error"] = f"{type(exc).__name__}: {exc}"
        row["balance_complete"] = False
    try:
        polymarket_cash, polymarket_allowance = polymarket_balance_amounts()
        total_balance += polymarket_cash
        row["polymarket_balance"] = polymarket_cash
        row["polymarket_allowance"] = polymarket_allowance
    except Exception as exc:
        row["polymarket_error"] = f"{type(exc).__name__}: {exc}"
        row["balance_complete"] = False
    row["total_balance"] = total_balance
    return row


def balance_line_from_row(row: dict[str, Any]) -> str:
    parts = []
    remaining = finite_float(row.get("seconds_to_expiry"))
    remaining_text = f" T={remaining:.1f}s" if remaining is not None else ""
    if row.get("kalshi_error"):
        parts.append(f"Kalshi ERROR {row['kalshi_error']}")
    else:
        kalshi_available = finite_float(row.get("kalshi_available_balance"))
        available_text = f" available {fmt_money(kalshi_available)}" if kalshi_available is not None else ""
        parts.append(f"Kalshi {fmt_money(row.get('kalshi_balance'))}{available_text}")
    if row.get("polymarket_error"):
        parts.append(f"Polymarket ERROR {row['polymarket_error']}")
    else:
        parts.append(f"Polymarket {fmt_money(row.get('polymarket_balance'))}")
    prefix = f"BALANCE{remaining_text}"
    separator = " | " if remaining_text else " "
    return f"{prefix}{separator}{' | '.join(parts)} | total {fmt_money(row.get('total_balance'))}"


def combined_balance_line() -> str:
    return balance_line_from_row(balance_snapshot_row())


async def record_balance_snapshot(
    csv_dir: Path,
    *,
    balance_event: str,
    ticker: str,
    close_time: str,
    remaining: float | None,
    polymarket_snapshot: dict[str, Any],
    source_snapshot: dict[str, Any],
) -> None:
    balance_row = await asyncio.to_thread(
        balance_snapshot_row,
        balance_event=balance_event,
        seconds_to_expiry=remaining,
        kalshi_ticker=ticker,
        kalshi_close_time=close_time,
        polymarket_ticker=str(polymarket_snapshot.get("ticker") or ""),
        polymarket_close_time=str(polymarket_snapshot.get("close_time") or ""),
        kalshi_target=source_snapshot.get("kalshi_target"),
        polymarket_target=source_snapshot.get("polymarket_target"),
    )
    append_log(balance_line_from_row(balance_row), concise=True)
    try:
        append_balance_csv_row(balance_csv_path(csv_dir), balance_row)
    except Exception as exc:
        append_log(f"BALANCE CSV ERROR {type(exc).__name__}: {exc}", concise=True)


def polymarket_post_order(
    market: dict[str, Any],
    contract: str,
    price: float,
    contracts: int,
    side: Any = None,
    order_type_name: str = "FOK",
) -> dict[str, Any]:
    from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

    token_id = token_ids_by_contract(market)[contract]
    client = polymarket_client_v2()
    order_side = side or Side.BUY
    order_type = getattr(OrderType, order_type_name.upper(), OrderType.FOK)
    response = client.create_and_post_order(
        order_args=OrderArgs(token_id=token_id, price=price, side=order_side, size=float(contracts)),
        options=PartialCreateOrderOptions(tick_size="0.01"),
        order_type=order_type,
    )
    if not isinstance(response, dict):
        return {"response": response}
    order_id = response.get("id") or response.get("order_id") or response.get("orderID") or response.get("orderId")
    if order_id:
        try:
            verified = client.get_order(str(order_id))
        except Exception as exc:
            response["verification_error"] = f"{type(exc).__name__}: {exc}"
        else:
            response["verified_order"] = verified
    return response


def polymarket_conditional_balance_amounts(token_id: str) -> tuple[float, float]:
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    client = polymarket_client_v2()
    params_kwargs = {"asset_type": AssetType.CONDITIONAL, "token_id": token_id}
    try:
        params = BalanceAllowanceParams(**params_kwargs)
    except TypeError:
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, asset_id=token_id)
    data = client.get_balance_allowance(params)
    if not isinstance(data, dict):
        raise RuntimeError(f"Polymarket conditional balance response {data}")
    balance = data.get("balance") or data.get("conditional_balance") or data.get("token_balance") or data.get("asset_balance")
    allowances = data.get("allowances")
    if isinstance(allowances, dict) and allowances:
        allowance = max(as_float(value) for value in allowances.values())
    else:
        allowance = data.get("allowance") or data.get("conditional_allowance") or data.get("token_allowance")
    allowance_amount = float("inf") if allowance in (None, "") else as_float(allowance) / 1_000_000.0
    return as_float(balance) / 1_000_000.0, allowance_amount


def polymarket_fill_summary(response: dict[str, Any], expected_price: float, expected_size: int) -> tuple[bool, float, float]:
    verified = response.get("verified_order")
    if isinstance(verified, dict):
        filled_size = as_float(
            verified.get("filled_size")
            or verified.get("matched_size")
            or verified.get("size_matched")
            or verified.get("fill_count")
        )
        average_price = as_float(verified.get("average_price") or verified.get("avg_price") or verified.get("price"))
        status = str(verified.get("status") or verified.get("state") or "").lower()
        if filled_size > 0 or "filled" in status or "matched" in status:
            return True, average_price or expected_price, filled_size or float(expected_size)
    executions = response.get("executions")
    if isinstance(executions, list) and executions:
        total_size = sum(as_float(item.get("quantity") or item.get("size")) for item in executions)
        total_cost = sum(
            as_float(item.get("price", {}).get("value") if isinstance(item.get("price"), dict) else item.get("price"))
            * as_float(item.get("quantity") or item.get("size"))
            for item in executions
        )
        if total_size:
            return True, total_cost / total_size, total_size
    status_text = " ".join(str(value).lower() for value in response.values())
    filled = any(word in status_text for word in ("filled", "matched"))
    return filled, expected_price, float(expected_size) if filled else 0.0


def polymarket_exit_plan(market: dict[str, Any], contract: str, contracts: int) -> tuple[float | None, float, dict[str, Any]]:
    token_id = token_ids_by_contract(market)[contract]
    orderbook = clob_get("/book", {"token_id": token_id})
    bid_levels, _ask_levels = polymarket_book_levels(orderbook)
    priced_levels = [(price, level_quantity(level)) for level in bid_levels if (price := level_price(level)) is not None]
    priced_levels.sort(key=lambda item: item[0], reverse=True)
    cumulative = 0.0
    levels = []
    selected_price: float | None = None
    for price, quantity in priced_levels:
        cumulative += quantity
        selected = selected_price is None and cumulative >= contracts
        if selected:
            selected_price = price
        levels.append({"price": price, "quantity": quantity, "cumulative": cumulative, "selected": selected})
        if selected:
            break
    return selected_price, cumulative, {"contract": contract, "token_id": token_id, "liquidity": cumulative, "levels": levels}


def polymarket_exit_position(market: dict[str, Any], contract: str, contracts: int) -> tuple[dict[str, Any], float]:
    from py_clob_client_v2 import Side

    token_id = token_ids_by_contract(market)[contract]
    last_balance_error: Exception | None = None
    checked_balance = False
    balance_ready = False
    for _attempt in range(POLYMARKET_SELL_BALANCE_ATTEMPTS):
        try:
            balance, allowance = polymarket_conditional_balance_amounts(token_id)
            checked_balance = True
            if balance >= contracts and allowance >= contracts:
                balance_ready = True
                break
            last_balance_error = RuntimeError(
                f"conditional balance/allowance below exit size: balance {balance:g}, allowance {allowance:g}, need {contracts:g}"
            )
        except Exception as exc:
            last_balance_error = exc
            break
        time.sleep(POLYMARKET_SELL_BALANCE_DELAY_SECONDS)
    if checked_balance and not balance_ready:
        raise RuntimeError(str(last_balance_error))

    bid, liquidity, _plan = polymarket_exit_plan(market, contract, contracts)
    if bid is None:
        raise RuntimeError(f"Polymarket {contract} exit liquidity {liquidity:g} < {contracts:g}")
    response = polymarket_post_order(market, contract, bid, contracts, side=Side.SELL)
    filled, fill_price, _size = polymarket_fill_summary(response, bid, contracts)
    if not filled:
        raise RuntimeError(f"Polymarket sell exit had no fill: {response}; balance check={last_balance_error}")
    response["best_bid"] = bid
    response["limit_price"] = bid
    return response, fill_price


def fresh_orderbook_snapshots(
    kalshi_market: dict[str, Any],
    polymarket_market: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        kalshi_future = executor.submit(
            kalshi_get,
            f"/markets/{kalshi_market['ticker']}/orderbook",
            {"depth": ORDERBOOK_DEPTH},
        )
        poly_future = executor.submit(polymarket_clob_orderbooks, polymarket_market)
        kalshi_orderbook = kalshi_future.result()
        poly_orderbook = poly_future.result()
    return make_snapshot(kalshi_market, kalshi_orderbook), make_polymarket_snapshot(polymarket_market, poly_orderbook)


def entry_time_reject_reason(remaining_seconds: float | None) -> str | None:
    if remaining_seconds is None:
        return "missing expiry time"
    if remaining_seconds <= MIN_ENTRY_SECONDS_TO_EXPIRY:
        return (
            f"T={remaining_seconds:.1f}s is inside or at minimum entry buffer "
            f"{MIN_ENTRY_SECONDS_TO_EXPIRY:.1f}s"
        )
    return None


def preflight_trade(
    kalshi_market: dict[str, Any],
    polymarket_market: dict[str, Any],
    contracts: int,
    profit_margin: float,
) -> dict[str, Any]:
    kalshi_snapshot, polymarket_snapshot = fresh_orderbook_snapshots(kalshi_market, polymarket_market)
    time_reason = entry_time_reject_reason(seconds_to_expiry(kalshi_snapshot))
    if time_reason:
        return {"decision": "SKIP", "reason": time_reason}
    if contracts < KALSHI_MIN_ORDER_CONTRACTS:
        return {"decision": "SKIP", "reason": f"entry size {contracts:g} below Kalshi minimum {KALSHI_MIN_ORDER_CONTRACTS:g}"}
    if contracts < POLYMARKET_MIN_ORDER_CONTRACTS:
        return {"decision": "SKIP", "reason": f"entry size {contracts:g} below Polymarket minimum {POLYMARKET_MIN_ORDER_CONTRACTS:g}"}
    best = best_arbitrage_candidate(kalshi_snapshot, polymarket_snapshot, profit_margin, contracts)
    if best is None:
        return {"decision": "SKIP", "reason": f"missing fresh liquid orderbook prices for size {contracts:g}"}
    kalshi_side = best["kalshi_contract"].lower()
    poly_contract = best["polymarket_contract"]
    kalshi_qty_key = "best_yes_ask_qty" if kalshi_side == "yes" else "best_no_ask_qty"
    poly_qty_key = "best_yes_ask_qty" if poly_contract == "YES" else "best_no_ask_qty"
    kalshi_qty = as_float(kalshi_snapshot.get(kalshi_qty_key))
    poly_qty = as_float(polymarket_snapshot.get(poly_qty_key))
    poly_notional = best["polymarket_price"] * contracts
    if kalshi_qty < contracts:
        return {"decision": "SKIP", "reason": f"fresh Kalshi {kalshi_side.upper()} ask liquidity {kalshi_qty:g} < {contracts:g}", "candidate": best}
    if poly_qty < contracts:
        return {"decision": "SKIP", "reason": f"fresh Polymarket {poly_contract} ask liquidity {poly_qty:g} < {contracts:g}", "candidate": best}
    if poly_notional < POLYMARKET_MIN_ORDER_NOTIONAL:
        return {"decision": "SKIP", "reason": f"Polymarket notional {fmt_money(poly_notional)} < {fmt_money(POLYMARKET_MIN_ORDER_NOTIONAL)} minimum", "candidate": best}
    if not best["profitable"]:
        return {
            "decision": "SKIP",
            "reason": f"fee-adjusted edge {fmt_money(best['fee_adjusted_edge'])} <= margin {fmt_money(profit_margin)}",
            "candidate": best,
        }
    return {
        "decision": "PLACE",
        "reason": "fresh books profitable and liquid",
        "candidate": best,
        "kalshi_snapshot": kalshi_snapshot,
        "polymarket_snapshot": polymarket_snapshot,
        "kalshi_liquidity": kalshi_qty,
        "polymarket_liquidity": poly_qty,
        "polymarket_notional": poly_notional,
    }


def response_order_id(response: dict[str, Any] | None) -> str:
    if not isinstance(response, dict):
        return "--"
    verified = response.get("verified_order")
    candidates = [response.get("order_id"), response.get("id"), response.get("orderID"), response.get("orderId")]
    if isinstance(verified, dict):
        candidates.extend([verified.get("order_id"), verified.get("id"), verified.get("orderID"), verified.get("orderId")])
    for candidate in candidates:
        if candidate not in (None, ""):
            return str(candidate)
    return "--"


def cleanup_result_message(label: str, result: Any, side: str = "") -> str:
    if isinstance(result, Exception):
        return f"{label} FAILED {type(result).__name__}: {result}"
    if isinstance(result, tuple) and len(result) >= 2:
        return f"{label} ok fill {fmt_cents(result[1])}"
    if isinstance(result, dict):
        fill = fill_count(result)
        price = filled_price(result, side, action="sell") if side else finite_float(result.get("limit_price"))
        return f"{label} ok filled {fill:g} at {fmt_cents(price)}"
    return f"{label} ok"


def exit_filled_contracts(result: Any, requested: int, side: str = "") -> int:
    if isinstance(result, Exception):
        return 0
    if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[0], dict):
        filled, _price, size = polymarket_fill_summary(result[0], as_float(result[1]), requested)
        return min(requested, int(round(size))) if filled else 0
    if isinstance(result, dict):
        filled = fill_count(result)
        return min(requested, int(round(filled))) if filled > 0 else 0
    return 0


def filled_contract_units(value: Any) -> int:
    return max(0, int(round(as_float(value))))


def weighted_fill_price(current_price: float | None, current_size: float, new_price: float | None, new_size: float) -> float | None:
    if new_price is None or new_size <= 0:
        return current_price
    if current_price is None or current_size <= 0:
        return float(new_price)
    total = current_size + new_size
    if total <= 0:
        return current_price
    return ((float(current_price) * current_size) + (float(new_price) * new_size)) / total


def retry_entry_plan(
    kalshi_market: dict[str, Any],
    polymarket_market: dict[str, Any],
    candidate_name: str,
    missing_kalshi: int,
    missing_poly: int,
    profit_margin: float,
) -> dict[str, Any]:
    retry_size = max(missing_kalshi, missing_poly)
    if retry_size <= 0:
        return {"decision": "COMPLETE", "reason": "no missing legs"}
    kalshi_snapshot, polymarket_snapshot = fresh_orderbook_snapshots(kalshi_market, polymarket_market)
    time_reason = entry_time_reject_reason(seconds_to_expiry(kalshi_snapshot))
    if time_reason:
        return {"decision": "STOP", "reason": time_reason}
    candidates = arbitrage_candidates(
        kalshi_snapshot,
        polymarket_snapshot,
        profit_margin,
        retry_size,
        require_liquidity=False,
    )
    candidate = next((item for item in candidates if item.get("name") == candidate_name), None)
    if candidate is None:
        return {"decision": "WAIT", "reason": f"{candidate_name} no longer has fresh prices"}
    edge = finite_float(candidate.get("fee_adjusted_edge"))
    if edge is None:
        return {"decision": "WAIT", "reason": f"{candidate_name} has no computable edge", "candidate": candidate}
    if edge <= 0:
        return {
            "decision": "STOP",
            "reason": f"{candidate_name} edge {fmt_money(edge)} <= 0",
            "candidate": candidate,
        }
    if edge <= profit_margin:
        return {
            "decision": "WAIT",
            "reason": f"{candidate_name} edge {fmt_money(edge)} <= margin {fmt_money(profit_margin)}",
            "candidate": candidate,
        }
    kalshi_side = candidate["kalshi_contract"].lower()
    poly_contract = candidate["polymarket_contract"]
    if missing_kalshi > 0:
        kalshi_qty_key = "best_yes_ask_qty" if kalshi_side == "yes" else "best_no_ask_qty"
        kalshi_qty = as_float(kalshi_snapshot.get(kalshi_qty_key))
        if missing_kalshi < KALSHI_MIN_ORDER_CONTRACTS:
            return {"decision": "STOP", "reason": f"missing Kalshi size {missing_kalshi:g} below minimum {KALSHI_MIN_ORDER_CONTRACTS:g}", "candidate": candidate}
        if kalshi_qty < missing_kalshi:
            return {"decision": "WAIT", "reason": f"fresh Kalshi {kalshi_side.upper()} ask liquidity {kalshi_qty:g} < {missing_kalshi:g}", "candidate": candidate}
    if missing_poly > 0:
        poly_qty_key = "best_yes_ask_qty" if poly_contract == "YES" else "best_no_ask_qty"
        poly_qty = as_float(polymarket_snapshot.get(poly_qty_key))
        poly_notional = candidate["polymarket_price"] * missing_poly
        if missing_poly < POLYMARKET_MIN_ORDER_CONTRACTS:
            return {"decision": "STOP", "reason": f"missing Polymarket size {missing_poly:g} below minimum {POLYMARKET_MIN_ORDER_CONTRACTS:g}", "candidate": candidate}
        if poly_qty < missing_poly:
            return {"decision": "WAIT", "reason": f"fresh Polymarket {poly_contract} ask liquidity {poly_qty:g} < {missing_poly:g}", "candidate": candidate}
        if poly_notional < POLYMARKET_MIN_ORDER_NOTIONAL:
            return {"decision": "WAIT", "reason": f"Polymarket retry notional {fmt_money(poly_notional)} < {fmt_money(POLYMARKET_MIN_ORDER_NOTIONAL)} minimum", "candidate": candidate}
    return {"decision": "PLACE", "reason": "same arbitrage still profitable and liquid", "candidate": candidate}


async def execute_entry(
    kalshi_market: dict[str, Any],
    polymarket_market: dict[str, Any],
    contracts: int,
    profit_margin: float,
    dry_run: bool,
) -> tuple[dict[str, Any] | None, str, bool]:
    preflight = await asyncio.to_thread(
        preflight_trade,
        kalshi_market,
        polymarket_market,
        contracts,
        profit_margin,
    )
    if preflight.get("decision") != "PLACE":
        candidate = preflight.get("candidate") or {}
        return None, (
            f"ENTRY SKIP {preflight.get('reason')} | "
            f"best {candidate.get('name', '--')} all-in {fmt_cents(candidate.get('all_in_cost'))} "
            f"edge {fmt_money(candidate.get('fee_adjusted_edge'))}"
        ), False
    candidate = preflight["candidate"]
    kalshi_side = candidate["kalshi_contract"].lower()
    poly_contract = candidate["polymarket_contract"]
    if dry_run:
        position = {
            "dry_run": True,
            "ticker": kalshi_market.get("ticker"),
            "close_time": kalshi_market.get("close_time") or kalshi_market.get("close_ts"),
            "kalshi_side": kalshi_side,
            "polymarket_contract": poly_contract,
            "contracts": contracts,
            "entry_time": iso_utc(),
            "kalshi_fill_price": candidate["kalshi_price"],
            "polymarket_fill_price": candidate["polymarket_price"],
            "entry_raw_cost": candidate["raw_cost"],
            "entry_total_fee": candidate["total_fee"],
            "entry_all_in_cost": candidate["all_in_cost"],
            "entry_fee_adjusted_edge": candidate["fee_adjusted_edge"],
        }
        return position, (
            f"DRY ENTRY {candidate['name']} size {contracts:g} | "
            f"K {kalshi_side.upper()} {fmt_cents(candidate['kalshi_price'])} + "
            f"P {poly_contract} {fmt_cents(candidate['polymarket_price'])} | "
            f"all-in {fmt_cents(candidate['all_in_cost'])} edge {fmt_money(candidate['fee_adjusted_edge'])}"
        ), False

    kalshi_task = asyncio.to_thread(
        kalshi_post_order,
        str(kalshi_market["ticker"]),
        kalshi_side,
        candidate["kalshi_price"],
        contracts,
        f"v2-entry-k-{uuid.uuid4().hex[:16]}",
        "buy",
    )
    poly_task = asyncio.to_thread(
        polymarket_post_order,
        polymarket_market,
        poly_contract,
        candidate["polymarket_price"],
        contracts,
    )
    kalshi_result, poly_result = await asyncio.gather(kalshi_task, poly_task, return_exceptions=True)

    kalshi_filled = 0.0
    kalshi_fill_price = None
    if isinstance(kalshi_result, dict):
        kalshi_filled = fill_count(kalshi_result)
        kalshi_fill_price = filled_price(kalshi_result, kalshi_side) if kalshi_filled > 0 else None
    poly_filled = 0.0
    poly_fill_price = None
    if isinstance(poly_result, dict):
        filled, poly_fill_price, poly_filled = polymarket_fill_summary(poly_result, candidate["polymarket_price"], contracts)
        if not filled:
            poly_filled = 0.0

    kalshi_order_for_id = kalshi_result if isinstance(kalshi_result, dict) else None
    poly_order_for_id = poly_result if isinstance(poly_result, dict) else None
    retry_messages: list[str] = []

    if (kalshi_filled > 0 or poly_filled > 0) and (kalshi_filled < contracts or poly_filled < contracts):
        for attempt in range(1, ENTRY_MISSING_LEG_RETRY_ATTEMPTS + 1):
            missing_kalshi = max(0, contracts - filled_contract_units(kalshi_filled))
            missing_poly = max(0, contracts - filled_contract_units(poly_filled))
            if missing_kalshi <= 0 and missing_poly <= 0:
                break
            plan = await asyncio.to_thread(
                retry_entry_plan,
                kalshi_market,
                polymarket_market,
                str(candidate["name"]),
                missing_kalshi,
                missing_poly,
                profit_margin,
            )
            if plan.get("decision") == "STOP":
                retry_messages.append(f"retry {attempt} stop: {plan.get('reason')}")
                break
            if plan.get("decision") != "PLACE":
                retry_messages.append(f"retry {attempt} wait: {plan.get('reason')}")
                if attempt < ENTRY_MISSING_LEG_RETRY_ATTEMPTS:
                    await asyncio.sleep(ENTRY_MISSING_LEG_RETRY_DELAY_SECONDS)
                continue
            retry_candidate = plan["candidate"]
            retry_attempts: list[tuple[str, Any]] = []
            if missing_kalshi > 0:
                retry_attempts.append(
                    (
                        "Kalshi",
                        asyncio.to_thread(
                            kalshi_post_order,
                            str(kalshi_market["ticker"]),
                            kalshi_side,
                            retry_candidate["kalshi_price"],
                            missing_kalshi,
                            f"v2-entry-retry-k-{uuid.uuid4().hex[:16]}",
                            "buy",
                        ),
                    )
                )
            if missing_poly > 0:
                retry_attempts.append(
                    (
                        "Polymarket",
                        asyncio.to_thread(
                            polymarket_post_order,
                            polymarket_market,
                            poly_contract,
                            retry_candidate["polymarket_price"],
                            missing_poly,
                        ),
                    )
                )
            retry_results = list(await asyncio.gather(*(item[1] for item in retry_attempts), return_exceptions=True))
            attempt_messages = []
            for (venue, _task), result in zip(retry_attempts, retry_results, strict=False):
                if venue == "Kalshi":
                    if isinstance(result, dict):
                        new_filled = fill_count(result)
                        new_price = filled_price(result, kalshi_side) if new_filled > 0 else None
                        kalshi_fill_price = weighted_fill_price(kalshi_fill_price, kalshi_filled, new_price, new_filled)
                        kalshi_filled += new_filled
                        if new_filled > 0:
                            kalshi_order_for_id = result
                        attempt_messages.append(f"K filled {new_filled:g}")
                    else:
                        attempt_messages.append(f"K {type(result).__name__}: {result}")
                elif venue == "Polymarket":
                    if isinstance(result, dict):
                        filled, new_price, new_filled = polymarket_fill_summary(
                            result,
                            retry_candidate["polymarket_price"],
                            missing_poly,
                        )
                        if not filled:
                            new_filled = 0.0
                        poly_fill_price = weighted_fill_price(poly_fill_price, poly_filled, new_price, new_filled)
                        poly_filled += new_filled
                        if new_filled > 0:
                            poly_order_for_id = result
                        attempt_messages.append(f"P filled {new_filled:g}")
                    else:
                        attempt_messages.append(f"P {type(result).__name__}: {result}")
            retry_messages.append(f"retry {attempt}: " + "; ".join(attempt_messages))
            if kalshi_filled >= contracts and poly_filled >= contracts:
                break
            if attempt < ENTRY_MISSING_LEG_RETRY_ATTEMPTS:
                await asyncio.sleep(ENTRY_MISSING_LEG_RETRY_DELAY_SECONDS)
        else:
            if kalshi_filled < contracts or poly_filled < contracts:
                retry_messages.append(f"retry attempts exhausted after {ENTRY_MISSING_LEG_RETRY_ATTEMPTS:g} attempts")

    if kalshi_filled >= contracts and poly_filled >= contracts:
        k_price = float(kalshi_fill_price or candidate["kalshi_price"])
        p_price = float(poly_fill_price or candidate["polymarket_price"])
        k_fee = kalshi_fee(k_price) or 0.0
        p_fee = polymarket_fee(p_price) or 0.0
        position = {
            "dry_run": False,
            "ticker": kalshi_market.get("ticker"),
            "close_time": kalshi_market.get("close_time") or kalshi_market.get("close_ts"),
            "kalshi_side": kalshi_side,
            "polymarket_contract": poly_contract,
            "contracts": contracts,
            "entry_time": iso_utc(),
            "kalshi_fill_price": k_price,
            "polymarket_fill_price": p_price,
            "entry_raw_cost": k_price + p_price,
            "entry_total_fee": k_fee + p_fee,
            "entry_all_in_cost": k_price + p_price + k_fee + p_fee,
            "entry_fee_adjusted_edge": 1.0 - (k_price + p_price + k_fee + p_fee),
            "kalshi_order_id": response_order_id(kalshi_order_for_id),
            "polymarket_order_id": response_order_id(poly_order_for_id),
        }
        retry_text = f" | retries {retry_messages[-3:]}" if retry_messages else ""
        return position, (
            f"ENTRY FILLED {candidate['name']} size {contracts:g} | "
            f"K {kalshi_side.upper()} {fmt_cents(k_price)} order {position['kalshi_order_id']} + "
            f"P {poly_contract} {fmt_cents(p_price)} order {position['polymarket_order_id']} | "
            f"all-in {fmt_cents(position['entry_all_in_cost'])} edge {fmt_money(position['entry_fee_adjusted_edge'])}"
            f"{retry_text}"
        ), False

    cleanup_attempts: list[tuple[str, str, Any]] = []
    remaining_kalshi_filled = filled_contract_units(kalshi_filled)
    remaining_poly_filled = filled_contract_units(poly_filled)
    if remaining_kalshi_filled > 0:
        cleanup_attempts.append(
            (
                f"Kalshi {kalshi_side.upper()} sell size {remaining_kalshi_filled:g}",
                kalshi_side,
                asyncio.to_thread(kalshi_exit_position, str(kalshi_market["ticker"]), kalshi_side, remaining_kalshi_filled),
            )
        )
    if remaining_poly_filled > 0:
        cleanup_attempts.append(
            (
                f"Polymarket {poly_contract} sell size {remaining_poly_filled:g}",
                "",
                asyncio.to_thread(polymarket_exit_position, polymarket_market, poly_contract, remaining_poly_filled),
            )
        )
    cleanup_results: list[Any] = []
    cleanup_messages: list[str] = []
    if cleanup_attempts:
        cleanup_results = list(
            await asyncio.gather(*(attempt[2] for attempt in cleanup_attempts), return_exceptions=True)
        )
        cleanup_messages = [
            cleanup_result_message(label, result, side)
            for (label, side, _task), result in zip(cleanup_attempts, cleanup_results, strict=False)
        ]
    any_fill = kalshi_filled > 0 or poly_filled > 0
    cleanup_ok = not cleanup_attempts or all(not isinstance(result, Exception) for result in cleanup_results)
    block_reentry = any_fill and not cleanup_ok
    remaining_kalshi = 0
    remaining_poly = 0
    if block_reentry:
        for (label, _side, _task), result in zip(cleanup_attempts, cleanup_results, strict=False):
            if not isinstance(result, Exception):
                continue
            if label.startswith("Kalshi"):
                remaining_kalshi = remaining_kalshi_filled
            elif label.startswith("Polymarket"):
                remaining_poly = remaining_poly_filled
    partial_position = None
    if remaining_kalshi > 0 or remaining_poly > 0:
        partial_position = {
            "dry_run": False,
            "partial_entry": True,
            "needs_exit": True,
            "ticker": kalshi_market.get("ticker"),
            "close_time": kalshi_market.get("close_time") or kalshi_market.get("close_ts"),
            "kalshi_side": kalshi_side,
            "polymarket_contract": poly_contract,
            "contracts": max(remaining_kalshi, remaining_poly),
            "kalshi_contracts": remaining_kalshi,
            "polymarket_contracts": remaining_poly,
            "entry_time": iso_utc(),
            "kalshi_fill_price": float(kalshi_fill_price or candidate["kalshi_price"]) if kalshi_filled > 0 else None,
            "polymarket_fill_price": float(poly_fill_price or candidate["polymarket_price"]) if poly_filled > 0 else None,
            "entry_raw_cost": (float(kalshi_fill_price or candidate["kalshi_price"]) if kalshi_filled > 0 else 0.0)
            + (float(poly_fill_price or candidate["polymarket_price"]) if poly_filled > 0 else 0.0),
            "entry_total_fee": 0.0,
            "entry_all_in_cost": math.nan,
            "entry_fee_adjusted_edge": math.nan,
            "kalshi_order_id": response_order_id(kalshi_result if isinstance(kalshi_result, dict) else None),
            "polymarket_order_id": response_order_id(poly_result if isinstance(poly_result, dict) else None),
        }
    block_text = " | ENTRY BLOCKED unresolved partial fill" if block_reentry else ""
    retry_text = f"; retries {retry_messages[-3:]}" if retry_messages else ""
    return partial_position, (
        f"ENTRY FAILED/PARTIAL | K result {type(kalshi_result).__name__} filled {kalshi_filled:g}; "
        f"P result {type(poly_result).__name__} filled {poly_filled:g}{retry_text}; cleanup {cleanup_messages}{block_text}"
    ), block_reentry


async def execute_emergency_exit(
    position: dict[str, Any],
    polymarket_market: dict[str, Any],
) -> tuple[bool, str]:
    label = "PARTIAL CLEANUP EXIT" if position.get("partial_entry") or position.get("needs_exit") else "EMERGENCY EXIT"
    if position.get("dry_run"):
        return True, (
            f"DRY {label} {position.get('ticker')} | "
            f"K {str(position.get('kalshi_side')).upper()} + P {position.get('polymarket_contract')}"
        )
    default_contracts = int(position.get("contracts") or 0)
    kalshi_contracts = int(position.get("kalshi_contracts", default_contracts) or 0)
    poly_contracts = int(position.get("polymarket_contracts", default_contracts) or 0)
    if kalshi_contracts <= 0 and poly_contracts <= 0:
        return True, f"{label} skipped: no contracts tracked"

    exit_attempts: list[tuple[str, str, str, int, Any]] = []
    if kalshi_contracts > 0:
        kalshi_side = str(position["kalshi_side"])
        kalshi_chunk = min(kalshi_contracts, EMERGENCY_EXIT_MAX_CHUNK_CONTRACTS)
        if kalshi_chunk >= KALSHI_MIN_ORDER_CONTRACTS:
            exit_attempts.append(
                (
                    f"Kalshi {kalshi_side.upper()} sell size {kalshi_chunk:g}/{kalshi_contracts:g}",
                    kalshi_side,
                    "kalshi",
                    kalshi_chunk,
                    asyncio.to_thread(
                        kalshi_exit_position,
                        str(position["ticker"]),
                        kalshi_side,
                        kalshi_chunk,
                    ),
                )
            )
    if poly_contracts > 0:
        poly_contract = str(position["polymarket_contract"])
        poly_chunk = min(poly_contracts, EMERGENCY_EXIT_MAX_CHUNK_CONTRACTS)
        if poly_chunk >= POLYMARKET_MIN_EXIT_CONTRACTS:
            exit_attempts.append(
                (
                    f"Polymarket {poly_contract} sell size {poly_chunk:g}/{poly_contracts:g}",
                    "",
                    "polymarket",
                    poly_chunk,
                    asyncio.to_thread(
                        polymarket_exit_position,
                        polymarket_market,
                        poly_contract,
                        poly_chunk,
                    ),
                )
            )

    if not exit_attempts:
        return False, (
            f"{label} INCOMPLETE | no exit chunk met minimum size "
            f"(Kalshi remaining {kalshi_contracts:g}, Polymarket remaining {poly_contracts:g})"
        )
    results = list(await asyncio.gather(*(attempt[4] for attempt in exit_attempts), return_exceptions=True))
    messages = [
        cleanup_result_message(label_text, result, side)
        for (label_text, side, _venue, _requested, _task), result in zip(exit_attempts, results, strict=False)
    ]
    for (_label_text, side, venue, requested, _task), result in zip(exit_attempts, results, strict=False):
        exited = exit_filled_contracts(result, requested, side)
        if exited <= 0:
            continue
        if venue == "kalshi":
            kalshi_contracts = max(0, kalshi_contracts - exited)
            position["kalshi_contracts"] = kalshi_contracts
        elif venue == "polymarket":
            poly_contracts = max(0, poly_contracts - exited)
            position["polymarket_contracts"] = poly_contracts
    ok = kalshi_contracts <= 0 and poly_contracts <= 0
    if ok:
        position["needs_exit"] = False
    return ok, f"{label} {'COMPLETE' if ok else 'INCOMPLETE'} | " + " | ".join(messages)


def status_line(
    runtime: ContractRuntime,
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    source_snapshot: dict[str, Any],
    profit_margin: float,
    contracts: int,
    active_position: dict[str, Any] | None,
) -> str:
    remaining = seconds_to_expiry(kalshi_snapshot)
    candidates = arbitrage_candidates(kalshi_snapshot, polymarket_snapshot, profit_margin, contracts)
    by_name = {candidate["name"]: candidate for candidate in candidates}
    k_np = by_name.get("K+NP", {})
    nk_p = by_name.get("NK+P", {})
    last = runtime.last_model_decision()
    remaining_text = f"{remaining:.1f}" if remaining is not None else "--"
    status_label = f"STATUS T={remaining_text}s"
    line1 = (
        f"{status_label:<24} | "
        f"K+NP raw {fmt_cents(k_np.get('raw_cost'))} all-in {fmt_cents(k_np.get('all_in_cost'))} "
        f"edge {fmt_money(k_np.get('fee_adjusted_edge'))} | "
        f"NK+P raw {fmt_cents(nk_p.get('raw_cost'))} all-in {fmt_cents(nk_p.get('all_in_cost'))} "
        f"edge {fmt_money(nk_p.get('fee_adjusted_edge'))}"
    )
    line2 = (
        f"{iso_utc()} | "
        f"BRTI {fmt_price(source_snapshot.get('kalshi_price'), 2)} "
        f"{fmt_price_delta(source_snapshot.get('kalshi_price'), source_snapshot.get('kalshi_target'))} | "
        f"RTDS {fmt_price(source_snapshot.get('polymarket_price'), 2)} "
        f"{fmt_price_delta(source_snapshot.get('polymarket_price'), source_snapshot.get('polymarket_target'))} | "
        f"strategy={ENTRY_STRATEGY_NAME} latch_tradable={runtime.tradable} "
        f"latch_horizon={runtime.latch_horizon or '--'} latch_action={runtime.latch_action or '--'}"
    )
    line3 = (
        f"{runtime.ticker:<24} | "
        f"model={last.get('horizon', '--')} prob={fmt_price(last.get('diverge_prob'), 4)} "
        f"threshold={fmt_price(last.get('threshold'), 4)} model_pass={last.get('model_pass', '--')} | "
        f"active_trade={bool(active_position)}"
    )
    return "\n".join([line1, line2, line3])


def model_decision_log_line(
    ticker: str,
    horizon: HorizonModel,
    decision: dict[str, Any],
    old_tradable: bool,
    detail: str,
) -> str:
    model_label = f"MODEL {horizon.name} status={decision['status']}"
    line1 = (
        f"{model_label:<24} | "
        f"diverge_prob={fmt_price(decision.get('diverge_prob'), 4)} "
        f"threshold={horizon.threshold:.4f} model_pass={decision.get('model_pass')}"
    )
    line2 = (
        f"{iso_utc()} | "
        f"strategy={ENTRY_STRATEGY_NAME} role={decision.get('strategy_role')} "
        f"latch_action={decision.get('latch_action')}"
    )
    line3 = (
        f"{ticker:<24} | "
        f"latch_tradable={decision.get('latch_tradable')} "
        f"latch_horizon={decision.get('latch_horizon') or '--'} "
        f"(was {old_tradable}){detail}"
    )
    return "\n".join([line1, line2, line3])


async def feature_sampler_loop(
    context_getter: Any,
    shared: TraderSharedState,
    profit_margin: float,
    contracts: int,
    csv_dir: Path,
    csv_save_interval: float,
) -> None:
    while True:
        loop_started = time.monotonic()
        try:
            context = context_getter()
            kalshi_market, kalshi_snapshot, _polymarket_market, polymarket_snapshot, source_snapshot = await context.snapshot()
            ticker = str(kalshi_snapshot.get("ticker") or kalshi_market.get("ticker") or "")
            sample_bucket = int(datetime.now(timezone.utc).timestamp() // MODEL_SAMPLE_INTERVAL_SECONDS)
            row_to_write: dict[str, Any] | None = None
            csv_path: Path | None = None

            async with shared.lock:
                runtime = shared.runtime
                if runtime is not None and runtime.ticker == ticker and shared.last_sample_bucket != sample_bucket:
                    row = build_csv_row(
                        kalshi_snapshot,
                        polymarket_snapshot,
                        source_snapshot,
                        runtime,
                        profit_margin,
                        contracts,
                        shared.active_position,
                    )
                    runtime.history.append(row)
                    shared.last_sample_bucket = sample_bucket
                    now = time.monotonic()
                    if now - runtime.last_csv_save_at >= csv_save_interval:
                        runtime.last_csv_save_at = now
                        row_to_write = row
                        csv_path = csv_path_for_contract(csv_dir, ticker)

            if row_to_write is not None and csv_path is not None:
                append_csv_row(csv_path, row_to_write)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            append_log(f"FEATURE SAMPLER ERROR {type(exc).__name__}: {exc}", concise=True)

        elapsed = time.monotonic() - loop_started
        await asyncio.sleep(max(0.1, MODEL_SAMPLE_INTERVAL_SECONDS - elapsed))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BTC 15m Kalshi/Polymarket horizon-model arbitrage trader v2.")
    parser.add_argument("--contracts", type=int, default=1, help="Contracts per leg. Default: 1.")
    parser.add_argument("--profit-margin", type=float, default=0.03, help="Required fee-adjusted edge per contract. Default: 0.03.")
    parser.add_argument("--csv-save-interval", type=float, default=2.0, help="Seconds between contract CSV rows. Default: 2.")
    parser.add_argument("--csv-dir", type=Path, default=DEFAULT_CSV_DIR, help=f"Directory for contract CSVs. Default: {DEFAULT_CSV_DIR}.")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR, help=f"Horizon model directory. Default: {DEFAULT_MODEL_DIR}.")
    parser.add_argument("--dry-run", action="store_true", help="Log trades without placing orders.")
    parser.add_argument(
        "--debug-model-features",
        action="store_true",
        help="Print and log the exact ordered feature vector sent to each horizon model before prediction.",
    )
    return parser.parse_args()


async def run() -> None:
    args = parse_args()
    contracts = max(1, int(args.contracts))
    profit_margin = max(0.0, float(args.profit_margin))
    csv_save_interval = max(0.25, float(args.csv_save_interval))
    models = load_horizon_models(args.model_dir)

    append_log(
        f"START cli_trader_v2 contracts={contracts} profit_margin={profit_margin:.4f} "
        f"csv_interval={csv_save_interval:g}s dry_run={args.dry_run} "
        f"debug_model_features={args.debug_model_features} models={','.join(models)}",
        concise=True,
    )
    context = AsyncMarketContext(fetch_market_state, logger=lambda line: append_log(line))
    await context.start()
    shared = TraderSharedState()

    def current_context() -> AsyncMarketContext:
        return context

    sampler_task = asyncio.create_task(
        feature_sampler_loop(
            current_context,
            shared,
            profit_margin,
            contracts,
            args.csv_dir,
            csv_save_interval,
        ),
        name="feature-sampler-v2",
    )

    runtime: ContractRuntime | None = None
    active_position: dict[str, Any] | None = None
    balance_recorded_events: set[tuple[str, str]] = set()

    try:
        while True:
            kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot = await context.wait_for_update(timeout=0.5)
            ticker = str(kalshi_snapshot.get("ticker") or kalshi_market.get("ticker") or "")
            close_time = str(kalshi_snapshot.get("close_time") or kalshi_market.get("close_time") or "")
            remaining = seconds_to_expiry(kalshi_snapshot)

            if remaining is not None and remaining <= -2:
                if runtime is not None and ticker == runtime.ticker:
                    append_log(f"CONTRACT ROLLOVER {ticker} expired; refreshing active market")
                if active_position is not None:
                    append_log(f"POSITION CLEAR {active_position.get('ticker')} reached expiry", concise=True)
                    active_position = None
                async with shared.lock:
                    shared.runtime = None
                    shared.active_position = None
                    shared.last_sample_bucket = None
                KALSHI_MARKET_CACHE.pop(SERIES_TICKER, None)
                await context.stop()
                context = AsyncMarketContext(fetch_market_state, logger=lambda line: append_log(line))
                await context.start()
                runtime = None
                continue

            if runtime is None or ticker != runtime.ticker:
                runtime = ContractRuntime(ticker=ticker, close_time=close_time)
                async with shared.lock:
                    shared.runtime = runtime
                    shared.active_position = active_position
                    shared.last_sample_bucket = None
                for horizon in models.values():
                    horizon.collection_started = False
                    horizon.evaluated = False
                    horizon.last_prob = None
                    horizon.last_status = ""
                append_log(
                    f"CONTRACT {ticker} | close {close_time} | Polymarket {polymarket_snapshot.get('ticker') or '--'} | "
                    f"K target {fmt_price(source_snapshot.get('kalshi_target'), 2)} | "
                    f"P target {fmt_price(source_snapshot.get('polymarket_target'), 2)}",
                    concise=True,
                )
                balance_event_key = (ticker, "contract_start")
                if balance_event_key not in balance_recorded_events:
                    await record_balance_snapshot(
                        args.csv_dir,
                        balance_event="contract_start",
                        ticker=ticker,
                        close_time=close_time,
                        remaining=remaining,
                        polymarket_snapshot=polymarket_snapshot,
                        source_snapshot=source_snapshot,
                    )
                    balance_recorded_events.add(balance_event_key)

            now = time.monotonic()

            if remaining is not None:
                balance_event_key = (ticker, "contract_midpoint")
                if (
                    remaining <= BALANCE_MIDPOINT_SECONDS_TO_EXPIRY
                    and balance_event_key not in balance_recorded_events
                ):
                    await record_balance_snapshot(
                        args.csv_dir,
                        balance_event="contract_midpoint",
                        ticker=ticker,
                        close_time=close_time,
                        remaining=remaining,
                        polymarket_snapshot=polymarket_snapshot,
                        source_snapshot=source_snapshot,
                    )
                    balance_recorded_events.add(balance_event_key)

                for horizon in models.values():
                    if not horizon.collection_started and remaining <= horizon.seconds + WINDOW_SECONDS:
                        horizon.collection_started = True
                        append_log(
                            f"COLLECT {horizon.name} window started for {ticker}; evaluation at T={horizon.seconds}s",
                            concise=True,
                        )

                for horizon in models.values():
                    if horizon.evaluated or remaining > horizon.seconds:
                        continue
                    decision = evaluate_horizon_model(
                        runtime,
                        horizon,
                        debug_features=args.debug_model_features,
                        logger=lambda message: append_log(message),
                    )
                    horizon.evaluated = True
                    horizon.last_status = str(decision.get("status") or "")
                    horizon.last_prob = finite_float(decision.get("diverge_prob"))
                    decision = apply_latch_hold_decision(runtime, decision)
                    old_tradable = bool(decision.get("old_tradable"))
                    runtime.horizon_decisions[horizon.name] = decision
                    detail = ""
                    if decision["status"] != "ok":
                        detail = (
                            f" rows={fmt_price(decision.get('window_rows'), 0)}/"
                            f"{fmt_price(decision.get('expected_window_rows'), 0)}"
                            f" min={fmt_price(decision.get('min_window_rows'), 0)}"
                            f" sampled={fmt_price(decision.get('sampled_history_rows'), 0)}"
                            f" raw={fmt_price(decision.get('raw_history_rows'), 0)}"
                            f" asof_gap={fmt_price(decision.get('asof_gap_seconds'), 1)}s"
                        )
                    append_log(
                        model_decision_log_line(ticker, horizon, decision, old_tradable, detail),
                        concise=True,
                        prefix_timestamp=False,
                    )
                    if decision.get("partial_window"):
                        append_log(
                            f"MODEL WARNING {horizon.name} {ticker} | partial feature window "
                            f"rows={fmt_price(decision.get('window_rows'), 0)}/"
                            f"{fmt_price(decision.get('expected_window_rows'), 0)} "
                            f"min={fmt_price(decision.get('min_window_rows'), 0)} "
                            f"status={decision['status']} asof_gap={fmt_price(decision.get('asof_gap_seconds'), 1)}s",
                            concise=True,
                        )
                    if active_position is not None and active_position.get("needs_exit"):
                        active_position["last_emergency_exit_attempt_monotonic"] = time.monotonic()
                        ok, message = await execute_emergency_exit(active_position, polymarket_market)
                        append_log(message, concise=True)
                        if ok:
                            active_position = None
                            runtime.entry_blocked_reason = ""
                            async with shared.lock:
                                shared.active_position = None

            exit_required = active_position is not None and active_position.get("needs_exit")
            if exit_required:
                last_exit_attempt = float(active_position.get("last_emergency_exit_attempt_monotonic") or 0.0)
                if now - last_exit_attempt >= 2.0:
                    active_position["last_emergency_exit_attempt_monotonic"] = now
                    ok, message = await execute_emergency_exit(active_position, polymarket_market)
                    append_log(message, concise=True)
                    if ok:
                        active_position = None
                        runtime.entry_blocked_reason = ""
                        async with shared.lock:
                            shared.active_position = None

            if now - runtime.last_status_log_at >= STATUS_LOG_INTERVAL_SECONDS:
                append_log(
                    status_line(
                        runtime,
                        kalshi_snapshot,
                        polymarket_snapshot,
                        source_snapshot,
                        profit_margin,
                        contracts,
                        active_position,
                    ),
                    prefix_timestamp=False,
                )
                runtime.last_status_log_at = now

            if active_position is not None or runtime.entry_in_progress or runtime.entry_blocked_reason or not runtime.tradable:
                continue
            if remaining is None or remaining <= MIN_ENTRY_SECONDS_TO_EXPIRY:
                continue
            best = best_arbitrage_candidate(kalshi_snapshot, polymarket_snapshot, profit_margin, contracts)
            if best is None or not best["profitable"]:
                continue
            if context.failsafe_required():
                await context.refresh_after_event("pre-trade stale websocket")
            runtime.entry_in_progress = True
            runtime.entry_attempt_count += 1
            try:
                position, message, block_reentry = await execute_entry(
                    kalshi_market,
                    polymarket_market,
                    contracts,
                    profit_margin,
                    args.dry_run,
                )
            finally:
                runtime.entry_in_progress = False
            append_log(message, concise=True)
            if block_reentry:
                runtime.entry_blocked_reason = message
            if position is not None:
                active_position = position
            async with shared.lock:
                shared.active_position = active_position
    except asyncio.CancelledError:
        raise
    finally:
        sampler_task.cancel()
        await asyncio.gather(sampler_task, return_exceptions=True)
        await context.stop()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        append_log("STOP cli_trader_v2 interrupted", concise=True)
    except Exception as exc:
        append_log(f"FATAL {type(exc).__name__}: {exc}\n{traceback.format_exc()}", concise=True)
        raise


if __name__ == "__main__":
    main()
