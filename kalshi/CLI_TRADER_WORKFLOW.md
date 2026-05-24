# `cli_trader.py` Workflow

This document explains how [`cli_trader.py`](cli_trader.py) works, with emphasis on the live trading path, its if/else decisions, and how it manages open or partially open positions.

## Purpose

`cli_trader.py` is a command-line BTC 15-minute arbitrage trader for Kalshi and Polymarket. It continuously:

1. Discovers the active Kalshi BTC 15m market.
2. Finds the matching Polymarket BTC up/down market.
3. Builds order book snapshots for both venues.
4. Computes the best cross-venue arbitrage using `cli_server.best_arbitrage`.
5. Logs snapshots to CSV.
6. Optionally places live fill-or-kill orders when all entry checks pass.
7. Tracks one open position and exits it when hold checks fail or a partial entry needs cleanup.

Dry-run mode is the default. Live orders are only submitted with `--live`.

## Important Constants

| Name | Value | Meaning |
| --- | ---: | --- |
| `SETTLEMENT_PAYOUT_AFTER_FEES` | `0.98` | Assumed payout after the 2 percent winner fee. |
| `CONTRACT_WINDOW_SECONDS` | `900` | BTC 15m contract length. |
| `CONTRACT_BOUNDARY_NO_TRADE_SECONDS` | `60.0` | No new entries in the first or last 60 seconds of a contract. |
| `EXIT_LIMIT_DEVIATION` | `0.01` | Exit orders are priced 1 cent through the current bid/ask reference. |
| `POLYMARKET_MIN_ORDER_NOTIONAL` | `1.0` | Minimum Polymarket notional required before placing an entry order. |
| `PROFIT_CAPTURE_MIN_EDGE` | `0.07` | Minimum executable liquidation edge over entry for take-profit exits. |
| `EXIT_CUSHION` | `0.03` | Minimum non-emergency executable edge over entry for hold-fail exits. |

## Runtime Inputs

`parse_args()` defines the CLI surface:

| Flag | Default | Role |
| --- | ---: | --- |
| `--interval` | `btc.POLL_SECONDS` | Poll cadence in seconds. Clamped to at least `0.1`. |
| `--csv-dir` | `btc.DATA_DIR` | Directory for per-contract CSV snapshots. |
| `--flush-every` | `1` | Number of rows to buffer before writing CSV. |
| `--min-profit` | `0.0` | Raw displayed arbitrage threshold before entry checks run. |
| `--min-adjusted-profit` | `0.02` | Executable profit threshold after live book sizing. |
| `--min-profit-after-fees` | `0.05` | Minimum settlement profit after the 2 percent winner fee. |
| `--source-gap-threshold` | `100.0` | Maximum BTC source price gap allowed for entry and hold. |
| `--target-divergence-threshold` | `35.0` | Maximum target divergence allowed between Kalshi and Polymarket. |
| `--hold-distance-multiplier` | `0.25` | Hold distance threshold as a fraction of entry distance. |
| `--take-profit-exit-value` | `1.04` | Exit a matched position when executable liquidation reaches this total and has cushion. |
| `--profit-capture-min-edge` | `0.07` | Exit a matched position when executable liquidation exceeds entry by this amount. |
| `--exit-cushion` | `0.03` | Minimum non-emergency executable exit edge over entry. |
| `--contracts` | `1` | Maximum matched contracts/shares per venue leg. |
| `--max-trades` | `1` | Maximum counted entries for the process. |
| `--live` | false | Enables real orders. Without it, the trader only simulates. |
| `--once` | false | Run one polling cycle and exit. |
| `--print-arb-orderbook` | false | Print liquidity details when raw arbitrage is profitable. |
| `--book-depth-levels` | `6` | Number of book levels shown by `--print-arb-orderbook`. |
| `--disable-websocket` | false | Use HTTP polling instead of websocket-driven updates. |
| `--ws-report-interval` | `2.0` | Maximum wait between websocket loop reports/refreshes. |
| `--ws-stale-seconds` | `5.0` | Treat websocket books as stale after this many seconds. |
| `--chase-interval` | `2.0` | Seconds to wait before walking a passive exit order. |
| `--chase-max-steps` | `6` | Maximum cancel/replace attempts for a walking exit order. |

## External Modules

The script imports:

