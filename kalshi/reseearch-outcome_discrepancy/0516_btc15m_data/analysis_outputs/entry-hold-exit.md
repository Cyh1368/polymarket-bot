# Entry, Hold, and Exit Filter Logic

This document describes the current arb filter stack for BTC 15-minute Kalshi/Polymarket markets after incorporating outcome-discrepancy risk, hold-filter coherence, and 2% winner-payout fees.

Current source-gap configuration:

```text
source_gap_threshold = 100.0
```

This replaces the earlier `$5` source-gap threshold. Historical analysis tables in older reports may still reflect results computed under `$5` unless explicitly rerun.

## Core Accounting

The bot should evaluate strategy PnL at the contract level, not the tick level.

Correct unit:

```text
one contract entry -> held or exited -> one realized PnL
```

Incorrect unit:

```text
sum every allowed tick as if each tick were an independent trade
```

Fee model:

```text
winning normalized payout: 1.00 gross -> 0.98 net
losing payout: 0.00 -> 0.00
fee_adjusted_pnl = fee_adjusted_payout - arb_cost
```

This means a normal one-winner arb must have:

```text
arb_cost < 0.98
```

just to be positive after fees. A practical minimum spread screen is stricter:

```text
arb_cost <= 0.96
```

For live deployment, `arb_cost <= 0.95` is the conservative setting until more data confirms the residual discrepancy rate.

## Entry Filter

The entry filter decides whether to open a new arb position.

Inputs:

```text
kalshi_btc_price
polymarket_btc_price
kalshi_btc_target
polymarket_btc_target
seconds_to_expiry
source_price_gap
arb_cost
polymarket_error
kalshi_status
```

### 1. Data Validity

Reject if Polymarket data is stale or unavailable:

```python
if row.get("polymarket_error"):
    return False
```

Reject if the Kalshi contract is not active:

```python
if row.get("kalshi_status") != "active":
    return False
```

### 2. Direction Agreement

Both price feeds must currently imply the same direction relative to their own platform-specific opening target.

```python
kalshi_direction = kalshi_btc_price > kalshi_btc_target
poly_direction = polymarket_btc_price > polymarket_btc_target
direction_agreement = kalshi_direction == poly_direction
```

Require:

```python
direction_agreement is True
```

This blocks cases where one source is already above its threshold while the other is below its threshold.

### 3. Source Price Gap

The live BTC source feeds must be close enough:

```python
source_gap = abs(kalshi_btc_price - polymarket_btc_price)
```

Require:

```python
source_gap <= 100.0
```

This is a discrepancy-risk screen. Large feed gaps can make the two venues settle differently.

### 4. Time-Scaled Distance From Target

The current BTC prices must be far enough from their resolution thresholds.

```python
kalshi_distance = abs(kalshi_btc_price - kalshi_btc_target)
poly_distance = abs(polymarket_btc_price - polymarket_btc_target)
min_distance = min(kalshi_distance, poly_distance)
```

Required distance:

```python
entry_required_distance = max(10.0, seconds_to_expiry * 0.05)
```

Require:

```python
min_distance >= entry_required_distance
```

This scales the required cushion with time remaining. A contract with more time left needs more distance from the threshold because BTC has more time to drift back into the danger zone.

### 5. Target Divergence Guard

The two venues can have different opening targets. This is not a clean standalone predictor, but extreme divergence is still treated as a caution/no-trade condition.

```python
target_divergence = abs(kalshi_btc_target - polymarket_btc_target)
```

Require:

```python
target_divergence <= 35.0
```

Important: the old `$15` target-divergence cap was rejected as too strict. It blocked multiple safe, profitable opportunities and did not cleanly separate safe from dangerous contracts.

### 6. Spread / Fee Viability

Because a winning one-leg payout nets `0.98`, the entry must clear a minimum spread threshold.

Minimum viable:

```python
arb_cost <= 0.98
```

Recommended current threshold:

```python
arb_cost <= 0.96
```

Conservative live threshold:

```python
arb_cost <= 0.95
```

The spread screen is not optional. Without it, many safe arbs are zero or negative after fees.

## Entry Function

```python
def should_enter_arb(row, max_arb_cost=0.96) -> bool:
    if row.get("polymarket_error"):
        return False
    if row.get("kalshi_status") != "active":
        return False

    kalshi_price = float(row["kalshi_btc_price"])
    poly_price = float(row["polymarket_btc_price"])
    kalshi_target = float(row["kalshi_btc_target"])
    poly_target = float(row["polymarket_btc_target"])
    seconds_to_expiry = float(row["seconds_to_expiry"])
    arb_cost = float(row["arb_cost"])

    source_gap = abs(kalshi_price - poly_price)
    target_divergence = abs(kalshi_target - poly_target)
    direction_agreement = (kalshi_price > kalshi_target) == (poly_price > poly_target)

    min_distance = min(
        abs(kalshi_price - kalshi_target),
        abs(poly_price - poly_target),
    )
    entry_required_distance = max(10.0, seconds_to_expiry * 0.05)

    return (
        direction_agreement
        and source_gap <= 100.0
        and min_distance >= entry_required_distance
        and target_divergence <= 35.0
        and arb_cost <= max_arb_cost
    )
```

