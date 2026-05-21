# Exit Liquidity Fixes

## Summary

This change tightens the BTC 15-minute arbitrage trader around the main loss mode observed after switching to Kalshi-first entry: exits that looked profitable at top-of-book but failed on Polymarket FOK, leaving the bot exposed after Kalshi had already exited.

## What Changed

- Added a depth-aware Polymarket exit planner that fetches the current CLOB book, walks bid levels, and computes full-size sell liquidity, VWAP, and worst executable price.
- Added a matching Kalshi exit planner for full-size bid-side exit checks.
- Replaced top-of-book liquidation review with executable liquidation value when deciding whether to exit.
- Re-checks full-size venue liquidity immediately before exit orders are placed.
- Changed two-leg exit sequencing to close Polymarket first, then Kalshi after Polymarket confirms filled.
- Added `--exit-cushion`, defaulting to `0.03`, so non-emergency exits require at least a 3c executable edge over entry.
- Kept emergency exits immediate for `held_winners=0`.

## Default Parameter Updates

- `--min-adjusted-profit`: `0.00` to `0.02`
- `--take-profit-exit-value`: `1.02` to `1.04`
- `--profit-capture-min-edge`: `0.04` to `0.06`
- `--exit-cushion`: new, default `0.03`

## Expected Impact

These changes should reduce avoidable losses from stale top-of-book checks, insufficient Polymarket exit liquidity, and partial exits where Kalshi closes before Polymarket can fill.

Verification performed:

```bash
python3 -m py_compile cli_trader.py
python3 cli_trader.py --help
```
