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

from scripts.analyze_profit_margin_latch_2m_1m import (
    DATA_DIR,
    HORIZON_DIR,
    MARGINS,
    PLOT_DIR,
    build_states,
    latch_interval,
    load_predictions,
    md_table,
    read_contract_opportunities,
    split_contracts,
)


OUT_CSV = HORIZON_DIR / "profit_margin_divergence_probability.csv"
OUT_REPORT = HORIZON_DIR / "profit_margin_divergence_probability_report.md"
OUT_PLOT = PLOT_DIR / "profit_margin_divergence_probability.png"


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return math.nan, math.nan
    p = successes / trials
    denom = 1.0 + z * z / trials
    center = (p + z * z / (2.0 * trials)) / denom
    half = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * trials)) / trials) / denom
    return max(0.0, center - half), min(1.0, center + half)


def max_edge_for_rows(rows: pd.DataFrame) -> float:
    if rows.empty:
        return math.nan
    edges = 1.0 - pd.to_numeric(rows["best_all_in_cost"], errors="coerce")
    if edges.dropna().empty:
        return math.nan
    return float(edges.max())


def full_contract_edges(labels: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    eligible = labels[labels["training_eligible"]].dropna(subset=["diverge"]).copy()
    for record in eligible.to_dict("records"):
        opportunities = read_contract_opportunities(str(record["source_file"]))
        close_time = pd.Timestamp(opportunities["close_time"].dropna().iloc[0]) if not opportunities.empty else pd.NaT
        in_contract = opportunities[opportunities["timestamp"] <= close_time] if pd.notna(close_time) else opportunities
        rows.append(
            {
                "contract_id": str(record["contract_id"]),
                "diverge": int(record["diverge"]),
                "max_edge": max_edge_for_rows(in_contract),
            }
        )
    return pd.DataFrame(rows).dropna(subset=["max_edge"])


def latch_window_edges(labels: pd.DataFrame) -> pd.DataFrame:
    contract_sets = split_contracts(labels)
    predictions, _thresholds = load_predictions()
    states = build_states(predictions, contract_sets["all"])
    rows: list[dict[str, Any]] = []
    for state in states.values():
        latched = latch_interval(state)
        if latched is None:
            max_edge = math.nan
            latch_horizon = ""
        else:
            start, latch_horizon = latched
            window = state.opportunities[
                (state.opportunities["timestamp"] >= start)
                & (state.opportunities["timestamp"] <= state.close_time)
            ]
            max_edge = max_edge_for_rows(window)
        rows.append(
            {
                "contract_id": state.contract_id,
                "diverge": int(state.diverge),
                "max_edge": max_edge,
                "latch_horizon": latch_horizon,
            }
        )
    return pd.DataFrame(rows)


def summarize(edges: pd.DataFrame, sample: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    universe = len(edges)
    base_divergences = int(edges["diverge"].sum())
    base_rate = base_divergences / universe if universe else math.nan
    for margin in MARGINS:
        occurred = edges[edges["max_edge"].notna() & (edges["max_edge"] >= float(margin))]
        if occurred.empty:
            continue
        divergences = int(occurred["diverge"].sum())
        count = int(len(occurred))
        probability = divergences / count
        ci_low, ci_high = wilson_interval(divergences, count)
        rows.append(
            {
                "sample": sample,
                "profit_margin": float(margin),
                "profit_margin_cents": int(round(float(margin) * 100)),
                "universe_contracts": int(universe),
                "base_divergences": base_divergences,
                "base_divergence_rate": base_rate,
                "occurrence_contracts": count,
                "occurrence_rate": count / universe if universe else math.nan,
                "divergent_occurrence_contracts": divergences,
                "p_diverge_given_margin_occurred": probability,
                "wilson_ci_low": ci_low,
                "wilson_ci_high": ci_high,
            }
        )
    return pd.DataFrame(rows)


def plot_probability(summary: pd.DataFrame) -> None:
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    fig, (ax_prob, ax_count) = plt.subplots(
        2,
        1,
        figsize=(11, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [2.2, 1.0]},
    )
    colors = {"full_contract": "#2563eb", "latch_2m_1m_window": "#dc2626"}
    labels = {
        "full_contract": "Full contract",
        "latch_2m_1m_window": "Latch 2m/1m window",
    }
    for sample in ["full_contract", "latch_2m_1m_window"]:
        frame = summary[summary["sample"].eq(sample)].sort_values("profit_margin")
        if frame.empty:
            continue
        x = frame["profit_margin_cents"].to_numpy()
        y = frame["p_diverge_given_margin_occurred"].to_numpy()
        yerr = np.vstack(
            [
                np.maximum(0.0, y - frame["wilson_ci_low"].to_numpy()),
                np.maximum(0.0, frame["wilson_ci_high"].to_numpy() - y),
            ]
        )
        ax_prob.errorbar(
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
            label=labels[sample],
        )
        ax_count.plot(
            x,
            frame["occurrence_contracts"].to_numpy(),
            "-o",
            markersize=3,
            linewidth=1.5,
            alpha=0.85,
            color=colors[sample],
            label=labels[sample],
        )
    ax_prob.set_ylabel("P(diverge | margin occurred)")
    ax_prob.set_ylim(0, 1)
    ax_prob.grid(True, alpha=0.25)
    ax_prob.legend()
    ax_count.set_xlabel("Profit margin threshold, cents")
    ax_count.set_ylabel("Contracts")
    ax_count.grid(True, alpha=0.25)
    fig.suptitle("Outcome discrepancy probability conditional on observed arbitrage margin")
    fig.tight_layout()
    fig.savefig(OUT_PLOT, dpi=170)
    plt.close(fig)


def main() -> None:
    labels = pd.read_csv(HORIZON_DIR / "horizon_contract_labels.csv")
    full_edges = full_contract_edges(labels)
    latch_edges = latch_window_edges(labels)
    summary = pd.concat(
        [
            summarize(full_edges, "full_contract"),
            summarize(latch_edges, "latch_2m_1m_window"),
        ],
        ignore_index=True,
    )
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(OUT_CSV, index=False)
    plot_probability(summary)

    selected_cents = [0, 5, 10, 15, 18, 20, 25, 30, 40, 50]
    selected = summary[summary["profit_margin_cents"].isin(selected_cents)].copy()
    selected = selected[
        [
            "sample",
            "profit_margin_cents",
            "occurrence_contracts",
            "occurrence_rate",
            "divergent_occurrence_contracts",
            "p_diverge_given_margin_occurred",
            "wilson_ci_low",
            "wilson_ci_high",
        ]
    ]
    max_rows = summary.loc[
        summary.groupby("sample")["profit_margin"].idxmax(),
        [
            "sample",
            "profit_margin_cents",
            "occurrence_contracts",
            "divergent_occurrence_contracts",
            "p_diverge_given_margin_occurred",
        ],
    ].copy()
    report = [
        "# Divergence Probability Conditional On Profit Margin Occurrence",
        "",
        "## Definition",
        "",
        "- `profit_margin_cents = x` means a fee-adjusted arbitrage edge of at least `x` cents appeared at least once.",
        "- Fee-adjusted edge is `1 - best_all_in_cost`, using the same odds-dependent Kalshi and Polymarket fee equations as the prior profit-margin sweep.",
        "- `full_contract` checks every available row up to contract close.",
        "- `latch_2m_1m_window` checks only rows after the current strategy first latches tradable via the 2m or 1m model. Contracts that never latch cannot satisfy this condition.",
        "- The probability reported is `P(contract diverged | edge >= x cents occurred at least once)`.",
        "",
        "## Selected Margins",
        "",
        md_table(selected),
        "",
        "## Highest Available Margin In Each Sample",
        "",
        md_table(max_rows),
        "",
        "## Plot",
        "",
        f"![Divergence probability conditional on margin occurrence]({OUT_PLOT.relative_to(HORIZON_DIR)})",
        "",
        "The upper panel is the conditional divergence probability with Wilson 95% intervals. The lower panel shows how many contracts satisfy the margin condition at each threshold; high-margin points with few contracts are noisy.",
        "",
        "## Output Files",
        "",
        f"- Full table: `{OUT_CSV.relative_to(HORIZON_DIR)}`",
        f"- Plot: `{OUT_PLOT.relative_to(HORIZON_DIR)}`",
    ]
    OUT_REPORT.write_text("\n".join(report) + "\n")
    print(f"Wrote {OUT_CSV}")
    print(f"Wrote {OUT_REPORT}")
    print(f"Wrote {OUT_PLOT}")


if __name__ == "__main__":
    main()
