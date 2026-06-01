#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "kp-0529-research"
HORIZON_DIR = DATA_DIR / "horizon_models"
PLOT_DIR = HORIZON_DIR / "plots"

RANDOM_STATE = 20260529
BOOTSTRAP_STATE = 20260531
BOOTSTRAPS = 2000
HORIZONS = ["2m", "1m"]
MARGINS = np.round(np.arange(0.00, 1.00, 0.01), 2)
KALSHI_FEE_RATE = 0.07
POLYMARKET_FEE_RATE = 0.05
CONTRACTS_PER_LEG = 1.0
MAX_OPPORTUNITY_INTERVAL_SECONDS = 5.0

OUT_SWEEP = HORIZON_DIR / "profit_margin_latch_2m_1m_sweep.csv"
OUT_TRADES = HORIZON_DIR / "profit_margin_latch_2m_1m_trades.csv"
OUT_REPORT = HORIZON_DIR / "profit_margin_latch_2m_1m_report.md"
OUT_PLOT = PLOT_DIR / "profit_margin_latch_2m_1m_expected_profit.png"
OUT_TRADES_PLOT = PLOT_DIR / "profit_margin_latch_2m_1m_total_trades.png"
OUT_DURATION_PLOT = PLOT_DIR / "profit_margin_latch_2m_1m_avg_arb_duration.png"


def md_table(df: pd.DataFrame, floatfmt: str = ".4f") -> str:
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_float_dtype(out[col]):
            out[col] = out[col].map(lambda x: "" if pd.isna(x) else format(x, floatfmt))
        else:
            out[col] = out[col].map(lambda x: "" if pd.isna(x) else str(x))
    header = "| " + " | ".join(out.columns) + " |"
    sep = "| " + " | ".join("---" for _ in out.columns) + " |"
    rows = ["| " + " | ".join(str(v).replace("|", "\\|") for v in row) + " |" for row in out.values]
    return "\n".join([header, sep, *rows])


def fee(price: pd.Series | float, rate: float) -> pd.Series | float:
    return rate * CONTRACTS_PER_LEG * price * (1.0 - price)


def split_contracts(labels: pd.DataFrame) -> dict[str, set[str]]:
    eligible = labels[labels["training_eligible"]].dropna(subset=["diverge"]).copy()
    eligible["diverge"] = eligible["diverge"].astype(int)
    contracts = eligible["contract_id"].astype(str).to_numpy()
    y = eligible["diverge"].to_numpy()
    train_contracts, test_contracts, y_train, _y_test = train_test_split(
        contracts,
        y,
        test_size=0.20,
        random_state=RANDOM_STATE,
        stratify=y,
    )
    _core_contracts, calib_contracts, _y_core, _y_calib = train_test_split(
        train_contracts,
        y_train,
        test_size=0.25,
        random_state=RANDOM_STATE,
        stratify=y_train,
    )
    return {
        "calibration": set(map(str, calib_contracts)),
        "test": set(map(str, test_contracts)),
        "all": set(map(str, contracts)),
    }


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

    k_yes_p_no = (
        raw["kalshi_yes_ask"]
        + raw["polymarket_no_ask"]
        + fee(raw["kalshi_yes_ask"], KALSHI_FEE_RATE)
        + fee(raw["polymarket_no_ask"], POLYMARKET_FEE_RATE)
    )
    k_no_p_yes = (
        raw["kalshi_no_ask"]
        + raw["polymarket_yes_ask"]
        + fee(raw["kalshi_no_ask"], KALSHI_FEE_RATE)
        + fee(raw["polymarket_yes_ask"], POLYMARKET_FEE_RATE)
    )
    raw["k_yes_p_no_all_in_cost"] = k_yes_p_no
    raw["k_no_p_yes_all_in_cost"] = k_no_p_yes
    raw["best_all_in_cost"] = np.minimum(k_yes_p_no, k_no_p_yes)
    raw["best_direction"] = np.where(k_yes_p_no <= k_no_p_yes, "K+NP", "NK+P")
    out = raw[
        [
            "timestamp",
            "close_time",
            "best_all_in_cost",
            "best_direction",
            "k_yes_p_no_all_in_cost",
            "k_no_p_yes_all_in_cost",
        ]
    ].dropna(subset=["timestamp", "close_time", "best_all_in_cost"])
    out = out.sort_values("timestamp").reset_index(drop=True)
    next_timestamp = out["timestamp"].shift(-1)
    interval_end = pd.concat([next_timestamp, out["close_time"]], axis=1).min(axis=1)
    interval_seconds = (interval_end - out["timestamp"]).dt.total_seconds()
    out["raw_interval_seconds"] = interval_seconds.clip(lower=0.0).fillna(0.0)
    out["interval_seconds"] = out["raw_interval_seconds"].clip(upper=MAX_OPPORTUNITY_INTERVAL_SECONDS)
    return out


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


