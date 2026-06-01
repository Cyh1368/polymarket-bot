#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.analyze_profit_margin_latch_2m_1m import (
    BOOTSTRAP_STATE,
    HORIZON_DIR,
    build_states,
    first_entry_for_margin,
    latch_interval,
    load_predictions,
    md_table,
    split_contracts,
)


MARGINS = [0.05, 0.18, 0.40]
BOOTSTRAPS = 5000
CONTRACT_MINUTES = 15.0
OUT_SUMMARY = HORIZON_DIR / "latch_2m_1m_selected_margin_backtest.csv"
OUT_TRADES = HORIZON_DIR / "latch_2m_1m_selected_margin_backtest_trades.csv"
OUT_REPORT = HORIZON_DIR / "latch_2m_1m_selected_margin_backtest_report.md"


def ci(values: np.ndarray) -> tuple[float, float]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return math.nan, math.nan
    return float(np.quantile(finite, 0.025)), float(np.quantile(finite, 0.975))


def fmt_ci(mean: float, low: float, high: float, digits: int = 4) -> str:
    if not (math.isfinite(mean) and math.isfinite(low) and math.isfinite(high)):
        return "--"
    return f"{mean:.{digits}f} [{low:.{digits}f}, {high:.{digits}f}]"


def bootstrap_metrics(trade_flags: np.ndarray, costs: np.ndarray, profits: np.ndarray, divergences: np.ndarray) -> dict[str, float]:
    rng = np.random.default_rng(BOOTSTRAP_STATE)
    n = len(trade_flags)
    if n == 0:
        return {}
    idx = rng.integers(0, n, size=(BOOTSTRAPS, n))
    sampled_flags = trade_flags[idx]
    sampled_costs = costs[idx]
    sampled_profits = profits[idx]
    sampled_divergences = divergences[idx]

    trade_counts = sampled_flags.sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        trade_rates = trade_counts / n
        minutes_between = np.where(trade_counts > 0, CONTRACT_MINUTES * n / trade_counts, math.nan)
        entry_means = np.where(
            trade_counts > 0,
            np.nansum(sampled_costs, axis=1) / trade_counts,
            math.nan,
        )
        profit_per_trade = np.where(
            trade_counts > 0,
            sampled_profits.sum(axis=1) / trade_counts,
            math.nan,
        )
        divergence_rates = np.where(
            trade_counts > 0,
            np.nansum(sampled_divergences, axis=1) / trade_counts,
            math.nan,
        )
    profit_per_interval = sampled_profits.sum(axis=1) / n
    total_profit = sampled_profits.sum(axis=1)

    trade_rate_low, trade_rate_high = ci(trade_rates)
    minutes_low, minutes_high = ci(minutes_between)
    entry_low, entry_high = ci(entry_means)
    profit_trade_low, profit_trade_high = ci(profit_per_trade)
    profit_interval_low, profit_interval_high = ci(profit_per_interval)
    div_rate_low, div_rate_high = ci(divergence_rates)
    total_low, total_high = ci(total_profit)

    return {
        "trade_rate_ci_low": trade_rate_low,
        "trade_rate_ci_high": trade_rate_high,
        "minutes_between_trades_ci_low": minutes_low,
        "minutes_between_trades_ci_high": minutes_high,
        "entry_price_ci_low": entry_low,
        "entry_price_ci_high": entry_high,
        "profit_per_traded_contract_ci_low": profit_trade_low,
        "profit_per_traded_contract_ci_high": profit_trade_high,
        "profit_per_15m_contract_ci_low": profit_interval_low,
        "profit_per_15m_contract_ci_high": profit_interval_high,
        "divergence_rate_ci_low": div_rate_low,
        "divergence_rate_ci_high": div_rate_high,
        "total_profit_ci_low": total_low,
        "total_profit_ci_high": total_high,
    }