## Hold Filter

The hold filter monitors an already-open position. It should be coherent with the entry rule, meaning it must derive its distance threshold from the same entry formula.

Do not use an unrelated fixed hold threshold like `$25` by itself. That creates inconsistent behavior:

```text
entry_required_distance = max(10, seconds_to_expiry * 0.05)
hold_required_distance = 25
```

At high seconds-to-expiry, entry required distance can exceed `$25`, making the hold threshold less strict than entry. At lower seconds-to-expiry, the fixed `$25` threshold can cause immediate entry-then-exit churn.

Instead, define hold distance as a multiplier of the entry distance:

```python
entry_required_distance = max(10.0, seconds_to_expiry * 0.05)
hold_required_distance = distance_multiplier * entry_required_distance
```

Tested settings:

```text
1.0x  coherent, but not stricter than entry
1.25x meaningfully stricter, recommended initial setting
1.5x  more conservative, causes many immediate exits
2.0x+ too aggressive for current entry rule
```

## Hold Function

```python
def should_hold_arb(row, distance_multiplier=1.25) -> bool:
    if row.get("polymarket_error"):
        return False

    kalshi_price = float(row["kalshi_btc_price"])
    poly_price = float(row["polymarket_btc_price"])
    kalshi_target = float(row["kalshi_btc_target"])
    poly_target = float(row["polymarket_btc_target"])
    seconds_to_expiry = float(row["seconds_to_expiry"])

    source_gap = abs(kalshi_price - poly_price)
    target_divergence = abs(kalshi_target - poly_target)
    direction_agreement = (kalshi_price > kalshi_target) == (poly_price > poly_target)

    min_distance = min(
        abs(kalshi_price - kalshi_target),
        abs(poly_price - poly_target),
    )

    entry_required_distance = max(10.0, seconds_to_expiry * 0.05)
    hold_required_distance = distance_multiplier * entry_required_distance

    return (
        direction_agreement
        and source_gap <= 100.0
        and min_distance >= hold_required_distance
        and target_divergence <= 35.0
    )
```

## Exit Behavior

If `should_hold_arb(row)` returns `False`, the position should be considered unsafe to passively hold.

However, this does not automatically mean the bot should market-exit immediately. An exit decision needs unwind pricing:

```text
current liquidation bid value - original entry cost - fees
```

For example, for an `NK+P` position:

```text
liquidation_value = kalshi_no_bid + polymarket_yes_bid
exit_pnl = liquidation_value - entry_cost - exit_fees
```

For a `K+NP` position:

```text
liquidation_value = kalshi_yes_bid + polymarket_no_bid
exit_pnl = liquidation_value - entry_cost - exit_fees
```

The hold filter is therefore a risk alarm. The execution layer should decide whether to:

```text
exit immediately
hedge one side
reduce size
keep holding because unwind cost is worse than expected settlement risk
```

## Deployment Caveats

1. **Full hold logic should be re-tested after threshold changes**

Earlier runs with the old `$5` source-gap threshold caused nearly every canonical entry to exit before expiry. The current `$100` threshold is much looser, so any hold-exit frequency claims should be rerun under this configuration before live deployment.

2. **Immediate churn is possible**

With `distance_multiplier=1.25`, many positions that pass the entry filter fail the stricter hold filter immediately or very soon after entry. To avoid churn, either:

```text
use the same stricter threshold in entry,
add hysteresis,
add a minimum hold window,
or require exit pricing to be favorable before unwinding.
```

3. **Fees make small spreads unusable**

A pre-fee arb at `0.99` looks profitable under old accounting:

```text
1.00 - 0.99 = +0.01
```

But after winner-payout fee:

```text
0.98 - 0.99 = -0.01
```

The bot should not enter these.

4. **Target divergence is weak**

Large target divergence appears in both safe and dangerous contracts. Keep the extreme cap at `$35`, but do not treat target divergence alone as a strong predictor.

## Current Recommended Stack

Production entry:

```text
polymarket_error empty
kalshi_status == active
direction_agreement == True
source_gap <= 100
min_distance >= max(10, seconds_to_expiry * 0.05)
abs_target_divergence <= 35
arb_cost <= 0.96
```

Conservative entry:

```text
same as above, but arb_cost <= 0.95
```

Hold monitor:

```text
polymarket_error empty
direction_agreement == True
source_gap <= 100
min_distance >= 1.25 * max(10, seconds_to_expiry * 0.05)
abs_target_divergence <= 35
```

Exit:

```text
triggered by hold failure,
but executed only after checking bid-side unwind value, fees, and fill risk.
```
