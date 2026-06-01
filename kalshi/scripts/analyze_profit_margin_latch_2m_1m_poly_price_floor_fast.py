#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.analyze_profit_margin_latch_2m_1m import (  # noqa: E402
    BOOTSTRAP_STATE,
    CONTRACTS_PER_LEG,
    DATA_DIR,
    HORIZON_DIR,
    KALSHI_FEE_RATE,
    MARGINS,
    PLOT_DIR,
    POLYMARKET_FEE_RATE,
    fee,
    load_predictions,
    md_table,
    split_contracts,
)


BOOTSTRAPS = 500
POLY_PRICE_FLOORS = [("none", 0.0), ("25c", 0.25), ("33c", 0.33), ("50c", 0.50)]

OUT_SWEEP = HORIZON_DIR / "profit_margin_latch_2m_1m_poly_price_floor_sweep.csv"
OUT_TRADES = HORIZON_DIR / "profit_margin_latch_2m_1m_poly_price_floor_trades.csv"
OUT_REPORT = HORIZON_DIR / "profit_margin_latch_2m_1m_poly_price_floor_report.md"
OUT_PROFIT_PLOT = PLOT_DIR / "profit_margin_latch_2m_1m_poly_price_floor_expected_profit.png"
OUT_TRADES_PLOT = PLOT_DIR / "profit_margin_latch_2m_1m_poly_price_floor_total_trades.png"


def bootstrap_sum_ci(values: np.ndarray, rng: np.random.Generator) -> tuple[float, float, float]:
    if values.size == 0:
        return math.nan, math.nan, math.nan
    samples = rng.choice(values, size=(BOOTSTRAPS, values.size), replace=True).sum(axis=1)
    return float(samples.mean()), float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975))


