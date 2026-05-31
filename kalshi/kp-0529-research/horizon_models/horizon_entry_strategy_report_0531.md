# Horizon Entry Strategy Backtest

Generated from `kp-0529-research/horizon_models` with a live-log sanity check from `kp-0530-research/concise_trader_log.txt`.

## Scope And Assumptions

- Horizons analyzed: `5m, 3m, 2m, 1m`.
- `Pass` means `diverge_prob <` that horizon model's saved recommended trading threshold.
- Saved thresholds: 5m=0.0493, 3m=0.0788, 2m=0.0788, 1m=0.1378.
- Entry price simulation scans each historical contract CSV after the model decision and enters at the first row with `best_all_in_cost < 0.97`; this matches the current default `profit_margin=0.03`.
- Fees use the same odds-dependent fee equations as `cli_trader_v2.py`, with `N=1` per leg for unit backtest returns.
- `hold_total_return` is the no-emergency-exit, hold-to-expiry result: `(1 - all_in_cost)` when platforms agree and `(-all_in_cost)` when they diverge.
- `emergency_full_loss_total_return` is a conservative proxy for the current sequential emergency-exit design: if a later horizon flips to `tradable=False`, that trade is marked as a full stake loss `(-all_in_cost)` even if the contract later agrees.
- Strategy results below are on the original final held-out test split unless marked otherwise.

## Live Log Context

The latest live session starts at the last `START cli_trader_v2` line. The relevant pattern is that entries can be profitable when held, but later model flips force emergency exits:

```text
2026-05-31T00:42:00.248Z | MODEL 3m KXBTC15M-26MAY302045-45 | status=ok diverge_prob=0.0678 threshold=0.0788 tradable=True (was True)
2026-05-31T00:43:00.258Z | MODEL 2m KXBTC15M-26MAY302045-45 | status=ok diverge_prob=0.0558 threshold=0.0788 tradable=True (was True)
2026-05-31T00:44:00.271Z | MODEL 1m KXBTC15M-26MAY302045-45 | status=ok diverge_prob=0.1151 threshold=0.1378 tradable=True (was True)
2026-05-31T00:55:00.641Z | MODEL 5m KXBTC15M-26MAY302100-00 | status=ok diverge_prob=0.1546 threshold=0.0493 tradable=False (was False)
2026-05-31T00:57:00.404Z | MODEL 3m KXBTC15M-26MAY302100-00 | status=ok diverge_prob=0.0762 threshold=0.0788 tradable=True (was False)
2026-05-31T00:58:00.592Z | MODEL 2m KXBTC15M-26MAY302100-00 | status=ok diverge_prob=0.0662 threshold=0.0788 tradable=True (was True)
2026-05-31T00:59:00.400Z | MODEL 1m KXBTC15M-26MAY302100-00 | status=ok diverge_prob=0.0065 threshold=0.1378 tradable=True (was True)
2026-05-31T01:10:00.412Z | MODEL 5m KXBTC15M-26MAY302115-15 | status=ok diverge_prob=0.0873 threshold=0.0493 tradable=False (was False)
2026-05-31T01:12:00.482Z | MODEL 3m KXBTC15M-26MAY302115-15 | status=ok diverge_prob=0.0829 threshold=0.0788 tradable=False (was False)
2026-05-31T01:13:00.353Z | MODEL 2m KXBTC15M-26MAY302115-15 | status=ok diverge_prob=0.0897 threshold=0.0788 tradable=False (was False)
2026-05-31T01:14:00.300Z | MODEL 1m KXBTC15M-26MAY302115-15 | status=ok diverge_prob=0.0545 threshold=0.1378 tradable=True (was False)
2026-05-31T01:25:00.316Z | MODEL 5m KXBTC15M-26MAY302130-30 | status=ok diverge_prob=0.0334 threshold=0.0493 tradable=True (was False)
2026-05-31T01:27:00.256Z | MODEL 3m KXBTC15M-26MAY302130-30 | status=ok diverge_prob=0.0777 threshold=0.0788 tradable=True (was True)
2026-05-31T01:27:11.058Z | ENTRY FILLED NK+P size 2 | K NO 2.0c order d4e8091d-bc07-4e45-ab04-f6612e24bc2f + P YES 94.0c order 0xde604b5d53df143915b2640ab3b1e429b0bc7f47f308588d6e45eb7b76a5f2e5 | all-in 96.4c edge $0.0358
2026-05-31T01:28:00.407Z | MODEL 2m KXBTC15M-26MAY302130-30 | status=ok diverge_prob=0.0743 threshold=0.0788 tradable=True (was True)
2026-05-31T01:29:00.291Z | MODEL 1m KXBTC15M-26MAY302130-30 | status=ok diverge_prob=0.2801 threshold=0.1378 tradable=False (was True)
2026-05-31T01:29:01.981Z | EMERGENCY EXIT INCOMPLETE | Kalshi NO sell size 1/2 ok filled 1 at 92.6c | Polymarket YES sell size 1/2 ok fill 8.0c
2026-05-31T01:29:03.076Z | EMERGENCY EXIT INCOMPLETE | Kalshi NO sell size 1/1 ok filled 1 at 85.0c | Polymarket YES sell size 1/1 FAILED PolyApiException: PolyApiException[status_code=400, error_message={'error': 'not enough balance / allowance: the balance is not enough -> balance: 1000000, sum of matched orders: 1000000, order amount (inc. fees): 1000000'}]
2026-05-31T01:29:04.737Z | EMERGENCY EXIT COMPLETE | Polymarket YES sell size 1/1 ok fill 9.0c
2026-05-31T01:40:00.260Z | MODEL 5m KXBTC15M-26MAY302145-45 | status=ok diverge_prob=0.0492 threshold=0.0493 tradable=True (was False)
2026-05-31T01:42:00.442Z | MODEL 3m KXBTC15M-26MAY302145-45 | status=ok diverge_prob=0.0713 threshold=0.0788 tradable=True (was True)
2026-05-31T01:43:00.283Z | MODEL 2m KXBTC15M-26MAY302145-45 | status=ok diverge_prob=0.0596 threshold=0.0788 tradable=True (was True)
2026-05-31T01:43:23.476Z | ENTRY FILLED K+NP size 2 | K YES 14.0c order f275245f-3970-4f69-b6dd-304038cee715 + P NO 79.0c order 0xadb007d3c8ed2a7aeb8ee4748dc2247b1b5e8ec134673da5a3aac493c2c5716b | all-in 94.7c edge $0.0533
2026-05-31T01:44:00.260Z | MODEL 1m KXBTC15M-26MAY302145-45 | status=ok diverge_prob=0.1606 threshold=0.1378 tradable=False (was True)
2026-05-31T01:44:00.841Z | EMERGENCY EXIT INCOMPLETE | Kalshi YES sell size 1/2 FAILED RuntimeError: POST /portfolio/orders failed: HTTP 409 {"error":{"code":"fill_or_kill_insufficient_resting_volume","message":"fill or kill insufficient resting volume"}} | Polymarket NO sell size 1/2 ok fill 75.0c
2026-05-31T01:44:03.236Z | EMERGENCY EXIT INCOMPLETE | Kalshi YES sell size 1/2 ok filled 1 at 5.3c | Polymarket NO sell size 1/1 ok fill 87.0c
2026-05-31T01:44:04.956Z | EMERGENCY EXIT COMPLETE | Kalshi YES sell size 1/1 ok filled 1 at 4.1c
2026-05-31T01:47:41.402Z | STOP cli_trader_v2 interrupted
```

