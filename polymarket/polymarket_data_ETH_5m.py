#!/usr/bin/env python3
"""Collect live Polymarket ETH 5-minute Up/Down market data."""

from polymarket_data_BTC_5m import main_with_default_coin


if __name__ == "__main__":
    raise SystemExit(main_with_default_coin("ETH"))
