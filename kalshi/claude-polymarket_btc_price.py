"""
Polymarket RTDS — Live BTC/USD (Chainlink) Price Feed
Connects to Polymarket's Real-Time Data Socket and streams the exact
Chainlink BTC/USD price used to resolve 15m BTC Up/Down markets.

WebSocket: wss://ws-live-data.polymarket.com
Topic:     crypto_prices_chainlink
Symbol:    btc/usd
Auth:      None required

Requirements:
    pip install websockets

Usage:
    python polymarket_btc_price.py
"""

import asyncio
import json
import time
from datetime import datetime, timezone

try:
    import websockets
except ImportError:
    raise SystemExit("websockets not installed. Run: pip install websockets")

WS_URL    = "wss://ws-live-data.polymarket.com"
SYMBOL    = "btc/usd"
PING_INTERVAL = 5   # seconds — required by Polymarket to keep connection alive

SUBSCRIBE_MSG = json.dumps({
    "action": "subscribe",
    "subscriptions": [
        {
            "topic": "crypto_prices_chainlink",
            "type": "*",
            "filters": json.dumps({"symbol": SYMBOL}),
        }
    ],
})


def fmt_time(ms_timestamp: int | None) -> str:
    if ms_timestamp is None:
        return "—"
    return datetime.fromtimestamp(ms_timestamp / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3]  # trim to milliseconds


async def ping_loop(ws):
    """Send a PING every 5 s to keep the connection alive."""
    while True:
        await asyncio.sleep(PING_INTERVAL)
        try:
            await ws.send("PING")
        except Exception:
            break


async def stream():
    reconnect_delay = 1

    print(f"Connecting to Polymarket RTDS…")
    print(f"Topic : crypto_prices_chainlink")
    print(f"Symbol: {SYMBOL}")
    print(f"Source: Chainlink Data Streams (via Polymarket)\n")
    print(f"{'Time received (UTC)':<28}  {'Price (USD)':>14}  {'Feed ts (UTC)'}")
    print("─" * 75)

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=None,   # we handle pings manually
                open_timeout=10,
            ) as ws:
                reconnect_delay = 1   # reset on successful connect
                await ws.send(SUBSCRIBE_MSG)

                ping_task = asyncio.create_task(ping_loop(ws))

                async for raw in ws:
                    # Ignore PONG heartbeats
                    if isinstance(raw, str) and raw.strip().upper() in ("PONG", "PING"):
                        continue

                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    topic   = msg.get("topic", "")
                    mtype   = msg.get("type", "")
                    payload = msg.get("payload", {})

                    # ── Snapshot (historical last ~2 min) ──────────────────
                    if mtype == "snapshot" and topic == "crypto_prices_chainlink":
                        items = payload if isinstance(payload, list) else [payload]
                        for item in items:
                            sym   = item.get("symbol", "")
                            val   = item.get("value")
                            ts    = item.get("timestamp")
                            recv  = msg.get("timestamp")
                            if val is not None:
                                print(
                                    f"[SNAP] {fmt_time(recv):<23}  ${float(val):>13,.2f}  "
                                    f"{fmt_time(ts)}  {sym}"
                                )
                        print("─" * 75)
                        print("  ↳ live stream starting…\n")
                        continue

                    # ── Live update ────────────────────────────────────────
                    if mtype == "update" and topic == "crypto_prices_chainlink":
                        sym   = payload.get("symbol", "")
                        val   = payload.get("value")
                        ts    = payload.get("timestamp")
                        recv  = msg.get("timestamp")

                        if val is None or sym.lower() != SYMBOL:
                            continue

                        recv_str  = fmt_time(recv)
                        feed_str  = fmt_time(ts)

                        # latency: difference between feed timestamp and received timestamp
                        latency_ms = (recv - ts) if (recv and ts) else None
                        lat_str = f"  ({latency_ms:+d} ms)" if latency_ms is not None else ""

                        print(
                            f"{recv_str:<28}  ${float(val):>13,.2f}  {feed_str}{lat_str}"
                        )

        except (websockets.exceptions.ConnectionClosed,
                ConnectionResetError, OSError) as exc:
            print(f"\n[!] Connection lost: {exc}. Reconnecting in {reconnect_delay}s…\n")
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        except Exception as exc:
            print(f"\n[!] Unexpected error: {exc}. Reconnecting in {reconnect_delay}s…\n")
        finally:
            try:
                ping_task.cancel()
            except Exception:
                pass

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 30)  # exponential backoff, cap 30s


if __name__ == "__main__":
    asyncio.run(stream())
