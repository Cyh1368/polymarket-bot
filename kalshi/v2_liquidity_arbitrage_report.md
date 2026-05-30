# V2 Liquidity-Aware Arbitrage Report

Generated: 2026-05-30

## What Produced The `ENTRY SKIP fresh ...` Lines

The repeated lines came from the live entry path in `cli_trader_v2.py`.

1. A horizon model first marked the contract as tradable, for example:

   ```text
   MODEL 3m ... diverge_prob=0.0333 threshold=0.0788 tradable=True
   ```

2. On each market update, the main loop checked for a best arbitrage candidate with `best_arbitrage_candidate(...)`.

3. If the current in-memory snapshot had a profitable candidate, the bot called `execute_entry(...)`.

4. `execute_entry(...)` called `preflight_trade(...)`, which fetched fresh Kalshi and Polymarket orderbooks.

5. `preflight_trade(...)` recomputed the best candidate using fresh books, then checked ask-side liquidity for the two buy legs:

   - `K+NP`: buy Kalshi YES and buy Polymarket NO.
   - `NK+P`: buy Kalshi NO and buy Polymarket YES.

6. If the fresh top-of-book ask quantity on either leg was below `--contracts`, the script returned a skip message such as:

   ```text
   ENTRY SKIP fresh Polymarket NO ask liquidity 0 < 2 | best K+NP all-in 54.8c edge $0.4522
   ```

So the old flow could detect a price-arb first, then discover during preflight that one leg had no executable size.

## Why A Price Arbitrage Existed With No Liquidity

The immediate cause was that arbitrage candidate construction was price-only. It used `kalshi_yes_ask + polymarket_no_ask` or `kalshi_no_ask + polymarket_yes_ask`, but did not require nonzero ask quantity before declaring a candidate.

Polymarket made this more visible because the snapshot builder may have a synthetic opposite-side price. For example, if a YES bid exists, the code can infer:

```text
NO ask ~= 1 - YES bid
```

That inferred price is useful for display and modeling, but it is not necessarily an executable NO ask. If `best_no_ask_qty` is zero, there are no displayed NO shares to buy at that price. The result was a very cheap-looking `K+NP` candidate with `Polymarket NO ask liquidity 0`.

This is not a model issue. The model decided the contract was safe enough to trade. The trade layer then found a quoted/arithmetic opportunity that was not executable at the requested size.

## Implementation Change

Arbitrage candidates are now liquidity-aware at construction time.

The updated logic requires:

```text
Kalshi ask liquidity for the selected side >= --contracts
Polymarket ask liquidity for the selected side >= --contracts
```

before a candidate is returned by:

- `arbitrage_candidates(...)`
- `best_arbitrage_candidate(...)`

This changes the behavior in three places:

1. The status line no longer displays `K+NP` / `NK+P` as an arbitrage candidate unless both legs have enough executable ask quantity.
2. The main entry loop no longer calls preflight for a price-only candidate with zero visible liquidity.
3. `preflight_trade(...)` also recomputes only liquid fresh candidates, so a stale current snapshot cannot turn into an order unless fresh books still have sufficient size.

The bid-sum model features (`k_plus_np`, `nk_plus_p`) are unchanged because those are historical/model inputs, not execution eligibility checks.

## Expected Live Effect

The repeated pattern below should stop:

```text
ENTRY SKIP fresh Polymarket NO ask liquidity 0 < 2 | best K+NP ...
```

because a `K+NP` candidate with `Polymarket NO ask liquidity 0` is no longer considered an actionable arbitrage candidate.

It is still possible to see a skip if liquidity existed in the current websocket snapshot but disappeared before fresh preflight. That is the correct behavior: the script will not place an order unless the fresh pre-trade books are liquid enough.

