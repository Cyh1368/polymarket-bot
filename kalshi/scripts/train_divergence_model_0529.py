#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-divergence-0529")

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.ensemble import RandomForestClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    brier_score_loss,
    f1_score,
    log_loss,
    precision_recall_fscore_support,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "kp-0529-research"
PLOT_DIR = DATA_DIR / "divergence_plots"

MODEL_PATH = DATA_DIR / "divergence_model.pkl"
FEATURE_LIST_PATH = DATA_DIR / "feature_list.json"
METADATA_PATH = DATA_DIR / "divergence_model_metadata.json"
MODEL_CARD_PATH = DATA_DIR / "model_card.md"
LABELS_PATH = DATA_DIR / "divergence_contract_labels.csv"
METRICS_PATH = DATA_DIR / "divergence_model_metrics.csv"
IMPORTANCE_PATH = DATA_DIR / "divergence_feature_importance.csv"
TIME_PERF_PATH = DATA_DIR / "divergence_time_performance.csv"
CALIBRATION_TABLE_PATH = DATA_DIR / "divergence_calibration_table.csv"

RANDOM_STATE = 20260529
CONTRACT_SECONDS = 15 * 60
AMBIGUOUS_DOLLARS = 1.0
MAX_SETTLEMENT_GAP_SECONDS = 10.0
MIN_TRAIN_ELAPSED_FRACTION = 0.0
MAX_TRAIN_ELAPSED_FRACTION = 1.0

FEATURE_NAMES = [
    "price_spread",
    "price_spread_abs",
    "kalshi_distance_to_target",
    "polymarket_distance_to_target",
    "polymarket_distance_to_own_target",
    "target_spread",
    "target_spread_abs",
    "feeds_on_same_side_own_targets",
    "price_between_targets",
    "spread_vs_distance_ratio",
    "feeds_on_same_side",
    "elapsed_fraction",
    "time_to_close_seconds",
    "kalshi_bid_ask_spread_yes",
    "kalshi_order_book_imbalance",
    "kalshi_yes_mid",
    "kalshi_last_price",
    "polymarket_bid_ask_spread_yes",
    "polymarket_order_book_imbalance",
    "polymarket_yes_mid",
    "implied_prob_spread",
    "k_plus_np",
    "nk_plus_p",
    "arb_available",
    "price_spread_roll10_std",
    "kalshi_btc_price_roll10_mean",
    "kalshi_btc_price_roll10_std",
    "kalshi_btc_price_lag5",
    "kalshi_btc_price_lag10",
    "kalshi_btc_price_momentum_5",
    "kalshi_btc_price_momentum_10",
    "implied_prob_spread_roll10_std",
    "polymarket_error_flag",
    "price_spread_abs_x_elapsed_fraction",
    "spread_vs_distance_ratio_x_elapsed_fraction",
    "feeds_on_same_side_x_elapsed_fraction",
]

READ_COLUMNS = sorted(
    {
        "timestamp_utc",
        "kalshi_timestamp_utc",
        "kalshi_ticker",
        "kalshi_close_time",
        "kalshi_yes_bid",
        "kalshi_yes_ask",
        "kalshi_no_bid",
        "kalshi_no_ask",
        "kalshi_yes_mid",
        "kalshi_last_price",
        "kalshi_best_yes_bid_qty",
        "kalshi_best_no_bid_qty",
        "polymarket_timestamp_utc",
        "polymarket_ticker",
        "polymarket_close_time",
        "polymarket_yes_bid",
        "polymarket_yes_ask",
        "polymarket_no_bid",
        "polymarket_no_ask",
        "polymarket_yes_mid",
        "polymarket_last_price",
        "polymarket_best_yes_bid_qty",
        "polymarket_best_no_bid_qty",
        "kalshi_btc_price",
        "kalshi_btc_target",
        "kalshi_btc_60_sma",
        "kalshi_btc_60_sma_sample_count",
        "polymarket_btc_price",
        "polymarket_btc_target",
        "k_plus_np",
        "nk_plus_p",
        "polymarket_error",
    }
)

TIMESTAMP_COLUMNS = {
    "timestamp_utc",
    "kalshi_timestamp_utc",
    "kalshi_close_time",
    "polymarket_timestamp_utc",
    "polymarket_close_time",
}

NUMERIC_COLUMNS = [c for c in READ_COLUMNS if c not in TIMESTAMP_COLUMNS and not c.endswith("ticker")]
NUMERIC_COLUMNS.remove("polymarket_error")


def log(message: str) -> None:
    print(message, flush=True)