## Model Pass Probabilities

Primary interpretation should use the `test` row. The `all` row is useful as a larger-sample diagnostic, but includes contracts used to fit/calibrate the models.

| sample | contracts | P(5m) | 5m_pass_count | P(3m) | 3m_pass_count | P(2m) | 2m_pass_count | P(1m) | 1m_pass_count | P(all_four) | all_four_count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| test | 232 | 0.1897 | 44 | 0.5819 | 135 | 0.6207 | 144 | 0.8534 | 198 | 0.1509 | 35 |
| all | 1156 | 0.2119 | 245 | 0.5303 | 613 | 0.5787 | 669 | 0.8486 | 981 | 0.1522 | 176 |

## Conditional Pass Probabilities

| sample | condition | denominator_pass_count | joint_count | joint_probability | conditional_probability |
| --- | --- | --- | --- | --- | --- |
| test | 3m given 5m | 44 | 35 | 0.1509 | 0.7955 |
| test | 2m given 3m | 135 | 114 | 0.4914 | 0.8444 |
| test | 1m given 2m | 144 | 139 | 0.5991 | 0.9653 |
| all | 3m given 5m | 245 | 202 | 0.1747 | 0.8245 |
| all | 2m given 3m | 613 | 496 | 0.4291 | 0.8091 |
| all | 1m given 2m | 669 | 653 | 0.5649 | 0.9761 |

## Fixed-Threshold Strategy Backtest

These strategies use the saved per-horizon thresholds. The hold columns assume no emergency exits. The emergency-full-loss columns instead assume every later model flip after entry loses the whole stake.

