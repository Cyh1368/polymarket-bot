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
BOOTSTRAP_STATE = 20260601
BOOTSTRAPS = 2000
HORIZONS = ["2m", "1m"]
PROFIT_MARGIN = 0.18
KALSHI_FEE_RATE = 0.07
POLYMARKET_FEE_RATE = 0.05
CONTRACTS_PER_LEG = 1.0
THRESHOLD_GRID = np.round(np.arange(0.0, 0.3001, 0.005), 6)

OUT_SCAN = HORIZON_DIR / "latch_2m_1m_threshold_pair_scan_pm018.csv"
OUT_SELECTED = HORIZON_DIR / "latch_2m_1m_threshold_pair_selected_pm018.csv"
OUT_TRADES = HORIZON_DIR / "latch_2m_1m_threshold_pair_trades_pm018.csv"
OUT_REPORT = HORIZON_DIR / "latch_2m_1m_threshold_pair_report_pm018.md"
OUT_CALIB_HEATMAP = PLOT_DIR / "latch_2m_1m_threshold_pair_calibration_profit_pm018.png"
OUT_TEST_HEATMAP = PLOT_DIR / "latch_2m_1m_threshold_pair_test_profit_pm018.png"
OUT_TRADE_HEATMAP = PLOT_DIR / "latch_2m_1m_threshold_pair_test_trades_pm018.png"


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
    eligible = labels[labels["training_eligible"].astype(bool)].dropna(subset=["diverge"]).copy()
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


def saved_thresholds() -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for horizon in HORIZONS:
        with (HORIZON_DIR / f"divergence_horizon_{horizon}_metadata.json").open() as handle:
            metadata = json.load(handle)
        thresholds[horizon] = float(metadata["metrics"]["recommended_trade_threshold"])
    return thresholds


def threshold_values() -> np.ndarray:
    saved = list(saved_thresholds().values())
    return np.array(sorted(set(float(x) for x in [*THRESHOLD_GRID, *saved])), dtype=float)


def load_predictions() -> pd.DataFrame:
    dataset = pd.read_csv(HORIZON_DIR / "horizon_aggregated_dataset.csv")
    frames: list[pd.DataFrame] = []
    for horizon in HORIZONS:
        model = joblib.load(HORIZON_DIR / f"divergence_horizon_{horizon}_model.pkl")
        with (HORIZON_DIR / f"divergence_horizon_{horizon}_feature_list.json").open() as handle:
            features = json.load(handle)
        frame = dataset[
            (dataset["horizon"].eq(horizon))
            & dataset["training_eligible_label"].astype(bool)
            & dataset["aggregation_status"].eq("ok")
            & dataset["diverge"].notna()
        ].copy()
        frame["predicted_diverge_prob"] = model.predict_proba(frame[features])[:, 1]
        frames.append(
            frame[
                [
                    "contract_id",
                    "source_file",
                    "horizon",
                    "asof_time",
                    "diverge",
                    "predicted_diverge_prob",
                ]
            ]
        )
    predictions = pd.concat(frames, ignore_index=True)
    predictions["contract_id"] = predictions["contract_id"].astype(str)
    predictions["asof_time"] = pd.to_datetime(predictions["asof_time"], utc=True, errors="coerce")
    predictions["diverge"] = predictions["diverge"].astype(int)
    return predictions


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
    return out.sort_values("timestamp").reset_index(drop=True)


@dataclass(frozen=True)
class EntryCandidate:
    entry_time: pd.Timestamp
    direction: str
    all_in_cost: float
    realized_profit: float
    model_expected_profit: float


@dataclass(frozen=True)
class ContractState:
    contract_id: str
    source_file: str
    diverge: int
    close_time: pd.Timestamp
    asof: dict[str, pd.Timestamp]
    prob: dict[str, float]
    entry: dict[str, EntryCandidate | None]


