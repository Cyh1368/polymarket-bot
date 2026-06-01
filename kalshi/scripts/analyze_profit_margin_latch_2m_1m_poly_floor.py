#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.analyze_profit_margin_latch_2m_1m import (  # noqa: E402
    BOOTSTRAP_STATE,
    BOOTSTRAPS,
    CONTRACTS_PER_LEG,
    DATA_DIR,
    HORIZON_DIR,
    HORIZONS,
    KALSHI_FEE_RATE,
    MARGINS,
    MAX_OPPORTUNITY_INTERVAL_SECONDS,
    PLOT_DIR,
    POLYMARKET_FEE_RATE,
    fee,
    md_table,
    split_contracts,
)


POLY_PRICE_FLOORS = [
    ("none", 0.0),
    ("25c", 0.25),
    ("33c", 0.33),
    ("50c", 0.50),
]
BOOTSTRAPS_LOCAL = 500

OUT_SWEEP = HORIZON_DIR / "profit_margin_latch_2m_1m_poly_floor_sweep.csv"
OUT_TRADES = HORIZON_DIR / "profit_margin_latch_2m_1m_poly_floor_trades.csv"
OUT_REPORT = HORIZON_DIR / "profit_margin_latch_2m_1m_poly_floor_report.md"
OUT_PROFIT_PLOT = PLOT_DIR / "profit_margin_latch_2m_1m_poly_floor_expected_profit.png"
OUT_TRADES_PLOT = PLOT_DIR / "profit_margin_latch_2m_1m_poly_floor_total_trades.png"


def bootstrap_ci(values: np.ndarray, rng: np.random.Generator) -> tuple[float, float, float]:
    if values.size == 0:
        return math.nan, math.nan, math.nan
    samples = rng.choice(values, size=(BOOTSTRAPS_LOCAL, values.size), replace=True).sum(axis=1)
    return float(samples.mean()), float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975))


@dataclass(frozen=True)
class ContractState:
    contract_id: str
    source_file: str
    diverge: int
    close_time: pd.Timestamp
    asof: dict[str, pd.Timestamp]
    prob: dict[str, float]
    passed: dict[str, bool]
    opportunities: pd.DataFrame


def load_predictions() -> tuple[pd.DataFrame, dict[str, float]]:
    dataset = pd.read_csv(HORIZON_DIR / "horizon_aggregated_dataset.csv")
    frames: list[pd.DataFrame] = []
    thresholds: dict[str, float] = {}
    for horizon in HORIZONS:
        model = joblib.load(HORIZON_DIR / f"divergence_horizon_{horizon}_model.pkl")
        with (HORIZON_DIR / f"divergence_horizon_{horizon}_feature_list.json").open() as handle:
            features = json.load(handle)
        with (HORIZON_DIR / f"divergence_horizon_{horizon}_metadata.json").open() as handle:
            metadata = json.load(handle)
        threshold = float(metadata["metrics"]["recommended_trade_threshold"])
        thresholds[horizon] = threshold

        frame = dataset[
            (dataset["horizon"].eq(horizon))
            & dataset["training_eligible_label"].astype(bool)
            & dataset["aggregation_status"].eq("ok")
            & dataset["diverge"].notna()
        ].copy()
        frame["predicted_diverge_prob"] = model.predict_proba(frame[features])[:, 1]
        frame["model_pass"] = frame["predicted_diverge_prob"] < threshold
        frames.append(
            frame[
                [
                    "contract_id",
                    "source_file",
                    "horizon",
                    "asof_time",
                    "diverge",
                    "predicted_diverge_prob",
                    "model_pass",
                ]
            ]
        )
    predictions = pd.concat(frames, ignore_index=True)
    predictions["contract_id"] = predictions["contract_id"].astype(str)
    predictions["asof_time"] = pd.to_datetime(predictions["asof_time"], utc=True, errors="coerce")
    predictions["diverge"] = predictions["diverge"].astype(int)
    predictions["model_pass"] = predictions["model_pass"].astype(bool)
    return predictions, thresholds


