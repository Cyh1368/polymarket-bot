# Horizon Entry Strategy Backtest

Generated from `kp-0529-research/horizon_models` with a live-log sanity check from `kp-0530-research/concise_trader_log.txt`.

## Scope And Assumptions

- Horizons analyzed: `5m, 3m, 2m, 1m`.
- `Pass` means `diverge_prob <` that horizon model's saved recommended trading threshold.
- Saved thresholds: 5m=0.0493, 3m=0.0788, 2m=0.0788, 1m=0.1378.
- Entry price simulation scans each historical contract CSV after the model decision and enters at the first row with `best_all_in_cost < 0.97`; this matches the current default `profit_margin=0.03`.
- Fees use the same odds-dependent fee equations as `cli_trader_v2.py`, with `N=1` per leg for unit backtest returns.
- Settlement return is `(1 - all_in_cost)` when platforms agree and `(-all_in_cost)` when they diverge, i.e. discrepancy loses the whole stake.
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

| sample | event | P(3m,5m) | joint_count | P(3m\|5m) | 5m_pass_count | P(2m,3m) | P(2m\|3m) | 3m_pass_count | P(1m,2m) | P(1m\|2m) | 2m_pass_count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| test | 3m\|5m | 0.1509 | 35 | 0.7955 | 44.0000 |  |  |  |  |  |  |
| test | 2m\|3m |  | 114 |  |  | 0.4914 | 0.8444 | 135.0000 |  |  |  |
| test | 1m\|2m |  | 139 |  |  |  |  |  | 0.5991 | 0.9653 | 144.0000 |
| all | 3m\|5m | 0.1747 | 202 | 0.8245 | 245.0000 |  |  |  |  |  |  |
| all | 2m\|3m |  | 496 |  |  | 0.4291 | 0.8091 | 613.0000 |  |  |  |
| all | 1m\|2m |  | 653 |  |  |  |  |  | 0.5649 | 0.9761 | 669.0000 |

## Fixed-Threshold Strategy Backtest

These strategies use the saved per-horizon thresholds. All variants below hold to settlement once entered; the `would_emergency_exits` column counts trades that would have seen a later model fail under the current sequential emergency-exit design.

| strategy | contracts | model_signal_contracts | trades | trade_rate | divergences | divergence_rate | mean_all_in_cost | mean_return_per_trade | total_return | would_emergency_exits | would_emergency_exit_rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| any_3_2_1_latch_hold | 232 | 206 | 184 | 0.7931 | 6 | 0.0326 | 0.7414 | 0.2260 | 41.5798 | 25 | 0.1359 |
| any_2_1_latch_hold | 232 | 203 | 168 | 0.7241 | 4 | 0.0238 | 0.7293 | 0.2469 | 41.4711 | 5 | 0.0298 |
| any_5_3_2_1_latch_hold | 232 | 209 | 189 | 0.8147 | 6 | 0.0317 | 0.7691 | 0.1991 | 37.6343 | 32 | 0.1693 |
| latest_state_entry_hold | 232 | 209 | 186 | 0.8017 | 6 | 0.0323 | 0.7682 | 0.1996 | 37.1169 | 22 | 0.1183 |
| single_2m_hold | 232 | 144 | 122 | 0.5259 | 2 | 0.0164 | 0.6826 | 0.3010 | 36.7174 | 5 | 0.0410 |
| single_3m_hold | 232 | 135 | 124 | 0.5345 | 3 | 0.0242 | 0.7190 | 0.2568 | 31.8402 | 21 | 0.1694 |
| require_3m_and_2m_enter_2m | 232 | 114 | 95 | 0.4095 | 1 | 0.0105 | 0.6691 | 0.3204 | 30.4362 | 1 | 0.0105 |
| single_1m_hold | 232 | 198 | 110 | 0.4741 | 3 | 0.0273 | 0.7487 | 0.2240 | 24.6394 | 0 | 0.0000 |
| require_2m_and_1m_enter_1m | 232 | 139 | 64 | 0.2759 | 1 | 0.0156 | 0.6737 | 0.3107 | 19.8857 | 0 | 0.0000 |
| require_3m_2m_1m_enter_1m | 232 | 113 | 51 | 0.2198 | 1 | 0.0196 | 0.6848 | 0.2955 | 15.0729 | 0 | 0.0000 |
| require_5m_and_3m_enter_3m | 232 | 35 | 30 | 0.1293 | 1 | 0.0333 | 0.6624 | 0.3043 | 9.1279 | 0 | 0.0000 |
| single_5m_hold | 232 | 44 | 40 | 0.1724 | 1 | 0.0250 | 0.8043 | 0.1707 | 6.8267 | 8 | 0.2000 |
| require_all_four_enter_1m | 232 | 35 | 17 | 0.0733 | 1 | 0.0588 | 0.6970 | 0.2442 | 4.1511 | 0 | 0.0000 |