- `cli_server as cli`: formatting, CSV writing, arbitrage selection, log output.
- `kalshi_btc15_server as btc`: Kalshi/Polymarket market discovery, order book parsing, auth, source price snapshots.
- `py_clob_client_v2`: imported lazily only when Polymarket balances or orders are needed.

## State Held In Memory

`main()` maintains process-local state:

| Variable | Meaning |
| --- | --- |
| `pending_rows` | Buffered CSV rows keyed by destination path. |
| `trades_done` | Number of counted entries or partial entries. |
| `open_position` | The single currently tracked position, or `None`. |
| `last_contract_key` | Last seen Kalshi ticker, used to detect new contracts. |
| `kalshi_brtis` | Rolling Kalshi source price window used for reference delta display/filter metrics. |

The script does not persist `open_position` across process restarts. Position tracking is in memory only.

## High-Level Polling Loop

`main()` starts by parsing arguments, normalizing thresholds, printing mode/balances, and entering `while True`.

```text
main:
    parse and clamp args
    print mode and startup balances

    while True:
        fetch market state
        compute best arbitrage
        append CSV row to pending buffer
        detect contract changes and update reference deltas

        if open_position is stale because contract changed or expired:
            clear open_position

        boundary_reason = no_trade_boundary_reason(...)

        if new contract:
            print balances and contract header

        if arbitrage exists:
            print the snapshot line

        if open_position exists:
            manage or exit the existing position
        else:
            consider a new entry

        if enough CSV rows are buffered:
            flush rows

        if --once:
            break
        else:
            sleep the remaining poll interval
```

Errors inside the loop are caught and logged. `KeyboardInterrupt` flushes pending rows and re-raises. `FatalTradeError` flushes rows and breaks, although the current code does not raise `FatalTradeError` in the visible trading paths.

## Market Data Path

`fetch_market_state()` is the central market data function:

1. `cached_active_kalshi_market()` returns a cached Kalshi market if it is still valid, otherwise calls `btc.discover_active_market()`.
2. It downloads the Kalshi order book and creates a Kalshi snapshot with `btc.make_snapshot()`.
3. It uses `(ticker, close_time)` as the Polymarket cache key.
4. It discovers the matching Polymarket market with `btc.discover_polymarket_market()` if not cached.
5. It downloads Polymarket CLOB order books and creates the Polymarket snapshot.
6. It computes source price data through `btc.source_price_snapshot()`.

If either active market is missing, the function raises and the loop logs an `ERROR`.

## Contract Boundary Logic

`no_trade_boundary_reason()` suppresses new entries during:

- The first 60 seconds of a 15-minute contract.
- The last 60 seconds before expiry.

For existing positions, boundary behavior is different:

- If a position is open and there is a boundary reason, the script logs `POSITION REVIEW` once.
- It does not automatically exit just because the boundary window begins.
- New entries are blocked while `boundary_reason is not None`.

## Entry and Hold Metrics

`source_filter_metrics()` builds the shared metric dictionary used by both entry and hold filters:

| Metric | Meaning |
| --- | --- |
| `source_gap` | Absolute difference between Kalshi and Polymarket source BTC prices. |
| `target_divergence` | Absolute difference between Kalshi and Polymarket contract targets. |
| `kalshi_distance` | Distance between the Kalshi direction price and Kalshi target. |
| `polymarket_distance` | Distance between Polymarket source price and Polymarket target. |
| `min_distance` | Minimum of Kalshi and Polymarket directional distances. |
| `direction_agreement` | Whether both venues imply the same side of their target. |
| `entry_required_distance` | `max(10.0, seconds_to_expiry * 0.05)`. |
| `profit_after_fees` | `SETTLEMENT_PAYOUT_AFTER_FEES - arb_cost`. |

The Kalshi direction price comes from a rolling simple moving average of Kalshi source prices, not only the latest tick.

## Entry Filter If/Else Structure

`evaluate_entry_filter()` returns `ENTER` only when every check passes:

```text
if Polymarket data has an error:
    SKIP
elif Kalshi status is not active:
    SKIP
elif Kalshi target is missing:
    SKIP
elif Polymarket target is missing:
    SKIP
elif direction_agreement is not True:
    SKIP
elif source_gap is missing or source_gap > threshold:
    SKIP
elif min_distance is missing:
    SKIP
elif min_distance < max(10, seconds_to_expiry * 0.05):
    SKIP
elif target_divergence is missing or target_divergence > threshold:
    SKIP
elif profit_after_fees is missing or profit_after_fees < min_profit_after_fees:
    SKIP
else:
    ENTER
```

