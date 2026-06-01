#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.analyze_profit_margin_latch_2m_1m import (  # noqa: E402
    HORIZON_DIR,
    build_states,
    load_predictions,
    md_table,
    split_contracts,
)
from scripts.backtest_latch_2m_1m_selected_margins import summarize  # noqa: E402


MARGINS = np.round(np.arange(0.01, 1.00, 0.01), 2)
OUT_SUMMARY = HORIZON_DIR / "latch_2m_1m_margin_sweep_001_099.csv"
OUT_REPORT = HORIZON_DIR / "latch_2m_1m_margin_sweep_001_099_report.md"
OUT_PLOT = HORIZON_DIR / "plots" / "latch_2m_1m_margin_sweep_001_099_key_metrics.png"


def plot_metrics(summary: pd.DataFrame) -> None:
    frame = summary[summary["sample"].eq("all")].sort_values("profit_margin").copy()
    x = frame["profit_margin"].to_numpy()

    fig, axes = plt.subplots(3, 2, figsize=(14, 12), sharex=True)
    axes = axes.ravel()

    y = frame["expected_profit_per_15m_contract"].to_numpy()
    axes[0].plot(x, y, color="#2563eb", lw=2)
    axes[0].fill_between(
        x,
        frame["profit_per_15m_contract_ci_low"].to_numpy(),
        frame["profit_per_15m_contract_ci_high"].to_numpy(),
        color="#93c5fd",
        alpha=0.35,
        linewidth=0,
    )
    axes[0].set_ylabel("profit / all contracts")
    axes[0].set_title("Profit Per 15m Contract")

    axes[1].plot(x, frame["approved_trades"].to_numpy(), color="#059669", lw=2)
    axes[1].set_ylabel("approved trades")
    axes[1].set_title("Approved Trades")

    y = frame["expected_minutes_between_trades"].replace([np.inf, -np.inf], np.nan).to_numpy()
    axes[2].plot(x, y, color="#7c3aed", lw=2)
    axes[2].fill_between(
        x,
        frame["minutes_between_trades_ci_low"].replace([np.inf, -np.inf], np.nan).to_numpy(),
        frame["minutes_between_trades_ci_high"].replace([np.inf, -np.inf], np.nan).to_numpy(),
        color="#c4b5fd",
        alpha=0.35,
        linewidth=0,
    )
    axes[2].set_yscale("log")
    axes[2].set_ylabel("minutes / trade")
    axes[2].set_title("Expected Time Between Trades")

    axes[3].plot(x, frame["divergent_trades"].to_numpy(), color="#dc2626", lw=2)
    axes[3].set_ylabel("divergent trades")
    axes[3].set_title("Divergent Approved Trades")

    y = frame["expected_entry_price"].to_numpy()
    axes[4].plot(x, y, color="#d97706", lw=2)
    axes[4].fill_between(
        x,
        frame["entry_price_ci_low"].to_numpy(),
        frame["entry_price_ci_high"].to_numpy(),
        color="#fed7aa",
        alpha=0.45,
        linewidth=0,
    )
    axes[4].set_ylabel("all-in entry price")
    axes[4].set_title("Expected Entry Price")

    axes[5].axis("off")
    for ax in axes[:5]:
        ax.grid(True, alpha=0.25)
        ax.set_xlabel("profit margin")
    fig.suptitle("latch_2m_1m backtest: key metrics vs profit margin", fontsize=15)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    OUT_PLOT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PLOT, dpi=160)
    plt.close(fig)


def main() -> None:
    labels = pd.read_csv(HORIZON_DIR / "horizon_contract_labels.csv")
    predictions, thresholds = load_predictions()
    states = build_states(predictions, split_contracts(labels)["all"])

    rows = []
    for margin in MARGINS:
        row, _trades = summarize(states, "all", float(margin))
        rows.append(row)
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_SUMMARY, index=False)
    plot_metrics(summary)

    best = summary.sort_values(
        ["expected_profit_per_15m_contract", "expected_profit_per_traded_contract", "approved_trades"],
        ascending=[False, False, False],
    ).iloc[0]

    display = summary[
        [
            "profit_margin",
            "contracts_evaluated",
            "model_signal_contracts",
            "approved_trades",
            "approved_trade_rate",
            "expected_minutes_between_trades",
            "divergent_trades",
            "divergence_rate",
            "expected_entry_price",
            "expected_profit_per_traded_contract",
            "expected_profit_per_15m_contract",
            "total_profit",
        ]
    ].copy()

    report = [
        "# Backtest Sweep: `latch_2m_1m` Profit Margins `$0.01` To `$0.99`",
        "",
        "## Scope",
        "",
        "- Data/artifacts: `kp-0529-research`.",
        "- Strategy: current `latch_2m_1m` using the current horizon models.",
        f"- Thresholds: `2m < {thresholds['2m']:.4f}`, `1m < {thresholds['1m']:.4f}`.",
        "- Margins swept in `$0.01` increments from `$0.01` through `$0.99`.",
        "- Entry rule: after the first passing latch model, enter at the first historical row where `best_all_in_cost < 1 - profit_margin`.",
        "- Divergence return rule: divergent trades lose the full all-in entry cost; non-divergent trades settle at `1.00`.",
        "- Confidence intervals in the CSV/plot are contract-level bootstrap 95% intervals.",
        "",
        "## Best Margin By Profit / All Contracts",
        "",
        f"The best margin in this sweep is `${float(best['profit_margin']):.2f}` with profit / all contracts `{float(best['expected_profit_per_15m_contract']):.4f}`.",
        "",
        "## Full Sweep",
        "",
        md_table(display),
        "",
        "## Plot",
        "",
        f"![Key metrics vs profit margin]({OUT_PLOT.relative_to(HORIZON_DIR)})",
        "",
        "## Output Files",
        "",
        f"- Sweep CSV: `{OUT_SUMMARY.relative_to(ROOT)}`",
        f"- Plot: `{OUT_PLOT.relative_to(ROOT)}`",
        f"- Report: `{OUT_REPORT.relative_to(ROOT)}`",
    ]
    OUT_REPORT.write_text("\n".join(report) + "\n")
    print(f"Wrote {OUT_SUMMARY}")
    print(f"Wrote {OUT_PLOT}")
    print(f"Wrote {OUT_REPORT}")


if __name__ == "__main__":
    main()