## Calibration-Retuned Threshold Checks

I also scanned simple thresholds on the calibration split only, then evaluated the best calibration choice on the held-out test split. This avoids selecting a strategy only because it happened to fit the final test contracts.

Best single-horizon thresholds on calibration:

| horizon | threshold | trades | divergence_rate | mean_all_in_cost | mean_return_per_trade | total_return |
| --- | --- | --- | --- | --- | --- | --- |
| 2m | 0.0837 | 126 | 0.0317 | 0.7538 | 0.2145 | 27.0236 |
| 3m | 0.1083 | 198 | 0.0707 | 0.8079 | 0.1214 | 24.0426 |
| 1m | 0.1132 | 103 | 0.0583 | 0.7640 | 0.1778 | 18.3109 |
| 5m | 0.1231 | 207 | 0.0628 | 0.8626 | 0.0746 | 15.4364 |

Best common-threshold latch variants on calibration:

| family | threshold | trades | divergence_rate | mean_all_in_cost | mean_return_per_trade | total_return |
| --- | --- | --- | --- | --- | --- | --- |
| any_3_2_1_latch | 0.0641 | 138 | 0.0435 | 0.7242 | 0.2324 | 32.0656 |
| any_5_3_2_1_latch | 0.0493 | 123 | 0.0325 | 0.7227 | 0.2448 | 30.1138 |
| any_2_1_latch | 0.0641 | 128 | 0.0391 | 0.7269 | 0.2340 | 29.9563 |

Held-out test performance of those calibration-retuned choices:

| strategy | threshold | trades | trade_rate | divergences | divergence_rate | mean_all_in_cost | mean_return_per_trade | total_return | would_emergency_exits | would_emergency_exit_rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| retuned_any_3_2_1_latch | 0.0641 | 142 | 0.6121 | 3 | 0.0211 | 0.6842 | 0.2947 | 41.8466 | 19 | 0.1338 |
| retuned_any_2_1_latch | 0.0641 | 128 | 0.5517 | 2 | 0.0156 | 0.6826 | 0.3017 | 38.6208 | 10 | 0.0781 |
| retuned_single_2m | 0.0837 | 128 | 0.5517 | 3 | 0.0234 | 0.6921 | 0.2845 | 36.4154 | 128 | 1.0000 |
| retuned_any_5_3_2_1_latch | 0.0493 | 128 | 0.5517 | 2 | 0.0156 | 0.7060 | 0.2784 | 35.6347 | 44 | 0.3438 |
| retuned_single_3m | 0.1083 | 198 | 0.8534 | 13 | 0.0657 | 0.7735 | 0.1608 | 31.8482 | 198 | 1.0000 |
| retuned_single_1m | 0.1132 | 100 | 0.4310 | 3 | 0.0300 | 0.7345 | 0.2355 | 23.5465 | 0 | 0.0000 |
| retuned_single_5m | 0.1231 | 196 | 0.8448 | 12 | 0.0612 | 0.8358 | 0.1030 | 20.1812 | 196 | 1.0000 |

## Recommendation

Using the fixed saved thresholds, the best held-out total return in this search is `any_3_2_1_latch_hold` with `184` trades, mean return `0.2260`, total return `41.5798`, and would-be emergency-exit rate `0.1359`.
With calibration-retuned simple thresholds, the best held-out result is `retuned_any_3_2_1_latch` at threshold `0.0641`, with `142` trades, mean return `0.2947`, and total return `41.8466`.

The operational conclusion is to remove emergency exits from model disagreement. If an entry is opened, hold it to expiry under the no-exit assumption. To reduce the chance of entering before a later model flip, prefer a later-horizon rule over the current sequential latch. In this backtest, `single_1m_hold` and the calibration-retuned `1m` rule are the cleanest deployment candidates because there is no later horizon that can force an exit.

The 5m signal is not useless, but it is not a good standalone trigger for the current bot design: its pass rate is low, and a non-trivial fraction of 5m passes do not survive to later horizons. It is better used as an early warning or as one input to a later confirmation rule, not as an entry permission that can later be revoked.

## Limitations

- The historical CSVs do not encode all live order placement failures, stale books, or minimum-notional failures; this is a decision/price backtest, not a fill simulator.
- Entry uses the first qualifying historical row after a model pass. Live fills can be worse, especially near expiry.
- The calibration-retuned threshold search is deliberately simple. A production retune should reserve a fresh forward test period.