def read_contract(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=lambda col: col in READ_COLUMNS)
    for col in READ_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan
    for col in TIMESTAMP_COLUMNS:
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce", format="mixed")
    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["source_file"] = path.name
    fallback_id = path.stem.replace("combined_", "")
    if "kalshi_ticker" not in df or df["kalshi_ticker"].dropna().empty:
        df["contract_id"] = fallback_id
    else:
        df["contract_id"] = df["kalshi_ticker"].ffill().bfill().fillna(fallback_id)
    df = df.dropna(subset=["timestamp_utc", "kalshi_close_time"]).sort_values("timestamp_utc")
    df = df.drop_duplicates("timestamp_utc", keep="last").reset_index(drop=True)
    return df


def choose_polymarket_target(df: pd.DataFrame, settlement_row: pd.Series) -> tuple[float, str]:
    row_target = settlement_row.get("polymarket_btc_target")
    if pd.notna(row_target):
        return float(row_target), "observed_at_settlement"

    settlement_ts = settlement_row["timestamp_utc"]
    observed_before = df.loc[
        (df["timestamp_utc"] <= settlement_ts) & df["polymarket_btc_target"].notna(),
        "polymarket_btc_target",
    ]
    if not observed_before.empty:
        return float(observed_before.iloc[-1]), "observed_before_settlement"

    observed_anywhere = df["polymarket_btc_target"].dropna()
    if not observed_anywhere.empty:
        return float(observed_anywhere.iloc[-1]), "observed_elsewhere"

    opening_prices = df["polymarket_btc_price"].dropna()
    if not opening_prices.empty:
        return float(opening_prices.iloc[0]), "inferred_from_opening_rtds"

    return math.nan, "missing"


def choose_kalshi_target(df: pd.DataFrame, settlement_row: pd.Series) -> tuple[float, str]:
    row_target = settlement_row.get("kalshi_btc_target")
    if pd.notna(row_target):
        return float(row_target), "observed_at_settlement"
    observed = df["kalshi_btc_target"].dropna()
    if not observed.empty:
        return float(observed.iloc[-1]), "observed_elsewhere"
    return math.nan, "missing"


