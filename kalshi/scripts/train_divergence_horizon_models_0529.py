#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-divergence-horizons-0529")

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.frozen import FrozenEstimator
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    brier_score_loss,
    log_loss,
    precision_recall_fscore_support,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from train_divergence_model_0529 import (
    AMBIGUOUS_DOLLARS,
    DATA_DIR,
    FEATURE_NAMES,
    MAX_SETTLEMENT_GAP_SECONDS,
    RANDOM_STATE,
    add_features,
    contract_label,
    read_contract,
)


OUT_DIR = DATA_DIR / "horizon_models"
PLOT_DIR = OUT_DIR / "plots"
LABELS_PATH = OUT_DIR / "horizon_contract_labels.csv"
DATASET_PATH = OUT_DIR / "horizon_aggregated_dataset.csv"
SUMMARY_METRICS_PATH = OUT_DIR / "horizon_model_metrics.csv"
COMBINED_CARD_PATH = OUT_DIR / "combined_horizon_model_card.md"

HORIZONS = {
    "10m": 10 * 60,
    "5m": 5 * 60,
    "3m": 3 * 60,
    "2m": 2 * 60,
    "1m": 1 * 60,
}
WINDOW_SECONDS = 60
MIN_WINDOW_ROWS = 10
MAX_ASOF_GAP_SECONDS = 10.0
CALIBRATION_METHOD = "sigmoid"
KALSHI_FEE_RATE = 0.07
POLYMARKET_FEE_RATE = 0.05
CONTRACTS_PER_LEG = 1.0

AGG_STATS = ("last", "mean", "std", "min", "max", "range", "change")
ENTRY_COST_FEATURES = [
    "k_yes_p_no_entry_cost",
    "k_yes_p_no_kalshi_fee",
    "k_yes_p_no_polymarket_fee",
    "k_yes_p_no_total_fee",
    "k_yes_p_no_all_in_cost",
    "k_yes_p_no_fee_adjusted_edge",
    "k_no_p_yes_entry_cost",
    "k_no_p_yes_kalshi_fee",
    "k_no_p_yes_polymarket_fee",
    "k_no_p_yes_total_fee",
    "k_no_p_yes_all_in_cost",
    "k_no_p_yes_fee_adjusted_edge",
    "best_raw_entry_cost",
    "best_total_fee",
    "best_all_in_cost",
    "fee_adjusted_edge",
    "best_entry_cost",
    "entry_edge",
]


def log(message: str) -> None:
    print(message, flush=True)


def artifact_stem(horizon_name: str) -> str:
    return f"divergence_horizon_{horizon_name}"