def first_entry(
    opportunities: pd.DataFrame,
    start: pd.Timestamp,
    close_time: pd.Timestamp,
    diverge: int,
    prob: float,
) -> EntryCandidate | None:
    rows = opportunities[
        (opportunities["timestamp"] >= start)
        & (opportunities["timestamp"] <= close_time)
        & (opportunities["best_all_in_cost"] < 1.0 - PROFIT_MARGIN)
    ]
    if rows.empty:
        return None
    row = rows.iloc[0]
    all_in_cost = float(row["best_all_in_cost"])
    return EntryCandidate(
        entry_time=pd.Timestamp(row["timestamp"]),
        direction=str(row["best_direction"]),
        all_in_cost=all_in_cost,
        realized_profit=(0.0 if diverge else 1.0) - all_in_cost,
        model_expected_profit=1.0 - all_in_cost - prob,
    )


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
        close_time = pd.Timestamp(opportunities["close_time"].dropna().iloc[0])
        diverge = int(by_horizon["diverge"].iloc[0])
        asof = {h: pd.Timestamp(by_horizon.at[h, "asof_time"]) for h in HORIZONS}
        prob = {h: float(by_horizon.at[h, "predicted_diverge_prob"]) for h in HORIZONS}
        entries = {
            h: first_entry(opportunities, asof[h], close_time, diverge, prob[h])
            for h in HORIZONS
        }
        states[str(contract_id)] = ContractState(
            contract_id=str(contract_id),
            source_file=source_file,
            diverge=diverge,
            close_time=close_time,
            asof=asof,
            prob=prob,
            entry=entries,
        )
    return states


def selected_entry(state: ContractState, threshold_2m: float, threshold_1m: float) -> tuple[str, EntryCandidate] | None:
    if state.prob["2m"] < threshold_2m:
        entry = state.entry["2m"]
        return ("2m", entry) if entry is not None else None
    if state.prob["1m"] < threshold_1m:
        entry = state.entry["1m"]
        return ("1m", entry) if entry is not None else None
    return None


