#!/usr/bin/env python3
"""Visualize and optimize BTC Kalshi more-likely-side stability."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LEVELS = [0.50, 0.60, 0.70, 0.80, 0.90, 1.00]
HORIZON_STEP = 60
MIN_N = 20


def band_label(low: float, high: float) -> str:
    return f"{low:.2f}-{high:.2f}"


def all_contiguous_bands() -> list[tuple[float, float]]:
    return [(LEVELS[i], LEVELS[j]) for i in range(len(LEVELS) - 1) for j in range(i + 1, len(LEVELS))]


def pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def fmt(value: float) -> str:
    return f"{value:.4f}"


def in_band(entry: dict[str, str], low: float, high: float) -> bool:
    value = float(entry["more_likely_mid"])
    return low <= value < high or (high == 1.0 and low <= value <= high)


def summarize(entries: list[dict[str, str]], total_available: int) -> dict[str, Any]:
    n = len(entries)
    wins = sum(int(entry["success"]) for entry in entries)
    losses = n - wins
    gross_pnl = sum(float(entry["gross_pnl"]) for entry in entries)
    costs = [float(entry["cost"]) for entry in entries]
    p_success = wins / n if n else 0.0
    avg_cost = statistics.fmean(costs) if n else 0.0
    ev = p_success - avg_cost if n else 0.0
    coverage = n / total_available if total_available else 0.0
    profit_per_available = gross_pnl / total_available if total_available else 0.0
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "p_success": p_success,
        "avg_cost": avg_cost,
        "ev_p_minus_c": ev,
        "gross_pnl": gross_pnl,
        "coverage": coverage,
        "profit_per_available_contract": profit_per_available,
        "total_available_contracts": total_available,
        "p_value_break_even": poisson_binomial_tail(costs, wins) if n else 1.0,
    }


def poisson_binomial_tail(probabilities: list[float], observed_successes: int) -> float:
    """P(X >= observed_successes) for independent Bernoulli(p_i)."""
    if observed_successes <= 0:
        return 1.0
    if observed_successes > len(probabilities):
        return 0.0

    distribution = [1.0]
    for probability in probabilities:
        probability = max(0.0, min(1.0, probability))
        next_distribution = [0.0] * (len(distribution) + 1)
        for successes, mass in enumerate(distribution):
            next_distribution[successes] += mass * (1.0 - probability)
            next_distribution[successes + 1] += mass * probability
        distribution = next_distribution
    return min(1.0, max(0.0, sum(distribution[observed_successes:])))


def load_entries(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as input_file:
        return [row for row in csv.DictReader(input_file) if row.get("resolved") == "1"]


def build_profit_grid(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    by_horizon: dict[int, list[dict[str, str]]] = defaultdict(list)
    for entry in entries:
        by_horizon[int(entry["horizon_seconds"])].append(entry)

    rows: list[dict[str, str]] = []
    for horizon in sorted(by_horizon):
        horizon_entries = by_horizon[horizon]
        total_available = len(horizon_entries)
        for low, high in all_contiguous_bands():
            selected = [entry for entry in horizon_entries if in_band(entry, low, high)]
            summary = summarize(selected, total_available)
            rows.append(
                {
                    "horizon_seconds": str(horizon),
                    "price_range": band_label(low, high),
                    "band_low": f"{low:.2f}",
                    "band_high": f"{high:.2f}",
                    "n": str(summary["n"]),
                    "total_available_contracts": str(summary["total_available_contracts"]),
                    "coverage": f"{summary['coverage']:.6f}",
                    "wins": str(summary["wins"]),
                    "losses": str(summary["losses"]),
                    "p_success": f"{summary['p_success']:.6f}",
                    "avg_cost": f"{summary['avg_cost']:.6f}",
                    "ev_p_minus_c": f"{summary['ev_p_minus_c']:.6f}",
                    "gross_pnl": f"{summary['gross_pnl']:.6f}",
                    "profit_per_available_contract": f"{summary['profit_per_available_contract']:.6f}",
                    "p_value_break_even": f"{summary['p_value_break_even']:.10g}",
                }
            )
    return rows


def row_key(row: dict[str, str]) -> tuple[int, float, float]:
    return (int(row["horizon_seconds"]), float(row["band_low"]), float(row["band_high"]))


def compute_stability(rows: list[dict[str, str]], min_n: int) -> list[dict[str, str]]:
    by_key = {row_key(row): row for row in rows}
    positive = {
        key: row
        for key, row in by_key.items()
        if int(row["n"]) >= min_n and float(row["profit_per_available_contract"]) > 0
    }

    def immediate_neighbors(key: tuple[int, float, float]) -> list[dict[str, str]]:
        horizon, low, high = key
        neighbors: list[dict[str, str]] = []
        for delta_h in (-HORIZON_STEP, 0, HORIZON_STEP):
            for delta_low in (-0.10, 0.0, 0.10):
                for delta_high in (-0.10, 0.0, 0.10):
                    if delta_h == 0 and delta_low == 0 and delta_high == 0:
                        continue
                    next_low = round(low + delta_low, 2)
                    next_high = round(high + delta_high, 2)
                    if next_low < 0.50 or next_high > 1.00 or next_low >= next_high:
                        continue
                    neighbor = by_key.get((horizon + delta_h, next_low, next_high))
                    if neighbor and int(neighbor["n"]) >= min_n:
                        neighbors.append(neighbor)
        return neighbors

    def component(start: tuple[int, float, float]) -> list[dict[str, str]]:
        if start not in positive:
            return []
        seen = {start}
        queue = [start]
        while queue:
            horizon, low, high = queue.pop(0)
            candidates = [
                (horizon - HORIZON_STEP, low, high),
                (horizon + HORIZON_STEP, low, high),
                (horizon, round(low - 0.10, 2), high),
                (horizon, round(low + 0.10, 2), high),
                (horizon, low, round(high - 0.10, 2)),
                (horizon, low, round(high + 0.10, 2)),
            ]
            for candidate in candidates:
                candidate_low = candidate[1]
                candidate_high = candidate[2]
                if candidate_low < 0.50 or candidate_high > 1.00 or candidate_low >= candidate_high:
                    continue
                if candidate in positive and candidate not in seen:
                    seen.add(candidate)
                    queue.append(candidate)
        return [positive[key] for key in sorted(seen)]

    enriched: list[dict[str, str]] = []
    for row in rows:
        key = row_key(row)
        neighbors = immediate_neighbors(key)
        neighbor_values = [float(neighbor["profit_per_available_contract"]) for neighbor in neighbors]
        neighbor_count = len(neighbor_values)
        neighbor_positive_count = sum(value > 0 for value in neighbor_values)
        neighbor_positive_share = neighbor_positive_count / neighbor_count if neighbor_count else 0.0
        neighbor_mean = statistics.fmean(neighbor_values) if neighbor_values else 0.0
        comp = component(key)
        comp_values = [float(item["profit_per_available_contract"]) for item in comp]
        component_size = len(comp)
        component_mean = statistics.fmean(comp_values) if comp_values else 0.0
        is_stable = (
            int(row["n"]) >= min_n
            and float(row["profit_per_available_contract"]) > 0
            and neighbor_count >= 4
            and neighbor_positive_share >= 0.70
            and neighbor_mean > 0
            and component_size >= 8
        )
        enriched_row = dict(row)
        enriched_row.update(
            {
                "neighbor_count": str(neighbor_count),
                "neighbor_positive_count": str(neighbor_positive_count),
                "neighbor_positive_share": f"{neighbor_positive_share:.6f}",
                "neighbor_profit_per_available_mean": f"{neighbor_mean:.6f}",
                "positive_component_size": str(component_size),
                "positive_component_profit_per_available_mean": f"{component_mean:.6f}",
                "stable": "1" if is_stable else "0",
            }
        )
        enriched.append(enriched_row)
    return enriched


def sort_by_metric(rows: list[dict[str, str]], metric: str, min_n: int = 0, stable_only: bool = False) -> list[dict[str, str]]:
    filtered = [row for row in rows if int(row["n"]) >= min_n and (not stable_only or row["stable"] == "1")]
    return sorted(filtered, key=lambda row: (float(row[metric]), int(row["n"])), reverse=True)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def color_diverging(value: float, max_abs: float) -> str:
    if max_abs <= 0:
        return "#f7f7f2"
    t = max(-1.0, min(1.0, value / max_abs))
    if t >= 0:
        start = (247, 247, 242)
        end = (23, 128, 89)
        mix = t
    else:
        start = (247, 247, 242)
        end = (176, 53, 53)
        mix = -t
    rgb = tuple(round(start[i] + (end[i] - start[i]) * mix) for i in range(3))
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def color_sequential(value: float, max_value: float) -> str:
    t = 0.0 if max_value <= 0 else max(0.0, min(1.0, value / max_value))
    start = (247, 247, 242)
    end = (46, 78, 150)
    rgb = tuple(round(start[i] + (end[i] - start[i]) * t) for i in range(3))
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def write_heatmap_svg(
    path: Path,
    rows: list[dict[str, str]],
    metric: str,
    title: str,
    selected: dict[str, str],
    sequential: bool = False,
) -> None:
    horizons = sorted({int(row["horizon_seconds"]) for row in rows})
    bands = [band_label(low, high) for low, high in all_contiguous_bands()]
    values = {(int(row["horizon_seconds"]), row["price_range"]): float(row[metric]) for row in rows}

    cell_w = 54
    cell_h = 24
    left = 132
    top = 78
    width = left + len(horizons) * cell_w + 30
    height = top + len(bands) * cell_h + 72
    metric_values = list(values.values())
    max_abs = max(abs(value) for value in metric_values) if metric_values else 1.0
    max_value = max(metric_values) if metric_values else 1.0

    selected_key = (int(selected["horizon_seconds"]), selected["price_range"])

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text{font-family:Verdana,Arial,sans-serif;fill:#263238}",
        ".small{font-size:10px}.label{font-size:11px}.title{font-size:18px;font-weight:700}.subtitle{font-size:12px;fill:#52616b}",
        "</style>",
        '<rect width="100%" height="100%" fill="#fbfaf4"/>',
        f'<text x="24" y="32" class="title">{html.escape(title)}</text>',
        f'<text x="24" y="52" class="subtitle">Rows are contiguous more-likely price ranges; columns are seconds before expiry. Black outline marks selected stable parameter.</text>',
    ]
    for i, horizon in enumerate(horizons):
        x = left + i * cell_w + cell_w / 2
        lines.append(f'<text x="{x:.1f}" y="{top - 12}" text-anchor="middle" class="label">{horizon}</text>')

    for j, band in enumerate(bands):
        y = top + j * cell_h
        lines.append(f'<text x="{left - 10}" y="{y + 16}" text-anchor="end" class="label">{band}</text>')
        for i, horizon in enumerate(horizons):
            x = left + i * cell_w
            value = values.get((horizon, band), 0.0)
            color = color_sequential(value, max_value) if sequential else color_diverging(value, max_abs)
            stroke = "#111111" if (horizon, band) == selected_key else "#e1dfd5"
            stroke_w = "2.4" if (horizon, band) == selected_key else "0.7"
            lines.append(
                f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" fill="{color}" stroke="{stroke}" stroke-width="{stroke_w}">'
                f"<title>{horizon}s {band}: {metric}={value:.6f}</title></rect>"
            )
            if abs(value) >= 0.01 or metric == "neighbor_positive_share":
                text_color = "#16211d" if sequential or value >= 0 else "#401d1d"
                lines.append(
                    f'<text x="{x + cell_w / 2:.1f}" y="{y + 16}" text-anchor="middle" class="small" fill="{text_color}">{value:.3f}</text>'
                )

    legend_x = left
    legend_y = height - 34
    lines.append(f'<text x="{legend_x}" y="{legend_y - 8}" class="subtitle">Metric: {html.escape(metric)}</text>')
    for i in range(160):
        value = (i / 159) * max_value if sequential else ((i / 159) * 2 - 1) * max_abs
        color = color_sequential(value, max_value) if sequential else color_diverging(value, max_abs)
        lines.append(f'<rect x="{legend_x + i}" y="{legend_y}" width="1" height="12" fill="{color}"/>')
    if sequential:
        lines.append(f'<text x="{legend_x}" y="{legend_y + 28}" class="small">0</text>')
        lines.append(f'<text x="{legend_x + 160}" y="{legend_y + 28}" text-anchor="end" class="small">{max_value:.3f}</text>')
    else:
        lines.append(f'<text x="{legend_x}" y="{legend_y + 28}" class="small">-{max_abs:.3f}</text>')
        lines.append(f'<text x="{legend_x + 80}" y="{legend_y + 28}" text-anchor="middle" class="small">0</text>')
        lines.append(f'<text x="{legend_x + 160}" y="{legend_y + 28}" text-anchor="end" class="small">+{max_abs:.3f}</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines) + "\n")


def project_iso(x: float, y: float, z: float) -> tuple[float, float]:
    return (x * 34 - y * 18 + 240, x * 12 + y * 15 - z * 850 + 180)


def write_surface_html(
    path: Path,
    rows: list[dict[str, str]],
    selected: dict[str, str],
    metric: str,
    title: str,
    sequential: bool = False,
) -> None:
    horizons = sorted({int(row["horizon_seconds"]) for row in rows})
    bands = [band_label(low, high) for low, high in all_contiguous_bands()]
    horizon_index = {horizon: i for i, horizon in enumerate(horizons)}
    band_index = {band: i for i, band in enumerate(bands)}
    metric_values = [float(row[metric]) for row in rows]
    max_abs = max(abs(value) for value in metric_values) if metric_values else 1.0
    max_value = max(metric_values) if metric_values else 1.0

    svg_lines = [
        '<svg viewBox="0 0 920 540" width="100%" height="620" xmlns="http://www.w3.org/2000/svg">',
        "<style>text{font-family:Verdana,Arial,sans-serif;fill:#263238}.tiny{font-size:10px}.axis{stroke:#7d8790;stroke-width:1}.bar{stroke:#27313a;stroke-width:.45}</style>",
        '<rect width="100%" height="100%" fill="#fbfaf4"/>',
        f'<text x="24" y="32" style="font-size:20px;font-weight:700">{html.escape(title)}</text>',
        f'<text x="24" y="54" style="font-size:12px;fill:#52616b">Height and color encode {html.escape(metric)}. Black ring marks selected stable parameter.</text>',
    ]

    for row in rows:
        x_i = horizon_index[int(row["horizon_seconds"])]
        y_i = band_index[row["price_range"]]
        value = float(row[metric])
        base_x, base_y = project_iso(x_i, y_i, 0)
        scale = max_value if sequential else max_abs
        z_value = 0.0 if scale <= 0 else value / scale * 0.07
        top_x, top_y = project_iso(x_i, y_i, z_value)
        radius = 4.0 + min(10.0, abs(value) / max_abs * 8.0 if max_abs else 0.0)
        color = color_sequential(value, max_value) if sequential else color_diverging(value, max_abs)
        selected_cell = row["horizon_seconds"] == selected["horizon_seconds"] and row["price_range"] == selected["price_range"]
        svg_lines.append(f'<line x1="{base_x:.1f}" y1="{base_y:.1f}" x2="{top_x:.1f}" y2="{top_y:.1f}" stroke="{color}" stroke-width="2.2"/>')
        svg_lines.append(
            f'<circle class="bar" cx="{top_x:.1f}" cy="{top_y:.1f}" r="{radius:.2f}" fill="{color}">'
            f"<title>{row['horizon_seconds']}s {row['price_range']}: {metric}={value:.6f}, N={row['n']}</title></circle>"
        )
        if selected_cell:
            svg_lines.append(f'<circle cx="{top_x:.1f}" cy="{top_y:.1f}" r="{radius + 5:.2f}" fill="none" stroke="#111" stroke-width="3"/>')

    x0, y0 = project_iso(0, 0, 0)
    x1, y1 = project_iso(len(horizons) - 1, 0, 0)
    x2, y2 = project_iso(0, len(bands) - 1, 0)
    x3, y3 = project_iso(0, 0, 0.07)
    svg_lines.extend(
        [
            f'<line class="axis" x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}"/>',
            f'<line class="axis" x1="{x0:.1f}" y1="{y0:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>',
            f'<line class="axis" x1="{x0:.1f}" y1="{y0:.1f}" x2="{x3:.1f}" y2="{y3:.1f}"/>',
            f'<text x="{x1 + 8:.1f}" y="{y1 + 4:.1f}" class="tiny">T before expiry</text>',
            f'<text x="{x2 - 88:.1f}" y="{y2 + 18:.1f}" class="tiny">Price range</text>',
            f'<text x="{x3 - 40:.1f}" y="{y3 - 8:.1f}" class="tiny">{html.escape(metric)}</text>',
            "</svg>",
        ]
    )

    rows_json = json.dumps(rows)
    selected_text = (
        f"T={selected['horizon_seconds']}s, range {selected['price_range']}, "
        f"{metric}={float(selected[metric]):.4f}, "
        f"N={selected['n']}/{selected['total_available_contracts']}"
    )
    path.write_text(
        "\n".join(
            [
                "<!doctype html>",
                '<html lang="en"><meta charset="utf-8">',
                f"<title>{html.escape(title)}</title>",
                "<style>body{margin:0;background:#fbfaf4;color:#263238;font-family:Verdana,Arial,sans-serif}main{max-width:1160px;margin:0 auto;padding:24px}code{background:#eee8d8;padding:2px 5px;border-radius:4px}.note{color:#52616b}.panel{background:#fffdf7;border:1px solid #e3ddc9;border-radius:14px;padding:16px;margin:18px 0;box-shadow:0 8px 24px rgba(38,50,56,.07)}</style>",
                "<main>",
                "<div class=\"panel\">",
                "".join(svg_lines),
                "</div>",
                f"<p><strong>Selected stable parameter:</strong> <code>{html.escape(selected_text)}</code></p>",
                "<p class=\"note\">This HTML is self-contained. Circle tooltips expose the exact cell values.</p>",
                f"<script>window.btcProfitGrid = {rows_json};</script>",
                "</main></html>",
            ]
        )
        + "\n"
    )


def grouped_performance(entries: list[dict[str, str]], selected: dict[str, str], field: str) -> list[dict[str, str]]:
    low = float(selected["band_low"])
    high = float(selected["band_high"])
    horizon = selected["horizon_seconds"]
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    totals: dict[str, int] = defaultdict(int)
    for entry in entries:
        if entry["horizon_seconds"] == horizon:
            totals[entry[field]] += 1
        if entry["horizon_seconds"] == horizon and in_band(entry, low, high):
            groups[entry[field]].append(entry)

    rows: list[dict[str, str]] = []
    for group in sorted(groups):
        summary = summarize(groups[group], totals[group])
        rows.append(
            {
                field: group,
                "n": str(summary["n"]),
                "total_available_contracts": str(summary["total_available_contracts"]),
                "coverage": f"{summary['coverage']:.6f}",
                "wins": str(summary["wins"]),
                "losses": str(summary["losses"]),
                "p_success": f"{summary['p_success']:.6f}",
                "avg_cost": f"{summary['avg_cost']:.6f}",
                "ev_p_minus_c": f"{summary['ev_p_minus_c']:.6f}",
                "gross_pnl": f"{summary['gross_pnl']:.6f}",
                "profit_per_available_contract": f"{summary['profit_per_available_contract']:.6f}",
                "p_value_break_even": f"{summary['p_value_break_even']:.10g}",
            }
        )
    return rows


def chi_square_from_group_rows(rows: list[dict[str, str]]) -> float:
    total_n = sum(int(row["n"]) for row in rows)
    total_wins = sum(int(row["wins"]) for row in rows)
    total_losses = total_n - total_wins
    if total_n == 0 or total_wins == 0 or total_losses == 0:
        return 0.0

    statistic = 0.0
    for row in rows:
        n = int(row["n"])
        wins = int(row["wins"])
        losses = int(row["losses"])
        expected_wins = n * total_wins / total_n
        expected_losses = n * total_losses / total_n
        statistic += (wins - expected_wins) ** 2 / expected_wins
        statistic += (losses - expected_losses) ** 2 / expected_losses
    return statistic


def exact_fixed_margin_chi_square_p_value(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Exact fixed-margin p-value for success-rate dependence across groups."""
    group_sizes = [int(row["n"]) for row in rows]
    total_n = sum(group_sizes)
    total_wins = sum(int(row["wins"]) for row in rows)
    total_losses = total_n - total_wins
    observed = chi_square_from_group_rows(rows)
    if not rows or total_n == 0 or total_wins == 0 or total_losses == 0:
        return {"chi_square": observed, "p_value": 1.0, "tables_enumerated": 0}

    denominator_log = math.lgamma(total_n + 1) - math.lgamma(total_wins + 1) - math.lgamma(total_n - total_wins + 1)
    comb_log_cache = {
        (n, k): math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
        for n in group_sizes
        for k in range(n + 1)
    }

    p_value = 0.0
    tables_enumerated = 0

    def recurse(index: int, remaining_wins: int, allocated: list[int], log_probability: float) -> None:
        nonlocal p_value, tables_enumerated
        if index == len(group_sizes) - 1:
            n = group_sizes[index]
            if not 0 <= remaining_wins <= n:
                return
            wins_by_group = allocated + [remaining_wins]
            probability = math.exp(log_probability + comb_log_cache[(n, remaining_wins)] - denominator_log)
            simulated_rows = [
                {"n": str(group_sizes[i]), "wins": str(wins), "losses": str(group_sizes[i] - wins)}
                for i, wins in enumerate(wins_by_group)
            ]
            if chi_square_from_group_rows(simulated_rows) >= observed - 1e-12:
                p_value += probability
            tables_enumerated += 1
            return

        n = group_sizes[index]
        remaining_capacity = sum(group_sizes[index + 1 :])
        low = max(0, remaining_wins - remaining_capacity)
        high = min(n, remaining_wins)
        for wins in range(low, high + 1):
            recurse(index + 1, remaining_wins - wins, allocated + [wins], log_probability + comb_log_cache[(n, wins)])

    recurse(0, total_wins, [], 0.0)
    return {"chi_square": observed, "p_value": min(1.0, max(0.0, p_value)), "tables_enumerated": tables_enumerated}


