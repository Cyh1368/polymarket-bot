#!/usr/bin/env python3
"""Compute regime labels for each labeled Polymarket BTC 5m training contract.

For each contract, looks up the BTC price at T1 (event_end - 180s) from the
Kraken hourly OHLCV and computes:
  btc_1h_ret  = (price_T1 - price_T1-1h) / price_T1-1h
  btc_4h_ret  = (price_T1 - price_T1-4h) / price_T1-4h

Regime assignment:
  UP   : btc_4h_ret > +UP_THRESH
  DOWN : btc_4h_ret < -DOWN_THRESH
  FLAT : otherwise

If any regime has < MIN_REGIME_SIZE contracts, collapses DOWN+FLAT → NON_UP.

Output: 2026-06-16-research/contract_regime_labels.csv
"""
from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OHLCV_PATH = Path(__file__).resolve().parent / "btc_kraken_1h.csv"
OUTCOMES_CSV = ROOT / "polymarket" / "polymarket_btc_5m_official_outcomes.csv"
OUT = Path(__file__).resolve().parent / "contract_regime_labels.csv"

UP_THRESH   = 0.003   # +0.3%
DOWN_THRESH = 0.003   # -0.3%
MIN_REGIME_SIZE = 300

T1_OFFSET = 180  # seconds before market close


def load_hourly_prices() -> dict[int, float]:
    """Returns ts_unix (floored to hour) → close price."""
    prices: dict[int, float] = {}
    with open(OHLCV_PATH) as f:
        for row in csv.DictReader(f):
            ts = int(row["ts_unix"])
            close = float(row["close"])
            prices[ts] = close
    return prices


def get_price_at(prices: dict[int, float], ts: int) -> float | None:
    """Return the Kraken close for the most recent completed hourly candle before ts."""
    hour_ts = (ts // 3600) * 3600
    # Walk back up to 3 hours to find a valid candle
    for offset in [0, 3600, 7200, 10800]:
        p = prices.get(hour_ts - offset)
        if p is not None:
            return p
    return None


def main() -> None:
    print("Loading Kraken hourly prices ...")
    prices = load_hourly_prices()
    print(f"  {len(prices)} hourly candles loaded")

    print("Loading official outcomes ...")
    labeled: dict[str, dict] = {}
    with open(OUTCOMES_CSV) as f:
        for row in csv.DictReader(f):
            labeled[row["market_slug"]] = row

    print(f"  {len(labeled)} labeled contracts")

    records = []
    missing_spot = 0
    missing_lookback = 0

    for slug, outcome in sorted(labeled.items()):
        event_end = outcome.get("event_end_utc", "")
        if not event_end:
            missing_spot += 1
            continue

        try:
            dt = datetime.fromisoformat(event_end.replace("Z", "+00:00"))
        except ValueError:
            missing_spot += 1
            continue

        t1_ts = int(dt.timestamp()) - T1_OFFSET

        p_t1   = get_price_at(prices, t1_ts)
        p_1h   = get_price_at(prices, t1_ts - 3600)
        p_4h   = get_price_at(prices, t1_ts - 4 * 3600)

        if p_t1 is None:
            missing_spot += 1
            continue
        if p_1h is None or p_4h is None:
            missing_lookback += 1
            continue

        ret_1h = (p_t1 - p_1h) / p_1h
        ret_4h = (p_t1 - p_4h) / p_4h

        if ret_4h > UP_THRESH:
            regime = "UP"
        elif ret_4h < -DOWN_THRESH:
            regime = "DOWN"
        else:
            regime = "FLAT"

        records.append({
            "market_slug": slug,
            "t1_ts": t1_ts,
            "price_t1": round(p_t1, 2),
            "price_1h_ago": round(p_1h, 2),
            "price_4h_ago": round(p_4h, 2),
            "btc_1h_ret": round(ret_1h, 6),
            "btc_4h_ret": round(ret_4h, 6),
            "regime": regime,
        })

    print(f"\n  Contracts with regime labels: {len(records)}")
    print(f"  Missing spot price: {missing_spot}")
    print(f"  Missing 4h lookback: {missing_lookback}")

    # Distribution
    counts = {"UP": 0, "FLAT": 0, "DOWN": 0}
    for r in records:
        counts[r["regime"]] += 1

    print(f"\nRegime distribution (threshold=±{UP_THRESH*100:.1f}%):")
    for regime, n in counts.items():
        pct = 100.0 * n / max(len(records), 1)
        print(f"  {regime:6s}: {n:4d} contracts ({pct:.1f}%)")

    # Collapse to 2 regimes if needed
    use_two_regimes = any(n < MIN_REGIME_SIZE for n in counts.values())
    if use_two_regimes:
        print(f"\n  WARNING: Regime(s) below MIN_REGIME_SIZE={MIN_REGIME_SIZE}.")
        print("  Collapsing DOWN+FLAT → NON_UP for 2-regime system.")
        for r in records:
            if r["regime"] != "UP":
                r["regime"] = "NON_UP"
        new_counts: dict[str, int] = {}
        for r in records:
            new_counts[r["regime"]] = new_counts.get(r["regime"], 0) + 1
        print("  New distribution:")
        for regime, n in new_counts.items():
            pct = 100.0 * n / max(len(records), 1)
            print(f"    {regime:8s}: {n:4d} ({pct:.1f}%)")

    # btc_4h_ret stats per regime
    from collections import defaultdict
    import statistics
    by_regime: dict[str, list[float]] = defaultdict(list)
    for r in records:
        by_regime[r["regime"]].append(r["btc_4h_ret"])

    print("\nbtc_4h_ret stats per regime:")
    for regime, vals in sorted(by_regime.items()):
        mean = statistics.mean(vals)
        std  = statistics.stdev(vals) if len(vals) > 1 else 0
        mn   = min(vals)
        mx   = max(vals)
        print(f"  {regime:8s}: n={len(vals):4d}  mean={mean*100:+.3f}%  std={std*100:.3f}%  "
              f"min={mn*100:+.3f}%  max={mx*100:+.3f}%")

    # Write output
    fields = ["market_slug", "t1_ts", "price_t1", "price_1h_ago", "price_4h_ago",
              "btc_1h_ret", "btc_4h_ret", "regime"]
    with open(OUT, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)

    print(f"\nSaved → {OUT}")


if __name__ == "__main__":
    main()