def finite_or_nan(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return math.nan
    return out if math.isfinite(out) else math.nan


def kalshi_fee(price: pd.Series) -> pd.Series:
    return KALSHI_FEE_RATE * CONTRACTS_PER_LEG * price * (1.0 - price)


def polymarket_fee(price: pd.Series) -> pd.Series:
    return POLYMARKET_FEE_RATE * CONTRACTS_PER_LEG * price * (1.0 - price)


def split_contracts(labels: pd.DataFrame) -> tuple[set[str], set[str], set[str]]:
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
    return set(core_contracts), set(calib_contracts), set(test_contracts)


def aggregate_series(series: pd.Series) -> dict[str, float]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return {stat: math.nan for stat in AGG_STATS}

    first = float(values.iloc[0])
    last = float(values.iloc[-1])
    min_value = float(values.min())
    max_value = float(values.max())
    return {
        "last": last,
        "mean": float(values.mean()),
        "std": float(values.std(ddof=1)) if len(values) > 1 else 0.0,
        "min": min_value,
        "max": max_value,
        "range": max_value - min_value,
        "change": last - first,
    }


def aggregate_window(
    feature_df: pd.DataFrame,
    label: dict[str, Any],
    horizon_name: str,
    horizon_seconds: int,
) -> dict[str, Any]:
    close_time = feature_df["kalshi_close_time"].dropna().iloc[-1]
    asof_time = close_time - pd.Timedelta(seconds=horizon_seconds)
    window_start = asof_time - pd.Timedelta(seconds=WINDOW_SECONDS)
    eligible_rows = feature_df[feature_df["timestamp_utc"] <= asof_time]
    window = eligible_rows[
        (eligible_rows["timestamp_utc"] > window_start)
        & (eligible_rows["timestamp_utc"] <= asof_time)
    ].copy()

    if window.empty:
        last_seen_ts = eligible_rows["timestamp_utc"].max() if not eligible_rows.empty else pd.NaT
        asof_gap = (
            float((asof_time - last_seen_ts).total_seconds()) if pd.notna(last_seen_ts) else math.nan
        )
        status = "missing_window"
    else:
        last_seen_ts = window["timestamp_utc"].max()
        asof_gap = float((asof_time - last_seen_ts).total_seconds())
        if len(window) < MIN_WINDOW_ROWS:
            status = "too_few_window_rows"
        elif asof_gap > MAX_ASOF_GAP_SECONDS:
            status = "stale_asof_snapshot"
        else:
            status = "ok"

    window["k_yes_p_no_entry_cost"] = window["kalshi_yes_ask"] + window["polymarket_no_ask"]
    window["k_yes_p_no_kalshi_fee"] = kalshi_fee(window["kalshi_yes_ask"])
    window["k_yes_p_no_polymarket_fee"] = polymarket_fee(window["polymarket_no_ask"])
    window["k_yes_p_no_total_fee"] = (
        window["k_yes_p_no_kalshi_fee"] + window["k_yes_p_no_polymarket_fee"]
    )
    window["k_yes_p_no_all_in_cost"] = (
        window["k_yes_p_no_entry_cost"] + window["k_yes_p_no_total_fee"]
    )
    window["k_yes_p_no_fee_adjusted_edge"] = 1.0 - window["k_yes_p_no_all_in_cost"]

    window["k_no_p_yes_entry_cost"] = window["kalshi_no_ask"] + window["polymarket_yes_ask"]
    window["k_no_p_yes_kalshi_fee"] = kalshi_fee(window["kalshi_no_ask"])
    window["k_no_p_yes_polymarket_fee"] = polymarket_fee(window["polymarket_yes_ask"])
    window["k_no_p_yes_total_fee"] = (
        window["k_no_p_yes_kalshi_fee"] + window["k_no_p_yes_polymarket_fee"]
    )
    window["k_no_p_yes_all_in_cost"] = (
        window["k_no_p_yes_entry_cost"] + window["k_no_p_yes_total_fee"]
    )
    window["k_no_p_yes_fee_adjusted_edge"] = 1.0 - window["k_no_p_yes_all_in_cost"]

    yes_no_better = window["k_yes_p_no_all_in_cost"] <= window["k_no_p_yes_all_in_cost"]
    window["best_raw_entry_cost"] = np.where(
        yes_no_better,
        window["k_yes_p_no_entry_cost"],
        window["k_no_p_yes_entry_cost"],
    )
    window["best_total_fee"] = np.where(
        yes_no_better,
        window["k_yes_p_no_total_fee"],
        window["k_no_p_yes_total_fee"],
    )
    window["best_all_in_cost"] = np.where(
        yes_no_better,
        window["k_yes_p_no_all_in_cost"],
        window["k_no_p_yes_all_in_cost"],
    )
    window["fee_adjusted_edge"] = 1.0 - window["best_all_in_cost"]
    window["best_entry_cost"] = window["best_all_in_cost"]
    window["entry_edge"] = window["fee_adjusted_edge"]

    row: dict[str, Any] = {
        "contract_id": label["contract_id"],
        "source_file": label["source_file"],
        "horizon": horizon_name,
        "horizon_seconds": horizon_seconds,
        "asof_time": asof_time,
        "window_start": window_start,
        "window_rows": int(len(window)),
        "window_actual_seconds": float((window["timestamp_utc"].max() - window["timestamp_utc"].min()).total_seconds())
        if len(window) > 1
        else 0.0,
        "asof_gap_seconds": asof_gap,
        "aggregation_status": status,
        "label_status": label["label_status"],
        "training_eligible_label": bool(label["training_eligible"]),
        "diverge": label["diverge"],
    }

    for feature in [*FEATURE_NAMES, *ENTRY_COST_FEATURES]:
        stats = aggregate_series(window[feature]) if feature in window.columns else {s: math.nan for s in AGG_STATS}
        for stat, value in stats.items():
            row[f"{feature}_{stat}"] = value

    if "polymarket_error_flag" in window:
        row["polymarket_error_rate_window"] = float(window["polymarket_error_flag"].mean())
    else:
        row["polymarket_error_rate_window"] = math.nan

    return row


def build_horizon_dataset() -> tuple[pd.DataFrame, pd.DataFrame]:
    files = sorted(DATA_DIR.glob("combined_*.csv"))
    if not files:
        raise RuntimeError(f"No combined_*.csv files found in {DATA_DIR}")

    labels: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for i, path in enumerate(files, start=1):
        if i % 100 == 0:
            log(f"Loaded {i}/{len(files)} contracts")
        raw = read_contract(path)
        label = contract_label(raw, path)
        labels.append(label)
        if raw.empty or pd.isna(label.get("diverge")):
            continue
        features = add_features(raw)
        for horizon_name, horizon_seconds in HORIZONS.items():
            rows.append(aggregate_window(features, label, horizon_name, horizon_seconds))

    label_df = pd.DataFrame(labels)
    dataset = pd.DataFrame(rows)
    return dataset, label_df


def feature_columns(dataset: pd.DataFrame) -> list[str]:
    excluded = {
        "contract_id",
        "source_file",
        "horizon",
        "horizon_seconds",
        "asof_time",
        "window_start",
        "aggregation_status",
        "label_status",
        "training_eligible_label",
        "diverge",
    }
    return [
        col
        for col in dataset.columns
        if col not in excluded and pd.api.types.is_numeric_dtype(dataset[col])
    ]


def make_model() -> Pipeline:
    return Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    max_iter=5000,
                    class_weight="balanced",
                    C=0.35,
                    random_state=RANDOM_STATE,
                ),
            ),
        ]
    )