def first_entry_for_margin(state: ContractState, margin: float) -> dict[str, Any] | None:
    latched = latch_interval(state)
    if latched is None:
        return None
    start, decision_horizon = latched
    cutoff = 1.0 - margin
    rows = state.opportunities[
        (state.opportunities["timestamp"] >= start)
        & (state.opportunities["timestamp"] <= state.close_time)
        & (state.opportunities["best_all_in_cost"] < cutoff)
    ]
    if rows.empty:
        return None
    row = rows.iloc[0]
    all_in_cost = float(row["best_all_in_cost"])
    realized_profit = (0.0 if state.diverge else 1.0) - all_in_cost
    model_expected_profit = 1.0 - all_in_cost - state.prob[decision_horizon]
    return {
        "contract_id": state.contract_id,
        "source_file": state.source_file,
        "entry_time": pd.Timestamp(row["timestamp"]),
        "decision_horizon": decision_horizon,
        "direction": str(row["best_direction"]),
        "profit_margin": float(margin),
        "all_in_cost": all_in_cost,
        "fee_adjusted_edge": 1.0 - all_in_cost,
        "predicted_diverge_prob": state.prob[decision_horizon],
        "diverge": state.diverge,
        "realized_profit": realized_profit,
        "model_expected_profit": model_expected_profit,
    }


def opportunity_episodes_for_margin(state: ContractState, margin: float) -> list[float]:
    latched = latch_interval(state)
    if latched is None:
        return []
    start, _decision_horizon = latched
    cutoff = 1.0 - margin
    rows = state.opportunities[
        (state.opportunities["timestamp"] >= start)
        & (state.opportunities["timestamp"] <= state.close_time)
    ].copy()
    if rows.empty:
        return []

    qualifies = rows["best_all_in_cost"].to_numpy() < cutoff
    if not bool(qualifies.any()):
        return []

    intervals = rows["interval_seconds"].to_numpy(dtype=float)
    raw_intervals = rows["raw_interval_seconds"].to_numpy(dtype=float)
    durations: list[float] = []
    current_duration = 0.0
    previous_qualified = False

    for idx, qualified in enumerate(qualifies):
        if not qualified:
            if current_duration > 0.0:
                durations.append(current_duration)
                current_duration = 0.0
            previous_qualified = False
            continue

        if (
            previous_qualified
            and current_duration > 0.0
            and idx > 0
            and raw_intervals[idx - 1] > MAX_OPPORTUNITY_INTERVAL_SECONDS
        ):
            durations.append(current_duration)
            current_duration = 0.0

        current_duration += float(intervals[idx])
        previous_qualified = True

    if current_duration > 0.0:
        durations.append(current_duration)
    return durations


def bootstrap_ci(values: np.ndarray, rng: np.random.Generator) -> tuple[float, float, float]:
    if values.size == 0:
        return math.nan, math.nan, math.nan
    samples = rng.choice(values, size=(BOOTSTRAPS, values.size), replace=True).sum(axis=1)
    return float(samples.mean()), float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975))