def summarize_pair(
    states: dict[str, ContractState],
    sample: str,
    threshold_2m: float,
    threshold_1m: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    model_signal_contracts = 0
    for state in states.values():
        if state.prob["2m"] < threshold_2m or state.prob["1m"] < threshold_1m:
            model_signal_contracts += 1
        selected = selected_entry(state, threshold_2m, threshold_1m)
        if selected is None:
            continue
        decision_horizon, entry = selected
        trades.append(
            {
                "sample": sample,
                "contract_id": state.contract_id,
                "source_file": state.source_file,
                "threshold_2m": float(threshold_2m),
                "threshold_1m": float(threshold_1m),
                "decision_horizon": decision_horizon,
                "entry_time": entry.entry_time,
                "direction": entry.direction,
                "all_in_cost": entry.all_in_cost,
                "fee_adjusted_edge": 1.0 - entry.all_in_cost,
                "predicted_diverge_prob": state.prob[decision_horizon],
                "diverge": state.diverge,
                "realized_profit": entry.realized_profit,
                "model_expected_profit": entry.model_expected_profit,
            }
        )

    returns = np.array([trade["realized_profit"] for trade in trades], dtype=float)
    divergences = int(sum(trade["diverge"] for trade in trades))
    total_profit = float(returns.sum()) if returns.size else 0.0
    summary = {
        "sample": sample,
        "threshold_2m": float(threshold_2m),
        "threshold_1m": float(threshold_1m),
        "profit_margin": PROFIT_MARGIN,
        "contracts": int(len(states)),
        "model_signal_contracts": int(model_signal_contracts),
        "trades": int(len(trades)),
        "trade_rate": float(len(trades) / len(states)) if states else math.nan,
        "divergences": divergences,
        "divergence_rate": float(divergences / len(trades)) if trades else math.nan,
        "mean_all_in_cost": float(np.mean([trade["all_in_cost"] for trade in trades])) if trades else math.nan,
        "mean_fee_adjusted_edge": float(np.mean([trade["fee_adjusted_edge"] for trade in trades])) if trades else math.nan,
        "mean_predicted_diverge_prob": float(np.mean([trade["predicted_diverge_prob"] for trade in trades])) if trades else math.nan,
        "mean_profit_per_trade": float(total_profit / len(trades)) if trades else math.nan,
        "mean_profit_per_contract": float(total_profit / len(states)) if states else math.nan,
        "total_profit": total_profit,
        "total_model_expected_profit": float(sum(trade["model_expected_profit"] for trade in trades)),
        "trades_from_2m": int(sum(trade["decision_horizon"] == "2m" for trade in trades)),
        "trades_from_1m": int(sum(trade["decision_horizon"] == "1m" for trade in trades)),
    }
    return summary, trades


def bootstrap_selected(
    states: dict[str, ContractState],
    threshold_2m: float,
    threshold_1m: float,
    rng: np.random.Generator,
) -> dict[str, float]:
    rows = []
    state_list = list(states.values())
    for state in state_list:
        selected = selected_entry(state, threshold_2m, threshold_1m)
        rows.append(float(selected[1].realized_profit) if selected is not None else 0.0)
    values = np.asarray(rows, dtype=float)
    if values.size == 0:
        return {
            "bootstrap_expected_total_profit": math.nan,
            "bootstrap_total_profit_ci_low": math.nan,
            "bootstrap_total_profit_ci_high": math.nan,
        }
    samples = rng.choice(values, size=(BOOTSTRAPS, values.size), replace=True).sum(axis=1)
    return {
        "bootstrap_expected_total_profit": float(samples.mean()),
        "bootstrap_total_profit_ci_low": float(np.quantile(samples, 0.025)),
        "bootstrap_total_profit_ci_high": float(np.quantile(samples, 0.975)),
    }


def run_scan(states_by_sample: dict[str, dict[str, ContractState]], thresholds: np.ndarray) -> tuple[pd.DataFrame, pd.DataFrame]:
    summaries: list[dict[str, Any]] = []
    selected_trades: list[dict[str, Any]] = []
    for sample, states in states_by_sample.items():
        for threshold_2m in thresholds:
            for threshold_1m in thresholds:
                summary, trades = summarize_pair(states, sample, float(threshold_2m), float(threshold_1m))
                summaries.append(summary)
                if sample in {"calibration", "test"}:
                    selected_trades.extend(trades)
    return pd.DataFrame(summaries), pd.DataFrame(selected_trades)


def best_pair(scan: pd.DataFrame, sample: str) -> pd.Series:
    frame = scan[scan["sample"].eq(sample)].copy()
    return frame.sort_values(
        ["total_profit", "mean_profit_per_trade", "trades", "threshold_2m", "threshold_1m"],
        ascending=[False, False, False, True, True],
    ).iloc[0]


def matching_row(scan: pd.DataFrame, sample: str, threshold_2m: float, threshold_1m: float) -> pd.Series:
    frame = scan[
        scan["sample"].eq(sample)
        & np.isclose(scan["threshold_2m"], threshold_2m)
        & np.isclose(scan["threshold_1m"], threshold_1m)
    ]
    if frame.empty:
        raise ValueError(f"Missing row for {sample} threshold pair {threshold_2m}, {threshold_1m}")
    return frame.iloc[0]


def plot_heatmap(scan: pd.DataFrame, sample: str, value: str, path: Path, title: str, cmap: str = "viridis") -> None:
    frame = scan[scan["sample"].eq(sample)].copy()
    pivot = frame.pivot(index="threshold_1m", columns="threshold_2m", values=value).sort_index().sort_index(axis=1)
    plt.figure(figsize=(10, 8))
    image = plt.imshow(pivot.to_numpy(), origin="lower", aspect="auto", cmap=cmap)
    plt.colorbar(image, label=value)
    x_values = pivot.columns.to_numpy(dtype=float)
    y_values = pivot.index.to_numpy(dtype=float)
    x_ticks = np.linspace(0, len(x_values) - 1, 7, dtype=int)
    y_ticks = np.linspace(0, len(y_values) - 1, 7, dtype=int)
    plt.xticks(x_ticks, [f"{x_values[i]:.3f}" for i in x_ticks], rotation=45)
    plt.yticks(y_ticks, [f"{y_values[i]:.3f}" for i in y_ticks])
    plt.xlabel("2m threshold")
    plt.ylabel("1m threshold")
    plt.title(title)
    plt.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path, dpi=170)
    plt.close()


