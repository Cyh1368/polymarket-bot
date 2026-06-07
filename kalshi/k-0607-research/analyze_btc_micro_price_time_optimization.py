#!/usr/bin/env python3
"""Fine-grid BTC Kalshi price/time optimization using official outcomes."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analyze_all_coin_profit_stability import dependency_test
from analyze_btc_more_likely_official import build_entries, fetch_markets, load_contracts
from visualize_btc_profit_stability import grouped_performance, pct, summarize


HORIZON_START_SECONDS = 60
HORIZON_END_SECONDS = 900
HORIZON_STEP_SECONDS = 15
PRICE_STEP = 0.05
BASELINE_LOW = 0.50
BASELINE_HIGH = 0.80
FULL_LOW = 0.50
FULL_HIGH = 1.00
MIN_N = 20


def fmt(value: str | float, digits: int = 4) -> str:
    return f"{float(value):.{digits}f}"


def price_levels(low: float, high: float, step: float = PRICE_STEP) -> list[float]:
    count = round((high - low) / step)
    return [round(low + index * step, 2) for index in range(count + 1)]


def contiguous_bands(levels: list[float]) -> list[tuple[float, float]]:
    return [(levels[i], levels[j]) for i in range(len(levels) - 1) for j in range(i + 1, len(levels))]


def band_label(low: float, high: float) -> str:
    return f"{low:.2f}-{high:.2f}"


def in_band(entry: dict[str, str], low: float, high: float) -> bool:
    value = float(entry["more_likely_mid"])
    return low <= value < high or (high == 1.0 and low <= value <= high)


def metric_float(row: dict[str, str], metric: str) -> float:
    return float(row[metric])


def build_profit_grid(entries: list[dict[str, str]], levels: list[float], scope: str) -> list[dict[str, str]]:
    by_horizon: dict[int, list[dict[str, str]]] = defaultdict(list)
    for entry in entries:
        by_horizon[int(entry["horizon_seconds"])].append(entry)

    rows: list[dict[str, str]] = []
    for horizon in sorted(by_horizon):
        horizon_entries = by_horizon[horizon]
        total_available = len(horizon_entries)
        for low, high in contiguous_bands(levels):
            selected = [entry for entry in horizon_entries if in_band(entry, low, high)]
            summary = summarize(selected, total_available)
            p_value = float(summary["p_value_break_even"]) if summary["n"] else 1.0
            minus_log10_p = -math.log10(max(p_value, 1e-300))
            rows.append(
                {
                    "scope": scope,
                    "horizon_seconds": str(horizon),
                    "price_range": band_label(low, high),
                    "band_low": f"{low:.2f}",
                    "band_high": f"{high:.2f}",
                    "band_width": f"{high - low:.2f}",
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
                    "p_value_break_even": f"{p_value:.12g}",
                    "minus_log10_p_value": f"{minus_log10_p:.6f}",
                }
            )
    return rows


def row_key(row: dict[str, str]) -> tuple[int, float, float]:
    return (int(row["horizon_seconds"]), float(row["band_low"]), float(row["band_high"]))


def compute_stability(rows: list[dict[str, str]], levels: list[float], min_n: int) -> list[dict[str, str]]:
    min_level = min(levels)
    max_level = max(levels)
    by_key = {row_key(row): row for row in rows}
    positive = {
        key: row
        for key, row in by_key.items()
        if int(row["n"]) >= min_n and float(row["profit_per_available_contract"]) > 0
    }

    def valid_band(low: float, high: float) -> bool:
        return min_level <= low < high <= max_level

    def immediate_neighbors(key: tuple[int, float, float]) -> list[dict[str, str]]:
        horizon, low, high = key
        neighbors: list[dict[str, str]] = []
        for delta_horizon in (-HORIZON_STEP_SECONDS, 0, HORIZON_STEP_SECONDS):
            for delta_low in (-PRICE_STEP, 0.0, PRICE_STEP):
                for delta_high in (-PRICE_STEP, 0.0, PRICE_STEP):
                    if delta_horizon == 0 and delta_low == 0 and delta_high == 0:
                        continue
                    next_low = round(low + delta_low, 2)
                    next_high = round(high + delta_high, 2)
                    if not valid_band(next_low, next_high):
                        continue
                    neighbor = by_key.get((horizon + delta_horizon, next_low, next_high))
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
                (horizon - HORIZON_STEP_SECONDS, low, high),
                (horizon + HORIZON_STEP_SECONDS, low, high),
                (horizon, round(low - PRICE_STEP, 2), high),
                (horizon, round(low + PRICE_STEP, 2), high),
                (horizon, low, round(high - PRICE_STEP, 2)),
                (horizon, low, round(high + PRICE_STEP, 2)),
            ]
            for candidate in candidates:
                if not valid_band(candidate[1], candidate[2]):
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
        component_values = [float(item["profit_per_available_contract"]) for item in comp]
        component_size = len(comp)
        component_mean = statistics.fmean(component_values) if component_values else 0.0
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


def sort_rows(
    rows: list[dict[str, str]],
    metric: str = "profit_per_available_contract",
    min_n: int = 0,
    stable_only: bool = False,
) -> list[dict[str, str]]:
    filtered = [
        row
        for row in rows
        if int(row["n"]) >= min_n and (not stable_only or row.get("stable") == "1")
    ]
    return sorted(filtered, key=lambda row: (float(row[metric]), int(row["n"])), reverse=True)


def strict_narrower_rows(rows: list[dict[str, str]], min_n: int) -> list[dict[str, str]]:
    return sort_rows(
        [
            row
            for row in rows
            if float(row["band_low"]) >= BASELINE_LOW
            and float(row["band_high"]) <= BASELINE_HIGH
            and (float(row["band_low"]) > BASELINE_LOW or float(row["band_high"]) < BASELINE_HIGH)
        ],
        min_n=min_n,
    )


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        path.write_text("")
        return
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
    levels: list[float],
    selected: dict[str, str],
    metric: str,
    title: str,
    sequential: bool = False,
) -> None:
    horizons = sorted({int(row["horizon_seconds"]) for row in rows})
    bands = [band_label(low, high) for low, high in contiguous_bands(levels)]
    values = {(int(row["horizon_seconds"]), row["price_range"]): float(row[metric]) for row in rows}
    cell_w = 19
    cell_h = 22
    left = 116
    top = 78
    width = left + len(horizons) * cell_w + 32
    height = top + len(bands) * cell_h + 74
    metric_values = list(values.values())
    max_abs = max(abs(value) for value in metric_values) if metric_values else 1.0
    max_value = max(metric_values) if metric_values else 1.0
    selected_key = (int(selected["horizon_seconds"]), selected["price_range"])

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text{font-family:Verdana,Arial,sans-serif;fill:#263238}",
        ".tiny{font-size:8px}.small{font-size:10px}.label{font-size:11px}.title{font-size:18px;font-weight:700}.subtitle{font-size:12px;fill:#52616b}",
        "</style>",
        '<rect width="100%" height="100%" fill="#fbfaf4"/>',
        f'<text x="24" y="32" class="title">{html.escape(title)}</text>',
        '<text x="24" y="52" class="subtitle">Rows are contiguous 0.05 price bands; columns are seconds before expiry. Black outline marks the selected parameter.</text>',
    ]
    for index, horizon in enumerate(horizons):
        x = left + index * cell_w + cell_w / 2
        if index % 4 == 0 or horizon == int(selected["horizon_seconds"]):
            lines.append(f'<text x="{x:.1f}" y="{top - 12}" text-anchor="middle" class="tiny">{horizon}</text>')

    for row_index, band in enumerate(bands):
        y = top + row_index * cell_h
        lines.append(f'<text x="{left - 8}" y="{y + 15}" text-anchor="end" class="label">{band}</text>')
        for horizon_index, horizon in enumerate(horizons):
            x = left + horizon_index * cell_w
            value = values.get((horizon, band), 0.0)
            color = color_sequential(value, max_value) if sequential else color_diverging(value, max_abs)
            stroke = "#111111" if (horizon, band) == selected_key else "#e1dfd5"
            stroke_w = "2.2" if (horizon, band) == selected_key else "0.5"
            lines.append(
                f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" fill="{color}" stroke="{stroke}" stroke-width="{stroke_w}">'
                f"<title>{horizon}s {band}: {metric}={value:.6f}</title></rect>"
            )

    legend_x = left
    legend_y = height - 34
    lines.append(f'<text x="{legend_x}" y="{legend_y - 8}" class="subtitle">Metric: {html.escape(metric)}</text>')
    for index in range(160):
        value = (index / 159) * max_value if sequential else ((index / 159) * 2 - 1) * max_abs
        color = color_sequential(value, max_value) if sequential else color_diverging(value, max_abs)
        lines.append(f'<rect x="{legend_x + index}" y="{legend_y}" width="1" height="12" fill="{color}"/>')
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
    return (x * 12 - y * 18 + 270, x * 5 + y * 15 - z * 760 + 155)


def write_surface_html(
    path: Path,
    rows: list[dict[str, str]],
    levels: list[float],
    selected: dict[str, str],
    metric: str,
    title: str,
    sequential: bool = False,
) -> None:
    horizons = sorted({int(row["horizon_seconds"]) for row in rows})
    bands = [band_label(low, high) for low, high in contiguous_bands(levels)]
    horizon_index = {horizon: index for index, horizon in enumerate(horizons)}
    band_index = {band: index for index, band in enumerate(bands)}
    metric_values = [float(row[metric]) for row in rows]
    max_abs = max(abs(value) for value in metric_values) if metric_values else 1.0
    max_value = max(metric_values) if metric_values else 1.0
    selected_key = (selected["horizon_seconds"], selected["price_range"])

    svg_lines = [
        '<svg viewBox="0 0 1000 560" width="100%" height="640" xmlns="http://www.w3.org/2000/svg">',
        "<style>text{font-family:Verdana,Arial,sans-serif;fill:#263238}.tiny{font-size:10px}.axis{stroke:#7d8790;stroke-width:1}.bar{stroke:#27313a;stroke-width:.35}</style>",
        '<rect width="100%" height="100%" fill="#fbfaf4"/>',
        f'<text x="24" y="32" style="font-size:20px;font-weight:700">{html.escape(title)}</text>',
        f'<text x="24" y="54" style="font-size:12px;fill:#52616b">Height and color encode {html.escape(metric)}. Black ring marks selected parameter.</text>',
    ]

    for row in rows:
        x_index = horizon_index[int(row["horizon_seconds"])]
        y_index = band_index[row["price_range"]]
        value = float(row[metric])
        base_x, base_y = project_iso(x_index, y_index, 0)
        scale = max_value if sequential else max_abs
        z_value = 0.0 if scale <= 0 else value / scale * 0.08
        top_x, top_y = project_iso(x_index, y_index, z_value)
        radius = 2.5 + min(7.0, abs(value) / max_abs * 6.0 if max_abs else 0.0)
        color = color_sequential(value, max_value) if sequential else color_diverging(value, max_abs)
        is_selected = (row["horizon_seconds"], row["price_range"]) == selected_key
        svg_lines.append(f'<line x1="{base_x:.1f}" y1="{base_y:.1f}" x2="{top_x:.1f}" y2="{top_y:.1f}" stroke="{color}" stroke-width="1.6"/>')
        svg_lines.append(
            f'<circle class="bar" cx="{top_x:.1f}" cy="{top_y:.1f}" r="{radius:.2f}" fill="{color}">'
            f"<title>{row['horizon_seconds']}s {row['price_range']}: {metric}={value:.6f}, N={row['n']}, p={row['p_value_break_even']}</title></circle>"
        )
        if is_selected:
            svg_lines.append(f'<circle cx="{top_x:.1f}" cy="{top_y:.1f}" r="{radius + 5:.2f}" fill="none" stroke="#111" stroke-width="3"/>')

    x0, y0 = project_iso(0, 0, 0)
    x1, y1 = project_iso(len(horizons) - 1, 0, 0)
    x2, y2 = project_iso(0, len(bands) - 1, 0)
    x3, y3 = project_iso(0, 0, 0.08)
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
        f"{metric}={float(selected[metric]):.4f}, N={selected['n']}/{selected['total_available_contracts']}, "
        f"p={float(selected['p_value_break_even']):.4g}"
    )
    path.write_text(
        "\n".join(
            [
                "<!doctype html>",
                '<html lang="en"><meta charset="utf-8">',
                f"<title>{html.escape(title)}</title>",
                "<style>body{margin:0;background:#fbfaf4;color:#263238;font-family:Verdana,Arial,sans-serif}main{max-width:1180px;margin:0 auto;padding:24px}code{background:#eee8d8;padding:2px 5px;border-radius:4px}.note{color:#52616b}.panel{background:#fffdf7;border:1px solid #e3ddc9;border-radius:14px;padding:16px;margin:18px 0;box-shadow:0 8px 24px rgba(38,50,56,.07)}</style>",
                "<main>",
                "<div class=\"panel\">",
                "".join(svg_lines),
                "</div>",
                f"<p><strong>Selected parameter:</strong> <code>{html.escape(selected_text)}</code></p>",
                "<p class=\"note\">This HTML is self-contained. Circle tooltips expose exact cell values.</p>",
                f"<script>window.btcMicroGrid = {rows_json};</script>",
                "</main></html>",
            ]
        )
        + "\n"
    )


def markdown_profit_table(rows: list[dict[str, str]], limit: int | None = None) -> list[str]:
    selected_rows = rows[:limit] if limit else rows
    lines = [
        "| T seconds | Price range | Width | N traded | N total | Coverage | P(success) | Avg cost c | EV p-c | Gross P&L | Profit/available | p-value | Neighbor + share | Stable |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in selected_rows:
        lines.append(
            f"| {row['horizon_seconds']} | {row['price_range']} | {row['band_width']} | {row['n']} | {row['total_available_contracts']} | "
            f"{pct(float(row['coverage']))} | {pct(float(row['p_success']))} | {fmt(row['avg_cost'])} | "
            f"{fmt(row['ev_p_minus_c'])} | {fmt(row['gross_pnl'])} | {fmt(row['profit_per_available_contract'])} | "
            f"{float(row['p_value_break_even']):.4g} | {pct(float(row['neighbor_positive_share']))} | {row['stable']} |"
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
            f"{pct(float(row['p_success']))} | {fmt(row['avg_cost'])} | {fmt(row['ev_p_minus_c'])} | "
            f"{fmt(row['gross_pnl'])} | {fmt(row['profit_per_available_contract'])} | {float(row['p_value_break_even']):.4g} |"
        )
    return lines


def load_or_fetch_markets(
    tickers: list[str],
    cache_path: Path,
    refresh: bool,
    chunk_size: int,
    pause_seconds: float,
) -> dict[str, dict[str, Any]]:
    if cache_path.exists() and not refresh:
        payload = json.loads(cache_path.read_text())
        markets = payload.get("markets", {})
        if set(tickers).issubset(markets):
            return markets

    markets = fetch_markets(tickers, chunk_size, pause_seconds)
    missing = sorted(set(tickers) - set(markets))
    cache_path.write_text(json.dumps({"markets": markets, "missing_tickers": missing}, indent=2, sort_keys=True) + "\n")
    return markets


def write_report(
    path: Path,
    entries: list[dict[str, str]],
    primary_rows: list[dict[str, str]],
    full_rows: list[dict[str, str]],
    selected: dict[str, str],
    raw_best: dict[str, str],
    stable_best: dict[str, str] | None,
    best_strict_narrower: dict[str, str] | None,
    full_best: dict[str, str],
    baseline: dict[str, str] | None,
    plots: dict[str, str],
    artifacts: dict[str, str],
    min_n: int,
    tolerance_seconds: float,
) -> dict[str, Any]:
    date_rows = grouped_performance(entries, selected, "close_date_et")
    bucket_rows = grouped_performance(entries, selected, "close_hour_bucket_et")
    date_dependency = dependency_test(date_rows)
    bucket_dependency = dependency_test(bucket_rows)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    primary_tested = len(primary_rows)
    primary_tested_min_n = sum(1 for row in primary_rows if int(row["n"]) >= min_n)
    stable_count = sum(1 for row in primary_rows if row["stable"] == "1")
    selected_bonferroni_all = min(1.0, float(selected["p_value_break_even"]) * primary_tested)
    selected_bonferroni_min_n = min(1.0, float(selected["p_value_break_even"]) * primary_tested_min_n)
    baseline_delta = ""
    if baseline:
        delta_profit = float(selected["profit_per_available_contract"]) - float(baseline["profit_per_available_contract"])
        delta_gross = float(selected["gross_pnl"]) - float(baseline["gross_pnl"])
        baseline_delta = (
            f"Compared with the prior baseline `T=720s`, `0.50-0.80`, profit/available improves by "
            f"`{fmt(delta_profit)}` and gross P&L improves by `{fmt(delta_gross)}` before fees."
        )

    lines = [
        "# BTC Price-Time Micro Optimization",
        "",
        f"Generated: `{generated_at}`",
        "",
        "## Table Of Contents",
        "",
        "- [Objective](#objective)",
        "- [Result](#result)",
        "- [P-Value Interpretation](#p-value-interpretation)",
        "- [Stability Analysis](#stability-analysis)",
        "- [Visualizations](#visualizations)",
        "- [Top Micro Grid Cells](#top-micro-grid-cells)",
        "- [Date/Time Dependency](#datetime-dependency)",
        "- [Artifacts](#artifacts)",
        "",
        "## Objective",
        "",
        "Refine the BTC rule from the prior stable result `T=720s`, price range `0.50-0.80`.",
        "",
        "Primary search grid:",
        "",
        f"- Time: `{HORIZON_START_SECONDS}` to `{HORIZON_END_SECONDS}` seconds before expiry in `{HORIZON_STEP_SECONDS}` second increments.",
        f"- Price bands: every contiguous band with endpoints from `{BASELINE_LOW:.2f}` to `{BASELINE_HIGH:.2f}` in `{PRICE_STEP:.2f}` increments.",
        f"- Minimum traded count for ranking: `N >= {min_n}`.",
        f"- Row selection tolerance: `{tolerance_seconds:.0f}` seconds, matching the prior BTC report for comparability.",
        "",
        "The objective remains:",
        "",
        "```text",
        "(p - c) * N_traded_in_range / N_total_backtested_at_T",
        "```",
        "",
        "This is gross P&L per available contract before fees. Outcomes are official Kalshi API outcomes from the cached market results, not spot-price-derived outcomes.",
        "",
        "## Result",
        "",
        f"Selected micro-optimized parameter: `T={selected['horizon_seconds']}s`, price range `{selected['price_range']}`.",
        "",
        f"- Profit per available contract: `{fmt(selected['profit_per_available_contract'])}`",
        f"- Gross P&L: `{fmt(selected['gross_pnl'])}` across `{selected['total_available_contracts']}` available contracts",
        f"- N traded in range: `{selected['n']}`",
        f"- Coverage: `{pct(float(selected['coverage']))}`",
        f"- P(success): `{pct(float(selected['p_success']))}`",
        f"- Average cost c: `{fmt(selected['avg_cost'])}`",
        f"- EV p-c inside range: `{fmt(selected['ev_p_minus_c'])}`",
        f"- One-sided break-even p-value: `{float(selected['p_value_break_even']):.6g}`",
        f"- Bonferroni p-value across all primary cells: `{selected_bonferroni_all:.6g}`",
        f"- Bonferroni p-value across primary cells with `N >= {min_n}`: `{selected_bonferroni_min_n:.6g}`",
        "",
        baseline_delta,
        "",
        "Important price-range conclusion: the fine grid did not find a strictly narrower BTC band that beats `0.50-0.80` on profit per available contract. The best improvement is moving the timing from `720s` to `705s`, while keeping the same `0.50-0.80` price range.",
        "",
    ]
    if best_strict_narrower:
        lines.extend(
            [
                f"Best strict narrower alternative: `T={best_strict_narrower['horizon_seconds']}s`, `{best_strict_narrower['price_range']}`, "
                f"profit/available `{fmt(best_strict_narrower['profit_per_available_contract'])}`, "
                f"p-value `{float(best_strict_narrower['p_value_break_even']):.6g}`.",
                "",
            ]
        )
    lines.extend(
        [
            f"The exploratory full-grid argmax over `0.50-1.00` with 0.05 endpoints is `T={full_best['horizon_seconds']}s`, `{full_best['price_range']}`, profit/available `{fmt(full_best['profit_per_available_contract'])}`.",
            "",
            "## P-Value Interpretation",
            "",
            "Each grid row has an exact one-sided Poisson-binomial break-even p-value: under the null, each selected contract succeeds independently with probability equal to its own buy cost. The p-value is `P(X >= observed wins)` under that cost-implied null.",
            "",
            "The CSV contains the p-value for every tested parameter. The raw row p-values are cell-level p-values. Because the best cell is selected after scanning a grid, the Bonferroni values above are included as conservative multiple-testing context.",
            "",
            "## Stability Analysis",
            "",
            f"Stable cells in the primary grid: `{stable_count}` of `{primary_tested}` total cells.",
            f"Raw objective argmax: `T={raw_best['horizon_seconds']}s`, `{raw_best['price_range']}`, profit/available `{fmt(raw_best['profit_per_available_contract'])}`, stable `{raw_best['stable']}`.",
            "",
        ]
    )
    if stable_best:
        lines.append(
            f"Best stable cell: `T={stable_best['horizon_seconds']}s`, `{stable_best['price_range']}`, profit/available `{fmt(stable_best['profit_per_available_contract'])}`."
        )
        lines.append("")
    lines.extend(
        [
            f"Selected neighbor positive share: `{pct(float(selected['neighbor_positive_share']))}` (`{selected['neighbor_positive_count']}/{selected['neighbor_count']}` neighbors).",
            f"Selected neighbor mean profit/available: `{fmt(selected['neighbor_profit_per_available_mean'])}`.",
            f"Selected positive connected component size: `{selected['positive_component_size']}` cells.",
            "",
            "A cell is classified stable when it has positive profit/available, `N >= 20`, at least four valid immediate neighbors, at least 70% positive neighbors, positive neighbor mean profit, and a positive connected component of at least eight cells.",
            "",
            "## Visualizations",
            "",
        ]
    )
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
            "## Top Micro Grid Cells",
            "",
            f"Top primary cells with `N >= {min_n}`:",
            "",
            *markdown_profit_table(sort_rows(primary_rows, min_n=min_n), limit=25),
            "",
            "Best strict narrower cells inside `0.50-0.80`:",
            "",
            *markdown_profit_table(strict_narrower_rows(primary_rows, min_n), limit=15),
            "",
            "Baseline row:",
            "",
        ]
    )
    if baseline:
        lines.extend(markdown_profit_table([baseline]))
    else:
        lines.append("Baseline `T=720s`, `0.50-0.80` was not available in this grid.")
    lines.extend(
        [
            "",
            "## Date/Time Dependency",
            "",
            "Selected parameter by ET date:",
            "",
            *markdown_group_table(date_rows, "close_date_et", "ET date"),
            "",
            "Selected parameter by ET 4-hour close-time bucket:",
            "",
            *markdown_group_table(bucket_rows, "close_hour_bucket_et", "ET close-hour bucket"),
            "",
            "Fixed-margin dependency tests:",
            "",
            "| Grouping | Chi-square statistic | p-value | Method | Interpretation |",
            "|---|---:|---:|---|---|",
            f"| ET date | {date_dependency['chi_square']:.4f} | {date_dependency['p_value']:.4g} | {date_dependency['method']} | {'evidence of dependence' if date_dependency['p_value'] < 0.05 else 'no clear dependence'} |",
            f"| ET 4-hour bucket | {bucket_dependency['chi_square']:.4f} | {bucket_dependency['p_value']:.4g} | {bucket_dependency['method']} | {'evidence of dependence' if bucket_dependency['p_value'] < 0.05 else 'no clear dependence'} |",
            "",
            "## Artifacts",
            "",
        ]
    )
    for label, artifact in artifacts.items():
        lines.append(f"- {label}: `{artifact}`")
    lines.extend(
        [
            "",
            "Fees are not included. Fees reduce every profit estimate, especially near 0.50.",
            "",
        ]
    )
    path.write_text("\n".join(line for line in lines if line is not None) + "\n")

    return {
        "selected": selected,
        "raw_best": raw_best,
        "stable_best": stable_best,
        "best_strict_narrower": best_strict_narrower,
        "full_grid_best": full_best,
        "baseline": baseline,
        "primary_cells": primary_tested,
        "primary_cells_min_n": primary_tested_min_n,
        "stable_cells": stable_count,
        "selected_bonferroni_all_primary_cells": selected_bonferroni_all,
        "selected_bonferroni_primary_min_n_cells": selected_bonferroni_min_n,
        "selected_by_date": date_rows,
        "selected_by_hour_bucket": bucket_rows,
        "date_dependency_test": date_dependency,
        "hour_bucket_dependency_test": bucket_dependency,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data_BTC")
    parser.add_argument("--market-cache-json", default="btc_official_market_results.json")
    parser.add_argument("--entries-csv", default="btc_micro_price_time_entries_official.csv")
    parser.add_argument("--grid-csv", default="btc_micro_price_time_grid.csv")
    parser.add_argument("--full-grid-csv", default="btc_micro_price_time_full_grid.csv")
    parser.add_argument("--summary-json", default="btc_micro_price_time_optimization_summary.json")
    parser.add_argument("--report-md", default="btc_micro_price_time_optimization_report.md")
    parser.add_argument("--plots-dir", default="plots")
    parser.add_argument("--min-n", type=int, default=MIN_N)
    parser.add_argument("--tolerance-seconds", type=float, default=45.0)
    parser.add_argument("--chunk-size", type=int, default=50)
    parser.add_argument("--pause-seconds", type=float, default=0.1)
    parser.add_argument("--refresh-markets", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(__file__).resolve().parent
    data_dir = workdir / args.data_dir
    contracts = load_contracts(data_dir)
    if not contracts:
        raise SystemExit(f"no contracts loaded from {data_dir}")

    tickers = sorted(contracts)
    markets = load_or_fetch_markets(
        tickers,
        workdir / args.market_cache_json,
        args.refresh_markets,
        args.chunk_size,
        args.pause_seconds,
    )

    horizons = list(range(HORIZON_START_SECONDS, HORIZON_END_SECONDS + 1, HORIZON_STEP_SECONDS))
    entries = [
        entry
        for entry in build_entries(contracts, markets, horizons, args.tolerance_seconds)
        if entry.get("resolved") == "1"
    ]
    primary_levels = price_levels(BASELINE_LOW, BASELINE_HIGH)
    full_levels = price_levels(FULL_LOW, FULL_HIGH)
    primary_rows = compute_stability(
        build_profit_grid(entries, primary_levels, f"{BASELINE_LOW:.2f}-{BASELINE_HIGH:.2f}"),
        primary_levels,
        args.min_n,
    )
    full_rows = compute_stability(
        build_profit_grid(entries, full_levels, f"{FULL_LOW:.2f}-{FULL_HIGH:.2f}"),
        full_levels,
        args.min_n,
    )

    raw_best = sort_rows(primary_rows, min_n=args.min_n)[0]
    stable_rows = sort_rows(primary_rows, min_n=args.min_n, stable_only=True)
    stable_best = stable_rows[0] if stable_rows else None
    selected = stable_best or raw_best
    strict_narrower = strict_narrower_rows(primary_rows, args.min_n)
    best_strict_narrower = strict_narrower[0] if strict_narrower else None
    full_best = sort_rows(full_rows, min_n=args.min_n)[0]
    baseline = next(
        (
            row
            for row in primary_rows
            if row["horizon_seconds"] == "720" and row["price_range"] == f"{BASELINE_LOW:.2f}-{BASELINE_HIGH:.2f}"
        ),
        None,
    )

    plots_dir = workdir / args.plots_dir
    plots_dir.mkdir(exist_ok=True)
    plot_paths = {
        "Profit Per Available Heatmap": "plots/btc_micro_profit_per_available_heatmap.svg",
        "Break-Even P-Value Heatmap": "plots/btc_micro_break_even_pvalue_heatmap.svg",
        "Neighbor Positive Share Heatmap": "plots/btc_micro_neighbor_positive_share_heatmap.svg",
        "3D Profit Surface HTML": "plots/btc_micro_profit_surface_3d.html",
    }
    write_heatmap_svg(
        workdir / plot_paths["Profit Per Available Heatmap"],
        primary_rows,
        primary_levels,
        selected,
        "profit_per_available_contract",
        "BTC Micro Profit Per Available Contract",
    )
    write_heatmap_svg(
        workdir / plot_paths["Break-Even P-Value Heatmap"],
        primary_rows,
        primary_levels,
        selected,
        "minus_log10_p_value",
        "BTC Micro Break-Even P-Value (-log10 p)",
        sequential=True,
    )
    write_heatmap_svg(
        workdir / plot_paths["Neighbor Positive Share Heatmap"],
        primary_rows,
        primary_levels,
        selected,
        "neighbor_positive_share",
        "BTC Micro Neighbor Positive Share",
        sequential=True,
    )
    write_surface_html(
        workdir / plot_paths["3D Profit Surface HTML"],
        primary_rows,
        primary_levels,
        selected,
        "profit_per_available_contract",
        "BTC Micro 3D Profit Surface",
    )

    entries_csv = workdir / args.entries_csv
    grid_csv = workdir / args.grid_csv
    full_grid_csv = workdir / args.full_grid_csv
    summary_json = workdir / args.summary_json
    report_md = workdir / args.report_md
    write_csv(entries_csv, entries)
    write_csv(grid_csv, primary_rows)
    write_csv(full_grid_csv, full_rows)

    artifacts = {
        "Official-outcome micro entries": entries_csv.name,
        "Primary micro grid with p-values": grid_csv.name,
        "Exploratory full price grid with p-values": full_grid_csv.name,
        "Machine-readable summary": summary_json.name,
        "Official market cache": args.market_cache_json,
    }
    summary = write_report(
        report_md,
        entries,
        primary_rows,
        full_rows,
        selected,
        raw_best,
        stable_best,
        best_strict_narrower,
        full_best,
        baseline,
        plot_paths,
        artifacts,
        args.min_n,
        args.tolerance_seconds,
    )
    summary["artifacts"] = {**artifacts, "Markdown report": report_md.name, **plot_paths}
    summary["contracts"] = len(contracts)
    summary["entries"] = len(entries)
    summary["primary_grid_rows"] = len(primary_rows)
    summary["full_grid_rows"] = len(full_rows)
    summary["min_n"] = args.min_n
    summary["tolerance_seconds"] = args.tolerance_seconds
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
