#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

import joblib
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "kp-0529-research"
HORIZON_DIR = DATA_DIR / "horizon_models"
LIVE_DIR = ROOT / "kp-0530-research"
REPORT_PATH = HORIZON_DIR / "horizon_entry_strategy_report_0531.md"

RANDOM_STATE = 20260529
HORIZONS = ["5m", "3m", "2m", "1m"]
THRESHOLD_GRID = np.linspace(0.01, 0.30, 60)
KALSHI_FEE_RATE = 0.07
POLYMARKET_FEE_RATE = 0.05
CONTRACTS_PER_LEG = 1.0
PROFIT_MARGIN = 0.03


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
    core_contracts, calib_contracts, _y_core, _y_calib = train_test_split(
        train_contracts,
        y_train,
        test_size=0.25,
        random_state=RANDOM_STATE,
        stratify=y_train,
    )
    return {
        "all": set(contracts),
        "core_train": set(core_contracts),
        "calibration": set(calib_contracts),
        "test": set(test_contracts),
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
                    "best_all_in_cost_last",
                    "fee_adjusted_edge_last",
                ]
            ]
        )
    return pd.concat(frames, ignore_index=True), thresholds


def probability_tables(predictions: pd.DataFrame, contract_sets: dict[str, set[str]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    pass_pivot = predictions.pivot(index="contract_id", columns="horizon", values="model_pass")
    rows = []
    cond_rows = []
    for sample in ["test", "all"]:
        frame = pass_pivot.loc[pass_pivot.index.intersection(contract_sets[sample]), HORIZONS].dropna()
        row: dict[str, object] = {"sample": sample, "contracts": int(len(frame))}
        for horizon in HORIZONS:
            row[f"P({horizon})"] = float(frame[horizon].mean())
            row[f"{horizon}_pass_count"] = int(frame[horizon].sum())
        row["P(all_four)"] = float(frame.all(axis=1).mean())
        row["all_four_count"] = int(frame.all(axis=1).sum())
        rows.append(row)
        for prev_h, next_h in [("5m", "3m"), ("3m", "2m"), ("2m", "1m")]:
            prev_count = int(frame[prev_h].sum())
            joint_count = int((frame[prev_h] & frame[next_h]).sum())
            cond_rows.append(
                {
                    "sample": sample,
                    "condition": f"{next_h} given {prev_h}",
                    "denominator_pass_count": prev_count,
                    "joint_count": joint_count,
                    "joint_probability": float(joint_count / len(frame)) if len(frame) else math.nan,
                    "conditional_probability": float(joint_count / prev_count) if prev_count else math.nan,
                }
            )
    return pd.DataFrame(rows), pd.DataFrame(cond_rows)


def fee(price: pd.Series, rate: float) -> pd.Series:
    return rate * CONTRACTS_PER_LEG * price * (1.0 - price)


@lru_cache(maxsize=None)
def read_contract_opportunities(source_file: str, cutoff: float) -> pd.DataFrame:
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
    raw["best_all_in_cost"] = np.minimum(k_yes_p_no, k_no_p_yes)
    raw["best_direction"] = np.where(k_yes_p_no <= k_no_p_yes, "K+NP", "NK+P")
    raw["profitable"] = raw["best_all_in_cost"] < cutoff
    return raw[["timestamp", "close_time", "best_all_in_cost", "best_direction", "profitable"]].dropna(
        subset=["timestamp", "close_time"]
    )


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


def build_states(predictions: pd.DataFrame, contract_ids: set[str], cutoff: float) -> dict[str, ContractState]:
    states: dict[str, ContractState] = {}
    pred = predictions[predictions["contract_id"].isin(contract_ids)].copy()
    pred["asof_time"] = pd.to_datetime(pred["asof_time"], utc=True, errors="coerce")
    for contract_id, group in pred.groupby("contract_id", sort=False):
        if set(group["horizon"]) != set(HORIZONS):
            continue
        source_file = str(group["source_file"].dropna().iloc[0])
        opportunities = read_contract_opportunities(source_file, cutoff)
        if opportunities.empty:
            continue
        close_time = opportunities["close_time"].dropna().iloc[0]
        by_horizon = group.set_index("horizon")
        states[str(contract_id)] = ContractState(
            contract_id=str(contract_id),
            source_file=source_file,
            diverge=int(by_horizon["diverge"].iloc[0]),
            close_time=close_time,
            asof={h: by_horizon.at[h, "asof_time"] for h in HORIZONS},
            prob={h: float(by_horizon.at[h, "predicted_diverge_prob"]) for h in HORIZONS},
            passed={h: bool(by_horizon.at[h, "model_pass"]) for h in HORIZONS},
            opportunities=opportunities,
        )
    return states


IntervalBuilder = Callable[[ContractState], tuple[list[tuple[pd.Timestamp, pd.Timestamp, str]], str]]


def intervals_single(horizon: str) -> IntervalBuilder:
    def build(state: ContractState) -> tuple[list[tuple[pd.Timestamp, pd.Timestamp, str]], str]:
        if state.passed[horizon]:
            return [(state.asof[horizon], state.close_time, horizon)], horizon
        return [], horizon

    return build


def intervals_latch(horizons: list[str]) -> IntervalBuilder:
    def build(state: ContractState) -> tuple[list[tuple[pd.Timestamp, pd.Timestamp, str]], str]:
        for horizon in horizons:
            if state.passed[horizon]:
                return [(state.asof[horizon], state.close_time, horizon)], horizon
        return [], horizons[-1]

    return build


def intervals_latest_state() -> IntervalBuilder:
    def build(state: ContractState) -> tuple[list[tuple[pd.Timestamp, pd.Timestamp, str]], str]:
        intervals: list[tuple[pd.Timestamp, pd.Timestamp, str]] = []
        for idx, horizon in enumerate(HORIZONS):
            if not state.passed[horizon]:
                continue
            end = state.asof[HORIZONS[idx + 1]] if idx + 1 < len(HORIZONS) else state.close_time
            intervals.append((state.asof[horizon], end, horizon))
        return intervals, "latest"

    return build


def intervals_pair(prev_horizon: str, horizon: str) -> IntervalBuilder:
    def build(state: ContractState) -> tuple[list[tuple[pd.Timestamp, pd.Timestamp, str]], str]:
        if state.passed[prev_horizon] and state.passed[horizon]:
            return [(state.asof[horizon], state.close_time, horizon)], horizon
        return [], horizon

    return build


def intervals_all_at(horizons: list[str], entry_horizon: str) -> IntervalBuilder:
    def build(state: ContractState) -> tuple[list[tuple[pd.Timestamp, pd.Timestamp, str]], str]:
        if all(state.passed[h] for h in horizons):
            return [(state.asof[entry_horizon], state.close_time, entry_horizon)], entry_horizon
        return [], entry_horizon

    return build


def first_entry(
    state: ContractState,
    intervals: list[tuple[pd.Timestamp, pd.Timestamp, str]],
) -> tuple[pd.Timestamp, float, str, str] | None:
    for start, end, decision_horizon in intervals:
        rows = state.opportunities[
            (state.opportunities["timestamp"] >= start)
            & (state.opportunities["timestamp"] <= end)
            & (state.opportunities["timestamp"] <= state.close_time)
            & state.opportunities["profitable"]
        ].sort_values("timestamp")
        if rows.empty:
            continue
        row = rows.iloc[0]
        return pd.Timestamp(row["timestamp"]), float(row["best_all_in_cost"]), str(row["best_direction"]), decision_horizon
    return None


def simulate_strategy(
    states: dict[str, ContractState],
    name: str,
    interval_builder: IntervalBuilder,
) -> tuple[dict[str, object], pd.DataFrame]:
    trades = []
    model_signals = 0
    would_emergency_exit = 0
    for state in states.values():
        intervals, nominal_horizon = interval_builder(state)
        if intervals:
            model_signals += 1
        entry = first_entry(state, intervals)
        if entry is None:
            continue
        entry_time, all_in_cost, direction, decision_horizon = entry
        future_horizons = [h for h in HORIZONS if state.asof[h] > state.asof[decision_horizon]]
        future_fail = any(not state.passed[h] for h in future_horizons)
        if future_fail:
            would_emergency_exit += 1
        hold_return = (0.0 if state.diverge else 1.0) - all_in_cost
        emergency_full_loss_return = -all_in_cost if future_fail else hold_return
        expected_return_at_entry = 1.0 - all_in_cost - state.prob.get(decision_horizon, math.nan)
        trades.append(
            {
                "strategy": name,
                "contract_id": state.contract_id,
                "decision_horizon": decision_horizon,
                "nominal_horizon": nominal_horizon,
                "entry_time": entry_time,
                "direction": direction,
                "all_in_cost": all_in_cost,
                "diverge": state.diverge,
                "predicted_diverge_prob": state.prob.get(decision_horizon, math.nan),
                "hold_return": hold_return,
                "emergency_full_loss_return": emergency_full_loss_return,
                "expected_return_at_entry": expected_return_at_entry,
                "would_emergency_exit_if_current_logic": future_fail,
            }
        )
    trades_df = pd.DataFrame(trades)
    n_contracts = len(states)
    if trades_df.empty:
        summary = {
            "strategy": name,
            "contracts": n_contracts,
            "model_signal_contracts": model_signals,
            "trades": 0,
            "trade_rate": 0.0,
            "divergences": 0,
            "divergence_rate": math.nan,
            "mean_all_in_cost": math.nan,
            "hold_mean_return_per_trade": math.nan,
            "hold_total_return": 0.0,
            "emergency_full_loss_mean_return_per_trade": math.nan,
            "emergency_full_loss_total_return": 0.0,
            "emergency_exit_nondivergences": 0,
            "mean_expected_return_at_entry": math.nan,
            "would_emergency_exits": 0,
            "would_emergency_exit_rate": math.nan,
        }
        return summary, trades_df
    summary = {
        "strategy": name,
        "contracts": n_contracts,
        "model_signal_contracts": model_signals,
        "trades": int(len(trades_df)),
        "trade_rate": float(len(trades_df) / n_contracts),
        "divergences": int(trades_df["diverge"].sum()),
        "divergence_rate": float(trades_df["diverge"].mean()),
        "mean_all_in_cost": float(trades_df["all_in_cost"].mean()),
        "hold_mean_return_per_trade": float(trades_df["hold_return"].mean()),
        "hold_total_return": float(trades_df["hold_return"].sum()),
        "emergency_full_loss_mean_return_per_trade": float(trades_df["emergency_full_loss_return"].mean()),
        "emergency_full_loss_total_return": float(trades_df["emergency_full_loss_return"].sum()),
        "emergency_exit_nondivergences": int(
            (trades_df["would_emergency_exit_if_current_logic"] & trades_df["diverge"].eq(0)).sum()
        ),
        "mean_expected_return_at_entry": float(trades_df["expected_return_at_entry"].mean()),
        "would_emergency_exits": int(would_emergency_exit),
        "would_emergency_exit_rate": float(would_emergency_exit / len(trades_df)),
    }
    summary["mean_return_per_trade"] = summary["hold_mean_return_per_trade"]
    summary["total_return"] = summary["hold_total_return"]
    return summary, trades_df


def missed_avoided_for_any_2_1(states: dict[str, ContractState]) -> tuple[pd.DataFrame, pd.DataFrame]:
    baseline_rows = []
    strategy_rows = []
    strategy_contracts: set[str] = set()
    _, strategy_trades = simulate_strategy(states, "any_2_1_latch_hold", intervals_latch(["2m", "1m"]))
    if not strategy_trades.empty:
        strategy_contracts = set(strategy_trades["contract_id"].astype(str))
        strategy_rows = strategy_trades.to_dict("records")

    for state in states.values():
        entry = first_entry(state, [(state.asof["2m"], state.close_time, "2m_or_1m_window")])
        if entry is None:
            continue
        entry_time, all_in_cost, direction, _decision_horizon = entry
        is_good = state.diverge == 0
        taken = state.contract_id in strategy_contracts
        baseline_rows.append(
            {
                "contract_id": state.contract_id,
                "baseline_entry_time": entry_time,
                "baseline_direction": direction,
                "baseline_all_in_cost": all_in_cost,
                "diverge": state.diverge,
                "trade_quality": "good" if is_good else "bad",
                "taken_by_any_2_1_latch_hold": taken,
                "good_missed": is_good and not taken,
                "bad_avoided": (not is_good) and not taken,
            }
        )

    baseline = pd.DataFrame(baseline_rows)
    strategy = pd.DataFrame(strategy_rows)
    if baseline.empty:
        summary = pd.DataFrame(
            [
                {
                    "strategy": "any_2_1_latch_hold",
                    "baseline": "profitable opportunity from 2m decision through expiry",
                    "baseline_opportunities": 0,
                    "baseline_good_trades": 0,
                    "baseline_bad_trades": 0,
                    "strategy_trades": 0,
                    "strategy_good_trades": 0,
                    "strategy_bad_trades": 0,
                    "good_trades_missed": 0,
                    "good_trades_missed_pct": math.nan,
                    "bad_trades_avoided": 0,
                    "bad_trades_avoided_pct": math.nan,
                }
            ]
        )
        return summary, baseline

    baseline_good = int(baseline["trade_quality"].eq("good").sum())
    baseline_bad = int(baseline["trade_quality"].eq("bad").sum())
    good_missed = int(baseline["good_missed"].sum())
    bad_avoided = int(baseline["bad_avoided"].sum())
    strategy_good = int((baseline["taken_by_any_2_1_latch_hold"] & baseline["trade_quality"].eq("good")).sum())
    strategy_bad = int((baseline["taken_by_any_2_1_latch_hold"] & baseline["trade_quality"].eq("bad")).sum())
    summary = pd.DataFrame(
        [
            {
                "strategy": "any_2_1_latch_hold",
                "baseline": "profitable opportunity from 2m decision through expiry",
                "baseline_opportunities": int(len(baseline)),
                "baseline_good_trades": baseline_good,
                "baseline_bad_trades": baseline_bad,
                "strategy_trades": int(baseline["taken_by_any_2_1_latch_hold"].sum()),
                "strategy_good_trades": strategy_good,
                "strategy_bad_trades": strategy_bad,
                "good_trades_missed": good_missed,
                "good_trades_missed_pct": float(good_missed / baseline_good) if baseline_good else math.nan,
                "bad_trades_avoided": bad_avoided,
                "bad_trades_avoided_pct": float(bad_avoided / baseline_bad) if baseline_bad else math.nan,
            }
        ]
    )
    return summary, baseline


def fixed_strategy_builders() -> dict[str, IntervalBuilder]:
    return {
        "single_5m_hold": intervals_single("5m"),
        "single_3m_hold": intervals_single("3m"),
        "single_2m_hold": intervals_single("2m"),
        "single_1m_hold": intervals_single("1m"),
        "any_5_3_2_1_latch_hold": intervals_latch(HORIZONS),
        "any_3_2_1_latch_hold": intervals_latch(["3m", "2m", "1m"]),
        "any_2_1_latch_hold": intervals_latch(["2m", "1m"]),
        "latest_state_entry_hold": intervals_latest_state(),
        "require_5m_and_3m_enter_3m": intervals_pair("5m", "3m"),
        "require_3m_and_2m_enter_2m": intervals_pair("3m", "2m"),
        "require_2m_and_1m_enter_1m": intervals_pair("2m", "1m"),
        "require_3m_2m_1m_enter_1m": intervals_all_at(["3m", "2m", "1m"], "1m"),
        "require_all_four_enter_1m": intervals_all_at(HORIZONS, "1m"),
    }


def retune_predictions(predictions: pd.DataFrame, thresholds: dict[str, float]) -> pd.DataFrame:
    out = predictions.copy()
    out["model_pass"] = out.apply(
        lambda row: bool(row["predicted_diverge_prob"] < thresholds[str(row["horizon"])]),
        axis=1,
    )
    return out


def optimize_single_horizon(
    base_predictions: pd.DataFrame,
    contract_sets: dict[str, set[str]],
    horizon: str,
) -> pd.DataFrame:
    rows = []
    for threshold in THRESHOLD_GRID:
        thresholds = {h: -1.0 for h in HORIZONS}
        thresholds[horizon] = float(threshold)
        predictions = retune_predictions(base_predictions, thresholds)
        states = build_states(predictions, contract_sets["calibration"], 1.0 - PROFIT_MARGIN)
        summary, _ = simulate_strategy(states, f"single_{horizon}_threshold_{threshold:.4f}", intervals_single(horizon))
        rows.append({"horizon": horizon, "threshold": float(threshold), **summary})
    return pd.DataFrame(rows)


def optimize_common_latch(
    base_predictions: pd.DataFrame,
    contract_sets: dict[str, set[str]],
    horizons: list[str],
    name: str,
) -> pd.DataFrame:
    rows = []
    for threshold in THRESHOLD_GRID:
        thresholds = {h: (float(threshold) if h in horizons else -1.0) for h in HORIZONS}
        predictions = retune_predictions(base_predictions, thresholds)
        states = build_states(predictions, contract_sets["calibration"], 1.0 - PROFIT_MARGIN)
        summary, _ = simulate_strategy(states, f"{name}_threshold_{threshold:.4f}", intervals_latch(horizons))
        rows.append({"family": name, "threshold": float(threshold), **summary})
    return pd.DataFrame(rows)


def latest_log_excerpt() -> list[str]:
    path = LIVE_DIR / "concise_trader_log.txt"
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    start_idxs = [i for i, line in enumerate(lines) if "START cli_trader_v2" in line]
    if not start_idxs:
        return []
    session = lines[start_idxs[-1] :]
    keep = [
        line
        for line in session
        if any(token in line for token in ["START cli_trader", "ENTRY FILLED", "EMERGENCY EXIT", "MODEL ", "STOP cli_trader"])
    ]
    return keep[:80]


def main() -> None:
    run_retune = "--retune" in sys.argv
    labels = pd.read_csv(HORIZON_DIR / "horizon_contract_labels.csv")
    contract_sets = split_contracts(labels)
    predictions, thresholds = load_predictions()

    pass_summary, conditional_summary = probability_tables(predictions, contract_sets)

    fixed_results = []
    fixed_trades = []
    test_states = build_states(predictions, contract_sets["test"], 1.0 - PROFIT_MARGIN)
    for name, builder in fixed_strategy_builders().items():
        summary, trades = simulate_strategy(test_states, name, builder)
        fixed_results.append(summary)
        if not trades.empty:
            fixed_trades.append(trades)
    fixed_results_df = pd.DataFrame(fixed_results).sort_values(
        ["emergency_full_loss_total_return", "hold_total_return"],
        ascending=False,
    )
    fixed_trades_df = pd.concat(fixed_trades, ignore_index=True) if fixed_trades else pd.DataFrame()
    missed_avoided_summary, missed_avoided_contracts = missed_avoided_for_any_2_1(test_states)

    pass_summary.to_csv(HORIZON_DIR / "entry_strategy_pass_probabilities.csv", index=False)
    conditional_summary.to_csv(HORIZON_DIR / "entry_strategy_conditional_probabilities.csv", index=False)
    fixed_results_df.to_csv(HORIZON_DIR / "entry_strategy_fixed_threshold_backtest.csv", index=False)
    if not fixed_trades_df.empty:
        fixed_trades_df.to_csv(HORIZON_DIR / "entry_strategy_fixed_threshold_trades.csv", index=False)
    missed_avoided_summary.to_csv(HORIZON_DIR / "entry_strategy_any_2_1_missed_avoided.csv", index=False)
    missed_avoided_contracts.to_csv(HORIZON_DIR / "entry_strategy_any_2_1_missed_avoided_contracts.csv", index=False)
    if run_retune:
        single_opt_rows = []
        for horizon in HORIZONS:
            single_opt_rows.append(optimize_single_horizon(predictions, contract_sets, horizon))
        single_opt = pd.concat(single_opt_rows, ignore_index=True)
        best_single_calib = (
            single_opt.sort_values(["total_return", "mean_return_per_trade"], ascending=False)
            .groupby("horizon", as_index=False)
            .head(1)
            .reset_index(drop=True)
        )

        latch_opt = pd.concat(
            [
                optimize_common_latch(predictions, contract_sets, HORIZONS, "any_5_3_2_1_latch"),
                optimize_common_latch(predictions, contract_sets, ["3m", "2m", "1m"], "any_3_2_1_latch"),
                optimize_common_latch(predictions, contract_sets, ["2m", "1m"], "any_2_1_latch"),
            ],
            ignore_index=True,
        )
        best_latch_calib = (
            latch_opt.sort_values(["total_return", "mean_return_per_trade"], ascending=False)
            .groupby("family", as_index=False)
            .head(1)
            .reset_index(drop=True)
        )

        retuned_rows = []
        for _, row in best_single_calib.iterrows():
            horizon = str(row["horizon"])
            threshold = float(row["threshold"])
            thresholds2 = {h: -1.0 for h in HORIZONS}
            thresholds2[horizon] = threshold
            pred2 = retune_predictions(predictions, thresholds2)
            states2 = build_states(pred2, contract_sets["test"], 1.0 - PROFIT_MARGIN)
            summary, _ = simulate_strategy(states2, f"retuned_single_{horizon}", intervals_single(horizon))
            summary["threshold"] = threshold
            retuned_rows.append(summary)
        for _, row in best_latch_calib.iterrows():
            family = str(row["family"])
            threshold = float(row["threshold"])
            horizons = {
                "any_5_3_2_1_latch": HORIZONS,
                "any_3_2_1_latch": ["3m", "2m", "1m"],
                "any_2_1_latch": ["2m", "1m"],
            }[family]
            thresholds2 = {h: (threshold if h in horizons else -1.0) for h in HORIZONS}
            pred2 = retune_predictions(predictions, thresholds2)
            states2 = build_states(pred2, contract_sets["test"], 1.0 - PROFIT_MARGIN)
            summary, _ = simulate_strategy(states2, f"retuned_{family}", intervals_latch(horizons))
            summary["threshold"] = threshold
            retuned_rows.append(summary)
        retuned_test_df = pd.DataFrame(retuned_rows).sort_values(["total_return", "mean_return_per_trade"], ascending=False)
        best_single_calib.to_csv(HORIZON_DIR / "entry_strategy_single_threshold_calibration_scan.csv", index=False)
        best_latch_calib.to_csv(HORIZON_DIR / "entry_strategy_latch_threshold_calibration_scan.csv", index=False)
        retuned_test_df.to_csv(HORIZON_DIR / "entry_strategy_retuned_threshold_test.csv", index=False)
    else:
        best_single_calib = pd.DataFrame()
        best_latch_calib = pd.DataFrame()
        retuned_test_df = pd.DataFrame()

    best_fixed = fixed_results_df.iloc[0]
    best_retuned = retuned_test_df.iloc[0] if run_retune and not retuned_test_df.empty else None
    log_excerpt = latest_log_excerpt()

    report = [
        "# Horizon Entry Strategy Backtest",
        "",
        "Generated from `kp-0529-research/horizon_models` with a live-log sanity check from `kp-0530-research/concise_trader_log.txt`.",
        "",
        "## Scope And Assumptions",
        "",
        f"- Horizons analyzed: `{', '.join(HORIZONS)}`.",
        "- `Pass` means `diverge_prob <` that horizon model's saved recommended trading threshold.",
        f"- Saved thresholds: {', '.join(f'{h}={thresholds[h]:.4f}' for h in HORIZONS)}.",
        f"- Entry price simulation scans each historical contract CSV after the model decision and enters at the first row with `best_all_in_cost < {1.0 - PROFIT_MARGIN:.2f}`; this matches the current default `profit_margin={PROFIT_MARGIN:.2f}`.",
        "- Fees use the same odds-dependent fee equations as `cli_trader_v2.py`, with `N=1` per leg for unit backtest returns.",
            "- `hold_total_return` is the no-emergency-exit, hold-to-expiry result: `(1 - all_in_cost)` when platforms agree and `(-all_in_cost)` when they diverge.",
            "- `emergency_full_loss_total_return` is a conservative proxy for the current sequential emergency-exit design: if a later horizon flips to `tradable=False`, that trade is marked as a full stake loss `(-all_in_cost)` even if the contract later agrees.",
            "- Strategy results below are on the original final held-out test split unless marked otherwise.",
        "",
        "## Live Log Context",
        "",
    ]
    if log_excerpt:
        report.extend(
            [
                "The latest live session starts at the last `START cli_trader_v2` line. The relevant pattern is that entries can be profitable when held, but later model flips force emergency exits:",
                "",
                "```text",
                *log_excerpt[-28:],
                "```",
                "",
            ]
        )
    else:
        report.append("No concise live log excerpt was found.")

    report.extend(
        [
            "## Model Pass Probabilities",
            "",
            "Primary interpretation should use the `test` row. The `all` row is useful as a larger-sample diagnostic, but includes contracts used to fit/calibrate the models.",
            "",
            md_table(pass_summary),
            "",
            "## Conditional Pass Probabilities",
            "",
            md_table(conditional_summary),
            "",
            "## Fixed-Threshold Strategy Backtest",
            "",
            "These strategies use the saved per-horizon thresholds. The hold columns assume no emergency exits. The emergency-full-loss columns instead assume every later model flip after entry loses the whole stake.",
            "",
            md_table(
                fixed_results_df[
                    [
                        "strategy",
                        "contracts",
                        "model_signal_contracts",
                        "trades",
                        "trade_rate",
                        "divergences",
                        "divergence_rate",
                        "mean_all_in_cost",
                        "hold_mean_return_per_trade",
                        "hold_total_return",
                        "would_emergency_exits",
                        "would_emergency_exit_rate",
                        "emergency_exit_nondivergences",
                        "emergency_full_loss_mean_return_per_trade",
                        "emergency_full_loss_total_return",
                    ]
                ].head(20)
            ),
            "",
            "## Return Arithmetic Example",
            "",
        ]
    )
    example = fixed_results_df[fixed_results_df["strategy"].eq("any_2_1_latch_hold")].iloc[0]
    missed_display = missed_avoided_summary.copy()
    missed_display["good_trades_missed_percent"] = missed_display["good_trades_missed_pct"] * 100.0
    missed_display["bad_trades_avoided_percent"] = missed_display["bad_trades_avoided_pct"] * 100.0
    report.extend(
        [
            "For `any_2_1_latch_hold`:",
            "",
            f"- Hold-to-expiry: `(trades - divergences) - trades * mean_all_in_cost = ({int(example['trades'])} - {int(example['divergences'])}) - {int(example['trades'])} * {example['mean_all_in_cost']:.6f} = {example['hold_total_return']:.4f}`.",
            f"- Emergency-full-loss proxy: `hold_total_return - emergency_exit_nondivergences = {example['hold_total_return']:.4f} - {int(example['emergency_exit_nondivergences'])} = {example['emergency_full_loss_total_return']:.4f}`.",
            "",
            "## Missed Good Trades And Avoided Bad Trades",
            "",
            "For this diagnostic, the baseline is every held-out test contract with at least one qualifying arbitrage opportunity from the 2m decision time through expiry. A `good` trade is a non-divergent contract that would settle for the full payout; a `bad` trade is a divergent contract that loses the stake.",
            "",
            md_table(
                missed_display[
                    [
                        "strategy",
                        "baseline_opportunities",
                        "baseline_good_trades",
                        "baseline_bad_trades",
                        "strategy_trades",
                        "strategy_good_trades",
                        "strategy_bad_trades",
                        "good_trades_missed",
                        "good_trades_missed_percent",
                        "bad_trades_avoided",
                        "bad_trades_avoided_percent",
                    ]
                ]
            ),
            "",
            "## Threshold Retuning",
            "",
            "This default report does not retune thresholds because the immediate question is whether the existing saved thresholds create bad sequential behavior. Run the script with `--retune` to perform the slower calibration-only threshold scan.",
            "",
            "## Recommendation",
            "",
            f"Using the fixed saved thresholds and the conservative emergency-full-loss assumption, the best held-out total return in this search is `{best_fixed['strategy']}` with `{best_fixed['trades']}` trades, hold total return `{best_fixed['hold_total_return']:.4f}`, emergency-full-loss total return `{best_fixed['emergency_full_loss_total_return']:.4f}`, and would-be emergency-exit rate `{best_fixed['would_emergency_exit_rate']:.4f}`.",
        ]
    )
    if best_retuned is not None:
        report.append(
            f"With calibration-retuned simple thresholds, the best held-out result is `{best_retuned['strategy']}` at threshold `{best_retuned['threshold']:.4f}`, with `{best_retuned['trades']}` trades, mean return `{best_retuned['mean_return_per_trade']:.4f}`, and total return `{best_retuned['total_return']:.4f}`."
        )
    report.extend(
        [
            "",
            "The operational conclusion is to remove emergency exits from model disagreement. If an entry is opened, hold it to expiry under the no-exit assumption. Under a conservative full-loss penalty for later model flips, `any_2_1_latch_hold` is strongest among the tested fixed-threshold rules because it avoids most 5m/3m flip damage while keeping broad coverage. `single_2m_hold` remains the strongest one-model rule.",
            "",
            "The 5m signal is not useless, but it is not a good standalone trigger for the current bot design: its pass rate is low, and a non-trivial fraction of 5m passes do not survive to later horizons. It is better used as an early warning or as one input to a later confirmation rule, not as an entry permission that can later be revoked.",
            "",
            "## Limitations",
            "",
            "- The historical CSVs do not encode all live order placement failures, stale books, or minimum-notional failures; this is a decision/price backtest, not a fill simulator.",
            "- Entry uses the first qualifying historical row after a model pass. Live fills can be worse, especially near expiry.",
            "- Optional threshold retuning is deliberately left out of the default report. A production retune should reserve a fresh forward test period.",
        ]
    )
    REPORT_PATH.write_text("\n".join(report) + "\n")
    print(f"Wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
