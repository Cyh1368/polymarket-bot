#!/usr/bin/env python3
import asyncio
import base64
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import kalshi_btc15_server as btc


MarketState = tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any) -> float | None:
    number = _as_float(value)
    if number is None or number <= 0:
        return None
    return number


def _best_bid_ask_from_message(message: dict[str, Any]) -> tuple[float | None, float | None]:
    bid = _positive_float(message.get("best_bid"))
    ask = _positive_float(message.get("best_ask"))
    bids = message.get("bids")
    if bid is None and isinstance(bids, list):
        prices = [
            price
            for item in bids
            if isinstance(item, dict)
            and _as_float(item.get("size")) not in (None, 0.0)
            and (price := _positive_float(item.get("price"))) is not None
        ]
        bid = max(prices) if prices else None
    asks = message.get("asks")
    if ask is None and isinstance(asks, list):
        prices = [
            price
            for item in asks
            if isinstance(item, dict)
            and _as_float(item.get("size")) not in (None, 0.0)
            and (price := _positive_float(item.get("price"))) is not None
        ]
        ask = min(prices) if prices else None
    return bid, ask


def _book_signature(kalshi_snapshot: dict[str, Any], polymarket_snapshot: dict[str, Any]) -> tuple[Any, ...]:
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


def _kalshi_ws_headers() -> dict[str, str]:
    key_id = (
        os.getenv("KALSHI_API_ID")
        or os.getenv("KALSHI_KEY_ID")
        or os.getenv("KALSHI_API_KEY_ID")
        or os.getenv("KALSHI_ACCESS_KEY")
    )
    pem = btc.private_key_pem()
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


class AsyncMarketContext:
    """Maintains local market snapshots and wakes the trader on websocket updates."""

    def __init__(
        self,
        fetch_market_state: Callable[[], MarketState],
        *,
        logger: Callable[[str], None] | None = None,
        report_interval: float = 2.0,
        stale_seconds: float = 5.0,
    ) -> None:
        self.fetch_market_state = fetch_market_state
        self.logger = logger or (lambda _line: None)
        self.report_interval = max(0.25, report_interval)
        self.stale_seconds = max(1.0, stale_seconds)
        self.lock = asyncio.Lock()
        self.update_event = asyncio.Event()
        self._state: MarketState | None = None
        self._last_signature: tuple[Any, ...] | None = None
        self._last_source: tuple[Any, Any] | None = None
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
            asyncio.create_task(self._kalshi_ws_loop(), name="kalshi-ws"),
            asyncio.create_task(self._polymarket_ws_loop(), name="polymarket-ws"),
            asyncio.create_task(self._source_loop(), name="source-ws"),
        ]

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def bootstrap(self, reason: str) -> MarketState:
        state = await asyncio.to_thread(self.fetch_market_state)
        async with self.lock:
            self._state = state
            self._last_signature = _book_signature(state[1], state[3])
            self._last_source = (state[4].get("kalshi_price"), state[4].get("polymarket_price"))
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
            state = await asyncio.to_thread(self.fetch_market_state)
        except Exception as exc:
            self.logger(f"WEBSOCKET refresh failed ({reason}): {type(exc).__name__}: {exc}")
            return
        async with self.lock:
            old_signature = self._last_signature
            old_source = self._last_source
            self._state = state
            self._last_signature = _book_signature(state[1], state[3])
            self._last_source = (state[4].get("kalshi_price"), state[4].get("polymarket_price"))
            changed = old_signature != self._last_signature or old_source != self._last_source
        if changed:
            self.update_event.set()

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
                url = os.getenv("KALSHI_WS_URL", "wss://external-api-ws.kalshi.com/trade-api/ws/v2")
                headers = _kalshi_ws_headers()
                async with websockets.connect(
                    url,
                    additional_headers=headers,
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
        if msg_type in ("orderbook_snapshot", "orderbook_delta"):
            await self.refresh_after_event(f"kalshi {msg_type}")
            return True
        return False

    async def _apply_kalshi_ticker(self, message: dict[str, Any]) -> bool:
        yes_bid = _positive_float(message.get("yes_bid_dollars") or message.get("yes_bid"))
        yes_ask = _positive_float(message.get("yes_ask_dollars") or message.get("yes_ask"))
        if yes_bid is None and yes_ask is None:
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
            old_signature = self._last_signature
            self._state = (kalshi_market, updated, polymarket_market, polymarket_snapshot, source_snapshot)
            self._last_signature = _book_signature(updated, polymarket_snapshot)
            return old_signature != self._last_signature

    async def _polymarket_ws_loop(self) -> None:
        import websockets

        backoff = 1.0
        while self._running:
            try:
                state = await self.snapshot()
                token_ids = self._polymarket_token_ids(state[2])
                if not token_ids:
                    await asyncio.sleep(backoff)
                    continue
                url = os.getenv(
                    "POLYMARKET_MARKET_WS_URL",
                    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                )
                async with websockets.connect(
                    url,
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

    def _polymarket_token_ids(self, market: dict[str, Any]) -> list[str]:
        token_ids = btc.parse_json_list(market.get("clobTokenIds"))
        return [str(token_id) for token_id in token_ids if token_id not in (None, "")]

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
                changed = True
        if changed and self._state is None:
            await self.refresh_after_event("polymarket book")
        return changed

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
                bid, ask = _best_bid_ask_from_message(payload)
                prefix = contract.lower()
                if bid != updated.get(f"{prefix}_bid"):
                    updated[f"{prefix}_bid"] = bid
                    changed = True
                if ask != updated.get(f"{prefix}_ask"):
                    updated[f"{prefix}_ask"] = ask
                    changed = True
            if not changed:
                return False
            old_signature = self._last_signature
            self._state = (kalshi_market, kalshi_snapshot, polymarket_market, updated, source_snapshot)
            self._last_signature = _book_signature(kalshi_snapshot, updated)
            return old_signature != self._last_signature

    def _polymarket_contract_by_token(self, market: dict[str, Any]) -> dict[str, str]:
        token_ids = btc.parse_json_list(market.get("clobTokenIds"))
        outcomes = [str(outcome).upper() for outcome in btc.parse_json_list(market.get("outcomes"))]
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
        while self._running:
            try:
                subscribe_msg = json.dumps(
                    {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "crypto_prices_chainlink",
                                "type": "*",
                                "filters": json.dumps({"symbol": btc.POLYMARKET_RTDS_SYMBOL}),
                            }
                        ],
                    }
                )
                async with websockets.connect(
                    btc.POLYMARKET_RTDS_URL,
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
                            await self._refresh_source_snapshot("source heartbeat")
                            continue
                        self.last_source_update = time.monotonic()
                        if self._source_message_matches(raw):
                            await self._refresh_source_snapshot("source websocket")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.source_connected = False
                self.logger(f"WEBSOCKET source refresh failed: {type(exc).__name__}: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    def _source_message_matches(self, raw: str | bytes) -> bool:
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return False
        payload = message.get("payload") if isinstance(message, dict) else None
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or btc.POLYMARKET_RTDS_SYMBOL).lower()
            if symbol == btc.POLYMARKET_RTDS_SYMBOL.lower():
                return True
            data = item.get("data")
            if isinstance(data, list) and data:
                return True
        return False

    async def _refresh_source_snapshot(self, reason: str) -> None:
        state = await self.snapshot()
        try:
            source = await asyncio.to_thread(btc.source_price_snapshot, state[0], state[2])
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


async def maybe_await(value: Any) -> Any:
    if isinstance(value, Awaitable):
        return await value
    return value