def bootstrap_ratio_ci(numerators: np.ndarray, denominators: np.ndarray, rng: np.random.Generator) -> tuple[float, float, float]:
    if numerators.size == 0 or float(denominators.sum()) == 0.0:
        return math.nan, math.nan, math.nan
    sample_idx = rng.integers(0, numerators.size, size=(BOOTSTRAPS, numerators.size))
    numerator_samples = numerators[sample_idx].sum(axis=1)
    denominator_samples = denominators[sample_idx].sum(axis=1)
    valid = denominator_samples > 0.0
    if not bool(valid.any()):
        return math.nan, math.nan, math.nan
    ratios = numerator_samples[valid] / denominator_samples[valid]
    return float(ratios.mean()), float(np.quantile(ratios, 0.025)), float(np.quantile(ratios, 0.975))


def summarize_margin(states: dict[str, ContractState], sample: str, margin: float, rng: np.random.Generator) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    returns = np.zeros(len(states), dtype=float)
    expected_returns = np.zeros(len(states), dtype=float)
    trade_flags = np.zeros(len(states), dtype=float)
    opportunity_seconds = np.zeros(len(states), dtype=float)
    opportunity_counts = np.zeros(len(states), dtype=float)
    for idx, state in enumerate(states.values()):
        episode_durations = opportunity_episodes_for_margin(state, margin)
        opportunity_seconds[idx] = float(sum(episode_durations))
        opportunity_counts[idx] = float(len(episode_durations))
        entry = first_entry_for_margin(state, margin)
        if entry is None:
            continue
        entries.append({**entry, "sample": sample})
        returns[idx] = float(entry["realized_profit"])
        expected_returns[idx] = float(entry["model_expected_profit"])
        trade_flags[idx] = 1.0

    boot_mean, boot_low, boot_high = bootstrap_ci(returns, rng)
    boot_trade_mean, boot_trade_low, boot_trade_high = bootstrap_ci(trade_flags, rng)
    boot_avg_duration_mean, boot_avg_duration_low, boot_avg_duration_high = bootstrap_ratio_ci(
        opportunity_seconds, opportunity_counts, rng
    )
    trades = len(entries)
    opportunity_count = int(opportunity_counts.sum())
    divergences = int(sum(entry["diverge"] for entry in entries))
    total_profit = float(returns.sum())
    total_model_expected_profit = float(expected_returns.sum())
    summary = {
        "sample": sample,
        "profit_margin": float(margin),
        "contracts": int(len(states)),
        "model_signal_contracts": int(sum(latch_interval(state) is not None for state in states.values())),
        "trades": int(trades),
        "trade_rate": float(trades / len(states)) if states else math.nan,
        "divergences": divergences,
        "divergence_rate": float(divergences / trades) if trades else math.nan,
        "mean_all_in_cost": float(np.mean([entry["all_in_cost"] for entry in entries])) if trades else math.nan,
        "mean_fee_adjusted_edge": float(np.mean([entry["fee_adjusted_edge"] for entry in entries])) if trades else math.nan,
        "mean_predicted_diverge_prob": float(np.mean([entry["predicted_diverge_prob"] for entry in entries])) if trades else math.nan,
        "mean_profit_per_trade": float(total_profit / trades) if trades else math.nan,
        "mean_profit_per_contract": float(total_profit / len(states)) if states else math.nan,
        "total_profit": total_profit,
        "total_model_expected_profit": total_model_expected_profit,
        "bootstrap_expected_total_profit": boot_mean,
        "bootstrap_total_profit_ci_low": boot_low,
        "bootstrap_total_profit_ci_high": boot_high,
        "bootstrap_expected_trades": boot_trade_mean,
        "bootstrap_trades_ci_low": boot_trade_low,
        "bootstrap_trades_ci_high": boot_trade_high,
        "arb_opportunities": opportunity_count,
        "total_arb_opportunity_seconds": float(opportunity_seconds.sum()),
        "avg_arb_opportunity_seconds": float(opportunity_seconds.sum() / opportunity_count) if opportunity_count else math.nan,
        "bootstrap_expected_avg_arb_opportunity_seconds": boot_avg_duration_mean,
        "bootstrap_avg_arb_opportunity_seconds_ci_low": boot_avg_duration_low,
        "bootstrap_avg_arb_opportunity_seconds_ci_high": boot_avg_duration_high,
    }
    return summary, entries


