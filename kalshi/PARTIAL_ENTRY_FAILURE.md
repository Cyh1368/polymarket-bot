# Partial Entry Failure: Polymarket Fill Before Kalshi Hedge

## Summary

The `kp-0520-research` run shows repeated partial-entry failures where the bot filled the Polymarket leg while the Kalshi hedge failed. The most common Kalshi failures were `fill_or_kill_insufficient_resting_volume` and transient `GET /portfolio/orders/... HTTP 404` verification failures.

This left the bot holding only Polymarket contracts. Immediate cleanup often failed because Polymarket conditional token balance was not yet visible:

```text
POLYMARKET EXIT FAILED: PolyApiException[status_code=400, error_message={'error': 'not enough balance / allowance: the balance is not enough -> balance: 0, order amount: 2000000'}]
```

When cleanup retried later, the Polymarket bid sometimes moved lower, turning an intended arbitrage into a realized loss.

## Evidence

Examples from `kp-0520-research/concise_trader_log.txt`:

- Line 98: Polymarket NO filled at 69c; Kalshi YES verification failed with HTTP 404; immediate Polymarket cleanup failed because balance was 0; later cleanup exited at 67c.
- Lines 396-411: repeated same-contract partial entries between `2026-05-20 01:42:52.4` and `01:43:32.9`; each filled Polymarket first, failed Kalshi, then retried Polymarket cleanup at changing prices.
- Lines 406-411: two large cleanup losses, exiting Polymarket NO at 62c after 75c entry and at 50c after 61c entry.

The loss mechanism is operational rather than a pure pricing model issue:

1. Polymarket buy succeeds.
2. Kalshi FOK order fails or cannot be verified.
3. The bot attempts to sell the Polymarket token immediately.
4. Polymarket reports conditional balance/allowance below the sell size.
5. The cleanup remains exposed to market movement until a later retry succeeds.

## Why Sequential Kalshi-First Entry Helps

The safer failure mode is to place and verify Kalshi first, then recheck Polymarket. If Polymarket fails after Kalshi fills, the bot is left with a Kalshi-only position, which the existing code can clean up by selling the same side or buying the opposite side as a hedge. That cleanup path does not depend on freshly minted Polymarket conditional token balance becoming visible.

This may miss some arbitrage opportunities because Polymarket can move after Kalshi fills. The tradeoff is intentional: it reduces the specific observed loss source, which was unhedged Polymarket exposure after Kalshi failure.

## Implemented Mitigation

Live entry now follows this sequence:

1. Run preflight as before.
2. Submit Kalshi FOK limit order.
3. Verify the Kalshi fill.
4. Recheck Polymarket ask liquidity for the actually filled Kalshi size.
5. Recompute total cost and adjusted profit using the actual Kalshi fill price.
6. Submit Polymarket FOK only if the rechecked edge still passes.
7. If Polymarket fails or does not fill, immediately clean up the Kalshi-only partial position.