def summarize(states: dict[str, Any], sample: str, margin: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ordered_states = list(states.values())
    n = len(ordered_states)
    trade_flags = np.zeros(n, dtype=float)
    costs = np.full(n, math.nan, dtype=float)
    profits = np.zeros(n, dtype=float)
    divergences = np.full(n, math.nan, dtype=float)
    trades: list[dict[str, Any]] = []
    model_signal_contracts = 0

    for i, state in enumerate(ordered_states):
        latched = latch_interval(state)
        if latched is not None:
            model_signal_contracts += 1
        entry = first_entry_for_margin(state, margin)
        if entry is None:
            continue
        trade_flags[i] = 1.0
        costs[i] = float(entry["all_in_cost"])
        profits[i] = float(entry["realized_profit"])
        divergences[i] = float(entry["diverge"])
        trades.append({**entry, "sample": sample})

    trade_count = int(trade_flags.sum())
    divergence_count = int(np.nansum(divergences)) if trade_count else 0
    total_profit = float(profits.sum())
    mean_entry = float(np.nanmean(costs)) if trade_count else math.nan
    mean_profit_trade = float(total_profit / trade_count) if trade_count else math.nan
    mean_profit_interval = float(total_profit / n) if n else math.nan
    trade_rate = float(trade_count / n) if n else math.nan
    minutes_between = float(CONTRACT_MINUTES / trade_rate) if trade_rate > 0 else math.inf
    divergence_rate = float(divergence_count / trade_count) if trade_count else math.nan
    boot = bootstrap_metrics(trade_flags, costs, profits, divergences)

    row = {
        "sample": sample,
        "profit_margin": float(margin),
        "contracts_evaluated": int(n),
        "model_signal_contracts": int(model_signal_contracts),
        "model_signal_rate": float(model_signal_contracts / n) if n else math.nan,
        "approved_trades": trade_count,
        "approved_trade_rate": trade_rate,
        "approved_trade_rate_ci_low": boot.get("trade_rate_ci_low", math.nan),
        "approved_trade_rate_ci_high": boot.get("trade_rate_ci_high", math.nan),
        "expected_minutes_between_trades": minutes_between,
        "minutes_between_trades_ci_low": boot.get("minutes_between_trades_ci_low", math.nan),
        "minutes_between_trades_ci_high": boot.get("minutes_between_trades_ci_high", math.nan),
        "divergent_trades": divergence_count,
        "divergence_rate": divergence_rate,
        "divergence_rate_ci_low": boot.get("divergence_rate_ci_low", math.nan),
        "divergence_rate_ci_high": boot.get("divergence_rate_ci_high", math.nan),
        "expected_entry_price": mean_entry,
        "entry_price_ci_low": boot.get("entry_price_ci_low", math.nan),
        "entry_price_ci_high": boot.get("entry_price_ci_high", math.nan),
        "expected_profit_per_traded_contract": mean_profit_trade,
        "profit_per_traded_contract_ci_low": boot.get("profit_per_traded_contract_ci_low", math.nan),
        "profit_per_traded_contract_ci_high": boot.get("profit_per_traded_contract_ci_high", math.nan),
        "expected_profit_per_15m_contract": mean_profit_interval,
        "profit_per_15m_contract_ci_low": boot.get("profit_per_15m_contract_ci_low", math.nan),
        "profit_per_15m_contract_ci_high": boot.get("profit_per_15m_contract_ci_high", math.nan),
        "total_profit": total_profit,
        "total_profit_ci_low": boot.get("total_profit_ci_low", math.nan),
        "total_profit_ci_high": boot.get("total_profit_ci_high", math.nan),
    }
    return row, trades


def display_table(summary: pd.DataFrame, sample: str) -> pd.DataFrame:
    rows = summary[summary["sample"].eq(sample)].copy()
    out = pd.DataFrame(
        {
            "profit_margin": rows["profit_margin"],
            "contracts": rows["contracts_evaluated"],
            "model_signal_contracts": rows["model_signal_contracts"],
            "model_signal_rate": rows["model_signal_rate"],
            "approved_trades": rows["approved_trades"],
            "approved_trade_rate": rows["approved_trade_rate"],
            "minutes_between_trades": rows.apply(
                lambda r: fmt_ci(
                    r["expected_minutes_between_trades"],
                    r["minutes_between_trades_ci_low"],
                    r["minutes_between_trades_ci_high"],
                    1,
                ),
                axis=1,
            ),
            "divergent_trades": rows["divergent_trades"],
            "divergence_rate": rows.apply(
                lambda r: fmt_ci(r["divergence_rate"], r["divergence_rate_ci_low"], r["divergence_rate_ci_high"], 4),
                axis=1,
            ),
            "entry_price": rows.apply(
                lambda r: fmt_ci(r["expected_entry_price"], r["entry_price_ci_low"], r["entry_price_ci_high"], 4),
                axis=1,
            ),
            "profit_per_traded_contract": rows.apply(
                lambda r: fmt_ci(
                    r["expected_profit_per_traded_contract"],
                    r["profit_per_traded_contract_ci_low"],
                    r["profit_per_traded_contract_ci_high"],
                    4,
                ),
                axis=1,
            ),
            "profit_per_15m_contract": rows.apply(
                lambda r: fmt_ci(
                    r["expected_profit_per_15m_contract"],
                    r["profit_per_15m_contract_ci_low"],
                    r["profit_per_15m_contract_ci_high"],
                    4,
                ),
                axis=1,
            ),
            "total_profit": rows.apply(
                lambda r: fmt_ci(r["total_profit"], r["total_profit_ci_low"], r["total_profit_ci_high"], 4),
                axis=1,
            ),
        }
    )
    return out


def main() -> None:
    labels = pd.read_csv(HORIZON_DIR / "horizon_contract_labels.csv")
    predictions, thresholds = load_predictions()
    splits = split_contracts(labels)
    # The requested backtest is over the available 0529 data. Keep the held-out test
    # split in the report as a reality check, but use all eligible contracts as the
    # direct answer to the prompt.
    samples = {
        "all": splits["all"],
        "test": splits["test"],
    }
    all_rows: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    for sample, contract_ids in samples.items():
        states = build_states(predictions, contract_ids)
        for margin in MARGINS:
            row, trades = summarize(states, sample, margin)
            all_rows.append(row)
            all_trades.extend(trades)

    summary = pd.DataFrame(all_rows)
    trades_df = pd.DataFrame(all_trades)
    summary.to_csv(OUT_SUMMARY, index=False)
    trades_df.to_csv(OUT_TRADES, index=False)

    all_display = display_table(summary, "all")
    test_display = display_table(summary, "test")

    report = [
        "# Backtest: `latch_2m_1m` At Selected Profit Margins",
        "",
        "## Scope",
        "",
        "- Requested data directory: `kp-0529-data`. This checkout has the matching dataset and current artifacts under `kp-0529-research`, so this report uses `kp-0529-research`.",
        "- Strategy: `latch_2m_1m`. The 2m model is evaluated first; if it passes, the contract is latched tradable through expiry. If 2m fails, 1m can latch the contract.",
        f"- Current thresholds: `2m < {thresholds['2m']:.4f}`, `1m < {thresholds['1m']:.4f}`.",
        "- Entry rule: after the first passing latch model, enter at the first historical row where `best_all_in_cost < 1 - profit_margin`.",
        "- Fees are included in `best_all_in_cost` using the current odds-dependent equations: Kalshi `0.07*p*(1-p)`, Polymarket `0.05*p*(1-p)`, with `N=1`.",
        "- Return assumption for the incomplete prompt sentence: if a trade diverges, the full all-in entry cost is lost, so profit is `-entry_price`; if it does not diverge, profit is `1 - entry_price`.",
        "- Historical CSVs do not guarantee displayed ask-side liquidity, so this remains a price/outcome backtest rather than a fill simulator.",
        "- Uncertainty intervals are contract-level bootstrap 95% intervals with no-trade contracts contributing zero to per-15m-contract profit.",
        "",
        "## Direct Answers On All Eligible 0529 Contracts",
        "",
        md_table(all_display),
        "",
        "Interpretation of the time column: because each market interval is 15 minutes, `minutes_between_trades = 15 / approved_trade_rate`. For example, a 50% approval rate means one expected approved trade every 30 minutes.",
        "",
        "## Held-Out Test Split Check",
        "",
        "The table below uses the same historical test split used in prior horizon-model reports. It is included to show sensitivity outside the full in-sample view.",
        "",
        md_table(test_display),
        "",
        "## Key Definitions",
        "",
        "- `model_signal_contracts`: contracts where either the 2m model or, if needed, the 1m model passed its divergence threshold.",
        "- `approved_trades`: contracts where the model signal existed and the post-latch price also met the requested profit margin.",
        "- `entry_price`: mean all-in cost of the first approved trade, including both platform fees.",
        "- `profit_per_traded_contract`: conditional profit per executed 1-lot paired trade.",
        "- `profit_per_15m_contract`: total profit divided by every evaluated 15-minute market interval, including intervals with no trade.",
        "",
        "## Output Files",
        "",
        f"- Summary CSV: `{OUT_SUMMARY.relative_to(ROOT)}`",
        f"- Trade rows CSV: `{OUT_TRADES.relative_to(ROOT)}`",
        f"- Report: `{OUT_REPORT.relative_to(ROOT)}`",
    ]
    OUT_REPORT.write_text("\n".join(report) + "\n")
    print(f"Wrote {OUT_REPORT}")
    print(f"Wrote {OUT_SUMMARY}")
    print(f"Wrote {OUT_TRADES}")


if __name__ == "__main__":
    main()