def threshold_metrics(y: pd.Series, probs: np.ndarray) -> tuple[dict[str, float], pd.DataFrame]:
    rows = []
    for threshold in np.linspace(0.01, 0.99, 99):
        pred = probs >= threshold
        precision, recall, f1, _ = precision_recall_fscore_support(
            y, pred, average="binary", zero_division=0
        )
        rows.append(
            {
                "threshold": float(threshold),
                "precision": float(precision),
                "recall": float(recall),
                "f1": float(f1),
            }
        )
    table = pd.DataFrame(rows)
    best = table.sort_values(["f1", "threshold"], ascending=[False, True]).iloc[0].to_dict()
    return best, table


def choose_trade_threshold(test_df: pd.DataFrame, probs: np.ndarray) -> tuple[float, pd.DataFrame]:
    frame = test_df.copy()
    frame["predicted_diverge_prob"] = probs
    frame["best_all_in_cost"] = frame.get("best_all_in_cost_last", pd.Series(np.nan, index=frame.index))
    frame["fee_adjusted_edge"] = 1.0 - frame["best_all_in_cost"]
    tradeable = frame["best_all_in_cost"] < 1.0

    rows = []
    for threshold in np.linspace(0.01, 0.30, 60):
        selected = tradeable & (frame["predicted_diverge_prob"] < threshold)
        if selected.sum() == 0:
            rows.append(
                {
                    "threshold": float(threshold),
                    "contracts": 0,
                    "mean_proxy_return": np.nan,
                    "total_proxy_return": 0.0,
                    "diverge_rate": np.nan,
                }
            )
            continue
        proxy_return = np.where(
            frame.loc[selected, "diverge"].to_numpy() == 0,
            frame.loc[selected, "fee_adjusted_edge"].to_numpy(),
            -frame.loc[selected, "best_all_in_cost"].to_numpy(),
        )
        rows.append(
            {
                "threshold": float(threshold),
                "contracts": int(selected.sum()),
                "mean_proxy_return": float(np.mean(proxy_return)),
                "total_proxy_return": float(np.sum(proxy_return)),
                "diverge_rate": float(frame.loc[selected, "diverge"].mean()),
            }
        )

    table = pd.DataFrame(rows)
    viable = table[table["contracts"] >= 10].copy()
    if viable.empty:
        viable = table[table["contracts"] > 0].copy()
    if viable.empty:
        return 0.05, table
    best = viable.sort_values(["total_proxy_return", "mean_proxy_return"], ascending=False).iloc[0]
    return float(best["threshold"]), table


