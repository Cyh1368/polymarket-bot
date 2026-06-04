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
    "trades": "n_traded",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive 3D scatter for Kalshi more-likely strategy optimization."
    )
    parser.add_argument("--grid", type=Path, default=DEFAULT_GRID, help="Optimization grid CSV.")
    parser.add_argument(
        "--metric",
        choices=sorted(METRICS),
        default="ev",
        help="Value used for color. Default: ev.",
    )
    parser.add_argument("--min-trades", type=int, default=0, help="Filter to rows with at least this many trades.")
    parser.add_argument("--top", type=int, default=0, help="Show only top N rows by selected metric. 0 means all.")
    parser.add_argument("--alpha", type=float, default=0.78, help="Marker alpha. Default: 0.78.")
    parser.add_argument("--save", type=Path, default=None, help="Optional path to save the current initial view.")
    parser.add_argument("--no-show", action="store_true", help="Save/prepare the plot but do not open a window.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metric_col = METRICS[args.metric]
    grid = pd.read_csv(args.grid)
    grid = grid[grid["n_traded"] >= args.min_trades].copy()
    if args.top > 0:
        grid = grid.sort_values(metric_col, ascending=False).head(args.top).copy()
    if grid.empty:
        raise SystemExit("No rows left after filtering.")

    values = grid[metric_col].to_numpy(dtype=float)
    trade_rate = grid["trade_rate"].to_numpy(dtype=float)
    sizes = 12 + 110 * np.clip(trade_rate, 0.0, 1.0)

    fig = plt.figure(figsize=(13, 9))
    ax = fig.add_subplot(111, projection="3d")
    scatter = ax.scatter(
        grid["t_minutes"],
        grid["band_low"],
        grid["band_high"],
        c=values,
        s=sizes,
        cmap="RdYlGn",
        alpha=args.alpha,
        linewidths=0,
        picker=True,
    )
    ax.set_title(f"Kalshi strategy 3D grid: {metric_col}", fontsize=14, fontweight="bold")
    ax.set_xlabel("T before close (minutes)")
    ax.set_ylabel("Band low")
    ax.set_zlabel("Band high")
    fig.colorbar(scatter, ax=ax, shrink=0.7, pad=0.08, label=metric_col)

    labels = grid["label"].tolist()
    annotations = [
        (
            f"{labels[i]}\n"
            f"{metric_col}={values[i]:.5f}\n"
            f"trades={int(grid.iloc[i]['n_traded'])}, trade_rate={grid.iloc[i]['trade_rate']:.2%}"
        )
        for i in range(len(grid))
    ]

    def on_pick(event) -> None:
        if event.artist is not scatter:
            return
        for idx in event.ind:
            print(annotations[int(idx)])

    fig.canvas.mpl_connect("pick_event", on_pick)
    fig.tight_layout()

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(args.save, dpi=180)
    if not args.no_show:
        print("Drag to rotate. Click a point to print its parameter set in the terminal.")
        plt.show()


if __name__ == "__main__":
    main()
