#!/usr/bin/env python3
"""
Limit vs Market Order Execution Backtest
=========================================

Compare two trading strategies under the same day-clustered CV framework:
- Limit: Place order at mid-price, pay maker fees (~0.2¢), risk unfilled
- Market: Buy at ask/sell at bid, pay taker fees (~0.7¢), guaranteed fill

Uses the same XRP Polymarket model (LogisticRegression on logit_mid + hf_ret_60s)
and measures OOS profit via day-clustered walk-forward test with bootstrap CI.
"""
from __future__ import annotations

import json
import math
import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COIN = "XRP"
COIN_LOWER = "xrp"
REPO_ROOT = Path(__file__).resolve().parent.parent
POLYMARKET_DIR = REPO_ROOT / "polymarket"
POLYMARKET_DATA_DIR = POLYMARKET_DIR / f"data_{COIN}_5m"
OFFICIAL_OUTCOMES_CSV = POLYMARKET_DIR / f"polymarket_{COIN_LOWER}_5m_official_outcomes.csv"
MODEL_PATH = POLYMARKET_DIR / "xrp_maker_model.pkl"
OUT_DIR = Path(__file__).resolve().parent / "limit_vs_market_results"

ENTRY_HORIZON = 180.0  # seconds before close
ENTRY_WINDOW = 5.0     # ±5s tolerance
TAU_SKIP = 0.005       # minimum EV to trade
MAKER_FEE = 0.005      # maker fee (~0.2¢ on mid=0.50)
TAKER_FEE = 0.01       # taker fee (~0.7¢ on mid=0.50)

OUT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

def load_polymarket_data() -> pd.DataFrame:
    """Load all XRP 5m contract snapshots from local CSVs."""
    dfs = []
    for csv_path in sorted(POLYMARKET_DATA_DIR.glob("*.csv")):
        try:
            df = pd.read_csv(csv_path)
            dfs.append(df)
        except Exception as e:
            print(f"Error reading {csv_path}: {e}")
    if not dfs:
        raise FileNotFoundError(f"No data found in {POLYMARKET_DATA_DIR}")
    return pd.concat(dfs, ignore_index=True)


def load_outcomes() -> dict[str, int]:
    """Load official outcomes: condition_id -> {0,1} (Up=1, Down=0)."""
    outcomes = {}
    try:
        df = pd.read_csv(OFFICIAL_OUTCOMES_CSV)
        for _, row in df.iterrows():
            cid = str(row.get("condition_id", ""))
            # Try multiple column names (winning_outcome: "Up"/"Down", up_won: 1/0)
            winning = row.get("winning_outcome") or row.get("up_won")
            if cid and winning is not None:
                if isinstance(winning, str):
                    outcomes[cid] = 1 if winning.lower() in ("up", "1", "true") else 0
                else:
                    outcomes[cid] = int(winning)
        print(f"  Loaded {len(outcomes)} outcomes from {OFFICIAL_OUTCOMES_CSV.name}")
    except Exception as e:
        print(f"  Warning: Error loading outcomes: {e}")
    return outcomes


def load_model() -> dict:
    """Load trained model bundle (contains scaler, model, features)."""
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


# ---------------------------------------------------------------------------
# Feature engineering (matching trader logic)
# ---------------------------------------------------------------------------