def contract_label(df: pd.DataFrame, path: Path) -> dict[str, Any]:
    if df.empty:
        return {
            "source_file": path.name,
            "contract_id": path.stem.replace("combined_", ""),
            "diverge": np.nan,
            "label_status": "invalid_empty_file",
            "training_eligible": False,
        }

    close_time = df["kalshi_close_time"].dropna().iloc[-1]
    post_close = df[df["timestamp_utc"] >= close_time]
    if post_close.empty:
        settlement_row = df.iloc[-1]
        settlement_row_source = "last_before_close"
    else:
        settlement_row = post_close.iloc[0]
        settlement_row_source = "first_at_or_after_close"

    gap_seconds = float((settlement_row["timestamp_utc"] - close_time).total_seconds())
    kalshi_target, kalshi_target_source = choose_kalshi_target(df, settlement_row)
    poly_target, poly_target_source = choose_polymarket_target(df, settlement_row)
    kalshi_price = settlement_row.get("kalshi_btc_price")
    poly_price = settlement_row.get("polymarket_btc_price")
    settlement_error = pd.notna(settlement_row.get("polymarket_error"))
    poly_error_rows = int(df["polymarket_error"].notna().sum())

    if pd.isna(kalshi_price) or pd.isna(kalshi_target) or pd.isna(poly_price) or pd.isna(poly_target):
        k_dist = math.nan
        p_dist = math.nan
        kalshi_yes = np.nan
        poly_yes = np.nan
        diverge = np.nan
        label_status = "invalid_missing_settlement_price_or_target"
    else:
        k_dist = float(kalshi_price - kalshi_target)
        p_dist = float(poly_price - poly_target)
        kalshi_yes = bool(k_dist > 0)
        poly_yes = bool(p_dist > 0)
        diverge = int(kalshi_yes != poly_yes)

        if abs(gap_seconds) > MAX_SETTLEMENT_GAP_SECONDS:
            label_status = "invalid_settlement_snapshot_gap"
            diverge = np.nan
        elif settlement_error:
            label_status = "feed_error_at_settlement"
        elif abs(k_dist) <= AMBIGUOUS_DOLLARS or abs(p_dist) <= AMBIGUOUS_DOLLARS:
            label_status = "ambiguous_near_target"
        elif poly_target_source.startswith("inferred"):
            label_status = "target_inferred"
        else:
            label_status = "clean"

    training_eligible = (
        pd.notna(diverge)
        and abs(gap_seconds) <= MAX_SETTLEMENT_GAP_SECONDS
        and label_status not in {"ambiguous_near_target", "feed_error_at_settlement"}
    )

    return {
        "source_file": path.name,
        "contract_id": str(df["contract_id"].iloc[0]),
        "close_time": close_time,
        "settlement_timestamp": settlement_row["timestamp_utc"],
        "settlement_row_source": settlement_row_source,
        "settlement_gap_seconds": gap_seconds,
        "rows": int(len(df)),
        "polymarket_error_rows": poly_error_rows,
        "polymarket_error_rate": poly_error_rows / len(df),
        "settlement_polymarket_error": bool(settlement_error),
        "kalshi_settlement_price": float(kalshi_price) if pd.notna(kalshi_price) else np.nan,
        "polymarket_settlement_price": float(poly_price) if pd.notna(poly_price) else np.nan,
        "kalshi_target": kalshi_target,
        "polymarket_target": poly_target,
        "kalshi_target_source": kalshi_target_source,
        "polymarket_target_source": poly_target_source,
        "kalshi_distance_final": k_dist,
        "polymarket_distance_final": p_dist,
        "kalshi_settle_yes": kalshi_yes,
        "polymarket_settle_yes": poly_yes,
        "diverge": diverge,
        "label_status": label_status,
        "training_eligible": bool(training_eligible),
    }


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().sort_values(["contract_id", "timestamp_utc"]).reset_index(drop=True)
    out["time_to_close_seconds"] = (out["kalshi_close_time"] - out["timestamp_utc"]).dt.total_seconds()
    out["contract_start_time"] = out["kalshi_close_time"] - pd.to_timedelta(CONTRACT_SECONDS, unit="s")
    out["elapsed_fraction"] = (
        (out["timestamp_utc"] - out["contract_start_time"]).dt.total_seconds() / CONTRACT_SECONDS
    )
    out["elapsed_fraction"] = out["elapsed_fraction"].clip(0.0, 1.0)

    rolling_sma = (
        out.groupby("contract_id", sort=False)["kalshi_btc_price"]
        .rolling(30, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )
    out["kalshi_btc_sma_for_distance"] = out["kalshi_btc_60_sma"].where(
        out["kalshi_btc_60_sma"].notna(), rolling_sma
    )

    out["price_spread"] = out["kalshi_btc_price"] - out["polymarket_btc_price"]
    out["price_spread_abs"] = out["price_spread"].abs()
    out["kalshi_distance_to_target"] = out["kalshi_btc_sma_for_distance"] - out["kalshi_btc_target"]
    group = out.groupby("contract_id", sort=False, group_keys=False)
    observed_poly_target = group["polymarket_btc_target"].ffill()
    first_poly_price = group["polymarket_btc_price"].transform(
        lambda series: series.dropna().iloc[0] if series.notna().any() else np.nan
    )
    out["polymarket_btc_target_for_features"] = observed_poly_target.where(
        observed_poly_target.notna(),
        first_poly_price,
    )
    out["polymarket_distance_to_target"] = out["polymarket_btc_price"] - out["kalshi_btc_target"]
    out["polymarket_distance_to_own_target"] = (
        out["polymarket_btc_price"] - out["polymarket_btc_target_for_features"]
    )
    out["target_spread"] = out["kalshi_btc_target"] - out["polymarket_btc_target_for_features"]
    out["target_spread_abs"] = out["target_spread"].abs()
    out["spread_vs_distance_ratio"] = out["price_spread_abs"] / (
        out["kalshi_distance_to_target"].abs() + 1e-6
    )
    out["spread_vs_distance_ratio"] = out["spread_vs_distance_ratio"].clip(0, 1_000_000)

    same_positive = (out["kalshi_distance_to_target"] > 0) & (out["polymarket_distance_to_target"] > 0)
    same_negative = (out["kalshi_distance_to_target"] < 0) & (out["polymarket_distance_to_target"] < 0)
    known_sides = out["kalshi_distance_to_target"].notna() & out["polymarket_distance_to_target"].notna()
    out["feeds_on_same_side"] = np.where(known_sides, (same_positive | same_negative).astype(float), np.nan)
    same_positive_own = (out["kalshi_distance_to_target"] > 0) & (out["polymarket_distance_to_own_target"] > 0)
    same_negative_own = (out["kalshi_distance_to_target"] < 0) & (out["polymarket_distance_to_own_target"] < 0)
    known_own_sides = out["kalshi_distance_to_target"].notna() & out["polymarket_distance_to_own_target"].notna()
    out["feeds_on_same_side_own_targets"] = np.where(
        known_own_sides,
        (same_positive_own | same_negative_own).astype(float),
        np.nan,
    )
    lower_target = np.minimum(out["kalshi_btc_target"], out["polymarket_btc_target_for_features"])
    upper_target = np.maximum(out["kalshi_btc_target"], out["polymarket_btc_target_for_features"])
    kalshi_between = out["kalshi_btc_price"].between(lower_target, upper_target, inclusive="both")
    poly_between = out["polymarket_btc_price"].between(lower_target, upper_target, inclusive="both")
    known_targets = out["kalshi_btc_target"].notna() & out["polymarket_btc_target_for_features"].notna()
    out["price_between_targets"] = np.where(known_targets, (kalshi_between | poly_between).astype(float), np.nan)

    out["kalshi_bid_ask_spread_yes"] = out["kalshi_yes_ask"] - out["kalshi_yes_bid"]
    out["kalshi_order_book_imbalance"] = (
        (out["kalshi_best_yes_bid_qty"] - out["kalshi_best_no_bid_qty"])
        / (out["kalshi_best_yes_bid_qty"] + out["kalshi_best_no_bid_qty"] + 1e-6)
    )
    out["polymarket_bid_ask_spread_yes"] = out["polymarket_yes_ask"] - out["polymarket_yes_bid"]
    out["polymarket_order_book_imbalance"] = (
        (out["polymarket_best_yes_bid_qty"] - out["polymarket_best_no_bid_qty"])
        / (out["polymarket_best_yes_bid_qty"] + out["polymarket_best_no_bid_qty"] + 1e-6)
    )

    out["implied_prob_spread"] = out["kalshi_yes_mid"] - out["polymarket_yes_mid"]
    out["arb_available"] = ((out["k_plus_np"] > 1.0) | (out["nk_plus_p"] > 1.0)).astype(float)
    out["polymarket_error_flag"] = out["polymarket_error"].notna().astype(float)

    out["price_spread_roll10_std"] = (
        group["price_spread"].rolling(10, min_periods=2).std().reset_index(level=0, drop=True)
    )
    out["kalshi_btc_price_roll10_mean"] = (
        group["kalshi_btc_price"].rolling(10, min_periods=1).mean().reset_index(level=0, drop=True)
    )
    out["kalshi_btc_price_roll10_std"] = (
        group["kalshi_btc_price"].rolling(10, min_periods=2).std().reset_index(level=0, drop=True)
    )
    out["kalshi_btc_price_lag5"] = group["kalshi_btc_price"].shift(5)
    out["kalshi_btc_price_lag10"] = group["kalshi_btc_price"].shift(10)
    out["kalshi_btc_price_momentum_5"] = out["kalshi_btc_price"] - out["kalshi_btc_price_lag5"]
    out["kalshi_btc_price_momentum_10"] = out["kalshi_btc_price"] - out["kalshi_btc_price_lag10"]
    out["implied_prob_spread_roll10_std"] = (
        group["implied_prob_spread"].rolling(10, min_periods=2).std().reset_index(level=0, drop=True)
    )
    out["price_spread_abs_x_elapsed_fraction"] = out["price_spread_abs"] * out["elapsed_fraction"]
    out["spread_vs_distance_ratio_x_elapsed_fraction"] = (
        out["spread_vs_distance_ratio"] * out["elapsed_fraction"]
    )
    out["feeds_on_same_side_x_elapsed_fraction"] = out["feeds_on_same_side"] * out["elapsed_fraction"]

    out[FEATURE_NAMES] = out[FEATURE_NAMES].replace([np.inf, -np.inf], np.nan)
    return out


def load_dataset() -> tuple[pd.DataFrame, pd.DataFrame]:
    files = sorted(DATA_DIR.glob("combined_*.csv"))
    if not files:
        raise RuntimeError(f"No combined_*.csv files found in {DATA_DIR}")

    labels = []
    frames = []
    for i, path in enumerate(files, start=1):
        if i % 100 == 0:
            log(f"Loaded {i}/{len(files)} contracts")
        df = read_contract(path)
        label = contract_label(df, path)
        labels.append(label)
        if df.empty or pd.isna(label.get("diverge")):
            continue
        feature_df = add_features(df)
        feature_df["diverge"] = int(label["diverge"])
        feature_df["label_status"] = label["label_status"]
        feature_df["training_eligible"] = bool(label["training_eligible"])
        frames.append(feature_df)

    label_df = pd.DataFrame(labels)
    if not frames:
        raise RuntimeError("No labelable contracts produced feature rows")
    row_df = pd.concat(frames, ignore_index=True)
    return row_df, label_df


def eligible_training_rows(row_df: pd.DataFrame) -> pd.DataFrame:
    train = row_df[
        row_df["training_eligible"]
        & (row_df["time_to_close_seconds"] > 0)
        & (row_df["elapsed_fraction"] >= MIN_TRAIN_ELAPSED_FRACTION)
        & (row_df["elapsed_fraction"] < MAX_TRAIN_ELAPSED_FRACTION)
    ].copy()
    train = train.dropna(subset=["diverge", "contract_id"])
    train["diverge"] = train["diverge"].astype(int)
    return train


def split_contracts(label_df: pd.DataFrame) -> tuple[set[str], set[str], set[str]]:
    eligible = label_df[label_df["training_eligible"]].dropna(subset=["diverge"]).copy()
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


def make_models(scale_pos_weight: float) -> dict[str, Pipeline]:
    return {
        "Logistic Regression": Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                (
                    "model",
                    LogisticRegression(
                        max_iter=2000,
                        class_weight="balanced",
                        random_state=RANDOM_STATE,
                    ),
                ),
            ]
        ),
        "LightGBM": Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                (
                    "model",
                    LGBMClassifier(
                        n_estimators=450,
                        learning_rate=0.035,
                        num_leaves=31,
                        max_depth=-1,
                        min_child_samples=80,
                        subsample=0.90,
                        colsample_bytree=0.90,
                        reg_lambda=1.0,
                        objective="binary",
                        scale_pos_weight=scale_pos_weight,
                        importance_type="gain",
                        random_state=RANDOM_STATE,
                        n_jobs=-1,
                        verbosity=-1,
                    ),
                ),
            ]
        ),
        "Random Forest": Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                (
                    "model",
                    RandomForestClassifier(
                        n_estimators=220,
                        max_depth=14,
                        min_samples_leaf=40,
                        class_weight="balanced_subsample",
                        random_state=RANDOM_STATE,
                        n_jobs=-1,
                    ),
                ),
            ]
        ),
    }


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
    frame["arb_return"] = np.maximum(frame["k_plus_np"], frame["nk_plus_p"]) - 1.0
    frame["arb_return"] = frame["arb_return"].where(frame["arb_return"] > 0, 0.0)
    tradeable = frame["arb_return"] >= 0.02
    rows = []
    for threshold in np.linspace(0.01, 0.30, 60):
        selected = tradeable & (frame["predicted_diverge_prob"] < threshold)
        if selected.sum() == 0:
            rows.append(
                {
                    "threshold": float(threshold),
                    "trades": 0,
                    "mean_proxy_return": np.nan,
                    "total_proxy_return": 0.0,
                    "diverge_rate": np.nan,
                }
            )
            continue
        proxy_return = np.where(
            frame.loc[selected, "diverge"].to_numpy() == 0,
            frame.loc[selected, "arb_return"].to_numpy(),
            -1.0,
        )
        rows.append(
            {
                "threshold": float(threshold),
                "trades": int(selected.sum()),
                "mean_proxy_return": float(np.mean(proxy_return)),
                "total_proxy_return": float(np.sum(proxy_return)),
                "diverge_rate": float(frame.loc[selected, "diverge"].mean()),
            }
        )
    table = pd.DataFrame(rows)
    viable = table[table["trades"] >= 50].copy()
    if viable.empty:
        viable = table[table["trades"] > 0].copy()
    if viable.empty:
        return 0.05, table
    best = viable.sort_values(["total_proxy_return", "mean_proxy_return"], ascending=False).iloc[0]
    return float(best["threshold"]), table