| strategy | contracts | model_signal_contracts | trades | trade_rate | divergences | divergence_rate | mean_all_in_cost | hold_mean_return_per_trade | hold_total_return | would_emergency_exits | would_emergency_exit_rate | emergency_exit_nondivergences | emergency_full_loss_mean_return_per_trade | emergency_full_loss_total_return |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| any_2_1_latch_hold | 232 | 203 | 168 | 0.7241 | 4 | 0.0238 | 0.7293 | 0.2469 | 41.4711 | 5 | 0.0298 | 4 | 0.2230 | 37.4711 |
| single_2m_hold | 232 | 144 | 122 | 0.5259 | 2 | 0.0164 | 0.6826 | 0.3010 | 36.7174 | 5 | 0.0410 | 4 | 0.2682 | 32.7174 |
| require_3m_and_2m_enter_2m | 232 | 114 | 95 | 0.4095 | 1 | 0.0105 | 0.6691 | 0.3204 | 30.4362 | 1 | 0.0105 | 1 | 0.3099 | 29.4362 |
| single_1m_hold | 232 | 198 | 110 | 0.4741 | 3 | 0.0273 | 0.7487 | 0.2240 | 24.6394 | 0 | 0.0000 | 0 | 0.2240 | 24.6394 |
| require_2m_and_1m_enter_1m | 232 | 139 | 64 | 0.2759 | 1 | 0.0156 | 0.6737 | 0.3107 | 19.8857 | 0 | 0.0000 | 0 | 0.3107 | 19.8857 |
| any_3_2_1_latch_hold | 232 | 206 | 184 | 0.7931 | 6 | 0.0326 | 0.7414 | 0.2260 | 41.5798 | 25 | 0.1359 | 22 | 0.1064 | 19.5798 |
| latest_state_entry_hold | 232 | 209 | 186 | 0.8017 | 6 | 0.0323 | 0.7682 | 0.1996 | 37.1169 | 22 | 0.1183 | 19 | 0.0974 | 18.1169 |
| require_3m_2m_1m_enter_1m | 232 | 113 | 51 | 0.2198 | 1 | 0.0196 | 0.6848 | 0.2955 | 15.0729 | 0 | 0.0000 | 0 | 0.2955 | 15.0729 |
| single_3m_hold | 232 | 135 | 124 | 0.5345 | 3 | 0.0242 | 0.7190 | 0.2568 | 31.8402 | 21 | 0.1694 | 19 | 0.1036 | 12.8402 |
| require_5m_and_3m_enter_3m | 232 | 35 | 30 | 0.1293 | 1 | 0.0333 | 0.6624 | 0.3043 | 9.1279 | 0 | 0.0000 | 0 | 0.3043 | 9.1279 |
| any_5_3_2_1_latch_hold | 232 | 209 | 189 | 0.8147 | 6 | 0.0317 | 0.7691 | 0.1991 | 37.6343 | 32 | 0.1693 | 29 | 0.0457 | 8.6343 |
| require_all_four_enter_1m | 232 | 35 | 17 | 0.0733 | 1 | 0.0588 | 0.6970 | 0.2442 | 4.1511 | 0 | 0.0000 | 0 | 0.2442 | 4.1511 |
| single_5m_hold | 232 | 44 | 40 | 0.1724 | 1 | 0.0250 | 0.8043 | 0.1707 | 6.8267 | 8 | 0.2000 | 8 | -0.0293 | -1.1733 |

## Return Arithmetic Example

For `any_2_1_latch_hold`:

- Hold-to-expiry: `(trades - divergences) - trades * mean_all_in_cost = (168 - 4) - 168 * 0.729339 = 41.4711`.
- Emergency-full-loss proxy: `hold_total_return - emergency_exit_nondivergences = 41.4711 - 4 = 37.4711`.

## Missed Good Trades And Avoided Bad Trades

For this diagnostic, the baseline is every held-out test contract with at least one qualifying arbitrage opportunity from the 2m decision time through expiry. A `good` trade is a non-divergent contract that would settle for the full payout; a `bad` trade is a divergent contract that loses the stake.

| strategy | baseline_opportunities | baseline_good_trades | baseline_bad_trades | strategy_trades | strategy_good_trades | strategy_bad_trades | good_trades_missed | good_trades_missed_percent | bad_trades_avoided | bad_trades_avoided_percent |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| any_2_1_latch_hold | 206 | 188 | 18 | 168 | 164 | 4 | 24 | 12.7660 | 14 | 77.7778 |

## Threshold Retuning

This default report does not retune thresholds because the immediate question is whether the existing saved thresholds create bad sequential behavior. Run the script with `--retune` to perform the slower calibration-only threshold scan.

## Recommendation

Using the fixed saved thresholds and the conservative emergency-full-loss assumption, the best held-out total return in this search is `any_2_1_latch_hold` with `168` trades, hold total return `41.4711`, emergency-full-loss total return `37.4711`, and would-be emergency-exit rate `0.0298`.

The operational conclusion is to remove emergency exits from model disagreement. If an entry is opened, hold it to expiry under the no-exit assumption. Under a conservative full-loss penalty for later model flips, `any_2_1_latch_hold` is strongest among the tested fixed-threshold rules because it avoids most 5m/3m flip damage while keeping broad coverage. `single_2m_hold` remains the strongest one-model rule.

The 5m signal is not useless, but it is not a good standalone trigger for the current bot design: its pass rate is low, and a non-trivial fraction of 5m passes do not survive to later horizons. It is better used as an early warning or as one input to a later confirmation rule, not as an entry permission that can later be revoked.

## Limitations

- The historical CSVs do not encode all live order placement failures, stale books, or minimum-notional failures; this is a decision/price backtest, not a fill simulator.
- Entry uses the first qualifying historical row after a model pass. Live fills can be worse, especially near expiry.
- Optional threshold retuning is deliberately left out of the default report. A production retune should reserve a fresh forward test period.
