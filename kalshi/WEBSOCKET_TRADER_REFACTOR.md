# Websocket Trader Refactor

## Summary

`cli_trader.py` now defaults to an asyncio-based websocket event loop instead of only sleeping between HTTP polling cycles. HTTP market fetches remain available and are still used for bootstrap and emergency fallback.

The exit path now has a passive "walking limit" implementation. Instead of immediately using aggressive FOK exit orders, live exits can place a resting sell limit near top-of-book, wait for a configurable interval, cancel if unfilled, and replace one tick closer to the market.

## Files Changed

| File | Purpose |
| --- | --- |
| `cli_trader.py` | Converted main loop to `asyncio`, added websocket CLI controls, added async exit routing, added cancel/GTC helpers. |
| `market_interface.py` | Added `AsyncMarketContext` for HTTP bootstrap, websocket tasks, reconnect loops, local snapshot updates, and stale-feed detection. |
| `logic/exits.py` | Added reusable `LimitWalker` for passive cancel/replace exit behavior. |
| `logic/__init__.py` | Marks `logic` as a package. |

## Runtime Behavior

Startup now bootstraps market state with the existing HTTP `fetch_market_state`. After bootstrap, background tasks subscribe to Kalshi, Polymarket, and source-price websocket streams. The tick loop waits for a market update event or a periodic report timeout.

Events that wake the loop include:

| Event | Effect |
| --- | --- |
| Kalshi ticker/orderbook update | Updates local Kalshi top-of-book and wakes the loop. |
| Polymarket book/price update | Updates local Polymarket top-of-book and wakes the loop. |
| Source price update | Updates BTC source snapshot and wakes the loop. |
| Websocket disconnect | Logs the disconnect, backs off, and reconnects. |

The legacy polling path remains available with `--disable-websocket`.

## Exit Behavior

Live exits go through `execute_position_exit_async` when websocket mode is enabled. If the websocket books are stale or disconnected while a position is live, the bot logs a failsafe message and uses the older HTTP cleanup path.

The walking exit is controlled by:

| Parameter | Default | Meaning |
| --- | ---: | --- |
| `--chase-interval` | `2` | Seconds to wait for a passive fill before cancel/replace. |
| `--chase-max-steps` | `6` | Maximum walking attempts before returning partial/unfilled. |
| `--ws-report-interval` | `2` | Maximum time to wait before reporting current websocket state. |
| `--ws-stale-seconds` | `5` | Age after which websocket books are considered unsafe for live cleanup. |

## Live Test

Command used:

```bash
.venv-cli-trader/bin/python cli_trader.py --live --max-trades 1 --contracts 1 --ws-report-interval 2 --chase-interval 2 --chase-max-steps 6
```

Observed behavior:

| Check | Result |
| --- | --- |
| Websocket bootstrap | Passed. The bot logged HTTP bootstrap and began streaming updates. |
| Contract rollover | Passed. The bot rolled from `03:15` to `03:30` and then to the next contract. |
| No-liquidity handling | Passed. High-edge signals were skipped with `CHECK SKIP Kalshi liquidity 0 < 1`. |
| Entry filters | Passed. Large raw edges were rejected by `entry_dist` and `direction` checks. |
| Polymarket disconnect handling | Partially verified. A disconnect was logged and the bot continued running afterward. |
| Walking exit | Not exercised. No live position was opened, so no exit order was placed. |

Balances stayed unchanged during the live test:

| Venue | Balance |
| --- | ---: |
| Kalshi | `$13.0100` |
| Polymarket | `$5.5748` |

## Follow-Up

Add explicit `WEBSOCKET ... connected/reconnected` log lines. The reconnect behavior was inferred from continued market updates after a disconnect, but the log should make recovery unambiguous.
