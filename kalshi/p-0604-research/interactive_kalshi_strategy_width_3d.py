#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
DEFAULT_GRID = ROOT / "kalshi_strategy_optimization_grid.csv"


METRICS = {
    "ev": "ev_per_available_contract_after_fee",
    "ci_low": "ci95_low_ev_all",
    "stability": "stability_score",
    "boot_positive": "bootstrap_prob_ev_positive",
    "winner_freq": "bootstrap_raw_winner_frequency",
}


def parse_widths(raw: str) -> list[float]:
    widths: list[float] = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            widths.append(float(item))
    return widths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive 3D width-slice plot for Kalshi strategy optimization."
    )
    parser.add_argument("--grid", type=Path, default=DEFAULT_GRID, help="Optimization grid CSV.")
    parser.add_argument(
        "--metric",
        choices=sorted(METRICS),
        default="ev",
        help="Y-axis value for the 3D slices. Default: ev.",
    )
    parser.add_argument(
        "--widths",
        default="0.05,0.10,0.20,0.30,0.50",
        help="Comma-separated band widths to plot. Default: 0.05,0.10,0.20,0.30,0.50.",
    )
    parser.add_argument("--min-trades", type=int, default=0, help="Filter to rows with at least this many trades.")
    parser.add_argument("--save", type=Path, default=None, help="Optional path to save the current initial view.")
    parser.add_argument("--no-show", action="store_true", help="Save/prepare the plot but do not open a window.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metric_col = METRICS[args.metric]
    widths = parse_widths(args.widths)
    grid = pd.read_csv(args.grid)
    grid = grid[grid["n_traded"] >= args.min_trades].copy()
    if grid.empty:
        raise SystemExit("No rows left after filtering.")

    fig = plt.figure(figsize=(13, 9))
    ax = fig.add_subplot(111, projection="3d")
    cmap = plt.get_cmap("tab10")

    plotted = 0
    for i, width in enumerate(widths):
        subset = grid[np.isclose(grid["band_width"], width)].copy()
        if subset.empty:
            continue
        subset = subset.sort_values(["band_center", "t_minutes"])
        ax.scatter(
            subset["t_minutes"],
            subset["band_center"],
            subset[metric_col],
            s=26,
            alpha=0.72,
            color=cmap(i % 10),
            label=f"width={width:.2f}",
            picker=True,
        )
        plotted += 1

    if plotted == 0:
        raise SystemExit("No selected widths exist after filtering.")

    ax.set_title(f"Kalshi strategy width slices: {metric_col}", fontsize=14, fontweight="bold")
    ax.set_xlabel("T before close (minutes)")
    ax.set_ylabel("Band center")
    ax.set_zlabel(metric_col)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(args.save, dpi=180)
    if not args.no_show:
        print("Drag to rotate the width slices.")
        plt.show()


if __name__ == "__main__":
    main()