def run_sweep(states_by_sample: dict[str, dict[str, ContractState]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(BOOTSTRAP_STATE)
    summaries: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for sample, states in states_by_sample.items():
        for margin in MARGINS:
            summary, entries = summarize_margin(states, sample, float(margin), rng)
            summaries.append(summary)
            trades.extend(entries)
    return pd.DataFrame(summaries), pd.DataFrame(trades)


def best_rows(sweep: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for sample, group in sweep.groupby("sample", sort=False):
        if group.empty:
            continue
        best = group.sort_values(["total_profit", "mean_profit_per_trade", "profit_margin"], ascending=[False, False, True]).iloc[0]
        rows.append(best)
    return pd.DataFrame(rows)


def plot_sweep(sweep: pd.DataFrame) -> None:
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(11, 6))
    colors = {"calibration": "#2563eb", "test": "#dc2626"}
    for sample in ["calibration", "test"]:
        frame = sweep[sweep["sample"].eq(sample)].sort_values("profit_margin")
        if frame.empty:
            continue
        x = frame["profit_margin"].to_numpy()
        y = frame["bootstrap_expected_total_profit"].to_numpy()
        yerr = np.vstack(
            [
                y - frame["bootstrap_total_profit_ci_low"].to_numpy(),
                frame["bootstrap_total_profit_ci_high"].to_numpy() - y,
            ]
        )
        plt.errorbar(
            x,
            y,
            yerr=yerr,
            fmt="-o",
            markersize=3,
            linewidth=1.5,
            elinewidth=0.8,
            capsize=2,
            alpha=0.85,
            color=colors[sample],
            label=sample,
        )
    plt.axhline(0, color="#111827", linewidth=1)
    plt.xlabel("profit margin")
    plt.ylabel("Expected total profit per split, 1 contract per leg")
    plt.title("Latch 2m/1m expected profit by profit margin")
    plt.grid(True, alpha=0.25)
    plt.legend()
    plt.tight_layout()
    plt.savefig(OUT_PLOT, dpi=170)
    plt.close()

    plt.figure(figsize=(11, 6))
    for sample in ["calibration", "test"]:
        frame = sweep[sweep["sample"].eq(sample)].sort_values("profit_margin")
        if frame.empty:
            continue
        x = frame["profit_margin"].to_numpy()
        y = frame["bootstrap_expected_trades"].to_numpy()
        yerr = np.vstack(
            [
                y - frame["bootstrap_trades_ci_low"].to_numpy(),
                frame["bootstrap_trades_ci_high"].to_numpy() - y,
            ]
        )
        plt.errorbar(
            x,
            y,
            yerr=yerr,
            fmt="-o",
            markersize=3,
            linewidth=1.5,
            elinewidth=0.8,
            capsize=2,
            alpha=0.85,
            color=colors[sample],
            label=sample,
        )
    plt.xlabel("profit margin")
    plt.ylabel("Total trades per split")
    plt.title("Latch 2m/1m trade count by profit margin")
    plt.grid(True, alpha=0.25)
    plt.legend()
    plt.tight_layout()
    plt.savefig(OUT_TRADES_PLOT, dpi=170)
    plt.close()

    plt.figure(figsize=(11, 6))
    for sample in ["calibration", "test"]:
        frame = sweep[sweep["sample"].eq(sample)].sort_values("profit_margin")
        frame = frame.dropna(
            subset=[
                "bootstrap_expected_avg_arb_opportunity_seconds",
                "bootstrap_avg_arb_opportunity_seconds_ci_low",
                "bootstrap_avg_arb_opportunity_seconds_ci_high",
            ]
        )
        if frame.empty:
            continue
        x = frame["profit_margin"].to_numpy()
        y = frame["bootstrap_expected_avg_arb_opportunity_seconds"].to_numpy()
        low = frame["bootstrap_avg_arb_opportunity_seconds_ci_low"].to_numpy()
        high = frame["bootstrap_avg_arb_opportunity_seconds_ci_high"].to_numpy()
        yerr = np.vstack(
            [
                np.maximum(0.0, y - low),
                np.maximum(0.0, high - y),
            ]
        )
        plt.errorbar(
            x,
            y,
            yerr=yerr,
            fmt="-o",
            markersize=3,
            linewidth=1.5,
            elinewidth=0.8,
            capsize=2,
            alpha=0.85,
            color=colors[sample],
            label=sample,
        )
    plt.xlabel("profit margin")
    plt.ylabel("Average qualifying arb duration per opportunity, seconds")
    plt.title("Latch 2m/1m average arbitrage opportunity duration by profit margin")
    duration_high = sweep[
        sweep["sample"].isin(["calibration", "test"])
    ]["bootstrap_avg_arb_opportunity_seconds_ci_high"].max()
    if pd.notna(duration_high):
        upper = max(10.0, math.ceil((float(duration_high) * 1.15) / 5.0) * 5.0)
        plt.ylim(0, upper)
    plt.grid(True, alpha=0.25)
    plt.legend()
    plt.tight_layout()
    plt.savefig(OUT_DURATION_PLOT, dpi=170)
    plt.close()


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
    plot_sweep(sweep)

    best_calib = best[best["sample"].eq("calibration")].iloc[0]
    test_at_calib_margin = sweep[
        sweep["sample"].eq("test") & sweep["profit_margin"].eq(float(best_calib["profit_margin"]))
    ].iloc[0]
    best_test = best[best["sample"].eq("test")].iloc[0]
    display_best = best[
        [
            "sample",
            "profit_margin",
            "contracts",
            "model_signal_contracts",
            "trades",
            "trade_rate",
            "divergences",
            "divergence_rate",
            "mean_all_in_cost",
            "mean_fee_adjusted_edge",
            "mean_profit_per_trade",
            "total_profit",
            "bootstrap_total_profit_ci_low",
            "bootstrap_total_profit_ci_high",
            "total_model_expected_profit",
        ]
    ].copy()
    near_calib = sweep[
        sweep["sample"].eq("calibration")
        & sweep["profit_margin"].between(max(0.0, float(best_calib["profit_margin"]) - 0.05), float(best_calib["profit_margin"]) + 0.05)
    ][
        [
            "profit_margin",
            "trades",
            "divergences",
            "mean_all_in_cost",
            "mean_fee_adjusted_edge",
            "mean_profit_per_trade",
            "total_profit",
            "bootstrap_total_profit_ci_low",
            "bootstrap_total_profit_ci_high",
        ]
    ].copy()
    test_near_calib = sweep[
        sweep["sample"].eq("test")
        & sweep["profit_margin"].between(max(0.0, float(best_calib["profit_margin"]) - 0.05), float(best_calib["profit_margin"]) + 0.05)
    ][
        [
            "profit_margin",
            "trades",
            "divergences",
            "mean_all_in_cost",
            "mean_fee_adjusted_edge",
            "mean_profit_per_trade",
            "total_profit",
            "bootstrap_total_profit_ci_low",
            "bootstrap_total_profit_ci_high",
        ]
    ].copy()

    report = [
        "# Profit Margin Sweep For `latch_2m_1m`",
        "",
        "## Scope",
        "",
        "- Strategy: current latch-hold entry logic using only `2m` and `1m` as latch candidates.",
        f"- Saved model thresholds: `2m={thresholds['2m']:.4f}`, `1m={thresholds['1m']:.4f}`.",
        "- Entry rule: after the first passing latch model, enter at the first historical row where `all_in_cost < 1 - profit_margin`.",
        "- Profit margins swept from `$0.00` through `$0.99` in `$0.01` increments.",
        "- Fees use the current odds-dependent equations from `cli_trader_v2.py`: `0.07*p*(1-p)` on Kalshi and `0.05*p*(1-p)` on Polymarket, with `N=1` per leg.",
        "- Return rule: if the platforms agree, profit is `1 - all_in_cost`; if they diverge, the full stake is lost and profit is `-all_in_cost`.",
        f"- Arbitrage duration is measured after the latch point as average continuous opportunity length. One opportunity is a contiguous sampled run where `all_in_cost < 1 - profit_margin`; each row contributes time until the next snapshot, clipped at `{MAX_OPPORTUNITY_INTERVAL_SECONDS:.0f}` seconds to avoid overstating stale data gaps.",
        "- Historical CSVs do not contain reliable ask-side liquidity for every row, so this is a price-and-outcome backtest, not a live fill simulator.",
        "",
        "## Recommendation",
        "",
        f"The calibration-optimal profit margin is `${float(best_calib['profit_margin']):.2f}`. On calibration it produced `{int(best_calib['trades'])}` trades, total profit `{best_calib['total_profit']:.4f}`, and a 95% bootstrap interval of `[{best_calib['bootstrap_total_profit_ci_low']:.4f}, {best_calib['bootstrap_total_profit_ci_high']:.4f}]`.",
        "",
        f"Applied unchanged to the held-out test split, `${float(best_calib['profit_margin']):.2f}` produced `{int(test_at_calib_margin['trades'])}` trades, total profit `{test_at_calib_margin['total_profit']:.4f}`, and a 95% bootstrap interval of `[{test_at_calib_margin['bootstrap_total_profit_ci_low']:.4f}, {test_at_calib_margin['bootstrap_total_profit_ci_high']:.4f}]`.",
        "",
        f"For reference only, the test-set best margin is `${float(best_test['profit_margin']):.2f}` with total profit `{best_test['total_profit']:.4f}`. That value is not the recommended parameter because it is selected on the held-out test split.",
        "",
        "## Best Margin By Split",
        "",
        md_table(display_best),
        "",
        "## Calibration Margins Near The Optimum",
        "",
        md_table(near_calib),
        "",
        "## Test Margins Near The Calibration Optimum",
        "",
        md_table(test_near_calib),
        "",
        "## Plot",
        "",
        f"![Expected profit vs profit margin]({OUT_PLOT.relative_to(HORIZON_DIR)})",
        "",
        f"![Total trades vs profit margin]({OUT_TRADES_PLOT.relative_to(HORIZON_DIR)})",
        "",
        f"![Average arbitrage duration vs profit margin]({OUT_DURATION_PLOT.relative_to(HORIZON_DIR)})",
        "",
        "The error bars are contract-level bootstrap 95% intervals over each split. Contracts with no entry at a given margin contribute zero profit and zero trades in that bootstrap. For duration, each bootstrap resamples contracts and plots total qualifying seconds divided by the number of continuous qualifying opportunities.",
        "",
        "## Output Files",
        "",
        f"- Sweep table: `{OUT_SWEEP.relative_to(ROOT)}`",
        f"- Trade table: `{OUT_TRADES.relative_to(ROOT)}`",
        f"- Plot: `{OUT_PLOT.relative_to(ROOT)}`",
        f"- Trades plot: `{OUT_TRADES_PLOT.relative_to(ROOT)}`",
        f"- Average arbitrage duration plot: `{OUT_DURATION_PLOT.relative_to(ROOT)}`",
        "",
        "## Interpretation",
        "",
        "A higher margin delays entry until the edge is larger, so per-trade profit rises while the number of entries falls. The selected margin is the point where the larger edge per trade outweighed the lost trade count on the calibration split.",
    ]
    OUT_REPORT.write_text("\n".join(report) + "\n")
    print(f"Wrote {OUT_REPORT}")
    print(f"Wrote {OUT_SWEEP}")
    print(f"Wrote {OUT_TRADES}")
    print(f"Wrote {OUT_PLOT}")
    print(f"Wrote {OUT_TRADES_PLOT}")
    print(f"Wrote {OUT_DURATION_PLOT}")


if __name__ == "__main__":
    main()
