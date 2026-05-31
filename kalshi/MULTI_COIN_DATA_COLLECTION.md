# Multi-Coin Data Collection

## What Was Added

I added a data-only collection layer for all seven 15-minute crypto direction markets:

- `cli_data_BTC.py`
- `cli_data_ETH.py`
- `cli_data_SOL.py`
- `cli_data_XRP.py`
- `cli_data_HYPE.py`
- `cli_data_DOGE.py`
- `cli_data_BNB.py`
- Shared implementation: `cli_data_common.py`

Each script collects live Kalshi orderbook data, Polymarket orderbook data, Kalshi/coin source price data, Polymarket RTDS price data, and the same fee-adjusted arbitrage columns used by `cli_trader_v2.py`. The collectors do not load models, do not set `tradable=True`, and never place orders.

Continuous mode also detects expired contracts, clears the active-market caches, restarts the live market context, and continues into the next 15-minute contract.

## Output Folders

Each token writes contract-specific CSVs to its own folder:

- `data_BTC/`
- `data_ETH/`
- `data_SOL/`
- `data_XRP/`
- `data_HYPE/`
- `data_DOGE/`
- `data_BNB/`

The CSV schema is the same 70-column schema used by `cli_trader_v2.py`, including fields such as:

- `timestamp_utc`
- Kalshi top-of-book and contract fields
- Polymarket top-of-book and contract fields
- `kalshi_btc_price`, `kalshi_btc_target`, `kalshi_btc_60_sma`
- `polymarket_btc_price`, `polymarket_btc_target`
- `k_plus_np`, `nk_plus_p`
- fee-adjusted all-in cost and edge columns

The BTC column names are intentionally preserved for compatibility with existing research scripts.

## Source Price Handling

For BTC, the collector preserves the BTC source behavior and uses BRTI when available.

For ETH, SOL, XRP, HYPE, DOGE, and BNB, the collector uses the Kalshi market `expiration_value` when available as the Kalshi-side source price and falls back to Kraken spot if needed. Polymarket-side price comes from Polymarket RTDS using the token's `symbol/usd` stream.

This means the collectors are ready for data collection, but before training production non-BTC divergence models, the exact Kalshi settlement-source semantics for each non-BTC market should be verified against Kalshi resolution documentation and historical settlement rows.

## Commands

Run continuous collection for a token:

```bash
.venv-cli-trader/bin/python cli_data_ETH.py
```

Run one row and exit:

```bash
.venv-cli-trader/bin/python cli_data_ETH.py --once
```

Run a validation fetch without writing CSV:

```bash
.venv-cli-trader/bin/python cli_data_ETH.py --dry-run
```

Change output interval or folder:

```bash
.venv-cli-trader/bin/python cli_data_ETH.py --csv-save-interval 2 --csv-dir data_ETH
```

## Display Server

I added `data_display_server.py`. It displays the latest CSV for all seven tokens and provides download links.

Run it with:

```bash
PORT=8010 .venv-cli-trader/bin/python data_display_server.py
```

Then open:

```text
http://localhost:8010
```

Useful endpoints:

- `/` displays latest CSV previews for all seven tokens.
- `/api/latest` returns the latest CSV summary as JSON.
- `/download/<TOKEN>` downloads the latest CSV for a token.
- `/files/<TOKEN>` lists all CSVs for a token.

For BTC, the display server checks `data_BTC/` first and falls back to existing `kalshi_btc15m_data/`.

## Dry Test Results

I ran `--dry-run` for all seven scripts. Each resolved a current Kalshi contract, a matching Polymarket 15-minute market, current source prices, orderbook prices, and both arbitrage sums. All scripts returned:

- `column_count = 70`
- `missing_columns = []`
- `missing_critical_values = []`

I then ran `--once` for all seven scripts and wrote one validation row into each token-specific folder.

I also ran a short websocket-backed smoke test for every collector with:

```bash
for coin in BTC ETH SOL XRP HYPE DOGE BNB; do
  .venv-cli-trader/bin/python "cli_data_${coin}.py" --max-seconds 3 --csv-save-interval 1
done
```

Every script bootstrapped through the live market context, resolved a contract, wrote rows, and stopped cleanly. The latest CSVs had non-missing critical fields on the last row:

| Token | Latest test rows | Missing critical values |
| --- | ---: | --- |
| BTC | 4 | none |
| ETH | 5 | none |
| SOL | 4 | none |
| XRP | 4 | none |
| HYPE | 4 | none |
| DOGE | 4 | none |
| BNB | 4 | none |

Observed contract mappings during the test:

| Token | Kalshi ticker example | Polymarket ticker example |
| --- | --- | --- |
| BTC | `KXBTC15M-26MAY311345-45` | `btc-updown-15m-1780248600` |
| ETH | `KXETH15M-26MAY311345-45` | `eth-updown-15m-1780248600` |
| SOL | `KXSOL15M-26MAY311345-45` | `sol-updown-15m-1780248600` |
| XRP | `KXXRP15M-26MAY311345-45` | `xrp-updown-15m-1780248600` |
| HYPE | `KXHYPE15M-26MAY311345-45` | `hype-updown-15m-1780248600` |
| DOGE | `KXDOGE15M-26MAY311345-45` | `doge-updown-15m-1780248600` |
| BNB | `KXBNB15M-26MAY311345-45` | `bnb-updown-15m-1780248600` |

One expected limitation from the one-shot tests: `polymarket_btc_target` can be blank early in the contract because RTDS target inference requires a historical RTDS point near the market start timestamp. This matches the existing BTC collection behavior.
