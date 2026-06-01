#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
HORIZON_DIR = ROOT / "kp-0529-research" / "horizon_models"
OUT_REPORT = HORIZON_DIR / "target_aware_horizon_model_card.md"
HORIZONS = ["10m", "5m", "3m", "2m", "1m"]
TARGET_AWARE_PREFIXES = (
    "polymarket_distance_to_own_target",
    "target_spread",
    "target_spread_abs",
    "feeds_on_same_side_own_targets",
    "price_between_targets",
)


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


def load_json(path: Path) -> dict:
    with path.open() as handle:
        return json.load(handle)


def feature_count(horizon: str) -> int:
    with (HORIZON_DIR / f"divergence_horizon_{horizon}_feature_list.json").open() as handle:
        return len(json.load(handle))


def target_aware_rank_table(horizon: str) -> pd.DataFrame:
    importance = pd.read_csv(HORIZON_DIR / f"divergence_horizon_{horizon}_feature_importance.csv")
    importance = importance.reset_index().rename(columns={"index": "rank"})
    importance["rank"] = importance["rank"] + 1
    rows = []
    for prefix in TARGET_AWARE_PREFIXES:
        if prefix == "target_spread":
            matched = importance[
                importance["feature"].str.startswith("target_spread_")
                & ~importance["feature"].str.startswith("target_spread_abs_")
            ].copy()
        else:
            matched = importance[importance["feature"].str.startswith(f"{prefix}_")].copy()
        if matched.empty:
            rows.append(
                {
                    "target_aware_family": prefix,
                    "best_rank": "",
                    "best_feature": "",
                    "importance_normalized": "",
                }
            )
            continue
        best = matched.iloc[0]
        rows.append(
            {
                "target_aware_family": prefix,
                "best_rank": int(best["rank"]),
                "best_feature": best["feature"],
                "importance_normalized": float(best["importance_normalized"]),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    metrics = pd.read_csv(HORIZON_DIR / "horizon_model_metrics.csv")
    labels = pd.read_csv(HORIZON_DIR / "horizon_contract_labels.csv")
    dataset = pd.read_csv(
        HORIZON_DIR / "horizon_aggregated_dataset.csv",
        usecols=[
            "horizon",
            "aggregation_status",
            "training_eligible_label",
            "diverge",
            "window_rows",
            "asof_gap_seconds",
        ],
    )

    label_counts = labels["label_status"].value_counts(dropna=False).rename_axis("label_status").reset_index(name="contracts")
    eligible = labels[labels["training_eligible"].astype(bool) & labels["diverge"].notna()].copy()
    eligible_base_rate = float(eligible["diverge"].mean())

    aggregation_counts = (
        dataset.groupby(["horizon", "aggregation_status"], dropna=False)
        .size()
        .reset_index(name="contracts")
        .sort_values(["horizon", "aggregation_status"])
    )
    horizon_summary = (
        dataset[dataset["training_eligible_label"].astype(bool) & dataset["aggregation_status"].eq("ok")]
        .groupby("horizon")
        .agg(
            contracts=("diverge", "count"),
            divergences=("diverge", "sum"),
            base_rate=("diverge", "mean"),
            mean_window_rows=("window_rows", "mean"),
            median_asof_gap_seconds=("asof_gap_seconds", "median"),
        )
        .reset_index()
        .sort_values("horizon")
    )

    metrics_display = metrics[
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
    ].copy()
    metrics_display["feature_count"] = metrics_display["horizon"].map(lambda h: feature_count(str(h)))

    coverage_display = metrics[
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
            "trade_threshold_pass_mean_all_in_cost",
            "trade_threshold_pass_mean_predicted_diverge_prob",
            "trade_threshold_pass_expected_return",
            "trade_threshold_pass_test_return",
        ]
    ].copy()

    report: list[str] = [
        "# Target-Aware Horizon Model Card",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## What Changed",
        "",
        "- The live feature path now recomputes Kalshi and Polymarket `yes_mid` from bid/ask before model aggregation, preventing stale websocket mids from entering `polymarket_yes_mid_*` and `implied_prob_spread_*`.",
        "- Horizon models were retrained with target-aware features so Polymarket geometry is measured against the Polymarket target, not only the Kalshi target.",
        "- New feature families include `polymarket_distance_to_own_target`, `target_spread`, `target_spread_abs`, `feeds_on_same_side_own_targets`, and `price_between_targets`.",
        "- Existing legacy features such as `polymarket_distance_to_target` and `feeds_on_same_side` remain in the feature set for continuity, but the model can now learn the own-target geometry explicitly.",
        "",
        "## Dataset And Label Quality",
        "",
        f"- Labelable contracts: `{labels['diverge'].notna().sum()}` of `{len(labels)}`.",
        f"- Training-eligible contracts: `{len(eligible)}`; eligible base divergence rate: `{eligible_base_rate:.4f}`.",
        "- Horizon rows aggregate exactly the trailing one-minute window ending at the horizon decision time, using a 2-second previous-tick sampling grid where possible.",
        "- Split policy remains contract-level: 60% core training, 20% calibration, 20% final test.",
        "",
        "### Label Status Counts",
        "",
        md_table(label_counts),
        "",
        "### Aggregation Status Counts",
        "",
        md_table(aggregation_counts),
        "",
        "### Horizon Dataset Summary",
        "",
        md_table(horizon_summary),
        "",
        "## Final Test Metrics",
        "",
        md_table(metrics_display),
        "",
        "## Trading Threshold Coverage",
        "",
        "A contract is tradable when one buy-side combination has positive fee-adjusted edge: `raw_combo_cost + Kalshi_fee + Polymarket_fee < 1.0`. `Expected return` uses mean predicted divergence probability; `Test return` uses actual held-out divergence results.",
        "",
        md_table(coverage_display),
        "",
        "## Per-Horizon Notes",
        "",
    ]

    for horizon in HORIZONS:
        meta = load_json(HORIZON_DIR / f"divergence_horizon_{horizon}_metadata.json")
        metric = metrics[metrics["horizon"].astype(str).eq(horizon)].iloc[0]
        importance = pd.read_csv(HORIZON_DIR / f"divergence_horizon_{horizon}_feature_importance.csv").head(15)
        report.extend(
            [
                f"### {horizon}",
                "",
                f"- Model artifact: `divergence_horizon_{horizon}_model.pkl`.",
                f"- Feature list: `divergence_horizon_{horizon}_feature_list.json`; feature count `{feature_count(horizon)}`.",
                f"- Test AUC `{metric['auc_roc']:.4f}`, Brier `{metric['brier']:.4f}`, F1 `{metric['f1']:.4f}`.",
                f"- Trading threshold: `diverge_prob < {metric['recommended_trade_threshold']:.4f}`.",
                f"- Calibration plot: `{meta['artifacts']['calibration_plot']}`.",
                f"- Feature-importance plot: `{meta['artifacts']['feature_importance_plot']}`.",
                "",
                "Top features:",
                "",
                md_table(importance[["feature", "importance_normalized", "importance_type"]]),
                "",
                "Best-ranked target-aware feature families:",
                "",
                md_table(target_aware_rank_table(horizon)),
                "",
            ]
        )

    report.extend(
        [
            "## Operational Interpretation",
            "",
            "- The later horizons are materially stronger. The 2m and 1m models have the highest held-out AUC and the best trade-filter divergence separation.",
            "- The target-aware features now appear in the feature lists and in the ranked coefficient table, so the model can explicitly react to target spread and whether each feed is on the same side of its own target.",
            "- The new 2m threshold remains `0.0788`; the new 1m threshold is `0.1329`, slightly lower than the previous `0.1378` after target-aware retraining.",
            "- These reports still assume historical quoted prices are executable. They do not model live order failures, minimum notional constraints, or stale ask-side liquidity beyond the CSV quotes.",
            "",
            "## Artifacts",
            "",
            "- Combined model card: `combined_horizon_model_card.md`.",
            "- Metrics table: `horizon_model_metrics.csv`.",
            "- Aggregated dataset: `horizon_aggregated_dataset.csv`.",
            "- Per-horizon model artifacts: `divergence_horizon_{horizon}_model.pkl`.",
        ]
    )

    OUT_REPORT.write_text("\n".join(report) + "\n")
    print(f"Wrote {OUT_REPORT}")


if __name__ == "__main__":
    main()