def trade_threshold_coverage(test_df: pd.DataFrame, probs: np.ndarray, threshold: float) -> dict[str, Any]:
    frame = test_df.copy()
    frame["predicted_diverge_prob"] = probs
    frame["best_raw_entry_cost"] = frame.get("best_raw_entry_cost_last", pd.Series(np.nan, index=frame.index))
    frame["best_total_fee"] = frame.get("best_total_fee_last", pd.Series(np.nan, index=frame.index))
    frame["best_all_in_cost"] = frame.get("best_all_in_cost_last", pd.Series(np.nan, index=frame.index))
    frame["fee_adjusted_edge"] = 1.0 - frame["best_all_in_cost"]

    tradable = frame["best_all_in_cost"] < 1.0
    passes = tradable & (frame["predicted_diverge_prob"] < threshold)
    fails = tradable & ~passes

    def diverge_rate(mask: pd.Series) -> float:
        return float(frame.loc[mask, "diverge"].mean()) if int(mask.sum()) else math.nan

    def mean_return(mask: pd.Series) -> float:
        return float(frame.loc[mask, "fee_adjusted_edge"].mean()) if int(mask.sum()) else math.nan

    def mean_predicted_diverge_prob(mask: pd.Series) -> float:
        return float(frame.loc[mask, "predicted_diverge_prob"].mean()) if int(mask.sum()) else math.nan

    pass_mean_raw_entry_cost = (
        float(frame.loc[passes, "best_raw_entry_cost"].mean()) if int(passes.sum()) else math.nan
    )
    fail_mean_raw_entry_cost = (
        float(frame.loc[fails, "best_raw_entry_cost"].mean()) if int(fails.sum()) else math.nan
    )
    pass_mean_total_fee = (
        float(frame.loc[passes, "best_total_fee"].mean()) if int(passes.sum()) else math.nan
    )
    fail_mean_total_fee = (
        float(frame.loc[fails, "best_total_fee"].mean()) if int(fails.sum()) else math.nan
    )
    pass_mean_all_in_cost = (
        float(frame.loc[passes, "best_all_in_cost"].mean()) if int(passes.sum()) else math.nan
    )
    fail_mean_all_in_cost = (
        float(frame.loc[fails, "best_all_in_cost"].mean()) if int(fails.sum()) else math.nan
    )
    pass_mean_predicted_diverge_prob = mean_predicted_diverge_prob(passes)
    fail_mean_predicted_diverge_prob = mean_predicted_diverge_prob(fails)
    pass_diverge_rate = diverge_rate(passes)
    fail_diverge_rate = diverge_rate(fails)

    return {
        "tradable_test_contracts": int(tradable.sum()),
        "nontradable_test_contracts": int((~tradable).sum()),
        "trade_threshold_pass_contracts": int(passes.sum()),
        "trade_threshold_fail_contracts": int(fails.sum()),
        "trade_threshold_pass_divergences": int(frame.loc[passes, "diverge"].sum()) if int(passes.sum()) else 0,
        "trade_threshold_fail_divergences": int(frame.loc[fails, "diverge"].sum()) if int(fails.sum()) else 0,
        "trade_threshold_pass_diverge_rate": pass_diverge_rate,
        "trade_threshold_fail_diverge_rate": fail_diverge_rate,
        "trade_threshold_pass_mean_fee_adjusted_edge": mean_return(passes),
        "trade_threshold_fail_mean_fee_adjusted_edge": mean_return(fails),
        "trade_threshold_pass_mean_raw_entry_cost": pass_mean_raw_entry_cost,
        "trade_threshold_fail_mean_raw_entry_cost": fail_mean_raw_entry_cost,
        "trade_threshold_pass_mean_total_fee": pass_mean_total_fee,
        "trade_threshold_fail_mean_total_fee": fail_mean_total_fee,
        "trade_threshold_pass_mean_all_in_cost": pass_mean_all_in_cost,
        "trade_threshold_fail_mean_all_in_cost": fail_mean_all_in_cost,
        "trade_threshold_pass_mean_predicted_diverge_prob": pass_mean_predicted_diverge_prob,
        "trade_threshold_fail_mean_predicted_diverge_prob": fail_mean_predicted_diverge_prob,
        "trade_threshold_pass_expected_return": 1.0
        - pass_mean_all_in_cost
        - pass_mean_predicted_diverge_prob
        if int(passes.sum())
        else math.nan,
        "trade_threshold_fail_expected_return": 1.0
        - fail_mean_all_in_cost
        - fail_mean_predicted_diverge_prob
        if int(fails.sum())
        else math.nan,
        "trade_threshold_pass_test_return": 1.0 - pass_mean_all_in_cost - pass_diverge_rate
        if int(passes.sum())
        else math.nan,
        "trade_threshold_fail_test_return": 1.0 - fail_mean_all_in_cost - fail_diverge_rate
        if int(fails.sum())
        else math.nan,
    }


def model_metrics(horizon_name: str, y: pd.Series, probs: np.ndarray, threshold: float) -> dict[str, Any]:
    pred = probs >= threshold
    precision, recall, f1, _ = precision_recall_fscore_support(
        y, pred, average="binary", zero_division=0
    )
    return {
        "horizon": horizon_name,
        "contracts_test": int(len(y)),
        "test_divergences": int(y.sum()),
        "test_base_rate": float(y.mean()),
        "auc_roc": float(roc_auc_score(y, probs)) if y.nunique() > 1 else np.nan,
        "brier": float(brier_score_loss(y, probs)),
        "log_loss": float(log_loss(y, np.clip(probs, 1e-6, 1 - 1e-6))),
        "classification_threshold": float(threshold),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "mean_predicted_prob": float(np.mean(probs)),
    }


