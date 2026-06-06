#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
DATASET_CSV = ROOT / "kalshi_t630_strategy_dataset.csv"
HISTOGRAM_CSV = ROOT / "kalshi_available_quantity_histogram.csv"
SUMMARY_CSV = ROOT / "kalshi_quantity_capacity_summary.csv"
SWEEP_CSV = ROOT / "kalshi_quantity_capacity_rule_sweep.csv"
PLOT_PATH = ROOT / "kalshi_available_quantity_occurrences.png"
MPL_CACHE = ROOT / ".matplotlib-cache"


def fee(price: pd.Series) -> pd.Series:
    return 0.07 * price * (1.0 - price)


def current_mask(data: pd.DataFrame) -> pd.Series:
    return data["mid_p"].gt(0.55) & data["mid_p"].lt(0.80)


def recommended_mask(data: pd.DataFrame) -> pd.Series:
    return data["ask_p"].gt(0.50) & data["ask_p"].lt(0.78) & data["spot_agrees"].fillna(False)


def rule_mask(
    data: pd.DataFrame,
    low: float,
    high: float,
    price_col: str,
    spot_mode: str,
    min_favorable_distance: float | None,
    max_abs_distance: float | None,
) -> pd.Series:
    mask = data[price_col].gt(low) & data[price_col].lt(high)
    if spot_mode == "agrees":
        mask &= data["spot_agrees"].fillna(False)
    if min_favorable_distance is not None:
        mask &= data["favorable_distance"].ge(min_favorable_distance)
    if max_abs_distance is not None:
        mask &= data["abs_distance"].le(max_abs_distance)
    return mask


def rule_label(
    price_col: str,
    low: float,
    high: float,
    spot_mode: str,
    min_favorable_distance: float | None,
    max_abs_distance: float | None,
) -> str:
    price = "mid p" if price_col == "mid_p" else "ask p"
    parts = [f"{low:.2f} < {price} < {high:.2f}"]
    if spot_mode == "agrees":
        parts.append("spot agrees with selected side")
    if min_favorable_distance is not None:
        parts.append(f"favored spot distance >= ${min_favorable_distance:.0f}")
    if max_abs_distance is not None:
        parts.append(f"|spot-target| <= ${max_abs_distance:.0f}")
    return "; ".join(parts)


def summarize_capacity(data: pd.DataFrame, mask: pd.Series, name: str) -> dict[str, float | int | str]:
    selected = data[mask].copy().sort_values("timestamp_utc")
    qty = np.floor(pd.to_numeric(selected["selected_ask_qty"], errors="coerce")).fillna(0.0)
    price = selected["sim_price"].astype(float)
    success = selected["sim_success"].astype(bool)
    gross = pd.Series(np.where(success, 1.0 - price, -price), index=selected.index) * qty
    fees = fee(price) * qty
    scaled_net = gross - fees
    cumulative = scaled_net.cumsum()
    drawdown = cumulative - cumulative.cummax()
    known_qty = pd.to_numeric(selected["selected_ask_qty"], errors="coerce").notna()
    positive_qty = qty[qty > 0]
    return {
        "rule": name,
        "opportunities": int(len(selected)),
        "known_quantity_rows": int(known_qty.sum()),
        "missing_quantity_rows": int((~known_qty).sum()),
        "successes": int(success.sum()),
        "failures": int((~success).sum()),
        "total_floor_contract_capacity": int(qty.sum()),
        "median_floor_contract_capacity": float(positive_qty.median()) if len(positive_qty) else np.nan,
        "max_floor_contract_capacity": int(positive_qty.max()) if len(positive_qty) else 0,
        "scaled_gross_profit_before_fees": float(gross.sum()),
        "scaled_fees": float(fees.sum()),
        "scaled_net_profit": float(scaled_net.sum()),
        "scaled_turnover_cost": float((price * qty).sum()),
        "max_single_trade_cost": float((price * qty).max()) if len(qty) else 0.0,
        "min_cumulative_pnl": float(cumulative.min()) if len(cumulative) else 0.0,
        "max_drawdown": float(drawdown.min()) if len(drawdown) else 0.0,
    }