def build_latch_table(predictions: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for contract_id, group in predictions.groupby("contract_id", sort=False):
        by_horizon = group.set_index("horizon")
        if "2m" not in by_horizon.index or "1m" not in by_horizon.index:
            continue
        if bool(by_horizon.at["2m", "model_pass"]):
            latch_horizon = "2m"
        elif bool(by_horizon.at["1m", "model_pass"]):
            latch_horizon = "1m"
        else:
            latch_horizon = ""
        rows.append(
            {
                "contract_id": str(contract_id),
                "source_file": str(by_horizon["source_file"].dropna().iloc[0]),
                "diverge": int(by_horizon["diverge"].iloc[0]),
                "latch_horizon": latch_horizon,
                "latch_time": pd.Timestamp(by_horizon.at[latch_horizon, "asof_time"]) if latch_horizon else pd.NaT,
                "predicted_diverge_prob": float(by_horizon.at[latch_horizon, "predicted_diverge_prob"]) if latch_horizon else math.nan,
            }
        )
    return pd.DataFrame(rows)


def read_directional_candidates(latch_row: pd.Series) -> pd.DataFrame:
    if not latch_row["latch_horizon"]:
        return pd.DataFrame()
    path = DATA_DIR / str(latch_row["source_file"])
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
    raw = raw[
        (raw["timestamp"] >= latch_row["latch_time"])
        & (raw["timestamp"] <= raw["close_time"])
    ].copy()
    if raw.empty:
        return pd.DataFrame()

    k_yes = pd.DataFrame(
        {
            "contract_id": latch_row["contract_id"],
            "source_file": latch_row["source_file"],
            "timestamp": raw["timestamp"],
            "direction": "K+NP",
            "all_in_cost": raw["kalshi_yes_ask"]
            + raw["polymarket_no_ask"]
            + fee(raw["kalshi_yes_ask"], KALSHI_FEE_RATE)
            + fee(raw["polymarket_no_ask"], POLYMARKET_FEE_RATE),
            "polymarket_price": raw["polymarket_no_ask"],
            "diverge": int(latch_row["diverge"]),
            "latch_horizon": latch_row["latch_horizon"],
            "predicted_diverge_prob": float(latch_row["predicted_diverge_prob"]),
        }
    )
    k_no = pd.DataFrame(
        {
            "contract_id": latch_row["contract_id"],
            "source_file": latch_row["source_file"],
            "timestamp": raw["timestamp"],
            "direction": "NK+P",
            "all_in_cost": raw["kalshi_no_ask"]
            + raw["polymarket_yes_ask"]
            + fee(raw["kalshi_no_ask"], KALSHI_FEE_RATE)
            + fee(raw["polymarket_yes_ask"], POLYMARKET_FEE_RATE),
            "polymarket_price": raw["polymarket_yes_ask"],
            "diverge": int(latch_row["diverge"]),
            "latch_horizon": latch_row["latch_horizon"],
            "predicted_diverge_prob": float(latch_row["predicted_diverge_prob"]),
        }
    )
    out = pd.concat([k_yes, k_no], ignore_index=True)
    out = out.dropna(subset=["timestamp", "all_in_cost", "polymarket_price"])
    out["fee_adjusted_edge"] = 1.0 - out["all_in_cost"]
    return out


def build_candidate_table(latches: pd.DataFrame) -> pd.DataFrame:
    frames = [read_directional_candidates(row) for _idx, row in latches[latches["latch_horizon"].ne("")].iterrows()]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def first_entries(candidates: pd.DataFrame, margin: float, floor: float) -> pd.DataFrame:
    eligible = candidates[
        (candidates["fee_adjusted_edge"] > margin)
        & (candidates["polymarket_price"] > floor)
    ]
    if eligible.empty:
        return eligible
    return (
        eligible.sort_values(["contract_id", "timestamp", "all_in_cost"])
        .groupby("contract_id", as_index=False)
        .first()
    )


def summarize(
    candidates: pd.DataFrame,
    latches: pd.DataFrame,
    contract_sets: dict[str, set[str]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(BOOTSTRAP_STATE)
    summaries: list[dict[str, Any]] = []
    trades: list[pd.DataFrame] = []
    latches_by_contract = latches.set_index("contract_id")

    for sample, contract_set in contract_sets.items():
        sample_contracts = sorted(str(contract_id) for contract_id in contract_set if str(contract_id) in latches_by_contract.index)
        sample_candidates = candidates[candidates["contract_id"].isin(sample_contracts)]
        model_signal_contracts = int(latches_by_contract.loc[sample_contracts, "latch_horizon"].ne("").sum())
        for floor_label, floor in POLY_PRICE_FLOORS:
            for margin in MARGINS:
                entries = first_entries(sample_candidates, float(margin), floor)
                returns = pd.Series(0.0, index=sample_contracts)
                expected_returns = pd.Series(0.0, index=sample_contracts)
                if not entries.empty:
                    realized = np.where(entries["diverge"].astype(int).to_numpy() == 1, 0.0, 1.0) - entries["all_in_cost"].to_numpy(dtype=float)
                    expected = 1.0 - entries["all_in_cost"].to_numpy(dtype=float) - entries["predicted_diverge_prob"].to_numpy(dtype=float)
                    returns.loc[entries["contract_id"].astype(str).to_numpy()] = realized
                    expected_returns.loc[entries["contract_id"].astype(str).to_numpy()] = expected
                    trades.append(entries.assign(sample=sample, floor_label=floor_label, profit_margin=float(margin)))

                values = returns.to_numpy(dtype=float)
                boot_mean, boot_low, boot_high = bootstrap_sum_ci(values, rng)
                trade_flags = (returns != 0.0).astype(float).to_numpy()
                boot_trade_mean, boot_trade_low, boot_trade_high = bootstrap_sum_ci(trade_flags, rng)
                trade_count = int(len(entries))
                divergences = int(entries["diverge"].sum()) if trade_count else 0
                total_profit = float(values.sum())
                summaries.append(
                    {
                        "sample": sample,
                        "floor_label": floor_label,
                        "polymarket_price_floor": float(floor),
                        "profit_margin": float(margin),
                        "profit_margin_cents": int(round(float(margin) * 100)),
                        "contracts": int(len(sample_contracts)),
                        "model_signal_contracts": model_signal_contracts,
                        "trades": trade_count,
                        "trade_rate": trade_count / len(sample_contracts) if sample_contracts else math.nan,
                        "divergences": divergences,
                        "divergence_rate": divergences / trade_count if trade_count else math.nan,
                        "mean_polymarket_price": float(entries["polymarket_price"].mean()) if trade_count else math.nan,
                        "mean_all_in_cost": float(entries["all_in_cost"].mean()) if trade_count else math.nan,
                        "mean_fee_adjusted_edge": float(entries["fee_adjusted_edge"].mean()) if trade_count else math.nan,
                        "mean_profit_per_trade": total_profit / trade_count if trade_count else math.nan,
                        "mean_profit_per_contract": total_profit / len(sample_contracts) if sample_contracts else math.nan,
                        "total_profit": total_profit,
                        "total_model_expected_profit": float(expected_returns.sum()),
                        "bootstrap_expected_total_profit": boot_mean,
                        "bootstrap_total_profit_ci_low": boot_low,
                        "bootstrap_total_profit_ci_high": boot_high,
                        "bootstrap_expected_trades": boot_trade_mean,
                        "bootstrap_trades_ci_low": boot_trade_low,
                        "bootstrap_trades_ci_high": boot_trade_high,
                    }
                )
    trade_table = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    return pd.DataFrame(summaries), trade_table


def best_rows(sweep: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (_sample, _floor_label), group in sweep.groupby(["sample", "floor_label"], sort=False):
        best = group.sort_values(
            ["total_profit", "mean_profit_per_trade", "profit_margin"],
            ascending=[False, False, True],
        ).iloc[0]
        rows.append(best)
    return pd.DataFrame(rows)


def plot_metric(sweep: pd.DataFrame, metric: str, low_col: str, high_col: str, ylabel: str, title: str, out_path: Path) -> None:
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    colors = {"none": "#111827", "25c": "#2563eb", "33c": "#16a34a", "50c": "#dc2626"}
    fig, axes = plt.subplots(1, 2, figsize=(14, 5), sharex=True)
    for ax, sample in zip(axes, ["calibration", "test"], strict=True):
        for floor_label, _floor in POLY_PRICE_FLOORS:
            frame = sweep[sweep["sample"].eq(sample) & sweep["floor_label"].eq(floor_label)].sort_values("profit_margin")
            x = frame["profit_margin"].to_numpy()
            y = frame[metric].to_numpy()
            ax.plot(x, y, "-o", markersize=2.5, linewidth=1.4, color=colors[floor_label], label=floor_label)
            ax.fill_between(x, frame[low_col].to_numpy(), frame[high_col].to_numpy(), color=colors[floor_label], alpha=0.09, linewidth=0)
        ax.set_title(sample)
        ax.set_xlabel("profit margin")
        ax.grid(True, alpha=0.25)
    axes[0].set_ylabel(ylabel)
    axes[1].legend(title="PM price floor")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def main() -> None:
    labels = pd.read_csv(HORIZON_DIR / "horizon_contract_labels.csv")
    contract_sets = split_contracts(labels)
    predictions, thresholds = load_predictions()
    latches = build_latch_table(predictions)
    candidates = build_candidate_table(latches)
    sweep, trades = summarize(candidates, latches, contract_sets)
    best = best_rows(sweep)

    OUT_SWEEP.parent.mkdir(parents=True, exist_ok=True)
    sweep.to_csv(OUT_SWEEP, index=False)
    trades.to_csv(OUT_TRADES, index=False)
    plot_metric(
        sweep,
        "bootstrap_expected_total_profit",
        "bootstrap_total_profit_ci_low",
        "bootstrap_total_profit_ci_high",
        "Expected total profit",
        "Latch 2m/1m expected profit by profit margin and Polymarket price floor",
        OUT_PROFIT_PLOT,
    )
    plot_metric(
        sweep,
        "bootstrap_expected_trades",
        "bootstrap_trades_ci_low",
        "bootstrap_trades_ci_high",
        "Total trades",
        "Latch 2m/1m trade count by profit margin and Polymarket price floor",
        OUT_TRADES_PLOT,
    )

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
    test_columns = [col for col in report_columns if col != "sample"]
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
        "- This simulation is direction-aware: a row can trade if either `K+NP` or `NK+P` has complete prices and passes the filters.",
        "- Return rule: if the platforms agree, profit is `1 - all_in_cost`; if they diverge, the full stake is lost and profit is `-all_in_cost`.",
        f"- Bootstrap intervals use `{BOOTSTRAPS}` contract-level resamples.",
        "",
        "## Calibration-Optimal Margins",
        "",
        md_table(calibration_best[report_columns].sort_values("polymarket_price_floor")),
        "",
        "## Held-Out Test At Calibration-Optimal Margins",
        "",
        md_table(test_at_calibration_best_df[test_columns].sort_values("polymarket_price_floor")),
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