In the main loop there are two entry-filter passes:

1. Preliminary filter using a fallback cost: `kalshi_price + polymarket_execution_price(polymarket_price)`.
2. Final filter using executable preflight cost: `preflight["kalshi_price"] + preflight["polymarket_price"]`.

This means a raw arbitrage can pass the first check and still be skipped after live book sizing.

## Hold Filter If/Else Structure

`evaluate_hold_filter()` is similar but intentionally omits Kalshi active status and profit-after-fees checks. It asks whether the original directional thesis is still good enough to hold.

```text
if Polymarket data has an error:
    EXIT_REVIEW
elif Kalshi target is missing:
    EXIT_REVIEW
elif Polymarket target is missing:
    EXIT_REVIEW
elif direction_agreement is not True:
    EXIT_REVIEW
elif source_gap is missing or source_gap > threshold:
    EXIT_REVIEW
elif min_distance is missing:
    EXIT_REVIEW
elif min_distance < hold_multiplier * entry_required_distance:
    EXIT_REVIEW
elif target_divergence is missing or target_divergence > threshold:
    EXIT_REVIEW
else:
    HOLD
```

If hold fails, the main loop logs the liquidation value if available, then calls `execute_position_exit()`.

## Exit Strategy Research Notes

The May 2026 research CSVs show that the worst realized losses came from emergency exits that fired after executable value had already collapsed. In the largest cases, liquidation was near breakeven in the final minute and then fell sharply inside the final 20 to 40 seconds. A pure "force immediate emergency exit" rule captured the cliff rather than avoiding it.

Backtest scope:

- Parsed unique logged trades: 68.
- Trades with usable matching `combined_*.csv` series: 36.
- PnL was measured per matched pair, before exit fees, using recorded best bids for the held Kalshi and Polymarket legs.
- The requested `kp-0524research/` directory was not present in the workspace; the available `kp-0521-research/`, `kp-0521-research-2/`, and `kalshi_btc15m_data/` CSVs were used.

Backtest result:

| Strategy | Total PnL | Avg PnL | Worst PnL | Losses <= -20c |
| --- | ---: | ---: | ---: | ---: |
| Approximate current emergency behavior | `-91.2c` | `-2.5c` | `-94.0c` | `4` |
| Hold to settlement | `+112.9c` | `+3.1c` | `-94.0c` | `2` |
| Max raw-profit protective config | `+154.4c` | `+4.3c` | `-19.0c` | `0` |
| Selected risk-adjusted protective config | `+151.6c` | `+4.2c` | `-12.0c` | `0` |

Selected strategy parameters:

- Keep take-profit exits when executable liquidation exceeds entry by at least `7c`, or when liquidation is at least `104c` with positive cushion.
- In the final `90s`, allow a protective exit when `min_distance <= 25`, direction has flipped, or `held_winners == 0`.
- Only take the protective exit if executable liquidation is at least `86c`, or if it is no worse than `entry_cost - 2c`.
- If `held_winners == 0` and liquidation is at least `80c`, exit immediately.
- If `held_winners == 0` and liquidation is below `80c`, wait up to `30s` for recovery to at least `80c` or at least `entry_cost - 2c`; if no recovery appears, exit at timeout or near-expiry deadline.

This hybrid outperformed both alternatives tested in isolation. Loosening emergency thresholds helps only if the bot exits while there is still acceptable liquidity. Waiting after an emergency helps some cases, but once the book has collapsed it is not reliable enough by itself.

## Liquidity Preflight

`trade_preflight()` converts a raw arbitrage into an executable entry decision.

It computes:

- The Kalshi side and limit price from the arbitrage.
- Kalshi executable liquidity by reading the opposite-side bids. Buying YES at price `p` requires NO bids at least `1 - p`; buying NO works symmetrically.
- Polymarket executable limit price from ask depth. It selects the ask price needed to fill the requested size and also calculates VWAP.
- Executable size as `min(max_contracts, int(kalshi_liquidity), int(poly_liquidity))`.
- Adjusted profit as `(1 / (kalshi_price + poly_order_price)) - 1`, if total cost is between 0 and 1.