@lru_cache(maxsize=None)
def read_contract_opportunities(source_file: str) -> pd.DataFrame:
    path = DATA_DIR / source_file
    cols = [
        "timestamp_utc",
        "kalshi_close_time",
        "kalshi_yes_ask",
        "kalshi_no_ask",
        "polymarket_yes_ask",
        "polymarket_no_ask",
    ]
    raw = pd.read_csv(path, usecols=cols)
    raw["timestamp"] = pd.to_datetime(raw["timestamp_utc"], utc=True, errors="coerce")
    raw["close_time"] = pd.to_datetime(raw["kalshi_close_time"], utc=True, errors="coerce")
    for col in ["kalshi_yes_ask", "kalshi_no_ask", "polymarket_yes_ask", "polymarket_no_ask"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")

    raw["k_yes_p_no_all_in_cost"] = (
        raw["kalshi_yes_ask"]
        + raw["polymarket_no_ask"]
        + fee(raw["kalshi_yes_ask"], KALSHI_FEE_RATE)
        + fee(raw["polymarket_no_ask"], POLYMARKET_FEE_RATE)
    )
    raw["k_no_p_yes_all_in_cost"] = (
        raw["kalshi_no_ask"]
        + raw["polymarket_yes_ask"]
        + fee(raw["kalshi_no_ask"], KALSHI_FEE_RATE)
        + fee(raw["polymarket_yes_ask"], POLYMARKET_FEE_RATE)
    )
    raw["k_yes_p_no_polymarket_price"] = raw["polymarket_no_ask"]
    raw["k_no_p_yes_polymarket_price"] = raw["polymarket_yes_ask"]

    out = raw[
        [
            "timestamp",
            "close_time",
            "k_yes_p_no_all_in_cost",
            "k_yes_p_no_polymarket_price",
            "k_no_p_yes_all_in_cost",
            "k_no_p_yes_polymarket_price",
        ]
    ].dropna(subset=["timestamp", "close_time"])
    out = out[
        out["k_yes_p_no_all_in_cost"].notna() | out["k_no_p_yes_all_in_cost"].notna()
    ].sort_values("timestamp").reset_index(drop=True)
    next_timestamp = out["timestamp"].shift(-1)
    interval_end = pd.concat([next_timestamp, out["close_time"]], axis=1).min(axis=1)
    raw_interval_seconds = (interval_end - out["timestamp"]).dt.total_seconds()
    out["raw_interval_seconds"] = raw_interval_seconds.clip(lower=0.0).fillna(0.0)
    out["interval_seconds"] = out["raw_interval_seconds"].clip(upper=MAX_OPPORTUNITY_INTERVAL_SECONDS)
    return out


def build_states(predictions: pd.DataFrame, contract_ids: set[str]) -> dict[str, ContractState]:
    states: dict[str, ContractState] = {}
    pred = predictions[predictions["contract_id"].isin(contract_ids)].copy()
    for contract_id, group in pred.groupby("contract_id", sort=False):
        if not set(HORIZONS).issubset(set(group["horizon"])):
            continue
        by_horizon = group.set_index("horizon")
        source_file = str(by_horizon["source_file"].dropna().iloc[0])
        opportunities = read_contract_opportunities(source_file)
        if opportunities.empty:
            continue
        states[str(contract_id)] = ContractState(
            contract_id=str(contract_id),
            source_file=source_file,
            diverge=int(by_horizon["diverge"].iloc[0]),
            close_time=pd.Timestamp(opportunities["close_time"].dropna().iloc[0]),
            asof={h: pd.Timestamp(by_horizon.at[h, "asof_time"]) for h in HORIZONS},
            prob={h: float(by_horizon.at[h, "predicted_diverge_prob"]) for h in HORIZONS},
            passed={h: bool(by_horizon.at[h, "model_pass"]) for h in HORIZONS},
            opportunities=opportunities,
        )
    return states


def latch_interval(state: ContractState) -> tuple[pd.Timestamp, str] | None:
    if state.passed["2m"]:
        return state.asof["2m"], "2m"
    if state.passed["1m"]:
        return state.asof["1m"], "1m"
    return None


def first_entry_for_margin(
    state: ContractState,
    margin: float,
    polymarket_price_floor: float,
) -> dict[str, Any] | None:
    latched = latch_interval(state)
    if latched is None:
        return None
    start, decision_horizon = latched
    rows = state.opportunities[
        (state.opportunities["timestamp"] >= start)
        & (state.opportunities["timestamp"] <= state.close_time)
    ]
    if rows.empty:
        return None

    cutoff = 1.0 - margin
    k_yes_cost = rows["k_yes_p_no_all_in_cost"].to_numpy(dtype=float)
    k_yes_poly_price = rows["k_yes_p_no_polymarket_price"].to_numpy(dtype=float)
    k_no_cost = rows["k_no_p_yes_all_in_cost"].to_numpy(dtype=float)
    k_no_poly_price = rows["k_no_p_yes_polymarket_price"].to_numpy(dtype=float)
    k_yes_ok = (
        np.isfinite(k_yes_cost)
        & np.isfinite(k_yes_poly_price)
        & (k_yes_cost < cutoff)
        & (k_yes_poly_price > polymarket_price_floor)
    )
    k_no_ok = (
        np.isfinite(k_no_cost)
        & np.isfinite(k_no_poly_price)
        & (k_no_cost < cutoff)
        & (k_no_poly_price > polymarket_price_floor)
    )
    eligible = k_yes_ok | k_no_ok
    if not bool(eligible.any()):
        return None

    first_idx = int(np.flatnonzero(eligible)[0])
    if k_yes_ok[first_idx] and (not k_no_ok[first_idx] or k_yes_cost[first_idx] <= k_no_cost[first_idx]):
        direction = "K+NP"
        all_in_cost = float(k_yes_cost[first_idx])
        polymarket_price = float(k_yes_poly_price[first_idx])
    else:
        direction = "NK+P"
        all_in_cost = float(k_no_cost[first_idx])
        polymarket_price = float(k_no_poly_price[first_idx])

    realized_profit = (0.0 if state.diverge else 1.0) - all_in_cost
    model_expected_profit = 1.0 - all_in_cost - state.prob[decision_horizon]
    return {
        "contract_id": state.contract_id,
        "source_file": state.source_file,
        "entry_time": pd.Timestamp(rows.iloc[first_idx]["timestamp"]),
        "decision_horizon": decision_horizon,
        "direction": direction,
        "profit_margin": float(margin),
        "polymarket_price_floor": float(polymarket_price_floor),
        "all_in_cost": all_in_cost,
        "fee_adjusted_edge": 1.0 - all_in_cost,
        "polymarket_price": polymarket_price,
        "predicted_diverge_prob": state.prob[decision_horizon],
        "diverge": state.diverge,
        "realized_profit": realized_profit,
        "model_expected_profit": model_expected_profit,
    }


def summarize_margin(
    states: dict[str, ContractState],
    sample: str,
    floor_label: str,
    polymarket_price_floor: float,
    margin: float,
    rng: np.random.Generator,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    returns = np.zeros(len(states), dtype=float)
    expected_returns = np.zeros(len(states), dtype=float)
    trade_flags = np.zeros(len(states), dtype=float)

    for idx, state in enumerate(states.values()):
        entry = first_entry_for_margin(state, margin, polymarket_price_floor)
        if entry is None:
            continue
        entries.append({**entry, "sample": sample, "floor_label": floor_label})
        returns[idx] = float(entry["realized_profit"])
        expected_returns[idx] = float(entry["model_expected_profit"])
        trade_flags[idx] = 1.0

    boot_mean, boot_low, boot_high = bootstrap_ci(returns, rng)
    boot_trade_mean, boot_trade_low, boot_trade_high = bootstrap_ci(trade_flags, rng)
    trades = len(entries)
    divergences = int(sum(entry["diverge"] for entry in entries))
    total_profit = float(returns.sum())
    summary = {
        "sample": sample,
        "floor_label": floor_label,
        "polymarket_price_floor": float(polymarket_price_floor),
        "profit_margin": float(margin),
        "profit_margin_cents": int(round(float(margin) * 100)),
        "contracts": int(len(states)),
        "model_signal_contracts": int(sum(latch_interval(state) is not None for state in states.values())),
        "trades": int(trades),
        "trade_rate": float(trades / len(states)) if states else math.nan,
        "divergences": divergences,
        "divergence_rate": float(divergences / trades) if trades else math.nan,
        "mean_polymarket_price": float(np.mean([entry["polymarket_price"] for entry in entries])) if trades else math.nan,
        "mean_all_in_cost": float(np.mean([entry["all_in_cost"] for entry in entries])) if trades else math.nan,
        "mean_fee_adjusted_edge": float(np.mean([entry["fee_adjusted_edge"] for entry in entries])) if trades else math.nan,
        "mean_profit_per_trade": float(total_profit / trades) if trades else math.nan,
        "mean_profit_per_contract": float(total_profit / len(states)) if states else math.nan,
        "total_profit": total_profit,
        "total_model_expected_profit": float(expected_returns.sum()),
        "bootstrap_expected_total_profit": boot_mean,
        "bootstrap_total_profit_ci_low": boot_low,
        "bootstrap_total_profit_ci_high": boot_high,
        "bootstrap_expected_trades": boot_trade_mean,
        "bootstrap_trades_ci_low": boot_trade_low,
        "bootstrap_trades_ci_high": boot_trade_high,
    }
    return summary, entries


def run_sweep(states_by_sample: dict[str, dict[str, ContractState]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(BOOTSTRAP_STATE)
    summaries: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for sample, states in states_by_sample.items():
        for floor_label, floor in POLY_PRICE_FLOORS:
            for margin in MARGINS:
                summary, entries = summarize_margin(states, sample, floor_label, floor, float(margin), rng)
                summaries.append(summary)
                trades.extend(entries)
    return pd.DataFrame(summaries), pd.DataFrame(trades)


def best_rows(sweep: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (sample, floor_label), group in sweep.groupby(["sample", "floor_label"], sort=False):
        if group.empty:
            continue
        best = group.sort_values(
            ["total_profit", "mean_profit_per_trade", "profit_margin"],
            ascending=[False, False, True],
        ).iloc[0]
        rows.append(best)
    return pd.DataFrame(rows)


def plot_metric(
    sweep: pd.DataFrame,
    sample: str,
    metric: str,
    low_col: str | None,
    high_col: str | None,
    ylabel: str,
    title: str,
    out_path: Path,
) -> None:
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    colors = {"none": "#111827", "25c": "#2563eb", "33c": "#16a34a", "50c": "#dc2626"}
    fig, axes = plt.subplots(1, 2, figsize=(14, 5), sharex=True)
    for ax, sample_name in zip(axes, ["calibration", "test"], strict=True):
        for floor_label, _floor in POLY_PRICE_FLOORS:
            frame = sweep[
                sweep["sample"].eq(sample_name) & sweep["floor_label"].eq(floor_label)
            ].sort_values("profit_margin")
            if frame.empty:
                continue
            x = frame["profit_margin"].to_numpy()
            y = frame[metric].to_numpy()
            ax.plot(
                x,
                y,
                "-o",
                markersize=2.5,
                linewidth=1.4,
                alpha=0.9,
                color=colors[floor_label],
                label=floor_label,
            )
            if low_col and high_col:
                low = frame[low_col].to_numpy()
                high = frame[high_col].to_numpy()
                ax.fill_between(x, low, high, color=colors[floor_label], alpha=0.09, linewidth=0)
        ax.set_title(sample_name)
        ax.set_xlabel("profit margin")
        ax.grid(True, alpha=0.25)
    axes[0].set_ylabel(ylabel)
    axes[1].legend(title="PM price floor", loc="best")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def plot_all(sweep: pd.DataFrame) -> None:
    plot_metric(
        sweep,
        "both",
        "bootstrap_expected_total_profit",
        "bootstrap_total_profit_ci_low",
        "bootstrap_total_profit_ci_high",
        "Expected total profit",
        "Latch 2m/1m expected profit by profit margin and Polymarket price floor",
        OUT_PROFIT_PLOT,
    )
    plot_metric(
        sweep,
        "both",
        "bootstrap_expected_trades",
        "bootstrap_trades_ci_low",
        "bootstrap_trades_ci_high",
        "Total trades",
        "Latch 2m/1m trade count by profit margin and Polymarket price floor",
        OUT_TRADES_PLOT,
    )


def main() -> None:
    labels = pd.read_csv(HORIZON_DIR / "horizon_contract_labels.csv")
    contract_sets = split_contracts(labels)
    predictions, thresholds = load_predictions()
    states_by_sample = {name: build_states(predictions, ids) for name, ids in contract_sets.items()}
    sweep, trades = run_sweep(states_by_sample)
    best = best_rows(sweep)

    OUT_SWEEP.parent.mkdir(parents=True, exist_ok=True)
    sweep.to_csv(OUT_SWEEP, index=False)
    trades.to_csv(OUT_TRADES, index=False)
    plot_all(sweep)

    calibration_best = best[best["sample"].eq("calibration")].copy()
    test_at_calibration_best = []
    for row in calibration_best.to_dict("records"):
        match = sweep[
            sweep["sample"].eq("test")
            & sweep["floor_label"].eq(row["floor_label"])
            & sweep["profit_margin"].eq(float(row["profit_margin"]))
        ]
        if not match.empty:
            test_at_calibration_best.append(match.iloc[0])
    test_at_calibration_best_df = pd.DataFrame(test_at_calibration_best)
    best_test = best[best["sample"].eq("test")].copy()

    report_columns = [
        "sample",
        "floor_label",
        "polymarket_price_floor",
        "profit_margin",
        "contracts",
        "model_signal_contracts",
        "trades",
        "trade_rate",
        "divergences",
        "divergence_rate",
        "mean_polymarket_price",
        "mean_all_in_cost",
        "mean_fee_adjusted_edge",
        "mean_profit_per_trade",
        "total_profit",
        "bootstrap_total_profit_ci_low",
        "bootstrap_total_profit_ci_high",
    ]
    test_at_columns = [
        "floor_label",
        "polymarket_price_floor",
        "profit_margin",
        "trades",
        "trade_rate",
        "divergences",
        "divergence_rate",
        "mean_polymarket_price",
        "mean_all_in_cost",
        "mean_profit_per_trade",
        "total_profit",
        "bootstrap_total_profit_ci_low",
        "bootstrap_total_profit_ci_high",
    ]
    report = [
        "# Profit Margin Sweep With Polymarket Price Floors",
        "",
        "## Scope",
        "",
        "- Strategy: same `latch_2m_1m` logic as `profit_margin_latch_2m_1m_report.md`.",
        f"- Saved model thresholds: `2m={thresholds['2m']:.4f}`, `1m={thresholds['1m']:.4f}`.",
        "- Entry rule: after the first passing latch model, enter at the first historical row where an arbitrage direction satisfies `all_in_cost < 1 - profit_margin`.",
        "- New constraint: the Polymarket ask price for the Polymarket leg in that direction must be strictly greater than the configured floor.",
        "- Tested floors: no floor, `>25c`, `>33c`, and `>50c`.",
        "- If both arb directions qualify in the same row, the cheaper all-in direction is used.",
        "- Return rule: if the platforms agree, profit is `1 - all_in_cost`; if they diverge, the full stake is lost and profit is `-all_in_cost`.",
        "",
        "## Calibration-Optimal Margins",
        "",
        md_table(calibration_best[report_columns].sort_values("polymarket_price_floor")),
        "",
        "## Held-Out Test At Calibration-Optimal Margins",
        "",
        md_table(test_at_calibration_best_df[test_at_columns].sort_values("polymarket_price_floor")),
        "",
        "## Test-Set Best Margins",
        "",
        "These are for reference only because they are selected on held-out test data.",
        "",
        md_table(best_test[report_columns].sort_values("polymarket_price_floor")),
        "",
        "## Plots",
        "",
        f"![Expected profit]({OUT_PROFIT_PLOT.relative_to(HORIZON_DIR)})",
        "",
        f"![Total trades]({OUT_TRADES_PLOT.relative_to(HORIZON_DIR)})",
        "",
        "## Output Files",
        "",
        f"- Sweep table: `{OUT_SWEEP.relative_to(HORIZON_DIR)}`",
        f"- Trade table: `{OUT_TRADES.relative_to(HORIZON_DIR)}`",
        f"- Expected profit plot: `{OUT_PROFIT_PLOT.relative_to(HORIZON_DIR)}`",
        f"- Total trades plot: `{OUT_TRADES_PLOT.relative_to(HORIZON_DIR)}`",
    ]
    OUT_REPORT.write_text("\n".join(report) + "\n")
    print(f"Wrote {OUT_REPORT}")
    print(f"Wrote {OUT_SWEEP}")
    print(f"Wrote {OUT_TRADES}")
    print(f"Wrote {OUT_PROFIT_PLOT}")
    print(f"Wrote {OUT_TRADES_PLOT}")


if __name__ == "__main__":
    main()
