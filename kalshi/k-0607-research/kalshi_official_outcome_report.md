# Official Kalshi Outcome Audit

- Generated at: `2026-06-07T13:30:34Z`
- Source CSV: `kalshi_trader_trades.csv`
- Official API: `https://external-api.kalshi.com/trade-api/v2/markets?tickers=...`
- Unique contracts in CSV: `262`
- Markets returned by Kalshi API: `262`
- Discrepancies: `11`
- Matches: `237`
- Unknown comparisons: `14`

## Outcome Counts

| Source | YES | NO | Missing |
| --- | ---: | ---: | ---: |
| Trader-inferred | 114 | 135 | 13 |
| Official Kalshi | 125 | 136 | 1 |

## Discrepancies

| Contract | Close Time | Trader Outcome | Official Outcome | Trader Price | Target | Official Expiration Value | Official Settlement Time |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| KXBTC15M-26JUN042130-30 | 2026-06-05T01:30:00Z | NO | YES | 63240.64 | 63243.21 | 63253.92 | 2026-06-05T01:30:04.462661Z |
| KXBTC15M-26JUN050645-45 | 2026-06-05T10:45:00Z | NO | YES | 62521.84 | 62545.35 | 62580.50 | 2026-06-05T10:45:04.454917Z |
| KXBTC15M-26JUN051515-15 | 2026-06-05T19:15:00Z | YES | NO | 59389.43 | 59366.86 | 59345.73 | 2026-06-05T19:15:04.463242Z |
| KXBTC15M-26JUN061215-15 | 2026-06-06T16:15:00Z | YES | NO | 60796.79 | 60775.32 | 60769.58 | 2026-06-06T16:15:04.411258Z |
| KXBTC15M-26JUN061345-45 | 2026-06-06T17:45:00Z | NO | YES | 60638.374375 | 60647.48 | 60663.66 | 2026-06-06T17:45:04.46146Z |
| KXBTC15M-26JUN062315-15 | 2026-06-07T03:15:00Z | NO | YES | 61468.51090909091 | 61480.47 | 61494.35 | 2026-06-07T03:15:04.520861Z |
| KXBTC15M-26JUN062345-45 | 2026-06-07T03:45:00Z | NO | YES | 61610.42636363637 | 61630.95 | 61632.25 | 2026-06-07T03:45:04.458507Z |
| KXBTC15M-26JUN070030-30 | 2026-06-07T04:30:00Z | NO | YES | 61512.38681818182 | 61518.26 | 61529.82 | 2026-06-07T04:30:04.462893Z |
| KXBTC15M-26JUN070115-15 | 2026-06-07T05:15:00Z | NO | YES | 61824.55809523809 | 61874.21 | 61884.50 | 2026-06-07T05:15:04.458184Z |
| KXBTC15M-26JUN070345-45 | 2026-06-07T07:45:00Z | NO | YES | 62190.98045454546 | 62194.96 | 62203.85 | 2026-06-07T07:45:04.4567Z |
| KXBTC15M-26JUN070445-45 | 2026-06-07T08:45:00Z | NO | YES | 62473.12523809524 | 62517.78 | 62526.76 | 2026-06-07T08:45:05.857941Z |

## Unknown Comparisons

| Contract | Close Time | Trader Outcome | Official Outcome | Note |
| --- | --- | ---: | ---: | --- |
| KXBTC15M-26JUN041915-15 | 2026-06-04T23:15:00Z |  | NO | no trader outcome row |
| KXBTC15M-26JUN042215-15 | 2026-06-05T02:15:00Z |  | NO | no trader outcome row |
| KXBTC15M-26JUN051000-00 | 2026-06-05T14:00:00Z |  | NO | no trader outcome row |
| KXBTC15M-26JUN051100-00 | 2026-06-05T15:00:00Z |  | YES | no trader outcome row |
| KXBTC15M-26JUN051115-15 | 2026-06-05T15:15:00Z |  | NO | no trader outcome row |
| KXBTC15M-26JUN051800-00 | 2026-06-05T22:00:00Z |  | NO | no trader outcome row |
| KXBTC15M-26JUN052330-30 | 2026-06-06T03:30:00Z |  | NO | no trader outcome row |
| KXBTC15M-26JUN061330-30 | 2026-06-06T17:30:00Z |  | NO | no trader outcome row |
| KXBTC15M-26JUN061600-00 | 2026-06-06T20:00:00Z |  | NO | no trader outcome row |
| KXBTC15M-26JUN061715-15 | 2026-06-06T21:15:00Z |  | YES | no trader outcome row |
| KXBTC15M-26JUN070015-15 | 2026-06-07T04:15:00Z |  | NO | no trader outcome row |
| KXBTC15M-26JUN070315-15 | 2026-06-07T07:15:00Z | NO |  | official result not available |
| KXBTC15M-26JUN070330-30 | 2026-06-07T07:30:00Z |  | YES | no trader outcome row |
| KXBTC15M-26JUN070900-00 | 2026-06-07T13:00:00Z |  | YES | no trader outcome row |
