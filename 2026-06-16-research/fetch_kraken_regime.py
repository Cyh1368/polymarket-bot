#!/usr/bin/env python3
"""Fetch Kraken 1-minute OHLCV for BTC/USD from June 1 2026 to present.

Output: 2026-06-16-research/btc_kraken_1h.csv
Columns: ts_unix, open, high, low, close, vwap, volume
"""
from __future__ import annotations

import csv
import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
PAIR = "XBTUSD"
INTERVAL = 60  # 60-minute candles; 720 candles = 30 days of history per call

OUT = Path(__file__).resolve().parent / "btc_kraken_1h.csv"

# June 1 2026 00:00 UTC
START_TS = int(datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())


def http_get(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "regime-fetch/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_kraken_1m(start_ts: int) -> list[tuple]:
    """Returns list of (ts_unix, open, high, low, close, vwap, volume)."""
    candles: list[tuple] = []
    since = start_ts
    now_ts = int(datetime.now(timezone.utc).timestamp())

    while since <= now_ts:
        url = f"{KRAKEN_OHLC_URL}?pair={PAIR}&interval={INTERVAL}&since={since}"
        try:
            data = http_get(url)
        except Exception as exc:
            print(f"  Kraken error at since={since}: {exc}")
            break

        errors = data.get("error") or []
        if errors:
            print(f"  Kraken API error: {errors}")
            break

        result = data.get("result", {})
        pair_key = next((k for k in result if k != "last"), None)
        if not pair_key:
            break

        raw = result[pair_key]
        if not raw:
            break

        for row in raw:
            ts   = int(row[0])
            o    = float(row[1])
            h    = float(row[2])
            lo   = float(row[3])
            c    = float(row[4])
            vwap = float(row[5])
            vol  = float(row[6])
            candles.append((ts, o, h, lo, c, vwap, vol))

        last_ts = int(result.get("last", raw[-1][0]))
        if last_ts <= since:
            break
        since = last_ts
        if since > now_ts:
            break
        time.sleep(0.5)

    return candles


def main() -> None:
    print(f"Fetching Kraken 1m OHLCV for {PAIR} from {datetime.utcfromtimestamp(START_TS)} UTC ...")
    candles = fetch_kraken_1m(START_TS)

    # Deduplicate by timestamp (keep last seen)
    seen: dict[int, tuple] = {}
    for c in candles:
        seen[c[0]] = c
    sorted_candles = sorted(seen.values(), key=lambda x: x[0])

    print(f"  {len(sorted_candles)} candles fetched")
    if sorted_candles:
        t0 = datetime.utcfromtimestamp(sorted_candles[0][0])
        t1 = datetime.utcfromtimestamp(sorted_candles[-1][0])
        print(f"  Range: {t0} UTC → {t1} UTC")

        # Check for gaps (> 90 minutes between consecutive candles)
        gaps = 0
        for i in range(1, len(sorted_candles)):
            diff = sorted_candles[i][0] - sorted_candles[i-1][0]
            if diff > 5400:
                gaps += 1
        pct_missing = 100.0 * gaps / max(len(sorted_candles), 1)
        print(f"  Gaps (>2min): {gaps} ({pct_missing:.1f}%)")

    with open(OUT, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ts_unix", "open", "high", "low", "close", "vwap", "volume"])
        writer.writerows(sorted_candles)

    print(f"Saved → {OUT}")


if __name__ == "__main__":
    main()