Decision branches:

```text
read Kalshi executable liquidity
read Polymarket ask execution plan
executable_contracts = min(max_contracts, Kalshi liquidity, Polymarket liquidity)

if executable_contracts <= 0:
    SKIP: no executable liquidity
elif Polymarket executable price is missing:
    SKIP: insufficient Polymarket liquidity
elif Polymarket notional < 1 USDC:
    SKIP: below minimum notional
elif adjusted_profit <= min_profit:
    SKIP: executable profit too small
else:
    PLACE trade
```

## Main Entry Gate

The main loop only considers a new trade when all of these are true:

```python
open_position is None
and boundary_reason is None
and arbitrage
and arbitrage["expected_profit"] > args.min_profit
and trades_done < max_trades
```

Then it applies the preliminary entry filter, liquidity preflight, final entry filter, and `execute_arbitrage()`.

```text
if open_position is not None:
    do not enter
elif boundary_reason is not None:
    do not enter
elif no raw arbitrage exists:
    do not enter
elif raw expected_profit <= min_profit:
    do not enter
elif trades_done >= max_trades:
    do not enter
else:
    build preliminary source metrics

    if preliminary entry filter fails:
        optionally log ENTRY SKIP
        sleep or continue
    else:
        run trade_preflight()
        log CHECK PLACE or CHECK SKIP
        build executable-cost source metrics

        if final entry filter fails:
            optionally log ENTRY SKIP
        elif preflight decision is not PLACE:
            log ENTRY SKIP with the preflight reason
        else:
            try execute_arbitrage()

            if PartialEntryError is raised:
                track the partial open_position
                trades_done += 1
            elif result starts with SKIP or dry-run skip:
                do not open a position
            else:
                track the open_position
                trades_done += 1
```

## Live Entry Execution

`execute_arbitrage()` is the function that actually submits live orders.

In dry-run mode:

- If preflight is not `PLACE`, it returns `DRY RUN would skip`.
- Otherwise it returns `DRY RUN would place ...`.
- No orders are submitted.

In live mode, the current order sequence is deliberately Kalshi first, then Polymarket.

```text
if preflight decision is not PLACE:
    return SKIP
else:
    run a fresh live_preflight

    if live_preflight decision is not PLACE:
        return SKIP from live recheck
    else:
        submit Kalshi BUY FOK

        if the Kalshi API/order call raises:
            return SKIP before any Polymarket order
        elif Kalshi order state is unknown:
            raise PartialEntryError and reconcile before cleanup
        elif Kalshi fill count <= 0:
            return SKIP and do not submit Polymarket
        elif Kalshi filled count < requested size:
            try to sell or hedge the partial Kalshi fill
            if cleanup succeeds:
                return SKIP with cleanup
            else:
                raise PartialEntryError for retry
        else:
            recheck Polymarket executable liquidity for the filled Kalshi size

            if Polymarket liquidity, notional, or adjusted profit is no longer acceptable:
                try to sell or hedge the Kalshi leg
                if cleanup succeeds:
                    return SKIP with cleanup
                else:
                    raise PartialEntryError for retry
            else:
                submit Polymarket BUY FOK

                if the Polymarket API/order call raises or fill is not verified:
                    try to sell or hedge the Kalshi leg
                    if cleanup succeeds:
                        return SKIP with cleanup
                    else:
                        raise PartialEntryError for retry
                else:
                    return TRADED with actual fills
```

### Why Kalshi First?

The current code buys Kalshi first and verifies the fill before submitting the Polymarket hedge. If Kalshi fails or cannot be verified filled, no Polymarket order is sent. After Kalshi fills, every later failure path attempts to sell the Kalshi leg or buy the opposite Kalshi side as an economic hedge. If cleanup fails, the script raises `PartialEntryError` and records the exposure in `open_position` for retry on the next tick.

### Live Recheck

Even though `main()` already ran `trade_preflight()`, live mode runs it again immediately before submitting orders. This catches stale order book conditions between the displayed check and the actual order.

### Partial Entry Recovery

`PartialEntryError` wraps a position dictionary created by `partial_entry_position()`. The position records:

- Which leg exists or is absent.
- Number of contracts per leg.
- Fill prices known so far.
- Whether each leg has already been exited.
- `exit_started=True`, which makes the next loop call `execute_position_exit()` immediately.

This allows cleanup to continue across polling cycles instead of losing track of a filled single-leg exposure.

## Live Exit Execution

`execute_position_exit()` handles both normal exits and partial-entry cleanup.

In dry-run mode it returns a simulated exit and marks it complete. In live mode:

1. It validates position size and leg identifiers.
2. It marks `exit_started=True`.
3. It decides which legs still need exit based on `kalshi_absent`, `polymarket_absent`, `kalshi_exited`, and `polymarket_exited`.
4. If both legs need exit, it exits them concurrently with two worker threads.
5. If only one leg needs exit, it exits that one.
6. If any required leg remains unexited, it returns `EXIT PARTIAL` and `False`; the next tick retries.
7. If all required legs are exited, it returns an `EXITED ...` PnL summary and `True`.

```text
if position is invalid:
    return EXIT FAILED, complete false
elif not live:
    return dry-run simulated exit, complete true
else:
    mark exit_started

    if Kalshi order state is unknown:
        reconcile the Kalshi order
        if state is still unknown:
            return EXIT WAIT, complete false

    compute need_poly and need_kalshi

    if both legs need exit:
        exit Polymarket and Kalshi concurrently
    elif only Polymarket needs exit:
        exit Polymarket
    elif only Kalshi needs exit:
        exit Kalshi
    else:
        make no new exit attempts

    if any required leg is still open:
        return EXIT PARTIAL, complete false
    else:
        build exit-value summary
        return EXITED, complete true
```

### Kalshi Exit Logic

`kalshi_exit_position()` first tries to sell the held side:

```text
read current bid for the held Kalshi side

if bid exists:
    submit sell FOK at bid - 1c
    if sell fills:
        return sell exit order
    else:
        record the sell error
else:
    record a no-bid sell error

compute the opposite-side ask

if opposite ask is missing:
    raise exit failure
else:
    buy opposite side FOK at ask + 1c
    if hedge fills:
        return buy-opposite hedge order
    else:
        raise exit failure
```

Buying the opposite side is treated as an exit because holding YES plus buying NO, or holding NO plus buying YES, economically locks the market outcome.

### Polymarket Exit Logic

`polymarket_exit_position()` is simpler:

```text
poll conditional token balance and allowance

if balance or allowance is below the cleanup size:
    wait and retry
elif balance/allowance remains insufficient after retries:
    raise balance/allowance failure
else:
    read current bid for the held token

    if bid is missing:
        raise no-bid failure
    else:
        submit sell FOK at bid - 1c
        if fill is not verified:
            raise no-fill failure
        else:
            return response and fill price
```

## Open Position Management

Only one `open_position` is tracked. The loop handles it before considering any new entry.

```text
if open_position is missing:
    entry logic may run
else:
    if current contract differs and there is no pending exit:
        clear internal tracking
    elif position expired and there is no pending exit:
        clear internal tracking
    elif position has a pending exit:
        execute_position_exit()
        if exit is complete:
            open_position = None
        else:
            keep open_position for retry
    elif boundary window is active:
        log POSITION REVIEW once
    else:
        evaluate hold filter
        if hold passes:
            keep holding
        else:
            log liquidation / EXIT_REVIEW
            execute_position_exit()
```

Note that `position_has_pending_exit()` returns true if any of these flags are set:

- `exit_started`
- `kalshi_exited`
- `polymarket_exited`
- `kalshi_absent`
- `polymarket_absent`

This lets partial positions continue cleanup. It also means positions with one absent or already-exited leg are treated as pending-exit states until the remaining leg is resolved.

## Order Types And Pricing

### Kalshi Orders

`kalshi_post_order()` sends authenticated REST orders to `/portfolio/orders` with:

- `time_in_force = "fill_or_kill"`
- `action = "buy"` or `"sell"`
- `side = "yes"` or `"no"`
- integer cent price from `cents(price)`, clamped to `[1, 99]`

After posting, it fetches `/portfolio/orders/{order_id}` when an order id is available, so downstream fill parsing uses verified order state.

### Polymarket Orders

`polymarket_post_order()` creates a `py_clob_client_v2` FOK order:

- `OrderArgs(token_id, price, side, size)`
- `PartialCreateOrderOptions(tick_size="0.01")`
- `OrderType.FOK`

It attempts to verify the order by id via `client.get_order()`.

### Entry Price Calculations

Polymarket displayed arbitrage price is adjusted by `polymarket_execution_price()`, which adds 1 cent and caps at 99 cents. Preflight replaces that fallback with the actual ask level needed to fill the requested size when available.

### Exit Price Calculations

Exit sell limits are `best_bid - 1c`, floored at 1 cent. Exit buy limits for opposite-side Kalshi hedges are `best_ask + 1c`, capped at 99 cents.

## If/Else Summary Of `main()`

The main loop can be read as this sequence:

```text
while True:
    fetch state, compute arbitrage, buffer CSV row

    if new contract:
        reset rolling Kalshi source window
        print balance and contract header

    if open_position:
        if contract changed and no pending exit:
            clear internal tracking
        elif position expired and no pending exit:
            clear internal tracking

    if arbitrage:
        print snapshot

    if open_position:
        if position_has_pending_exit(open_position):
            execute_position_exit()
            if complete: open_position = None
        elif boundary_reason:
            log one-time POSITION REVIEW
        else:
            evaluate_hold_filter()
            if hold failed:
                log EXIT_REVIEW
                execute_position_exit()
                if complete: open_position = None

    if flat and not boundary and raw arb passes and max_trades not reached:
        evaluate preliminary entry filter
        if preliminary filter failed:
            maybe log ENTRY SKIP
            sleep/continue

        run trade_preflight()
        evaluate final entry filter with executable cost

        if final entry filter failed:
            maybe log ENTRY SKIP
        elif preflight is not PLACE:
            log ENTRY SKIP preflight
        else:
            execute_arbitrage()
            if PartialEntryError:
                track partial position
                trades_done += 1
            elif result is not SKIP and not dry-run skip:
                track open position
                trades_done += 1

    flush CSV rows if needed
    if --once: break
    sleep until next interval
```

## Safety And Risk Controls Present In Code

- Dry-run by default.
- New entries disabled near contract start and expiry.
- One tracked position at a time.
- `max_trades` caps counted entries.
- Entry requires raw profit, source agreement, target availability, target closeness, directional agreement, distance from target, fee-adjusted profit, executable liquidity, minimum notional, and adjusted profit.
- Live mode rechecks preflight immediately before orders.
- FOK orders are used for entry on both venues.
- Polymarket is not ordered unless Kalshi is verified filled.
- If a later leg fails, cleanup is attempted immediately.
- If cleanup fails, partial exposure is tracked and retried on later ticks.
- Exit cleanup can use websocket-driven walking limit orders, with HTTP fallback when websocket data is stale.

## Important Limitations

- Position state is not durable. Restarting the process loses `open_position`.
- It tracks at most one position.
- It relies on response parsing heuristics for fills, especially Polymarket fill verification.
- Live entry currently orders Kalshi first, so the code can temporarily hold unhedged Kalshi exposure until Polymarket fills or cleanup succeeds.
- `trades_done` increments for partial entries as well as successful tracked entries.
- Crossing contract boundaries clears internal tracking only when no pending exit is set; it does not reconcile external exchange balances or positions.
- Fees are simplified around `SETTLEMENT_PAYOUT_AFTER_FEES` and "before exit fees" logging.

## Code Map

| Area | Functions |
| --- | --- |
| HTTP and numeric helpers | `http_json`, `as_float`, `finite_float`, `cents` |
| Balances | `kalshi_balance_amounts`, `polymarket_balance_amounts`, `combined_balance_line` |
| Kalshi order book and orders | `kalshi_liquidity_plan_for_buy`, `kalshi_post_order`, `kalshi_exit_position` |
| Polymarket client and orders | `polymarket_client_v2`, `polymarket_post_order`, `polymarket_execution_plan`, `polymarket_exit_position` |
| Market data | `cached_active_kalshi_market`, `fetch_market_state` |
| Filters | `source_filter_metrics`, `evaluate_entry_filter`, `evaluate_hold_filter` |
| Position lifecycle | `partial_entry_position`, `partial_entry_error`, `execute_position_exit` |
| Entry execution | `trade_preflight`, `execute_arbitrage` |
| Main loop | `parse_args`, `main` |