def main() -> None:
    labels = pd.read_csv(HORIZON_DIR / "horizon_contract_labels.csv")
    contract_sets = split_contracts(labels)
    predictions = load_predictions()
    states_by_sample = {name: build_states(predictions, ids) for name, ids in contract_sets.items()}
    thresholds = threshold_values()
    scan, selected_trades = run_scan(states_by_sample, thresholds)

    saved = saved_thresholds()
    best_calib = best_pair(scan, "calibration")
    best_test = best_pair(scan, "test")
    saved_calib = matching_row(scan, "calibration", saved["2m"], saved["1m"])
    saved_test = matching_row(scan, "test", saved["2m"], saved["1m"])
    test_at_calib = matching_row(scan, "test", float(best_calib["threshold_2m"]), float(best_calib["threshold_1m"]))
    all_at_calib = matching_row(scan, "all", float(best_calib["threshold_2m"]), float(best_calib["threshold_1m"]))

    rng = np.random.default_rng(BOOTSTRAP_STATE)
    selected_rows = []
    selected_specs = [
        ("saved_thresholds_calibration", "calibration", saved_calib),
        ("saved_thresholds_test", "test", saved_test),
        ("calibration_optimal_calibration", "calibration", best_calib),
        ("calibration_optimal_test", "test", test_at_calib),
        ("calibration_optimal_all", "all", all_at_calib),
        ("test_optimal_test_reference", "test", best_test),
    ]
    for label, sample, row in selected_specs:
        out = row.to_dict()
        out["selection"] = label
        out.update(
            bootstrap_selected(
                states_by_sample[sample],
                float(row["threshold_2m"]),
                float(row["threshold_1m"]),
                rng,
            )
        )
        selected_rows.append(out)
    selected = pd.DataFrame(selected_rows)

    selected_trade_rows: list[dict[str, Any]] = []
    for label, sample, row in selected_specs:
        _summary, trades = summarize_pair(
            states_by_sample[sample],
            sample,
            float(row["threshold_2m"]),
            float(row["threshold_1m"]),
        )
        selected_trade_rows.extend([{**trade, "selection": label} for trade in trades])
    selected_trade_df = pd.DataFrame(selected_trade_rows)

    OUT_SCAN.parent.mkdir(parents=True, exist_ok=True)
    scan.to_csv(OUT_SCAN, index=False)
    selected.to_csv(OUT_SELECTED, index=False)
    selected_trade_df.to_csv(OUT_TRADES, index=False)

    plot_heatmap(scan, "calibration", "total_profit", OUT_CALIB_HEATMAP, "Calibration total profit by 2m/1m threshold pair")
    plot_heatmap(scan, "test", "total_profit", OUT_TEST_HEATMAP, "Test total profit by 2m/1m threshold pair")
    plot_heatmap(scan, "test", "trades", OUT_TRADE_HEATMAP, "Test trades by 2m/1m threshold pair", cmap="magma")

    top_calib = scan[scan["sample"].eq("calibration")].sort_values(
        ["total_profit", "mean_profit_per_trade", "trades"],
        ascending=[False, False, False],
    ).head(12)
    top_test = scan[scan["sample"].eq("test")].sort_values(
        ["total_profit", "mean_profit_per_trade", "trades"],
        ascending=[False, False, False],
    ).head(12)

    display_cols = [
        "selection",
        "sample",
        "threshold_2m",
        "threshold_1m",
        "contracts",
        "model_signal_contracts",
        "trades",
        "trades_from_2m",
        "trades_from_1m",
        "divergences",
        "divergence_rate",
        "mean_all_in_cost",
        "mean_fee_adjusted_edge",
        "mean_predicted_diverge_prob",
        "mean_profit_per_trade",
        "total_profit",
        "bootstrap_total_profit_ci_low",
        "bootstrap_total_profit_ci_high",
    ]

    report = [
        "# Latch 2m/1m Threshold Pair Search At Profit Margin 18c",
        "",
        "## Scope",
        "",
        "- Strategy: `latch_2m_1m`, hold to settlement once entered.",
        f"- Profit margin fixed at `${PROFIT_MARGIN:.2f}`; an opportunity qualifies when `best_all_in_cost < {1.0 - PROFIT_MARGIN:.2f}`.",
        "- Fees use the odds-dependent equations from `cli_trader_v2.py`: `0.07*p*(1-p)` on Kalshi and `0.05*p*(1-p)` on Polymarket, with `N=1` per leg.",
        "- If the 2m model passes, the contract latches at 2m and the 1m model does not revoke that permission. If the 2m model fails, the 1m model can still latch at 1m.",
        "- Return rule: agreement pays `1.0 - all_in_cost`; divergence loses the stake and returns `-all_in_cost`.",
        "- Thresholds were scanned from `0.000` through `0.300` in `0.005` increments, plus the saved per-horizon thresholds exactly.",
        "- The recommended pair is selected on calibration only and then evaluated unchanged on the final test split.",
        "",
        "## Recommendation",
        "",
        f"The calibration-optimal pair is `2m < {float(best_calib['threshold_2m']):.4f}` and `1m < {float(best_calib['threshold_1m']):.4f}`.",
        f"On calibration this pair produced `{int(best_calib['trades'])}` trades, total profit `{best_calib['total_profit']:.4f}`, and mean profit per trade `{best_calib['mean_profit_per_trade']:.4f}`.",
        f"Applied unchanged to test, it produced `{int(test_at_calib['trades'])}` trades, total profit `{test_at_calib['total_profit']:.4f}`, mean profit per trade `{test_at_calib['mean_profit_per_trade']:.4f}`, and divergence rate `{test_at_calib['divergence_rate']:.4f}`.",
        "",
        f"For comparison, the saved retrained thresholds are `2m < {saved['2m']:.4f}` and `1m < {saved['1m']:.4f}`. On test they produced `{int(saved_test['trades'])}` trades and total profit `{saved_test['total_profit']:.4f}`.",
        "",
        (
            "Operationally, this scan does not justify loosening the live thresholds yet: "
            f"the saved pair beats the calibration-optimal pair on held-out test by `{float(saved_test['total_profit'] - test_at_calib['total_profit']):.4f}` "
            f"total profit and has a lower test divergence rate (`{saved_test['divergence_rate']:.4f}` vs. `{test_at_calib['divergence_rate']:.4f}`). "
            "Treat the calibration-optimal pair as a candidate for forward paper trading, not as a direct replacement."
        ),
        "",
        "The test-optimal row is shown only as a diagnostic reference, not as the deployable recommendation.",
        "",
        "## Selected Results",
        "",
        md_table(selected[display_cols]),
        "",
        "## Top Calibration Pairs",
        "",
        md_table(
            top_calib[
                [
                    "threshold_2m",
                    "threshold_1m",
                    "trades",
                    "trades_from_2m",
                    "trades_from_1m",
                    "divergences",
                    "divergence_rate",
                    "mean_all_in_cost",
                    "mean_profit_per_trade",
                    "total_profit",
                ]
            ]
        ),
        "",
        "## Top Test Pairs",
        "",
        md_table(
            top_test[
                [
                    "threshold_2m",
                    "threshold_1m",
                    "trades",
                    "trades_from_2m",
                    "trades_from_1m",
                    "divergences",
                    "divergence_rate",
                    "mean_all_in_cost",
                    "mean_profit_per_trade",
                    "total_profit",
                ]
            ]
        ),
        "",
        "## Plots",
        "",
        f"![Calibration total profit heatmap]({OUT_CALIB_HEATMAP.relative_to(HORIZON_DIR)})",
        "",
        f"![Test total profit heatmap]({OUT_TEST_HEATMAP.relative_to(HORIZON_DIR)})",
        "",
        f"![Test trade count heatmap]({OUT_TRADE_HEATMAP.relative_to(HORIZON_DIR)})",
        "",
        "## Output Files",
        "",
        f"- Full scan: `{OUT_SCAN.relative_to(ROOT)}`",
        f"- Selected rows: `{OUT_SELECTED.relative_to(ROOT)}`",
        f"- Selected trade rows: `{OUT_TRADES.relative_to(ROOT)}`",
        f"- Calibration heatmap: `{OUT_CALIB_HEATMAP.relative_to(ROOT)}`",
        f"- Test heatmap: `{OUT_TEST_HEATMAP.relative_to(ROOT)}`",
        f"- Test trade-count heatmap: `{OUT_TRADE_HEATMAP.relative_to(ROOT)}`",
        "",
        "## Interpretation",
        "",
        "The threshold pair controls coverage before price-entry filtering. A looser 2m threshold tends to dominate because it opens the full 2-minute entry window. The 1m threshold mostly matters for contracts that fail at 2m but become acceptable at 1m; those entries have less time to find an 18c opportunity.",
        "",
        "This remains a price-and-outcome backtest. It does not model live order failures, minimum Polymarket notional constraints, or queue priority.",
    ]
    OUT_REPORT.write_text("\n".join(report) + "\n")

    print(f"Wrote {OUT_REPORT}")
    print(f"Wrote {OUT_SCAN}")
    print(f"Wrote {OUT_SELECTED}")
    print(f"Wrote {OUT_TRADES}")
    print(f"Calibration-optimal thresholds: 2m={float(best_calib['threshold_2m']):.4f}, 1m={float(best_calib['threshold_1m']):.4f}")
    print(f"Test total profit at calibration optimum: {float(test_at_calib['total_profit']):.4f}")


if __name__ == "__main__":
    main()
