#!/usr/bin/env python3
"""Run official-outcome Kalshi profit-stability analysis for multiple coins."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import random
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analyze_btc_more_likely_official import HORIZONS_SECONDS, build_entries, fetch_markets, load_contracts, official_result
from visualize_btc_profit_stability import (
    compute_stability,
    fmt,
    build_profit_grid,
    grouped_performance,
    markdown_group_table,
    markdown_profit_table,
    pct,
    sort_by_metric,
    write_csv,
    write_heatmap_svg,
    write_surface_html,
)


COINS = ["BTC", "ETH", "BNB", "XRP", "SOL", "HYPE", "DOGE"]
MIN_N = 20


def coin_prefix(coin: str) -> str:
    return coin.lower()


def load_or_fetch_markets(
    tickers: list[str],
    cache_path: Path,
    chunk_size: int,
    pause_seconds: float,
    refresh: bool,
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


def count_fixed_margin_tables(group_sizes: list[int], total_wins: int, max_count: int) -> int:
    count = 0

    def recurse(index: int, remaining_wins: int) -> None:
        nonlocal count
        if count > max_count:
            return
        if index == len(group_sizes) - 1:
            if 0 <= remaining_wins <= group_sizes[index]:
                count += 1
            return

        n = group_sizes[index]
        remaining_capacity = sum(group_sizes[index + 1 :])
        low = max(0, remaining_wins - remaining_capacity)
        high = min(n, remaining_wins)
        for wins in range(low, high + 1):
            recurse(index + 1, remaining_wins - wins)

    recurse(0, total_wins)
    return count


def exact_fixed_margin_chi_square_p_value(rows: list[dict[str, str]], table_count: int) -> dict[str, Any]:
    group_sizes = [int(row["n"]) for row in rows]
    total_n = sum(group_sizes)
    total_wins = sum(int(row["wins"]) for row in rows)
    observed = chi_square_from_group_rows(rows)
    denominator_log = math.lgamma(total_n + 1) - math.lgamma(total_wins + 1) - math.lgamma(total_n - total_wins + 1)
    comb_log_cache = {
        (n, k): math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
        for n in group_sizes
        for k in range(n + 1)
    }

    p_value = 0.0

    def recurse(index: int, remaining_wins: int, allocated: list[int], log_probability: float) -> None:
        nonlocal p_value
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
            return

        n = group_sizes[index]
        remaining_capacity = sum(group_sizes[index + 1 :])
        low = max(0, remaining_wins - remaining_capacity)
        high = min(n, remaining_wins)
        for wins in range(low, high + 1):
            recurse(index + 1, remaining_wins - wins, allocated + [wins], log_probability + comb_log_cache[(n, wins)])

    recurse(0, total_wins, [], 0.0)
    return {
        "chi_square": observed,
        "p_value": min(1.0, max(0.0, p_value)),
        "method": "exact_fixed_margin",
        "tables_enumerated": table_count,
        "permutations": 0,
    }


def permutation_fixed_margin_chi_square_p_value(
    rows: list[dict[str, str]],
    permutations: int,
    seed: int,
    table_count: int,
) -> dict[str, Any]:
    rng = random.Random(seed)
    group_sizes = [int(row["n"]) for row in rows]
    outcomes = [1] * sum(int(row["wins"]) for row in rows)
    outcomes += [0] * sum(int(row["losses"]) for row in rows)
    observed = chi_square_from_group_rows(rows)
    greater_or_equal = 0

    for _ in range(permutations):
        rng.shuffle(outcomes)
        offset = 0
        simulated_rows = []
        for index, n in enumerate(group_sizes):
            group_outcomes = outcomes[offset : offset + n]
            offset += n
            wins = sum(group_outcomes)
            simulated_rows.append({"n": str(n), "wins": str(wins), "losses": str(n - wins)})
        if chi_square_from_group_rows(simulated_rows) >= observed - 1e-12:
            greater_or_equal += 1

    return {
        "chi_square": observed,
        "p_value": (greater_or_equal + 1) / (permutations + 1),
        "method": "fixed_margin_permutation",
        "tables_enumerated": table_count,
        "permutations": permutations,
    }


def dependency_test(
    rows: list[dict[str, str]],
    max_exact_tables: int = 2_000_000,
    permutations: int = 50_000,
    seed: int = 20260607,
) -> dict[str, Any]:
    if len(rows) <= 1:
        return {
            "chi_square": 0.0,
            "p_value": 1.0,
            "method": "not_applicable",
            "tables_enumerated": 0,
            "permutations": 0,
        }
    group_sizes = [int(row["n"]) for row in rows]
    total_wins = sum(int(row["wins"]) for row in rows)
    table_count = count_fixed_margin_tables(group_sizes, total_wins, max_exact_tables)
    if table_count <= max_exact_tables:
        return exact_fixed_margin_chi_square_p_value(rows, table_count)
    return permutation_fixed_margin_chi_square_p_value(rows, permutations, seed, table_count)


def write_coin_report(
    path: Path,
    coin: str,
    entries: list[dict[str, str]],
    rows: list[dict[str, str]],
    selected: dict[str, str],
    raw_best: dict[str, str],
    plots: dict[str, str],
    artifacts: dict[str, str],
    min_n: int,
    contract_count: int,
    resolved_contract_count: int,
    date_dependency: dict[str, Any],
    bucket_dependency: dict[str, Any],
) -> None:
    date_rows = grouped_performance(entries, selected, "close_date_et")
    bucket_rows = grouped_performance(entries, selected, "close_hour_bucket_et")
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    stable_text = "stable" if selected["stable"] == "1" else "not stable"
    if selected["stable"] == "1":
        result_sentence = f"The best parameter that is both profitable and stable is `T={selected['horizon_seconds']}s`, price range `{selected['price_range']}`."
        raw_best_sentence = (
            f"The raw objective argmax with `N >= {min_n}` is `T={raw_best['horizon_seconds']}s`, range `{raw_best['price_range']}`, "
            f"profit/available `{fmt(float(raw_best['profit_per_available_contract']))}`."
        )
    else:
        result_sentence = (
            f"No profitable parameter passed the stability criteria. The best exploratory objective cell is "
            f"`T={selected['horizon_seconds']}s`, price range `{selected['price_range']}`."
        )
        raw_best_sentence = (
            f"The raw objective argmax with `N >= {min_n}` is this same exploratory cell: "
            f"`T={raw_best['horizon_seconds']}s`, range `{raw_best['price_range']}`, "
            f"profit/available `{fmt(float(raw_best['profit_per_available_contract']))}`."
        )

    lines = [
        f"# {coin} Kalshi Profit-Per-Available Stability Analysis",
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
        "This equals gross P&L per available contract at that horizon. `N_total_backtested_at_T` is the count of resolved contracts with a usable Kalshi quote row at that T.",
        "",
        "## Result",
        "",
        result_sentence,
        "",
        f"- Stability classification: `{stable_text}`",
        f"- Profit per available contract: `{fmt(float(selected['profit_per_available_contract']))}`",
        f"- Gross P&L: `{fmt(float(selected['gross_pnl']))}` across `{selected['total_available_contracts']}` available contracts",
        f"- N traded in range: `{selected['n']}`",
        f"- Coverage: `{pct(float(selected['coverage']))}`",
        f"- P(success): `{pct(float(selected['p_success']))}`",
        f"- Average cost c: `{fmt(float(selected['avg_cost']))}`",
        f"- EV p-c inside range: `{fmt(float(selected['ev_p_minus_c']))}`",
        f"- One-sided break-even p-value: `{float(selected['p_value_break_even']):.6g}`",
        f"- Contracts in source data: `{contract_count}`",
        f"- Resolved official Kalshi outcomes: `{resolved_contract_count}`",
        "",
        raw_best_sentence,
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
            lines.extend([f"### {label}", "", f"![{label}]({plot_path})", ""])
        else:
            lines.append(f"- {label}: `{plot_path}`")
    lines.append("")

    lines.extend(
        [
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
            "## Date/Time Dependency Tests",
            "",
            "These tests ask whether the selected rule's win/loss outcomes vary by date or by time bucket. They condition on the total number of wins and group sizes. For small tables the p-value is exact over all fixed-margin allocations; otherwise it uses deterministic fixed-margin permutation sampling.",
            "",
            "| Grouping | Chi-square statistic | p-value | Method | Tables/permutations | Interpretation |",
            "|---|---:|---:|---|---:|---|",
            f"| ET date | {date_dependency['chi_square']:.4f} | {date_dependency['p_value']:.4f} | {date_dependency['method']} | {date_dependency['tables_enumerated'] or date_dependency['permutations']} | {'evidence of dependence' if date_dependency['p_value'] < 0.05 else 'no clear dependence'} |",
            f"| ET 4-hour bucket | {bucket_dependency['chi_square']:.4f} | {bucket_dependency['p_value']:.4f} | {bucket_dependency['method']} | {bucket_dependency['tables_enumerated'] or bucket_dependency['permutations']} | {'evidence of dependence' if bucket_dependency['p_value'] < 0.05 else 'no clear dependence'} |",
            "",
            "Conclusion: profitability may vary economically by date/time, but only p-values below 0.05 are flagged as statistically clear win-rate dependence in this report.",
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


def analyze_coin(
    coin: str,
    workdir: Path,
    chunk_size: int,
    pause_seconds: float,
    tolerance_seconds: float,
    min_n: int,
    refresh_markets: bool,
    dependency_permutations: int,
) -> dict[str, Any]:
    prefix = coin_prefix(coin)
    data_dir = workdir / f"data_{coin}"
    contracts = load_contracts(data_dir)
    if not contracts:
        raise RuntimeError(f"No contracts loaded for {coin} from {data_dir}")

    tickers = sorted(contracts)
    market_cache = workdir / f"{prefix}_official_market_results.json"
    markets = load_or_fetch_markets(tickers, market_cache, chunk_size, pause_seconds, refresh_markets)

    entries = build_entries(contracts, markets, HORIZONS_SECONDS, tolerance_seconds)
    resolved_entries = [entry for entry in entries if entry.get("resolved") == "1"]
    rows = compute_stability(build_profit_grid(resolved_entries), min_n)
    raw_best = sort_by_metric(rows, "profit_per_available_contract", min_n=min_n)[0]
    stable_rows = sort_by_metric(rows, "profit_per_available_contract", min_n=min_n, stable_only=True)
    selected = stable_rows[0] if stable_rows else raw_best

    entries_csv = workdir / f"{prefix}_more_likely_entries_official.csv"
    grid_csv = workdir / f"{prefix}_profit_per_available_grid.csv"
    summary_json = workdir / f"{prefix}_profit_stability_summary.json"
    report_md = workdir / f"{prefix}_profit_stability_report.md"
    write_csv(entries_csv, entries)
    write_csv(grid_csv, rows)

    plots_dir = workdir / "plots"
    plots_dir.mkdir(exist_ok=True)
    plot_paths = {
        "Profit Per Available Contract Heatmap": f"plots/{prefix}_profit_per_available_heatmap.svg",
        "EV p-c Heatmap": f"plots/{prefix}_ev_heatmap.svg",
        "Neighbor Positive Share Heatmap": f"plots/{prefix}_neighbor_positive_share_heatmap.svg",
        "Traded Coverage Heatmap": f"plots/{prefix}_traded_coverage_heatmap.svg",
        "3D Profit Surface HTML": f"plots/{prefix}_profit_surface_3d.html",
        "3D Stability Surface HTML": f"plots/{prefix}_stability_surface_3d.html",
    }
    write_heatmap_svg(workdir / plot_paths["Profit Per Available Contract Heatmap"], rows, "profit_per_available_contract", f"{coin} Profit Per Available Contract", selected)
    write_heatmap_svg(workdir / plot_paths["EV p-c Heatmap"], rows, "ev_p_minus_c", f"{coin} EV p-c By T And Price Range", selected)
    write_heatmap_svg(
        workdir / plot_paths["Neighbor Positive Share Heatmap"],
        rows,
        "neighbor_positive_share",
        f"{coin} Stability: Neighbor Positive Share",
        selected,
        sequential=True,
    )
    write_heatmap_svg(workdir / plot_paths["Traded Coverage Heatmap"], rows, "coverage", f"{coin} Traded Coverage By T And Price Range", selected, sequential=True)
    write_surface_html(workdir / plot_paths["3D Profit Surface HTML"], rows, selected, "profit_per_available_contract", f"{coin} 3D Profit Per Available Contract Surface")
    write_surface_html(workdir / plot_paths["3D Stability Surface HTML"], rows, selected, "neighbor_positive_share", f"{coin} 3D Neighbor Positive Share Stability Surface", sequential=True)

    date_rows = grouped_performance(resolved_entries, selected, "close_date_et")
    bucket_rows = grouped_performance(resolved_entries, selected, "close_hour_bucket_et")
    date_dependency = dependency_test(date_rows, permutations=dependency_permutations, seed=20260607 + len(coin))
    bucket_dependency = dependency_test(bucket_rows, permutations=dependency_permutations, seed=20260617 + len(coin))

    resolved_contract_count = sum(1 for ticker in contracts if official_result(markets.get(ticker, {})))
    artifacts = {
        "Entry ledger": entries_csv.name,
        "Profit-per-available grid": grid_csv.name,
        "Machine-readable summary": summary_json.name,
        "Official market cache": market_cache.name,
    }
    write_coin_report(
        report_md,
        coin,
        resolved_entries,
        rows,
        selected,
        raw_best,
        plot_paths,
        artifacts,
        min_n,
        len(contracts),
        resolved_contract_count,
        date_dependency,
        bucket_dependency,
    )

    summary = {
        "coin": coin,
        "contracts": len(contracts),
        "resolved_contracts": resolved_contract_count,
        "entries": len(entries),
        "resolved_entries": len(resolved_entries),
        "grid_rows": len(rows),
        "raw_best_min_n": raw_best,
        "selected": selected,
        "stable_best": stable_rows[0] if stable_rows else None,
        "stable_cell_count": sum(1 for row in rows if row["stable"] == "1"),
        "positive_n_min_count": sum(1 for row in rows if int(row["n"]) >= min_n and float(row["profit_per_available_contract"]) > 0),
        "selected_by_date": date_rows,
        "selected_by_hour_bucket": bucket_rows,
        "date_dependency_test": date_dependency,
        "hour_bucket_dependency_test": bucket_dependency,
        "artifacts": {
            **artifacts,
            "Markdown report": report_md.name,
            **plot_paths,
        },
    }
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def md_number(value: str | float, decimals: int = 4) -> str:
    return f"{float(value):.{decimals}f}"


def write_all_coin_summary(workdir: Path, summaries: list[dict[str, Any]]) -> None:
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    sorted_summaries = sorted(summaries, key=lambda item: float(item["selected"]["profit_per_available_contract"]), reverse=True)
    stable_count = sum(1 for item in summaries if item["selected"]["stable"] == "1")
    significant_break_even = [item["coin"] for item in summaries if float(item["selected"]["p_value_break_even"]) < 0.05]
    date_dependent = [item["coin"] for item in summaries if item["date_dependency_test"]["p_value"] < 0.05]
    bucket_dependent = [item["coin"] for item in summaries if item["hour_bucket_dependency_test"]["p_value"] < 0.05]

    lines = [
        "# All-Coin Kalshi Profit-Stability Summary",
        "",
        f"Generated: `{generated_at}`",
        "",
        "## Table Of Contents",
        "",
        "- [Method](#method)",
        "- [Leaderboard](#leaderboard)",
        "- [Statistical Flags](#statistical-flags)",
        "- [Per-Coin Reports](#per-coin-reports)",
        "",
        "## Method",
        "",
        "Each coin uses Kalshi quote columns for entry reconstruction and official Kalshi API market outcomes for settlement. Outcomes are not inferred from spot prices. The optimized objective is gross profit per available contract:",
        "",
        "```text",
        "(p - c) * N_traded_in_range / N_total_backtested_at_T",
        "```",
        "",
        "Stability requires positive profit per available contract, `N >= 20`, at least four valid neighboring cells, at least 70% positive neighbors, positive average neighbor profit, and membership in a positive connected component of at least eight cells.",
        "",
        "## Leaderboard",
        "",
        "| Rank | Coin | Selected T | Price range | Stable | N traded | N total | Coverage | P(success) | Avg cost | EV p-c | Profit/available | Gross P&L | Break-even p-value | Date dep p | Time-bucket dep p |",
        "|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, summary in enumerate(sorted_summaries, 1):
        selected = summary["selected"]
        lines.append(
            f"| {rank} | {summary['coin']} | {selected['horizon_seconds']} | {selected['price_range']} | {selected['stable']} | "
            f"{selected['n']} | {selected['total_available_contracts']} | {pct(float(selected['coverage']))} | "
            f"{pct(float(selected['p_success']))} | {md_number(selected['avg_cost'])} | {md_number(selected['ev_p_minus_c'])} | "
            f"{md_number(selected['profit_per_available_contract'])} | {md_number(selected['gross_pnl'])} | "
            f"{float(selected['p_value_break_even']):.4g} | {summary['date_dependency_test']['p_value']:.4g} | "
            f"{summary['hour_bucket_dependency_test']['p_value']:.4g} |"
        )

    lines.extend(
        [
            "",
            "## Statistical Flags",
            "",
            f"- Stable selected parameters: `{stable_count}/{len(summaries)}` coins.",
            f"- Break-even p-value < 0.05: `{', '.join(significant_break_even) if significant_break_even else 'none'}`.",
            f"- Date-dependence p-value < 0.05: `{', '.join(date_dependent) if date_dependent else 'none'}`.",
            f"- Time-bucket-dependence p-value < 0.05: `{', '.join(bucket_dependent) if bucket_dependent else 'none'}`.",
            "",
            "Interpretation: a high profit-per-available value with a non-significant break-even p-value should be treated as an exploratory signal, not confirmed edge. Date/time dependency flags indicate that the selected rule may be regime-sensitive.",
            "",
            "## Per-Coin Reports",
            "",
        ]
    )
    for summary in sorted(summaries, key=lambda item: item["coin"]):
        report = summary["artifacts"]["Markdown report"]
        lines.append(f"- [{summary['coin']}]({report})")
    lines.append("")
    (workdir / "all_coin_profit_stability_summary.md").write_text("\n".join(lines))
    (workdir / "all_coin_profit_stability_summary.json").write_text(json.dumps(summaries, indent=2, sort_keys=True) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coins", nargs="+", default=COINS)
    parser.add_argument("--chunk-size", type=int, default=50)
    parser.add_argument("--pause-seconds", type=float, default=0.05)
    parser.add_argument("--tolerance-seconds", type=float, default=45.0)
    parser.add_argument("--min-n", type=int, default=MIN_N)
    parser.add_argument("--refresh-markets", action="store_true")
    parser.add_argument("--dependency-permutations", type=int, default=50_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(__file__).resolve().parent
    summaries = []
    started = time.time()
    for coin in args.coins:
        print(f"analyzing {coin}...", flush=True)
        summaries.append(
            analyze_coin(
                coin.upper(),
                workdir,
                args.chunk_size,
                args.pause_seconds,
                args.tolerance_seconds,
                args.min_n,
                args.refresh_markets,
                args.dependency_permutations,
            )
        )
    write_all_coin_summary(workdir, summaries)
    print(json.dumps({"coins": args.coins, "seconds": round(time.time() - started, 3), "summary": "all_coin_profit_stability_summary.md"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