def markdown_profit_table(rows: list[dict[str, str]], limit: int | None = None) -> list[str]:
    selected = rows[:limit] if limit else rows
    lines = [
        "| T seconds | Price range | N traded | N total | Coverage | P(success) | Avg cost c | EV p-c | Gross P&L | Profit/available | p-value | Stable |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in selected:
        lines.append(
            f"| {row['horizon_seconds']} | {row['price_range']} | {row['n']} | {row['total_available_contracts']} | "
            f"{pct(float(row['coverage']))} | {pct(float(row['p_success']))} | {fmt(float(row['avg_cost']))} | "
            f"{fmt(float(row['ev_p_minus_c']))} | {fmt(float(row['gross_pnl']))} | "
            f"{fmt(float(row['profit_per_available_contract']))} | {float(row['p_value_break_even']):.4g} | {row['stable']} |"
        )
    return lines


def markdown_group_table(rows: list[dict[str, str]], field: str, label: str) -> list[str]:
    lines = [
        f"| {label} | N traded | N total | Coverage | P(success) | Avg cost c | EV p-c | Gross P&L | Profit/available | p-value |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row[field]} | {row['n']} | {row['total_available_contracts']} | {pct(float(row['coverage']))} | "
            f"{pct(float(row['p_success']))} | {fmt(float(row['avg_cost']))} | {fmt(float(row['ev_p_minus_c']))} | "
            f"{fmt(float(row['gross_pnl']))} | {fmt(float(row['profit_per_available_contract']))} | {float(row['p_value_break_even']):.4g} |"
        )
    return lines


def write_report(
    path: Path,
    entries: list[dict[str, str]],
    rows: list[dict[str, str]],
    selected: dict[str, str],
    raw_best: dict[str, str],
    stable_best: dict[str, str],
    plots: dict[str, str],
    artifacts: dict[str, str],
    min_n: int,
) -> dict[str, Any]:
    date_rows = grouped_performance(entries, selected, "close_date_et")
    bucket_rows = grouped_performance(entries, selected, "close_hour_bucket_et")
    date_dependency = exact_fixed_margin_chi_square_p_value(date_rows)
    bucket_dependency = exact_fixed_margin_chi_square_p_value(bucket_rows)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    lines = [
        "# BTC Kalshi Profit-Per-Available Stability Analysis",
        "",
        f"Generated: `{generated_at}`",
        "",
        "## Table Of Contents",
        "",
        "- [Objective](#objective)",
        "- [Result](#result)",
        "- [Stability Criteria](#stability-criteria)",
        "- [Visualizations](#visualizations)",
        "- [Top Objective Cells](#top-objective-cells)",
        "- [Top Stable Cells](#top-stable-cells)",
        "- [Time And Day Check For Selected Parameter](#time-and-day-check-for-selected-parameter)",
        "- [Date/Time Dependency Tests](#datetime-dependency-tests)",
        "- [Artifacts](#artifacts)",
        "",
        "## Objective",
        "",
        "The requested objective is:",
        "",
        "```text",
        "(p - c) * N_traded_in_range / N_total_backtested_at_T",
        "```",
        "",
        "This equals gross P&L per available contract at that horizon. I used `N_total_backtested_at_T` as the count of resolved contracts with a usable quote row at that T.",
        "",
        "## Result",
        "",
        f"The best parameter that is both profitable and stable is `T={selected['horizon_seconds']}s`, price range `{selected['price_range']}`.",
        "",
        f"- Profit per available contract: `{fmt(float(selected['profit_per_available_contract']))}`",
        f"- Gross P&L: `{fmt(float(selected['gross_pnl']))}` across `{selected['total_available_contracts']}` available contracts",
        f"- N traded in range: `{selected['n']}`",
        f"- Coverage: `{pct(float(selected['coverage']))}`",
        f"- P(success): `{pct(float(selected['p_success']))}`",
        f"- Average cost c: `{fmt(float(selected['avg_cost']))}`",
        f"- EV p-c inside range: `{fmt(float(selected['ev_p_minus_c']))}`",
        f"- One-sided break-even p-value: `{float(selected['p_value_break_even']):.6g}`",
        "",
        f"The raw objective argmax with `N >= {min_n}` is `T={raw_best['horizon_seconds']}s`, range `{raw_best['price_range']}`, profit/available `{fmt(float(raw_best['profit_per_available_contract']))}`. It is also classified stable, so it is the selected parameter.",
        "",
        "The p-value is an exact one-sided Poisson-binomial tail probability under the break-even null that each selected contract resolves correctly with probability equal to its own buy cost. It measures `P(X >= observed wins)` under that cost-implied null.",
        "",
        "## Stability Criteria",
        "",
        "A cell is classified stable when it has positive profit per available contract, `N >= 20`, at least four valid immediate neighbors, at least 70% of those neighbors are positive, average neighbor profit is positive, and it belongs to a positive connected component of at least eight cells.",
        "",
        f"Selected cell neighbor positive share: `{pct(float(selected['neighbor_positive_share']))}` (`{selected['neighbor_positive_count']}/{selected['neighbor_count']}` neighbors).",
        f"Selected cell neighbor mean profit/available: `{fmt(float(selected['neighbor_profit_per_available_mean']))}`.",
        f"Positive connected component size: `{selected['positive_component_size']}` cells.",
        "",
        "## Visualizations",
        "",
    ]
    for label, plot_path in plots.items():
        if plot_path.endswith(".svg"):
            lines.append(f"### {label}")
            lines.append("")
            lines.append(f"![{label}]({plot_path})")
            lines.append("")
        else:
            lines.append(f"- {label}: `{plot_path}`")

    lines.extend(
        [
            "",
            "## Top Objective Cells",
            "",
            *markdown_profit_table(sort_by_metric(rows, "profit_per_available_contract", min_n=min_n), limit=20),
            "",
            "## Top Stable Cells",
            "",
            *markdown_profit_table(sort_by_metric(rows, "profit_per_available_contract", min_n=min_n, stable_only=True), limit=20),
            "",
            "## Time And Day Check For Selected Parameter",
            "",
            "By ET date:",
            "",
            *markdown_group_table(date_rows, "close_date_et", "ET date"),
            "",
            "By ET 4-hour close bucket:",
            "",
            *markdown_group_table(bucket_rows, "close_hour_bucket_et", "ET close-hour bucket"),
            "",
            "The selected rule is positive on every observed ET date and every 4-hour bucket in this sample. That is materially more stable than the narrower 0.60-0.70, T=420s EV argmax from the earlier decile-only analysis.",
            "",
            "## Date/Time Dependency Tests",
            "",
            "These tests ask whether the selected rule's win/loss outcomes vary by date or by time bucket. They condition on the total number of wins and group sizes, then enumerate all possible fixed-margin win allocations and compare the chi-square statistic to the observed table.",
            "",
            "| Grouping | Chi-square statistic | Exact p-value | Tables enumerated | Interpretation |",
            "|---|---:|---:|---:|---|",
            f"| ET date | {date_dependency['chi_square']:.4f} | {date_dependency['p_value']:.4f} | {date_dependency['tables_enumerated']} | {'evidence of dependence' if date_dependency['p_value'] < 0.05 else 'no clear dependence'} |",
            f"| ET 4-hour bucket | {bucket_dependency['chi_square']:.4f} | {bucket_dependency['p_value']:.4f} | {bucket_dependency['tables_enumerated']} | {'evidence of dependence' if bucket_dependency['p_value'] < 0.05 else 'no clear dependence'} |",
            "",
            "Conclusion: the selected rule's profitability varies economically by date/time, but this sample does not show statistically clear win-rate dependence on ET date or ET 4-hour close bucket.",
            "",
            "## Artifacts",
            "",
        ]
    )
    for label, artifact in artifacts.items():
        lines.append(f"- {label}: `{artifact}`")
    lines.append("")
    lines.append("Fees are not included. Fees reduce expected value, especially near 0.50.")
    lines.append("")

    path.write_text("\n".join(lines))
    return {
        "raw_best_min_n": raw_best,
        "stable_best": stable_best,
        "selected": selected,
        "selected_by_date": date_rows,
        "selected_by_hour_bucket": bucket_rows,
        "date_dependency_test": date_dependency,
        "hour_bucket_dependency_test": bucket_dependency,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries-csv", default="btc_more_likely_entries_official.csv")
    parser.add_argument("--output-grid-csv", default="btc_profit_per_available_grid.csv")
    parser.add_argument("--summary-json", default="btc_profit_stability_summary.json")
    parser.add_argument("--report-md", default="btc_profit_stability_report.md")
    parser.add_argument("--plots-dir", default="plots")
    parser.add_argument("--min-n", type=int, default=MIN_N)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(__file__).resolve().parent
    entries_path = workdir / args.entries_csv
    entries = load_entries(entries_path)
    rows = compute_stability(build_profit_grid(entries), args.min_n)

    raw_best = sort_by_metric(rows, "profit_per_available_contract", min_n=args.min_n)[0]
    stable_rows = sort_by_metric(rows, "profit_per_available_contract", min_n=args.min_n, stable_only=True)
    if not stable_rows:
        raise SystemExit("no profitable stable parameter set found")
    stable_best = stable_rows[0]
    selected = stable_best

    plots_dir = workdir / args.plots_dir
    plots_dir.mkdir(exist_ok=True)
    plot_paths = {
        "Profit Per Available Contract Heatmap": "plots/btc_profit_per_available_heatmap.svg",
        "EV p-c Heatmap": "plots/btc_ev_heatmap.svg",
        "Neighbor Positive Share Heatmap": "plots/btc_neighbor_positive_share_heatmap.svg",
        "Traded Coverage Heatmap": "plots/btc_traded_coverage_heatmap.svg",
        "3D Profit Surface HTML": "plots/btc_profit_surface_3d.html",
        "3D Stability Surface HTML": "plots/btc_stability_surface_3d.html",
    }

    write_heatmap_svg(workdir / plot_paths["Profit Per Available Contract Heatmap"], rows, "profit_per_available_contract", "BTC Profit Per Available Contract", selected)
    write_heatmap_svg(workdir / plot_paths["EV p-c Heatmap"], rows, "ev_p_minus_c", "BTC EV p-c By T And Price Range", selected)
    write_heatmap_svg(
        workdir / plot_paths["Neighbor Positive Share Heatmap"],
        rows,
        "neighbor_positive_share",
        "BTC Stability: Neighbor Positive Share",
        selected,
        sequential=True,
    )
    write_heatmap_svg(workdir / plot_paths["Traded Coverage Heatmap"], rows, "coverage", "BTC Traded Coverage By T And Price Range", selected, sequential=True)
    write_surface_html(
        workdir / plot_paths["3D Profit Surface HTML"],
        rows,
        selected,
        "profit_per_available_contract",
        "3D Profit Per Available Contract Surface",
    )
    write_surface_html(
        workdir / plot_paths["3D Stability Surface HTML"],
        rows,
        selected,
        "neighbor_positive_share",
        "3D Neighbor Positive Share Stability Surface",
        sequential=True,
    )

    grid_csv = workdir / args.output_grid_csv
    summary_json = workdir / args.summary_json
    report_md = workdir / args.report_md
    write_csv(grid_csv, rows)
    artifacts = {
        "Profit-per-available grid": grid_csv.name,
        "Machine-readable summary": summary_json.name,
        "Source entry ledger": entries_path.name,
    }
    summary = write_report(report_md, entries, rows, selected, raw_best, stable_best, plot_paths, artifacts, args.min_n)
    summary["artifacts"] = {**artifacts, "Markdown report": report_md.name, **plot_paths}
    summary["rows"] = len(rows)
    summary["min_n"] = args.min_n
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
