# Leak detection — the maker-fill look-ahead, worked

These four files are a **real before/after** of a look-ahead leak caught on 2026-06-21.

| File | What it is |
|---|---|
| `maker_fill_LEAKY_example.py` | Original backtest. Decides whether a maker order "filled" by scanning **post-entry** snapshots (`up_best_bid` crossing `up_mid` *after* entry). **DO NOT copy this pattern.** |
| `maker_fill_FIXED_example.py` | Leak-free version. Assumes an unconditional fill at the entry mid; no future peek. |
| `results_LEAKY.csv` | XRP maker EV **+0.229/contract** (fabricated). |
| `results_FIXED.csv` | XRP maker EV **−0.001/contract** — the edge *was* the leak. |

## The diff that matters

Leaky (lines ~162–166 of the original): the fill flag is read from snapshots taken
*after* the entry timestamp —

```python
post = cdf[cdf["sample_epoch_ms"] > ts_ms]              # <-- FUTURE data
maker_no_fills = (post["up_best_bid"] <= (1 - um)).any() # fill iff price later moved your way
# ... and PnL counts only filled trades -> keeps only winners
```

Fixed:

```python
# assume the limit order at the entry mid always fills; no future peek
limit = um if side == 1 else (1 - um)
m_pnl.append(net_pnl(limit, lbl, side, MAKER_FEE))
```

## How it was confirmed a leak

The fill flag predicted the outcome almost perfectly — the smoking gun:

```
among rows where maker_no_fills=True,  Up-won mean = 0.261   (Down wins 74%)
among rows where maker_no_fills=False, Up-won mean = 0.997   (Down ~never wins)
```

A fill condition that is a near-oracle for settlement is selecting winners with hindsight.

## Note on paths

These scripts reference repo-relative paths (`2026-06-21-research/...`,
`polymarket/...`, `kraken_hf/...`) from their original location. They are kept here as
**reference artifacts**, not as runnable-in-place scripts. The originals run from the repo
root via `kalshi/.venv-cli-trader/bin/python`.