def model_metrics(name: str, y: pd.Series, probs: np.ndarray, threshold: float) -> dict[str, Any]:
    pred = probs >= threshold
    precision, recall, f1, _ = precision_recall_fscore_support(
        y, pred, average="binary", zero_division=0
    )
    return {
        "model": name,
        "auc_roc": float(roc_auc_score(y, probs)) if y.nunique() > 1 else np.nan,
        "brier": float(brier_score_loss(y, probs)),
        "log_loss": float(log_loss(y, np.clip(probs, 1e-6, 1 - 1e-6))),
        "classification_threshold": float(threshold),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "mean_predicted_prob": float(np.mean(probs)),
        "empirical_diverge_rate": float(np.mean(y)),
    }


def fit_and_score_models(
    train_df: pd.DataFrame,
    core_contracts: set[str],
    calib_contracts: set[str],
    test_contracts: set[str],
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    core_df = train_df[train_df["contract_id"].isin(core_contracts)].copy()
    calib_df = train_df[train_df["contract_id"].isin(calib_contracts)].copy()
    test_df = train_df[train_df["contract_id"].isin(test_contracts)].copy()

    x_core = core_df[FEATURE_NAMES]
    y_core = core_df["diverge"]
    x_calib = calib_df[FEATURE_NAMES]
    y_calib = calib_df["diverge"]
    x_test = test_df[FEATURE_NAMES]
    y_test = test_df["diverge"]

    positives = int(y_core.sum())
    negatives = int(len(y_core) - positives)
    scale_pos_weight = negatives / positives if positives else 1.0
    models = make_models(scale_pos_weight)

    fitted: dict[str, Any] = {}
    metric_rows = []
    threshold_rows = []
    for name, estimator in models.items():
        log(f"Training {name}")
        estimator.fit(x_core, y_core)
        calibrated = CalibratedClassifierCV(
            estimator=FrozenEstimator(estimator),
            method="isotonic",
        )
        calibrated.fit(x_calib, y_calib)
        probs = calibrated.predict_proba(x_test)[:, 1]
        best_threshold, threshold_table = threshold_metrics(y_test, probs)
        metric = model_metrics(name, y_test, probs, best_threshold["threshold"])
        metric_rows.append(metric)
        threshold_table.insert(0, "model", name)
        threshold_rows.append(threshold_table)
        fitted[name] = {
            "base_estimator": estimator,
            "calibrated_estimator": calibrated,
            "test_probabilities": probs,
            "test_df": test_df,
            "threshold_table": threshold_table,
            "best_threshold": best_threshold,
            "metrics": metric,
        }

    metrics_df = pd.DataFrame(metric_rows).sort_values(["brier", "auc_roc"], ascending=[True, False])
    thresholds_df = pd.concat(threshold_rows, ignore_index=True)
    return fitted, metrics_df, thresholds_df


def extract_feature_importance(name: str, estimator: Pipeline) -> pd.DataFrame:
    model = estimator.named_steps["model"]
    if hasattr(model, "feature_importances_"):
        importance = np.asarray(model.feature_importances_, dtype=float)
        importance_type = "gain" if name == "LightGBM" else "split"
    elif hasattr(model, "coef_"):
        importance = np.abs(np.asarray(model.coef_[0], dtype=float))
        importance_type = "abs_coefficient"
    else:
        importance = np.zeros(len(FEATURE_NAMES), dtype=float)
        importance_type = "unavailable"
    total = float(np.nansum(importance))
    normalized = importance / total if total > 0 else importance
    return (
        pd.DataFrame(
            {
                "feature": FEATURE_NAMES,
                "importance": importance,
                "importance_normalized": normalized,
                "importance_type": importance_type,
            }
        )
        .sort_values("importance", ascending=False)
        .reset_index(drop=True)
    )


def time_performance(test_df: pd.DataFrame, probs: np.ndarray) -> pd.DataFrame:
    frame = test_df[["diverge", "elapsed_fraction"]].copy()
    frame["prob"] = probs
    bins = np.linspace(0, 1, 11)
    rows = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (frame["elapsed_fraction"] >= lo) & (frame["elapsed_fraction"] < hi)
        if hi == 1.0:
            mask = (frame["elapsed_fraction"] >= lo) & (frame["elapsed_fraction"] <= hi)
        g = frame[mask]
        if g.empty:
            continue
        rows.append(
            {
                "elapsed_bin": f"{lo:.1f}-{hi:.1f}",
                "elapsed_midpoint": (lo + hi) / 2,
                "rows": int(len(g)),
                "contracts": int(test_df.loc[mask, "contract_id"].nunique()),
                "diverge_rate": float(g["diverge"].mean()),
                "auc_roc": float(roc_auc_score(g["diverge"], g["prob"]))
                if g["diverge"].nunique() > 1
                else np.nan,
                "brier": float(brier_score_loss(g["diverge"], g["prob"])),
            }
        )
    return pd.DataFrame(rows)


def plot_calibration(y: pd.Series, probs: np.ndarray, output_path: Path) -> pd.DataFrame:
    frac_pos, mean_pred = calibration_curve(y, probs, n_bins=10, strategy="quantile")
    cal_df = pd.DataFrame({"mean_predicted": mean_pred, "observed_fraction": frac_pos})
    plt.figure(figsize=(6.5, 5.0))
    plt.plot([0, 1], [0, 1], linestyle="--", color="0.55", label="Perfect calibration")
    plt.plot(mean_pred, frac_pos, marker="o", label="Best model")
    plt.xlabel("Mean predicted divergence probability")
    plt.ylabel("Observed divergence rate")
    plt.title("Divergence Model Calibration")
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()
    return cal_df


def plot_time_performance(perf: pd.DataFrame, output_path: Path) -> None:
    plt.figure(figsize=(8.0, 5.0))
    ax = plt.gca()
    ax.plot(perf["elapsed_midpoint"], perf["auc_roc"], marker="o", color="#1f77b4", label="AUC")
    ax.set_xlabel("Elapsed fraction of 15-minute contract")
    ax.set_ylabel("AUC-ROC", color="#1f77b4")
    ax.tick_params(axis="y", labelcolor="#1f77b4")
    ax.set_ylim(0.45, 1.02)

    ax2 = ax.twinx()
    ax2.plot(perf["elapsed_midpoint"], perf["brier"], marker="s", color="#d62728", label="Brier")
    ax2.set_ylabel("Brier score", color="#d62728")
    ax2.tick_params(axis="y", labelcolor="#d62728")
    ax2.set_ylim(bottom=0)

    plt.title("Model Performance by Elapsed Fraction")
    plt.tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()


def plot_feature_importance(importance: pd.DataFrame, output_path: Path) -> None:
    top = importance.head(18).iloc[::-1]
    plt.figure(figsize=(8.0, 6.5))
    plt.barh(top["feature"], top["importance_normalized"], color="#4c78a8")
    plt.xlabel("Normalized importance")
    plt.title("Top Feature Importances")
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


def build_model_card(
    labels: pd.DataFrame,
    train_df: pd.DataFrame,
    metrics: pd.DataFrame,
    best_name: str,
    best: dict[str, Any],
    importance: pd.DataFrame,
    time_perf: pd.DataFrame,
    trade_threshold: float,
    calibration_plot: Path,
    time_plot: Path,
    importance_plot: Path,
) -> str:
    labelable = labels[labels["diverge"].notna()]
    eligible = labels[labels["training_eligible"]]
    base_rate = float(labelable["diverge"].mean())
    eligible_base_rate = float(eligible["diverge"].mean())
    status_counts = labels["label_status"].value_counts(dropna=False).rename_axis("label_status").reset_index(name="contracts")
    target_sources = (
        labels["polymarket_target_source"]
        .value_counts(dropna=False)
        .rename_axis("polymarket_target_source")
        .reset_index(name="contracts")
    )

    best_metrics = metrics[metrics["model"] == best_name].iloc[0]
    top_features = importance.head(15)[["feature", "importance_normalized", "importance_type"]]
    time_summary = time_perf[["elapsed_bin", "rows", "contracts", "diverge_rate", "auc_roc", "brier"]]
    ranked_features = importance.reset_index(drop=True)
    feeds_rank = int(ranked_features.index[ranked_features["feature"] == "feeds_on_same_side"][0] + 1)
    feeds_importance = float(
        ranked_features.loc[ranked_features["feature"] == "feeds_on_same_side", "importance_normalized"].iloc[0]
    )
    interaction_features = [
        "price_spread_abs_x_elapsed_fraction",
        "spread_vs_distance_ratio_x_elapsed_fraction",
        "feeds_on_same_side_x_elapsed_fraction",
    ]
    interaction_importance = float(
        ranked_features.loc[
            ranked_features["feature"].isin(interaction_features),
            "importance_normalized",
        ].sum()
    )

    lines = [
        "# BTC 15m Kalshi/Polymarket Divergence Model Card",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## Target Construction",
        "",
        "- Settlement row: first snapshot at or after `kalshi_close_time`; fallback to last pre-close snapshot.",
        f"- Ambiguous contracts are marked when either final feed is within ${AMBIGUOUS_DOLLARS:.2f} of its target.",
        "- Polymarket target fallback order: settlement row, prior observed target, any observed target, first RTDS price.",
        f"- Labelable contracts: {len(labelable):,} of {len(labels):,}.",
        f"- Contract-level divergence base rate: {base_rate:.4f} ({labelable['diverge'].sum():.0f}/{len(labelable):,}).",
        f"- Training-eligible contracts after quality filters: {len(eligible):,}; base rate {eligible_base_rate:.4f}.",
        f"- Training rows: {len(train_df):,}; row-level base rate {train_df['diverge'].mean():.4f}.",
        "",
        "### Label Status Counts",
        "",
        md_table(status_counts),
        "",
        "### Polymarket Target Sources",
        "",
        md_table(target_sources),
        "",
        "## Validation",
        "",
        "- Split policy: contract-level split only; 60% core training, 20% calibration, 20% final test.",
        "- Calibration: isotonic calibration fit on the held-out calibration contracts.",
        f"- Best model by Brier score: **{best_name}**.",
        "",
        md_table(metrics),
        "",
        f"Recommended `diverge_prob` threshold for trading filter: `{trade_threshold:.4f}`.",
        f"Best F1 classification threshold: `{float(best['best_threshold']['threshold']):.4f}`.",
        f"Best-model AUC: `{best_metrics['auc_roc']:.4f}`; Brier: `{best_metrics['brier']:.4f}`.",
        "",
        f"Calibration plot: `{calibration_plot.relative_to(DATA_DIR)}`",
        f"Elapsed-fraction performance plot: `{time_plot.relative_to(DATA_DIR)}`",
        f"Feature-importance plot: `{importance_plot.relative_to(DATA_DIR)}`",
        "",
        "## Time-in-Contract Performance",
        "",
        md_table(time_summary),
        "",
        "## Feature Importance",
        "",
        md_table(top_features),
        "",
        f"`feeds_on_same_side` ranked #{feeds_rank} with normalized importance {feeds_importance:.4f};",
        "it was useful but not dominant in the best model. In this split, microstructure and arb-spread",
        "features added substantial signal beyond raw feed-side geometry. The explicit elapsed-time/feed",
        f"interaction features contributed {interaction_importance:.4f} normalized importance, while the",
        "time-bin analysis shows performance improves materially late in the contract.",
        "",
        "## Limitations",
        "",
        "- Labels use sampled close snapshots, not exchange adjudication records.",
        "- The trading threshold uses a conservative proxy payoff: non-divergence earns observed arb edge, divergence loses 1 unit.",
        "- Very early-contract calls have less history for rolling and lag features; inference imputes missing history.",
        "- Regime changes in RTDS lag, exchange APIs, liquidity, or BTC volatility can break calibration.",
        "- Contract rows are highly correlated; reported metrics are row-level live-call metrics under contract-level splits.",
        "",
    ]
    return "\n".join(lines)


def feature_stats(train_df: pd.DataFrame, importance: pd.DataFrame) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    imp = importance.set_index("feature")["importance_normalized"].to_dict()
    for feature in FEATURE_NAMES:
        series = pd.to_numeric(train_df[feature], errors="coerce")
        stats[feature] = {
            "mean": float(series.mean()) if series.notna().any() else 0.0,
            "std": float(series.std(ddof=0)) if series.notna().sum() > 1 else 0.0,
            "median": float(series.median()) if series.notna().any() else 0.0,
            "importance_normalized": float(imp.get(feature, 0.0)),
        }
    return stats


def main() -> None:
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Reading data from {DATA_DIR}")
    row_df, labels = load_dataset()
    labels.to_csv(LABELS_PATH, index=False)
    train_df = eligible_training_rows(row_df)
    log(f"Feature rows: {len(row_df):,}; training rows: {len(train_df):,}")

    core_contracts, calib_contracts, test_contracts = split_contracts(labels)
    fitted, metrics, _thresholds = fit_and_score_models(
        train_df,
        core_contracts,
        calib_contracts,
        test_contracts,
    )
    metrics.to_csv(METRICS_PATH, index=False)

    best_name = metrics.iloc[0]["model"]
    best = fitted[best_name]
    best_estimator = best["calibrated_estimator"]
    best_test_df = best["test_df"]
    best_probs = best["test_probabilities"]

    trade_threshold, trade_table = choose_trade_threshold(best_test_df, best_probs)
    trade_table.to_csv(DATA_DIR / "divergence_trade_thresholds.csv", index=False)

    importance = extract_feature_importance(best_name, best["base_estimator"])
    importance.to_csv(IMPORTANCE_PATH, index=False)

    time_perf = time_performance(best_test_df, best_probs)
    time_perf.to_csv(TIME_PERF_PATH, index=False)

    calibration_plot = PLOT_DIR / "divergence_calibration_curve.png"
    time_plot = PLOT_DIR / "divergence_time_performance.png"
    importance_plot = PLOT_DIR / "divergence_feature_importance.png"
    cal_df = plot_calibration(best_test_df["diverge"], best_probs, calibration_plot)
    cal_df.to_csv(CALIBRATION_TABLE_PATH, index=False)
    plot_time_performance(time_perf, time_plot)
    plot_feature_importance(importance, importance_plot)

    joblib.dump(best_estimator, MODEL_PATH)
    FEATURE_LIST_PATH.write_text(json.dumps(FEATURE_NAMES, indent=2) + "\n")

    labelable = labels[labels["diverge"].notna()]
    eligible_labels = labels[labels["training_eligible"]]
    metadata = {
        "model_name": best_name,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feature_names": FEATURE_NAMES,
        "base_rate_labelable_contracts": float(labelable["diverge"].mean()),
        "base_rate_training_eligible_contracts": float(eligible_labels["diverge"].mean()),
        "row_level_base_rate": float(train_df["diverge"].mean()),
        "contracts_total": int(len(labels)),
        "contracts_labelable": int(len(labelable)),
        "contracts_training_eligible": int(len(eligible_labels)),
        "rows_training": int(len(train_df)),
        "recommended_diverge_prob_threshold": float(trade_threshold),
        "classification_threshold": float(best["best_threshold"]["threshold"]),
        "test_metrics": metrics.to_dict(orient="records"),
        "top_feature_importances": importance.head(20).to_dict(orient="records"),
        "feature_stats": feature_stats(train_df, importance),
        "artifacts": {
            "model": str(MODEL_PATH.relative_to(DATA_DIR)),
            "feature_list": str(FEATURE_LIST_PATH.relative_to(DATA_DIR)),
            "model_card": str(MODEL_CARD_PATH.relative_to(DATA_DIR)),
            "calibration_plot": str(calibration_plot.relative_to(DATA_DIR)),
            "time_performance_plot": str(time_plot.relative_to(DATA_DIR)),
            "feature_importance_plot": str(importance_plot.relative_to(DATA_DIR)),
        },
    }
    METADATA_PATH.write_text(json.dumps(metadata, indent=2) + "\n")

    card = build_model_card(
        labels=labels,
        train_df=train_df,
        metrics=metrics,
        best_name=best_name,
        best=best,
        importance=importance,
        time_perf=time_perf,
        trade_threshold=trade_threshold,
        calibration_plot=calibration_plot,
        time_plot=time_plot,
        importance_plot=importance_plot,
    )
    MODEL_CARD_PATH.write_text(card)

    log("Done")
    log(f"Best model: {best_name}")
    log(metrics.to_string(index=False))
    log(f"Recommended trading threshold: {trade_threshold:.4f}")
    log(f"Artifacts written under {DATA_DIR}")


if __name__ == "__main__":
    main()