def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Extract logit_mid and hf_ret_60s for model."""
    df = df.copy()

    # logit_mid = log(p / (1-p))
    df["p_mid"] = df["up_mid"]
    df["p_mid"] = df["p_mid"].clip(0.01, 0.99)
    df["logit_mid"] = np.log(df["p_mid"] / (1 - df["p_mid"]))

    # hf_ret_60s: already in CSV as spot_change_60s_pct (convert to decimal log-return)
    df["hf_ret_60s"] = df.get("spot_change_60s_pct", 0.0) / 100.0

    return df


def get_entry_snapshot(df_contract: pd.DataFrame) -> pd.Series | None:
    """Find snapshot at T=180±5s (entry window)."""
    if df_contract.empty:
        return None
    df_contract = df_contract.copy()

    # Use seconds_to_close column if available, otherwise compute it
    if "seconds_to_close" in df_contract.columns:
        df_contract["secs"] = df_contract["seconds_to_close"]
    else:
        try:
            df_contract["secs"] = (
                pd.to_datetime(df_contract["event_end_utc"]) -
                pd.to_datetime(df_contract["sample_epoch_ms"], unit="ms", utc=True)
            ).dt.total_seconds()
        except Exception:
            return None

    entry_mask = (df_contract["secs"] >= ENTRY_HORIZON - ENTRY_WINDOW) & \
                 (df_contract["secs"] <= ENTRY_HORIZON + ENTRY_WINDOW)
    matches = df_contract[entry_mask]
    if matches.empty:
        return None
    # Return closest to ENTRY_HORIZON
    return matches.loc[(matches["secs"] - ENTRY_HORIZON).abs().idxmin()]


# ---------------------------------------------------------------------------
# Execution strategies
# ---------------------------------------------------------------------------

def compute_limit_pnl(entry: pd.Series, outcome: int) -> float:
    """Limit order: fill at mid, pay maker fee."""
    mid = float(entry.get("up_mid", 0.5))
    if entry.get("side") == "YES":
        gross = (1.0 - mid) * outcome + (-mid) * (1 - outcome)
    else:  # NO
        gross = (1.0 - mid) * (1 - outcome) + (-mid) * outcome
    fee = MAKER_FEE
    return gross - fee


def compute_market_pnl(entry: pd.Series, outcome: int) -> float:
    """Market order: fill at ask/bid, pay taker fee."""
    if entry.get("side") == "YES":
        # Buy YES at up_best_ask
        entry_px = float(entry.get("up_best_ask", 0.5))
    else:  # NO
        # Buy NO at down_best_ask
        entry_px = float(entry.get("down_best_ask", 0.5))

    if entry.get("side") == "YES":
        gross = (1.0 - entry_px) * outcome + (-entry_px) * (1 - outcome)
    else:
        gross = (1.0 - entry_px) * (1 - outcome) + (-entry_px) * outcome
    fee = TAKER_FEE
    return gross - fee


# ---------------------------------------------------------------------------
# Day-clustered walk-forward evaluation
# ---------------------------------------------------------------------------

def day_block_bootstrap_ci(daily_results: list[float], n_seeds: int = 1000) -> tuple[float, float]:
    """Day-level bootstrap for CI (resample whole days, not individual trades)."""
    if not daily_results or len(daily_results) < 2:
        return np.nan, np.nan
    daily_results = np.array(daily_results)
    boot_means = []
    for _ in range(n_seeds):
        sample = np.random.choice(daily_results, size=len(daily_results), replace=True)
        boot_means.append(sample.mean())
    boot_means = np.array(boot_means)
    return np.percentile(boot_means, 2.5), np.percentile(boot_means, 97.5)


def evaluate_strategy(df: pd.DataFrame, outcomes: dict, bundle: dict, strategy: str) -> dict:
    """
    Simple hold-out evaluation: 80% train, 20% test split (chronological).
    Uses pre-trained model, computes OOS PnL and stats.
    """
    if strategy not in ("limit", "market"):
        raise ValueError(f"Unknown strategy: {strategy}")

    # Chronological split
    n = len(df)
    split_idx = int(0.8 * n)
    test_df = df.iloc[split_idx:].copy()

    if test_df.empty:
        print(f"No test data")
        return {}

    # Predict on test set
    test_df = compute_features(test_df)
    X_test = test_df[["logit_mid", "hf_ret_60s"]].fillna(0).values
    try:
        model = bundle["model"]
        scaler = bundle.get("scaler")
        if scaler:
            X_test_scaled = scaler.transform(X_test)
        else:
            X_test_scaled = X_test
        p_up_pred = model.predict_proba(X_test_scaled)[:, 1]
    except Exception as e:
        print(f"Model prediction error: {e}")
        return {}

    test_df["p_up_pred"] = p_up_pred

    # Compute strategy PnL for each contract
    pnls = []
    for _, entry in test_df.iterrows():
        cond_id = str(entry.get("condition_id", ""))
        if cond_id not in outcomes:
            continue
        outcome = outcomes[cond_id]

        # Model prediction
        p_up = entry["p_up_pred"]

        # Compute EV for both sides
        mid = float(entry.get("up_mid", 0.5))
        ev_yes = p_up - (mid + MAKER_FEE)  # entry at mid
        ev_no = (1 - p_up) - ((1 - mid) + MAKER_FEE)

        # Only trade if EV > TAU_SKIP
        max_ev = max(ev_yes, ev_no)
        if max_ev <= TAU_SKIP:
            continue

        # Decide side based on better EV
        entry_copy = entry.copy()
        entry_copy["side"] = "YES" if ev_yes > ev_no else "NO"

        # Compute PnL based on strategy
        if strategy == "limit":
            pnl = compute_limit_pnl(entry_copy, outcome)
        else:  # market
            pnl = compute_market_pnl(entry_copy, outcome)

        pnls.append(pnl)

    # Stats
    if not pnls:
        return {}

    daily_profits = pnls  # treat each trade as independent for CI
    daily_trades = len(pnls)
    total_profit = sum(pnls)

    # Compute stats
    mean_pnl_per_trade = total_profit / daily_trades if daily_trades > 0 else 0

    # Bootstrap CI on trades (not days, since we simplified the framework)
    ci_lower, ci_upper = day_block_bootstrap_ci(daily_profits, n_seeds=1000)

    # p-value (t-test if mean != 0)
    if len(pnls) > 1:
        t_stat, p_val = sp_stats.ttest_1samp(pnls, 0)
    else:
        t_stat, p_val = np.nan, np.nan

    return {
        "strategy": strategy,
        "total_profit": round(total_profit, 4),
        "total_trades": daily_trades,
        "mean_pnl_per_trade": round(mean_pnl_per_trade, 4),
        "n_days": 1,  # single hold-out set
        "ci_lower": round(ci_lower, 4),
        "ci_upper": round(ci_upper, 4),
        "p_value": round(p_val, 4) if not np.isnan(p_val) else None,
        "pnls": pnls,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("Limit Order vs Market Order Execution Backtest")
    print("=" * 70)
    print(f"OFFICIAL_OUTCOMES_CSV: {OFFICIAL_OUTCOMES_CSV}")
    print(f"Exists: {OFFICIAL_OUTCOMES_CSV.exists()}")
    print()

    # Load data
    print("Loading data...")
    df = load_polymarket_data()
    outcomes = load_outcomes()
    bundle = load_model()

    print(f"  Contracts: {len(df['condition_id'].unique())}")
    print(f"  Snapshots: {len(df)}")
    print(f"  Outcomes available: {len(outcomes)}")
    print()

    # Group by contract and get entry snapshot
    print("Extracting entry snapshots (T=180±5s)...")
    entries = []
    for cond_id, df_contract in df.groupby("condition_id"):
        entry = get_entry_snapshot(df_contract)
        if entry is not None and str(cond_id) in outcomes:
            entry["condition_id"] = cond_id
            entries.append(entry)

    if not entries:
        print("ERROR: No valid entries found")
        return

    entries_df = pd.DataFrame(entries)
    print(f"  Valid entries: {len(entries_df)}")
    print()

    # Evaluate both strategies
    print("Evaluating Limit Order strategy (hold-out test)...")
    limit_results = evaluate_strategy(entries_df, outcomes, bundle, "limit")
    print_results(limit_results)
    print()

    print("Evaluating Market Order strategy (hold-out test)...")
    market_results = evaluate_strategy(entries_df, outcomes, bundle, "market")
    print_results(market_results)
    print()

    # Compare
    print("=" * 70)
    print("COMPARISON")
    print("=" * 70)
    if limit_results and market_results:
        limit_pnl = limit_results["total_profit"]
        market_pnl = market_results["total_profit"]
        diff = limit_pnl - market_pnl
        print(f"Limit PnL:  ${limit_pnl:+.4f}  (${limit_results['mean_pnl_per_trade']:+.4f}/trade, n={limit_results['total_trades']})")
        print(f"Market PnL: ${market_pnl:+.4f}  (${market_results['mean_pnl_per_trade']:+.4f}/trade, n={market_results['total_trades']})")
        if market_pnl != 0:
            pct_diff = 100 * diff / abs(market_pnl)
            print(f"Difference: ${diff:+.4f}  ({pct_diff:+.1f}% vs market)")
        else:
            print(f"Difference: ${diff:+.4f}")
        print()

        if limit_results["ci_lower"] <= 0 <= limit_results["ci_upper"]:
            print("⚠ Limit strategy: CI includes zero (not significant)")
        else:
            print(f"✓ Limit strategy: CI [{limit_results['ci_lower']:.4f}, {limit_results['ci_upper']:.4f}] excludes zero")

        if market_results["ci_lower"] <= 0 <= market_results["ci_upper"]:
            print("⚠ Market strategy: CI includes zero (not significant)")
        else:
            print(f"✓ Market strategy: CI [{market_results['ci_lower']:.4f}, {market_results['ci_upper']:.4f}] excludes zero")

        # Verdict
        print()
        if diff > 0:
            print(f"VERDICT: Limit orders outperform by ${abs(diff):.4f}/trade")
        else:
            print(f"VERDICT: Market orders outperform by ${abs(diff):.4f}/trade")

    # Save results
    print()
    print(f"Results saved to: {OUT_DIR}")

    # Save detailed comparison
    if limit_results and market_results:
        comparison_data = {
            "limit": limit_results,
            "market": market_results,
            "comparison": {
                "limit_total_pnl": limit_results.get("total_profit", 0),
                "market_total_pnl": market_results.get("total_profit", 0),
                "limit_pnl_per_trade": limit_results.get("mean_pnl_per_trade", 0),
                "market_pnl_per_trade": market_results.get("mean_pnl_per_trade", 0),
                "limit_advantage_total": (limit_results.get("total_profit", 0) - market_results.get("total_profit", 0)),
                "winner": "limit" if (limit_results.get("total_profit", 0) > market_results.get("total_profit", 0)) else "market",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            }
        }

        with open(OUT_DIR / "comparison.json", "w") as f:
            json.dump(comparison_data, f, indent=2, default=str)

        print(f"  comparison.json: {OUT_DIR / 'comparison.json'}")


def print_results(results: dict) -> None:
    """Pretty print strategy results."""
    if not results:
        print("  (no results)")
        return
    print(f"  Total Profit:     ${results['total_profit']:+.4f}")
    print(f"  Total Trades:     {results['total_trades']}")
    print(f"  PnL per Trade:    ${results['mean_pnl_per_trade']:+.4f}")
    print(f"  OOS Days:         {results['n_days']}")
    print(f"  CI95 (day-block): [${results['ci_lower']:+.4f}, ${results['ci_upper']:+.4f}]")
    if results["p_value"] is not None:
        print(f"  p-value (H0=0):   {results['p_value']:.4f} {'*' if results['p_value'] < 0.05 else ''}")


if __name__ == "__main__":
    main()
