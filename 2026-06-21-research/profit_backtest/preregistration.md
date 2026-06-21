# Pre-registration: Profit Backtest

*Written at 2026-06-21T19:57:53.727918+00:00 before any results.*

```json
{
  "analysis": "Parsimonious Mean-Reversion Profit Backtest \u2014 Polymarket BTC 5m",
  "date_utc": "2026-06-21T19:57:53.727918+00:00",
  "lockbox": {
    "days": [
      "2026-06-16",
      "2026-06-17",
      "2026-06-18",
      "2026-06-19",
      "2026-06-20"
    ],
    "rule": "last 5 dates in data"
  },
  "model": "L2 logistic regression, features: logit_mid + hf_ret_60s [+ obi_l1]",
  "primary_horizon_s": 180,
  "robustness_horizons_s": [
    120,
    240
  ],
  "l2_c_grid": [
    0.1,
    0.5,
    1.0
  ],
  "execution": {
    "taker": "cross spread at up_ask + taker_fee (always fills)",
    "maker": "rest at up_mid; fill only if opposing best crosses limit before close"
  },
  "profit_metric": "net PnL per available contract (SKIPs = 0 in denominator)",
  "inference": "wild cluster bootstrap (Rademacher, day-level clusters), N=2000",
  "pass_criterion": "deflated net PnL/contract with wild-bootstrap CI_lo > 0",
  "tau_skip": 0.005,
  "taker_fee": 0.01,
  "maker_fee": 0.005,
  "hypotheses": [
    "H1: taker is net negative (spread exceeds signal value)",
    "H2: maker earns spread, turns signal profitable IF fill rate is realistic",
    "H3: no-arb violations exist but are rare and small"
  ],
  "note": "Written before loading outcomes \u2014 cannot be cherry-picked post-hoc."
}
```