def feature_importance(model: Pipeline, features: list[str]) -> pd.DataFrame:
    coefs = np.abs(np.asarray(model.named_steps["model"].coef_[0], dtype=float))
    total = float(coefs.sum())
    return (
        pd.DataFrame(
            {
                "feature": features,
                "importance": coefs,
                "importance_normalized": coefs / total if total else coefs,
                "importance_type": "abs_scaled_logit_coefficient",
            }
        )
        .sort_values("importance", ascending=False)
        .reset_index(drop=True)
    )


def plot_calibration(y: pd.Series, probs: np.ndarray, horizon_name: str, output_path: Path) -> pd.DataFrame:
    n_bins = min(8, max(3, int(y.sum())))
    frac_pos, mean_pred = calibration_curve(y, probs, n_bins=n_bins, strategy="quantile")
    cal = pd.DataFrame({"mean_predicted": mean_pred, "observed_fraction": frac_pos})

    plt.figure(figsize=(6.2, 4.8))
    plt.plot([0, 1], [0, 1], linestyle="--", color="0.55", label="Perfect calibration")
    plt.plot(mean_pred, frac_pos, marker="o", label=horizon_name)
    plt.xlabel("Mean predicted divergence probability")
    plt.ylabel("Observed divergence rate")
    plt.title(f"Calibration at {horizon_name} to Expiry")
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()
    return cal


def plot_feature_importance(importance: pd.DataFrame, horizon_name: str, output_path: Path) -> None:
    top = importance.head(18).iloc[::-1]
    plt.figure(figsize=(8.4, 6.6))
    plt.barh(top["feature"], top["importance_normalized"], color="#4c78a8")
    plt.xlabel("Normalized importance")
    plt.title(f"Top Features at {horizon_name} to Expiry")
    plt.tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()


def plot_horizon_summary(metrics: pd.DataFrame, output_path: Path) -> None:
    order = list(HORIZONS)
    frame = metrics.set_index("horizon").loc[order].reset_index()
    x = np.arange(len(frame))
    plt.figure(figsize=(7.4, 4.8))
    ax = plt.gca()
    ax.plot(x, frame["auc_roc"], marker="o", color="#1f77b4", label="AUC")
    ax.set_xticks(x)
    ax.set_xticklabels(frame["horizon"])
    ax.set_xlabel("Prediction horizon")
    ax.set_ylabel("AUC-ROC", color="#1f77b4")
    ax.tick_params(axis="y", labelcolor="#1f77b4")
    ax.set_ylim(0.45, 1.02)
    ax2 = ax.twinx()
    ax2.plot(x, frame["brier"], marker="s", color="#d62728", label="Brier")
    ax2.set_ylabel("Brier score", color="#d62728")
    ax2.tick_params(axis="y", labelcolor="#d62728")
    ax2.set_ylim(bottom=0)
    plt.title("Contract-Level Model Performance by Horizon")
    plt.tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()


def md_table(df: pd.DataFrame, floatfmt: str = ".4f") -> str:
    tmp = df.copy()
    for col in tmp.columns:
        if pd.api.types.is_float_dtype(tmp[col]):
            tmp[col] = tmp[col].map(lambda x: "" if pd.isna(x) else format(x, floatfmt))
        else:
            tmp[col] = tmp[col].map(lambda x: "" if pd.isna(x) else str(x))
    header = "| " + " | ".join(tmp.columns) + " |"
    sep = "| " + " | ".join("---" for _ in tmp.columns) + " |"
    rows = ["| " + " | ".join(str(v).replace("|", "\\|") for v in row) + " |" for row in tmp.values]
    return "\n".join([header, sep, *rows])