def sweep_capacity_rules(data: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, float | int | str | None]] = []
    lows = np.round(np.arange(0.50, 0.805, 0.025), 3)
    highs = np.round(np.arange(0.575, 0.955, 0.025), 3)
    spot_modes = ["none", "agrees"]
    favorable_thresholds: list[float | None] = [None, 25, 50, 75]
    max_abs_thresholds: list[float | None] = [None, 75, 100, 150, 200]
    for price_col in ["mid_p", "ask_p"]:
        for low in lows:
            for high in highs:
                if high <= low:
                    continue
                for spot_mode in spot_modes:
                    for min_favorable in favorable_thresholds:
                        for max_abs in max_abs_thresholds:
                            if spot_mode == "none" and min_favorable is not None:
                                continue
                            if spot_mode == "none" and max_abs is not None:
                                continue
                            mask = rule_mask(data, low, high, price_col, spot_mode, min_favorable, max_abs)
                            if not mask.any():
                                continue
                            label = rule_label(price_col, low, high, spot_mode, min_favorable, max_abs)
                            summary = summarize_capacity(data, mask, label)
                            rows.append(
                                {
                                    "price_col": price_col,
                                    "low": low,
                                    "high": high,
                                    "spot_mode": spot_mode,
                                    "min_favorable_distance": min_favorable,
                                    "max_abs_distance": max_abs,
                                    **summary,
                                }
                            )
    sweep = pd.DataFrame(rows).sort_values(
        ["scaled_net_profit", "known_quantity_rows", "opportunities"], ascending=[False, False, False]
    )
    sweep.to_csv(SWEEP_CSV, index=False)
    return sweep


def write_histogram(data: pd.DataFrame) -> pd.DataFrame:
    quantity = pd.to_numeric(data["selected_ask_qty"], errors="coerce")
    rule_sets = {
        "logged_current_rule": current_mask(data),
        "recommended_known_quantity": recommended_mask(data) & quantity.notna(),
    }
    bins = [0, 100, 250, 500, 1_000, 2_000, 4_000, np.inf]
    labels = ["0-100", "100-250", "250-500", "500-1k", "1k-2k", "2k-4k", "4k+"]
    rows: list[dict[str, int | str]] = []
    for name, mask in rule_sets.items():
        binned = pd.cut(quantity[mask].dropna(), bins=bins, labels=labels, right=False)
        counts = binned.value_counts().reindex(labels, fill_value=0)
        for label, count in counts.items():
            rows.append({"quantity_bin": str(label), "rule": name, "occurrences": int(count)})
    histogram = pd.DataFrame(rows)
    histogram.to_csv(HISTOGRAM_CSV, index=False)
    return histogram


def write_plot(histogram: pd.DataFrame) -> None:
    MPL_CACHE.mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(MPL_CACHE))
    import matplotlib.pyplot as plt

    labels = list(histogram["quantity_bin"].drop_duplicates())
    current = (
        histogram[histogram["rule"] == "logged_current_rule"]
        .set_index("quantity_bin")
        .reindex(labels)["occurrences"]
        .fillna(0)
    )
    recommended = (
        histogram[histogram["rule"] == "recommended_known_quantity"]
        .set_index("quantity_bin")
        .reindex(labels)["occurrences"]
        .fillna(0)
    )
    x = np.arange(len(labels))
    width = 0.38

    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=160)
    ax.bar(x - width / 2, current, width, label="Logged current-rule entries", color="#4f8cff")
    ax.bar(x + width / 2, recommended, width, label="Recommended rule, known qty", color="#35b779")
    ax.set_title("Available Best-Ask Quantity vs Occurrences at T=630")
    ax.set_xlabel("Available quantity at selected best ask (contracts)")
    ax.set_ylabel("Occurrences")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.grid(axis="y", color="#d8d8d8", linewidth=0.8)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(PLOT_PATH)
    plt.close(fig)


def main() -> None:
    data = pd.read_csv(DATASET_CSV)
    for col in ["mid_p", "ask_p", "sim_price", "selected_ask_qty", "net_per_contract"]:
        data[col] = pd.to_numeric(data[col], errors="coerce")

    histogram = write_histogram(data)
    write_plot(histogram)

    summaries = [
        summarize_capacity(data, current_mask(data), "current: 0.55 < mid p < 0.80"),
        summarize_capacity(data, recommended_mask(data), "recommended: 0.50 < ask p < 0.78; spot agrees"),
        summarize_capacity(
            data,
            recommended_mask(data) & data["selected_ask_qty"].notna(),
            "recommended known-liquidity subset",
        ),
    ]
    pd.DataFrame(summaries).to_csv(SUMMARY_CSV, index=False)
    sweep_capacity_rules(data)
    print(f"plot -> {PLOT_PATH}")
    print(f"histogram -> {HISTOGRAM_CSV}")
    print(f"summary -> {SUMMARY_CSV}")
    print(f"rule sweep -> {SWEEP_CSV}")


if __name__ == "__main__":
    main()
