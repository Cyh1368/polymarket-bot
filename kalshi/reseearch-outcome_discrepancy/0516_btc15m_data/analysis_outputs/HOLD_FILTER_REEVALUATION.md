# Hold Filter Re-Evaluation

Canonical entry rule used here:

```text
price_direction_agreement
AND source_price_gap <= 100
AND min_distance_from_target >= max(10, seconds_to_expiry * 0.05)
AND abs_target_divergence <= 35
```

Under that entry rule there are 44 contract entries, 4 dangerous entries, and hold-to-expiry PnL of `-2.141`.

## Coherence Check

The current hold floor is fixed at `$25`. It is not guaranteed to be greater than the entry required distance because the entry rule scales with time. There are 26 canonical entries where:

```text
25 < max(10, seconds_to_expiry * 0.05)
```

Those entries are structurally incoherent because the hold threshold is less strict than the entry threshold at the same timestamp. The incoherent list is in `hold_filter_eval.csv` / console output from the analysis run.

## Hold Variant Results

Distance-only hold exits, assuming exited positions are flattened for zero settlement PnL:

| hold distance rule | immediate exits | any exit before expiry | held to expiry | dangerous held | PnL if exits flatten |
|---|---:|---:|---:|---:|---:|
| fixed `$25` | 7 | 37 | 7 | 0 | +0.108 |
| `1.0 * entry_required_distance` | 0 | 37 | 7 | 0 | +0.108 |
| `1.25 * entry_required_distance` | 20 | 40 | 4 | 0 | +0.073 |
| `1.5 * entry_required_distance` | 26 | 41 | 3 | 0 | +0.063 |
| `2.0 * entry_required_distance` | 38 | 42 | 2 | 0 | +0.043 |
| `max(20, seconds_to_expiry * 0.07)` | 25 | 41 | 3 | 0 | +0.063 |
| `max(25, seconds_to_expiry * 0.075)` | 26 | 41 | 3 | 0 | +0.063 |

Full hold criteria using distance plus `source_gap <= 100`, direction agreement, and `abs_target_divergence <= 35` caused every canonical entry to exit before expiry for every tested distance variant. That is probably too reactive unless the bot has a real unwind model and can exit cheaply.

## 170815 Trace

Entry:

```text
contract: KXBTC15M-26MAY170815-15
entry timestamp: 2026-05-17T12:09:10.618Z
seconds_to_expiry: 349.382
entry required distance: 17.469
min_distance: 31.046
source_gap: 3.722
arb_type: NK+P
entry cost: 0.95
hold-to-expiry PnL: -0.95
```

The fixed `$25` hold floor would have exited at `2026-05-17T12:09:12.578Z`, about 2 seconds after entry, when `min_distance` fell to `$24.18` and `source_gap` widened to `$12.83`.

Using the raw combined CSV for liquidation pricing, the `NK+P` position entered at `0.95`. At the fixed-25 exit tick, liquidation value was:

```text
kalshi_no_bid + polymarket_yes_bid = 0.76 + 0.16 = 0.92
```

So the estimated unwind PnL at that tick is `0.92 - 0.95 = -0.03`, versus `-0.95` if held to settlement. This ignores fees and fill risk.

Distance-only first fail times for 170815:

| hold rule | first fail | seconds to expiry | min distance | source gap | estimated result |
|---|---:|---:|---:|---:|---|
| fixed `$25` | 12:09:12.578Z | 347.422 | 24.180 | 12.826 | exit, about `-0.03` before fees |
| `1.0x` entry formula | 12:09:14.576Z | 345.424 | 15.490 | 21.518 | exit, about `-0.04` before fees |
| `1.5x` entry formula | 12:09:12.578Z | 347.422 | 24.180 | 12.826 | exit, about `-0.03` before fees |
| `2.0x` entry formula | entry tick | 349.382 | 31.046 | 3.722 | immediate reject/exit |
| `max(20, t*0.07)` | 12:09:12.578Z | 347.422 | 24.180 | 12.826 | exit, about `-0.03` before fees |

The tick trace is written to `trace_KXBTC15M-26MAY170815-15.csv`.

## Recommended Coherent Hold Filter

Use the same base formula as entry and multiply it. The multiplier must be at least `1.0` to guarantee the hold threshold is never below the entry threshold. A multiplier of `1.25` is the least aggressive tested variant that is meaningfully stricter; `1.5` is more conservative but creates many immediate exits if used right after entry.

Recommended initial setting:

```python
def should_hold_arb(row, distance_multiplier=1.25) -> bool:
    kalshi_price = float(row["kalshi_btc_price"])
    poly_price = float(row["polymarket_btc_price"])
    kalshi_target = float(row["kalshi_btc_target"])
    poly_target = float(row["polymarket_btc_target"])
    seconds_to_expiry = float(row["seconds_to_expiry"])

    if row.get("polymarket_error"):
        return False

    min_distance = min(
        abs(kalshi_price - kalshi_target),
        abs(poly_price - poly_target),
    )
    source_gap = abs(kalshi_price - poly_price)
    direction_agreement = (kalshi_price > kalshi_target) == (poly_price > poly_target)
    target_divergence = abs(kalshi_target - poly_target)

    entry_required_distance = max(10.0, seconds_to_expiry * 0.05)
    hold_required_distance = distance_multiplier * entry_required_distance

    if not direction_agreement:
        return False
    if min_distance < hold_required_distance:
        return False
    if source_gap > 100.0:
        return False
    if target_divergence > 35.0:
        return False
    return True
```

Deployment note: if the bot evaluates this immediately after entry, `1.25x` will still instantly exit 20 of 44 canonical entries in this sample. To avoid churn, either apply the same stricter threshold to entry too, or add a short minimum-hold / hysteresis rule. The cleanest coherent design is to use one stricter entry threshold and the same threshold for immediate post-entry hold checks.