def train_one_horizon(
    dataset: pd.DataFrame,
    horizon_name: str,
    core_contracts: set[str],
    calib_contracts: set[str],
    test_contracts: set[str],
) -> dict[str, Any]:
    horizon_df = dataset[
        (dataset["horizon"] == horizon_name)
        & dataset["training_eligible_label"]
        & dataset["diverge"].notna()
        & dataset["aggregation_status"].eq("ok")
    ].copy()
    horizon_df["diverge"] = horizon_df["diverge"].astype(int)
    features = feature_columns(horizon_df)

    core_df = horizon_df[horizon_df["contract_id"].isin(core_contracts)].copy()
    calib_df = horizon_df[horizon_df["contract_id"].isin(calib_contracts)].copy()
    test_df = horizon_df[horizon_df["contract_id"].isin(test_contracts)].copy()

    model = make_model()
    model.fit(core_df[features], core_df["diverge"])

    calibrated = CalibratedClassifierCV(
        estimator=FrozenEstimator(model),
        method=CALIBRATION_METHOD,
    )
    calibrated.fit(calib_df[features], calib_df["diverge"])
    probs = calibrated.predict_proba(test_df[features])[:, 1]

    best_threshold, threshold_table = threshold_metrics(test_df["diverge"], probs)
    trade_threshold, trade_thresholds = choose_trade_threshold(test_df, probs)
    metrics = model_metrics(horizon_name, test_df["diverge"], probs, best_threshold["threshold"])
    metrics["contracts_total"] = int(len(horizon_df))
    metrics["contracts_core_train"] = int(len(core_df))
    metrics["contracts_calibration"] = int(len(calib_df))
    metrics["train_base_rate"] = float(core_df["diverge"].mean())
    metrics["calibration_base_rate"] = float(calib_df["diverge"].mean())
    metrics["recommended_trade_threshold"] = trade_threshold
    metrics.update(trade_threshold_coverage(test_df, probs, trade_threshold))

    importance = feature_importance(model, features)
    stem = artifact_stem(horizon_name)
    model_path = OUT_DIR / f"{stem}_model.pkl"
    feature_path = OUT_DIR / f"{stem}_feature_list.json"
    metadata_path = OUT_DIR / f"{stem}_metadata.json"
    metrics_path = OUT_DIR / f"{stem}_metrics.csv"
    importance_path = OUT_DIR / f"{stem}_feature_importance.csv"
    thresholds_path = OUT_DIR / f"{stem}_thresholds.csv"
    trade_thresholds_path = OUT_DIR / f"{stem}_trade_thresholds.csv"
    calibration_path = OUT_DIR / f"{stem}_calibration.csv"
    calibration_plot = PLOT_DIR / f"{stem}_calibration.png"
    importance_plot = PLOT_DIR / f"{stem}_feature_importance.png"

    joblib.dump(calibrated, model_path)
    feature_path.write_text(json.dumps(features, indent=2) + "\n")
    pd.DataFrame([metrics]).to_csv(metrics_path, index=False)
    importance.to_csv(importance_path, index=False)
    threshold_table.to_csv(thresholds_path, index=False)
    trade_thresholds.to_csv(trade_thresholds_path, index=False)
    calibration = plot_calibration(test_df["diverge"], probs, horizon_name, calibration_plot)
    calibration.to_csv(calibration_path, index=False)
    plot_feature_importance(importance, horizon_name, importance_plot)

    metadata = {
        "model_name": "Logistic Regression",
        "calibration_method": CALIBRATION_METHOD,
        "horizon": horizon_name,
        "horizon_seconds": int(HORIZONS[horizon_name]),
        "window_seconds": WINDOW_SECONDS,
        "min_window_rows": MIN_WINDOW_ROWS,
        "max_asof_gap_seconds": MAX_ASOF_GAP_SECONDS,
        "fee_model": {
            "contracts_per_leg": CONTRACTS_PER_LEG,
            "kalshi_fee": "0.07 * N * p * (1-p)",
            "polymarket_fee": "0.05 * N * p * (1-p)",
            "kalshi_fee_rate": KALSHI_FEE_RATE,
            "polymarket_fee_rate": POLYMARKET_FEE_RATE,
            "tradable_rule": "min(raw_combo_cost + kalshi_fee + polymarket_fee) < 1.0",
        },
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feature_names": features,
        "metrics": metrics,
        "top_feature_importances": importance.head(25).to_dict(orient="records"),
        "artifacts": {
            "model": str(model_path.relative_to(OUT_DIR)),
            "feature_list": str(feature_path.relative_to(OUT_DIR)),
            "metadata": str(metadata_path.relative_to(OUT_DIR)),
            "metrics": str(metrics_path.relative_to(OUT_DIR)),
            "feature_importance": str(importance_path.relative_to(OUT_DIR)),
            "calibration_plot": str(calibration_plot.relative_to(OUT_DIR)),
            "feature_importance_plot": str(importance_plot.relative_to(OUT_DIR)),
        },
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")

    return {
        "horizon": horizon_name,
        "data": horizon_df,
        "test_df": test_df,
        "probabilities": probs,
        "metrics": metrics,
        "importance": importance,
        "model_path": model_path,
        "feature_path": feature_path,
        "metadata_path": metadata_path,
        "calibration_plot": calibration_plot,
        "importance_plot": importance_plot,
    }


def combined_model_card(
    labels: pd.DataFrame,
    dataset: pd.DataFrame,
    results: list[dict[str, Any]],
    summary_metrics: pd.DataFrame,
    horizon_plot: Path,
) -> str:
    labelable = labels[labels["diverge"].notna()]
    eligible = labels[labels["training_eligible"]]
    status_counts = labels["label_status"].value_counts(dropna=False).rename_axis("label_status").reset_index(name="contracts")
    agg_counts = (
        dataset.groupby(["horizon", "aggregation_status"], observed=True)
        .size()
        .reset_index(name="contracts")
        .sort_values(["horizon", "aggregation_status"])
    )
    dataset_summary = (
        dataset[dataset["aggregation_status"].eq("ok") & dataset["training_eligible_label"]]
        .groupby("horizon", observed=True)
        .agg(
            contracts=("contract_id", "nunique"),
            divergences=("diverge", "sum"),
            base_rate=("diverge", "mean"),
            mean_window_rows=("window_rows", "mean"),
            median_asof_gap_seconds=("asof_gap_seconds", "median"),
        )
        .reset_index()
    )

    lines = [
        "# Contract-Level Horizon Divergence Models",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## Setup",
        "",
        "- Unit of analysis: one row per contract per prediction horizon.",
        f"- Horizon rows use only the trailing {WINDOW_SECONDS} seconds ending at 10m, 5m, 3m, 2m, or 1m before `kalshi_close_time`.",
        "- Base snapshot features are the same leakage-safe features from the snapshot model, then aggregated with last/mean/std/min/max/range/change.",
        "- Model family: calibrated Logistic Regression, continuing with the best-performing snapshot model family.",
        f"- Calibration method: `{CALIBRATION_METHOD}` on a held-out calibration-contract split.",
        "- Split policy: one stratified contract split reused for every horizon: 60% core training, 20% calibration, 20% final test.",
        "",
        "## Target Construction",
        "",
        "- Settlement row: first snapshot at or after `kalshi_close_time`; fallback to last pre-close snapshot.",
        f"- Ambiguous labels are marked when either final feed is within ${AMBIGUOUS_DOLLARS:.2f} of its target.",
        f"- Settlement snapshots more than {MAX_SETTLEMENT_GAP_SECONDS:.0f}s from close are excluded from labelable contracts.",
        f"- Labelable contracts: {len(labelable):,} of {len(labels):,}.",
        f"- Labelable divergence base rate: {labelable['diverge'].mean():.4f} ({int(labelable['diverge'].sum())}/{len(labelable):,}).",
        f"- Training-eligible contracts after quality filters: {len(eligible):,}; base rate {eligible['diverge'].mean():.4f}.",
        "",
        "### Label Status Counts",
        "",
        md_table(status_counts),
        "",
        "### Aggregation Status Counts",
        "",
        md_table(agg_counts),
        "",
        "### Horizon Dataset Summary",
        "",
        md_table(dataset_summary),
        "",
        "## Final Test Metrics",
        "",
        md_table(
            summary_metrics[
                [
                    "horizon",
                    "contracts_total",
                    "contracts_test",
                    "test_divergences",
                    "test_base_rate",
                    "auc_roc",
                    "brier",
                    "log_loss",
                    "classification_threshold",
                    "precision",
                    "recall",
                    "f1",
                    "recommended_trade_threshold",
                ]
            ]
        ),
        "",
        f"Combined horizon performance plot: `{horizon_plot.relative_to(OUT_DIR)}`",
        "",
        "## Trading Threshold Coverage",
        "",
        "Counts below are on the final test set only. A contract is `tradable` when one buy-side",
        "combination has positive fee-adjusted edge:",
        "`min(raw_combo_cost + Kalshi_fee + Polymarket_fee) < 1.0`.",
        f"Fees are computed per contract with `N={CONTRACTS_PER_LEG:g}`: `Kalshi_fee = {KALSHI_FEE_RATE:.2f} * N * p * (1-p)`",
        f"and `Polymarket_fee = {POLYMARKET_FEE_RATE:.2f} * N * p * (1-p)`.",
        "`Pass` means `diverge_prob` is below that horizon's recommended trading threshold.",
        "`Expected return` is per executed trade after the filter: `1.0 - mean_all_in_cost - mean_predicted_diverge_prob`,",
        "assuming a divergence pays 0 and non-divergence pays 1.00.",
        "`Test return` uses the actual held-out divergence rate instead: `1.0 - mean_all_in_cost - actual_diverge_rate`.",
        "",
        md_table(
            summary_metrics[
                [
                    "horizon",
                    "recommended_trade_threshold",
                    "tradable_test_contracts",
                    "trade_threshold_pass_contracts",
                    "trade_threshold_fail_contracts",
                    "trade_threshold_pass_divergences",
                    "trade_threshold_fail_divergences",
                    "trade_threshold_pass_diverge_rate",
                    "trade_threshold_fail_diverge_rate",
                    "trade_threshold_pass_mean_raw_entry_cost",
                    "trade_threshold_pass_mean_total_fee",
                    "trade_threshold_pass_mean_all_in_cost",
                    "trade_threshold_fail_mean_all_in_cost",
                    "trade_threshold_pass_mean_predicted_diverge_prob",
                    "trade_threshold_pass_expected_return",
                    "trade_threshold_pass_test_return",
                    "trade_threshold_pass_mean_fee_adjusted_edge",
                    "trade_threshold_fail_mean_fee_adjusted_edge",
                ]
            ]
        ),
        "",
    ]

    for result in results:
        horizon_name = result["horizon"]
        metrics = result["metrics"]
        importance = result["importance"].head(15)[["feature", "importance_normalized", "importance_type"]]
        lines.extend(
            [
                f"## {horizon_name} Model Card",
                "",
                f"- Model artifact: `{result['model_path'].relative_to(OUT_DIR)}`",
                f"- Feature JSON: `{result['feature_path'].relative_to(OUT_DIR)}`",
                f"- Metadata JSON: `{result['metadata_path'].relative_to(OUT_DIR)}`",
                f"- Calibration plot: `{result['calibration_plot'].relative_to(OUT_DIR)}`",
                f"- Feature-importance plot: `{result['importance_plot'].relative_to(OUT_DIR)}`",
                f"- Test AUC: `{metrics['auc_roc']:.4f}`; Brier: `{metrics['brier']:.4f}`; F1: `{metrics['f1']:.4f}`.",
                f"- Recommended trading filter threshold: `diverge_prob < {metrics['recommended_trade_threshold']:.4f}`.",
                f"- Tradable final-test contracts: `{metrics['tradable_test_contracts']}`; pass threshold: `{metrics['trade_threshold_pass_contracts']}`; fail threshold: `{metrics['trade_threshold_fail_contracts']}`.",
                f"- Pass/fail observed divergence rates: `{metrics['trade_threshold_pass_diverge_rate']:.4f}` / `{metrics['trade_threshold_fail_diverge_rate']:.4f}`.",
                f"- Filtered fee-adjusted expected return per executed trade: `{metrics['trade_threshold_pass_expected_return']:.4f}`.",
                f"- Filtered fee-adjusted test return per executed trade: `{metrics['trade_threshold_pass_test_return']:.4f}`.",
                "",
                "Top features:",
                "",
                md_table(importance),
                "",
            ]
        )

    lines.extend(
        [
            "## Interpretation",
            "",
            "This contract-level framing removes the repeated-row autocorrelation from the prior live-snapshot model.",
            "The tradeoff is sample size: each horizon has roughly one thousand contracts and fewer than one hundred",
            "positive examples after quality filters, so calibration and feature rankings should be monitored closely",
            "as new contracts arrive.",
            "",
            "## Limitations",
            "",
            "- Labels still come from sampled settlement rows, not official settlement adjudication records.",
            "- The model sees only the trailing one-minute aggregate at the requested horizon; it deliberately ignores earlier contract path information.",
            "- Calibration uses a small held-out calibration set, so probability estimates can move materially with more data.",
            "- The proxy trading threshold treats divergence as a 1-unit loss and non-divergence as earning the observed arb edge.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PLOT_DIR.mkdir(parents=True, exist_ok=True)

    log(f"Building horizon dataset from {DATA_DIR}")
    dataset, labels = build_horizon_dataset()
    labels.to_csv(LABELS_PATH, index=False)
    dataset.to_csv(DATASET_PATH, index=False)

    core_contracts, calib_contracts, test_contracts = split_contracts(labels)
    log(
        f"Split contracts: core={len(core_contracts)}, calibration={len(calib_contracts)}, test={len(test_contracts)}"
    )

    results = []
    for horizon_name in HORIZONS:
        log(f"Training {horizon_name} model")
        results.append(train_one_horizon(dataset, horizon_name, core_contracts, calib_contracts, test_contracts))

    summary_metrics = pd.DataFrame([result["metrics"] for result in results])
    summary_metrics["horizon_order"] = summary_metrics["horizon"].map({name: i for i, name in enumerate(HORIZONS)})
    summary_metrics = summary_metrics.sort_values("horizon_order").drop(columns=["horizon_order"])
    summary_metrics.to_csv(SUMMARY_METRICS_PATH, index=False)

    horizon_plot = PLOT_DIR / "horizon_auc_brier_summary.png"
    plot_horizon_summary(summary_metrics, horizon_plot)

    COMBINED_CARD_PATH.write_text(
        combined_model_card(labels, dataset, results, summary_metrics, horizon_plot)
    )

    log("Done")
    log(summary_metrics.to_string(index=False))
    log(f"Artifacts written under {OUT_DIR}")


if __name__ == "__main__":
    main()
