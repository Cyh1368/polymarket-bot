#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PLOTS_DIR = ROOT / "plots"
GRID_CSV = ROOT / "kalshi_strategy_optimization_grid.csv"
SUMMARY_JSON = ROOT / "kalshi_strategy_optimization_summary.json"
REPORT_MD = ROOT / "kalshi_strategy_optimization_report.md"

CONTRACTS_PER_TRADE = 2
FEE_RATE = 0.07
T_GRID_SECONDS = list(range(0, 901, 30))
PRICE_ENDPOINTS = [round(0.50 + 0.05 * i, 2) for i in range(11)]
ENTRY_TOLERANCE_SECONDS = 45.0
BOOTSTRAPS = 1000
RNG_SEED = 20260604
MIN_TRADES_FOR_STABLE = 20


@dataclass(frozen=True)
class Entry:
    contract_id: str
    t_seconds: int
    p_mid: float
    side: str
    cost: float
    fee: float
    pnl: float
    success: int
    actual_label: int
    remaining_seconds: float


def fnum(value: object) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "--"
    return f"{100.0 * value:.2f}%"


def money(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "--"
    return f"{value:+.4f}"


def parse_time_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def first_valid(series: pd.Series) -> object | None:
    valid = series.dropna()
    if valid.empty:
        return None
    return valid.iloc[0]


def last_valid(series: pd.Series) -> object | None:
    valid = series.dropna()
    if valid.empty:
        return None
    return valid.iloc[-1]


def is_valid_base_quote(row: pd.Series) -> bool:
    yes_bid = fnum(row.get("kalshi_yes_bid"))
    no_bid = fnum(row.get("kalshi_no_bid"))
    yes_ask = fnum(row.get("kalshi_yes_ask"))
    no_ask = fnum(row.get("kalshi_no_ask"))
    yes_mid = fnum(row.get("kalshi_yes_mid"))
    prices = [yes_bid, no_bid, yes_ask, no_ask, yes_mid]
    if any(value is None for value in prices):
        return False
    if not (0.0 <= yes_bid <= 1.0 and 0.0 <= no_bid <= 1.0):
        return False
    if not (0.0 < yes_ask < 1.0 and 0.0 < no_ask < 1.0):
        return False
    if not (0.0 <= yes_mid <= 1.0):
        return False
    if yes_bid + no_bid > 1.0 + 1e-9:
        return False
    return True


def selected_trade(row: pd.Series) -> tuple[str, float, float, float] | None:
    yes_mid = fnum(row.get("kalshi_yes_mid"))
    if yes_mid is None:
        return None
    if yes_mid >= 0.5:
        side = "YES"
        p_mid = yes_mid
        cost = fnum(row.get("kalshi_yes_ask"))
        qty = fnum(row.get("kalshi_best_no_bid_qty"))
    else:
        side = "NO"
        p_mid = 1.0 - yes_mid
        cost = fnum(row.get("kalshi_no_ask"))
        qty = fnum(row.get("kalshi_best_yes_bid_qty"))
    if cost is None or not 0.0 < cost < 1.0:
        return None
    if qty is None or qty < CONTRACTS_PER_TRADE:
        return None
    return side, p_mid, cost, qty


def load_contract_entries(path: Path) -> tuple[str, list[Entry], dict[str, object]]:
    df = pd.read_csv(path)
    contract_id = path.stem.replace("cli_predictor_polymarket_", "")
    meta: dict[str, object] = {"contract_id": contract_id, "rows": len(df)}
    if df.empty:
        meta["skip_reason"] = "empty_csv"
        return contract_id, [], meta

    close_value = first_valid(df.get("kalshi_close_time", pd.Series(dtype=object)))
    close_time = pd.to_datetime(close_value, utc=True, errors="coerce")
    if pd.isna(close_time):
        meta["skip_reason"] = "missing_close_time"
        return contract_id, [], meta

    final_price = fnum(last_valid(df.get("kalshi_btc_price", pd.Series(dtype=object))))
    target = fnum(last_valid(df.get("kalshi_btc_target", pd.Series(dtype=object))))
    if final_price is None or target is None:
        meta["skip_reason"] = "missing_outcome"
        return contract_id, [], meta
    actual_label = int(final_price > target)
    meta.update({"final_price": final_price, "target": target, "actual_label": actual_label})

    df = df.copy()
    df["_timestamp"] = parse_time_series(df["timestamp_utc"])
    df["_remaining"] = (close_time - df["_timestamp"]).dt.total_seconds()
    df = df[df["_remaining"].notna() & (df["_remaining"] >= 0)].copy()
    if df.empty:
        meta["skip_reason"] = "no_preclose_rows"
        return contract_id, [], meta

    entries: list[Entry] = []
    for t_seconds in T_GRID_SECONDS:
        candidates = df[(df["_remaining"] - t_seconds).abs() <= ENTRY_TOLERANCE_SECONDS]
        if candidates.empty:
            continue
        closest_idx = (candidates["_remaining"] - t_seconds).abs().idxmin()
        row = candidates.loc[closest_idx]
        if not is_valid_base_quote(row):
            continue
        selected = selected_trade(row)
        if selected is None:
            continue
        side, p_mid, cost, _qty = selected
        predicted_label = 1 if side == "YES" else 0
        success = int(predicted_label == actual_label)
        fee = FEE_RATE * cost * (1.0 - cost)
        pnl = (1.0 - cost - fee) if success else (-cost - fee)
        entries.append(
            Entry(
                contract_id=contract_id,
                t_seconds=t_seconds,
                p_mid=p_mid,
                side=side,
                cost=cost,
                fee=fee,
                pnl=pnl,
                success=success,
                actual_label=actual_label,
                remaining_seconds=float(row["_remaining"]),
            )
        )
    return contract_id, entries, meta


def band_grid() -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for low in PRICE_ENDPOINTS[:-1]:
        for high in PRICE_ENDPOINTS[1:]:
            if high > low:
                out.append((low, high))
    return out


def combo_label(t_seconds: int, low: float, high: float) -> str:
    return f"T={t_seconds}s [{low:.2f},{high:.2f})"


def build_grid(entries_df: pd.DataFrame, contract_ids: list[str]) -> tuple[pd.DataFrame, np.ndarray]:
    n_contracts = len(contract_ids)
    contract_index = {contract_id: idx for idx, contract_id in enumerate(contract_ids)}
    bands = band_grid()
    rows: list[dict[str, object]] = []
    pnl_vectors: list[np.ndarray] = []

    grouped = {int(t): frame for t, frame in entries_df.groupby("t_seconds")}
    for t_seconds in T_GRID_SECONDS:
        frame = grouped.get(t_seconds, pd.DataFrame())
        for low, high in bands:
            vector = np.zeros(n_contracts, dtype=float)
            if frame.empty:
                traded = frame
            else:
                traded = frame[(frame["p_mid"] >= low) & (frame["p_mid"] < high)]
                for item in traded.itertuples(index=False):
                    vector[contract_index[item.contract_id]] = float(item.pnl)
            n_traded = int(len(traded))
            total_pnl = float(vector.sum())
            ev_all = total_pnl / n_contracts if n_contracts else float("nan")
            ev_traded = float(traded["pnl"].mean()) if n_traded else float("nan")
            sd_all = float(vector.std(ddof=1)) if n_contracts > 1 else float("nan")
            se_all = sd_all / math.sqrt(n_contracts) if n_contracts > 1 else float("nan")
            rows.append(
                {
                    "label": combo_label(t_seconds, low, high),
                    "t_seconds": t_seconds,
                    "t_minutes": t_seconds / 60.0,
                    "band_low": low,
                    "band_high": high,
                    "band_center": (low + high) / 2.0,
                    "band_width": high - low,
                    "n_contracts": n_contracts,
                    "n_traded": n_traded,
                    "trade_rate": n_traded / n_contracts if n_contracts else float("nan"),
                    "total_net_pnl_after_fee": total_pnl,
                    "ev_per_available_contract_after_fee": ev_all,
                    "ev_per_traded_contract_after_fee": ev_traded,
                    "se_ev_all": se_all,
                    "ci95_low_ev_all": ev_all - 1.96 * se_all if math.isfinite(se_all) else float("nan"),
                    "ci95_high_ev_all": ev_all + 1.96 * se_all if math.isfinite(se_all) else float("nan"),
                    "success_rate_traded": float(traded["success"].mean()) if n_traded else float("nan"),
                    "avg_mid_p_traded": float(traded["p_mid"].mean()) if n_traded else float("nan"),
                    "avg_cost_traded": float(traded["cost"].mean()) if n_traded else float("nan"),
                    "avg_fee_traded": float(traded["fee"].mean()) if n_traded else float("nan"),
                }
            )
            pnl_vectors.append(vector)

    grid = pd.DataFrame(rows)
    pnl_matrix = np.vstack(pnl_vectors)
    return grid, pnl_matrix


def add_local_stability(grid: pd.DataFrame) -> pd.DataFrame:
    grid = grid.copy()
    local_means: list[float] = []
    local_stds: list[float] = []
    local_mins: list[float] = []
    local_positive_rates: list[float] = []
    values = grid["ev_per_available_contract_after_fee"].to_numpy()
    for row in grid.itertuples(index=False):
        mask = (
            (grid["t_seconds"].sub(row.t_seconds).abs() <= 60)
            & (grid["band_low"].sub(row.band_low).abs() <= 0.05 + 1e-9)
            & (grid["band_high"].sub(row.band_high).abs() <= 0.05 + 1e-9)
        )
        neighbor_values = values[mask.to_numpy()]
        local_means.append(float(np.mean(neighbor_values)))
        local_stds.append(float(np.std(neighbor_values, ddof=1)) if len(neighbor_values) > 1 else 0.0)
        local_mins.append(float(np.min(neighbor_values)))
        local_positive_rates.append(float(np.mean(neighbor_values > 0.0)))
    grid["local_mean_ev_all"] = local_means
    grid["local_std_ev_all"] = local_stds
    grid["local_min_ev_all"] = local_mins
    grid["local_positive_fraction"] = local_positive_rates
    grid["stability_score"] = grid["ci95_low_ev_all"] - grid["local_std_ev_all"]
    return grid


def bootstrap_grid(grid: pd.DataFrame, pnl_matrix: np.ndarray) -> pd.DataFrame:
    rng = np.random.default_rng(RNG_SEED)
    n_contracts = pnl_matrix.shape[1]
    winner_counts = np.zeros(len(grid), dtype=int)
    positive_counts = np.zeros(len(grid), dtype=int)
    top_low_ci_values = np.zeros(len(grid), dtype=float)
    tracked = grid.index.to_numpy()
    for _ in range(BOOTSTRAPS):
        sample_idx = rng.integers(0, n_contracts, size=n_contracts)
        means = pnl_matrix[:, sample_idx].mean(axis=1)
        winner_counts[int(np.argmax(means))] += 1
        positive_counts += means > 0.0
        top_low_ci_values += means
    # The accumulated mean is not used for CIs; it is a cheap sanity field.
    out = grid.copy()
    out["bootstrap_raw_winner_frequency"] = winner_counts / BOOTSTRAPS
    out["bootstrap_prob_ev_positive"] = positive_counts / BOOTSTRAPS
    out["bootstrap_mean_ev_all"] = top_low_ci_values / BOOTSTRAPS
    return out


def table(df: pd.DataFrame, columns: list[str], limit: int = 10) -> str:
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, sep]
    for _, row in df.head(limit).iterrows():
        cells: list[str] = []
        for col in columns:
            val = row[col]
            if col in {"label"}:
                cells.append(str(val))
            elif col in {"t_seconds", "n_traded", "n_contracts"}:
                cells.append(str(int(val)))
            elif col in {"trade_rate", "success_rate_traded", "local_positive_fraction", "bootstrap_raw_winner_frequency", "bootstrap_prob_ev_positive"}:
                cells.append(pct(float(val)))
            elif col.startswith("ev_") or col.startswith("ci95") or col in {
                "total_net_pnl_after_fee",
                "avg_cost_traded",
                "avg_fee_traded",
                "local_mean_ev_all",
                "local_std_ev_all",
                "local_min_ev_all",
                "stability_score",
            }:
                cells.append(money(float(val)))
            else:
                cells.append(f"{float(val):.4f}" if isinstance(val, (float, np.floating)) else str(val))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def heatmap_plot(grid: pd.DataFrame, value_col: str, path: Path, title: str, cmap: str = "RdYlGn") -> None:
    top_bands = (
        grid.groupby(["band_low", "band_high"])["n_traded"]
        .sum()
        .sort_values(ascending=False)
        .head(35)
        .index.tolist()
    )
    filtered = grid.set_index(["band_low", "band_high"]).loc[top_bands].reset_index()
    filtered["band"] = filtered.apply(lambda r: f"{r.band_low:.2f}-{r.band_high:.2f}", axis=1)
    pivot = filtered.pivot_table(index="band", columns="t_seconds", values=value_col, aggfunc="mean")
    fig, ax = plt.subplots(figsize=(14, 10))
    im = ax.imshow(pivot.to_numpy(), aspect="auto", cmap=cmap)
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.set_xlabel("T before close (seconds)")
    ax.set_ylabel("More-likely midpoint price band")
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels([str(int(x)) for x in pivot.columns], rotation=90, fontsize=8)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index, fontsize=8)
    fig.colorbar(im, ax=ax, label=value_col)
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def scatter_3d_plot(grid: pd.DataFrame, path: Path) -> None:
    fig = plt.figure(figsize=(12, 9))
    ax = fig.add_subplot(111, projection="3d")
    values = grid["ev_per_available_contract_after_fee"].to_numpy()
    sizes = 10 + 90 * np.clip(grid["trade_rate"].to_numpy(), 0, 1)
    scatter = ax.scatter(
        grid["t_minutes"],
        grid["band_low"],
        grid["band_high"],
        c=values,
        s=sizes,
        cmap="RdYlGn",
        alpha=0.78,
        linewidths=0,
    )
    ax.set_title("Kalshi strategy objective over T and price bands", fontsize=14, fontweight="bold")
    ax.set_xlabel("T before close (minutes)")
    ax.set_ylabel("Band low")
    ax.set_zlabel("Band high")
    fig.colorbar(scatter, ax=ax, shrink=0.7, pad=0.08, label="After-fee EV per available contract")
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def width_surface_plot(grid: pd.DataFrame, path: Path) -> None:
    fig = plt.figure(figsize=(13, 9))
    ax = fig.add_subplot(111, projection="3d")
    selected_widths = [0.05, 0.10, 0.20, 0.30, 0.50]
    for width in selected_widths:
        subset = grid[np.isclose(grid["band_width"], width)]
        if subset.empty:
            continue
        x = subset["t_minutes"].to_numpy()
        y = subset["band_center"].to_numpy()
        z = subset["ev_per_available_contract_after_fee"].to_numpy()
        ax.scatter(x, y, z, s=18, alpha=0.65, label=f"width={width:.2f}")
    ax.set_title("After-fee EV by T, band center, and selected band widths", fontsize=14, fontweight="bold")
    ax.set_xlabel("T before close (minutes)")
    ax.set_ylabel("Band center")
    ax.set_zlabel("EV per available contract")
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def bootstrap_plot(grid: pd.DataFrame, path: Path) -> None:
    top = grid.sort_values("bootstrap_raw_winner_frequency", ascending=False).head(15).copy()
    labels = top["label"].tolist()
    values = top["bootstrap_raw_winner_frequency"].to_numpy()
    fig, ax = plt.subplots(figsize=(12, 7))
    ax.barh(range(len(top)), values, color="#2563eb")
    ax.set_yticks(range(len(top)))
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("Bootstrap winner frequency")
    ax.set_title("How often each parameter set is the best in bootstrap resamples", fontsize=14, fontweight="bold")
    ax.set_xlim(0, max(0.01, values.max() * 1.15))
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def trade_count_plot(grid: pd.DataFrame, path: Path) -> None:
    best_by_t = (
        grid.sort_values("ev_per_available_contract_after_fee", ascending=False)
        .groupby("t_seconds")
        .head(1)
        .sort_values("t_seconds")
    )
    fig, ax1 = plt.subplots(figsize=(12, 6))
    ax1.plot(best_by_t["t_minutes"], best_by_t["ev_per_available_contract_after_fee"], marker="o", label="Best EV/all")
    ax1.fill_between(
        best_by_t["t_minutes"],
        best_by_t["ci95_low_ev_all"],
        best_by_t["ci95_high_ev_all"],
        alpha=0.2,
        label="95% normal CI",
    )
    ax1.set_xlabel("T before close (minutes)")
    ax1.set_ylabel("After-fee EV per available contract")
    ax2 = ax1.twinx()
    ax2.bar(best_by_t["t_minutes"], best_by_t["n_traded"], width=0.28, alpha=0.25, color="#0f766e", label="Trades")
    ax2.set_ylabel("Traded contracts for best band")
    ax1.set_title("Best band at each T: objective and sample size", fontsize=14, fontweight="bold")
    handles1, labels1 = ax1.get_legend_handles_labels()
    handles2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(handles1 + handles2, labels1 + labels2, loc="best")
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def write_report(
    grid: pd.DataFrame,
    entries_df: pd.DataFrame,
    contract_meta: list[dict[str, object]],
    plots: dict[str, Path],
) -> None:
    n_contracts = len(contract_meta)
    raw_best = grid.sort_values("ev_per_available_contract_after_fee", ascending=False).iloc[0]
    lower_best = grid[grid["n_traded"] >= MIN_TRADES_FOR_STABLE].sort_values("ci95_low_ev_all", ascending=False).iloc[0]
    stable_pool = grid[
        (grid["n_traded"] >= MIN_TRADES_FOR_STABLE)
        & (grid["local_positive_fraction"] >= 0.70)
        & (grid["local_min_ev_all"] > 0)
    ]
    stable_best = (
        stable_pool.sort_values("stability_score", ascending=False).iloc[0]
        if not stable_pool.empty
        else lower_best
    )
    current = grid[
        (grid["t_seconds"] == 600)
        & np.isclose(grid["band_low"], 0.60)
        & np.isclose(grid["band_high"], 0.80)
    ].iloc[0]

    top_cols = [
        "label",
        "n_traded",
        "trade_rate",
        "ev_per_available_contract_after_fee",
        "ci95_low_ev_all",
        "ci95_high_ev_all",
        "ev_per_traded_contract_after_fee",
        "success_rate_traded",
        "avg_cost_traded",
        "bootstrap_prob_ev_positive",
        "bootstrap_raw_winner_frequency",
    ]
    stable_cols = [
        "label",
        "n_traded",
        "ev_per_available_contract_after_fee",
        "ci95_low_ev_all",
        "local_mean_ev_all",
        "local_min_ev_all",
        "local_positive_fraction",
        "stability_score",
        "bootstrap_prob_ev_positive",
    ]

    top_by_ev = grid.sort_values("ev_per_available_contract_after_fee", ascending=False)
    top_by_lower = grid[grid["n_traded"] >= MIN_TRADES_FOR_STABLE].sort_values("ci95_low_ev_all", ascending=False)
    top_by_stability = (
        stable_pool.sort_values("stability_score", ascending=False)
        if not stable_pool.empty
        else grid[grid["n_traded"] >= MIN_TRADES_FOR_STABLE].sort_values("stability_score", ascending=False)
    )

    best_by_t = (
        grid.sort_values("ev_per_available_contract_after_fee", ascending=False)
        .groupby("t_seconds")
        .head(1)
        .sort_values("t_seconds")
    )
    by_t_cols = [
        "label",
        "n_traded",
        "ev_per_available_contract_after_fee",
        "ci95_low_ev_all",
        "success_rate_traded",
        "avg_cost_traded",
    ]

    report = f"""# Kalshi More-Likely Strategy Parameter Optimization

Date: 2026-06-04

## Executive Summary

Objective metric: **after-fee EV per available contract**, not per traded contract. A skipped contract contributes `0`, so the score is:

```text
objective = sum(realized after-fee P&L over traded contracts) / total contracts in dataset
```

This matches the intended optimization target: maximize average profit opportunity across all contracts, while accounting for both profitability and trade frequency.

Main result:

- Raw best parameter set: `{raw_best.label}`, objective `{money(raw_best.ev_per_available_contract_after_fee)}` per available contract.
- Best 95% lower-bound parameter set with at least `{MIN_TRADES_FOR_STABLE}` trades: `{lower_best.label}`, lower bound `{money(lower_best.ci95_low_ev_all)}`.
- Stability-adjusted recommendation: `{stable_best.label}`, objective `{money(stable_best.ev_per_available_contract_after_fee)}`, local-min `{money(stable_best.local_min_ev_all)}`, bootstrap P(EV>0) `{pct(stable_best.bootstrap_prob_ev_positive)}`.
- Current live default-like strategy `T=600s [0.60,0.80)` scores `{money(current.ev_per_available_contract_after_fee)}` per available contract after fees, with `{int(current.n_traded)}` trades and `{pct(current.bootstrap_prob_ev_positive)}` bootstrap P(EV>0).

The raw optimum is useful, but the stable optimum is the safer trading candidate. The raw best has the highest point estimate, but nearby parameter sets include negative outcomes. The stability-adjusted recommendation sacrifices some point-estimate EV for a fully positive local neighborhood.

## Method

Input data:

- Source: `p-0604-research/data/*.csv`
- Contract files: `{n_contracts}`
- Raw rows: `{sum(int(m.get("rows", 0)) for m in contract_meta):,}`
- Valid entry observations after quote/liquidity checks: `{len(entries_df):,}`

Strategy family:

1. Choose a time `T` from `0` to `900` seconds before expiry, in 30-second steps.
2. At `T`, choose the Kalshi more-likely side from midpoint:
   - YES if `kalshi_yes_mid >= 0.5`
   - NO otherwise
3. Define `p = max(kalshi_yes_mid, 1 - kalshi_yes_mid)`.
4. Trade if `p` is inside a price band `[low, high)`.
5. Buy the selected side at its best ask.
6. Require at least `{CONTRACTS_PER_TRADE}` contracts of derived best-ask liquidity.
7. Reject invalid Kalshi books, including complement-crossed books where `yes_bid + no_bid > 1`.

Fee and P&L:

```text
fee = 0.07 * cost * (1 - cost)
win P&L = 1 - cost - fee
loss P&L = -cost - fee
```

Every table below reports the objective as **EV per available contract after fees** unless explicitly labeled per-traded-contract.

Uncertainty:

- Normal 95% intervals are computed on the full per-contract P&L vector, including zeros for skipped contracts.
- Bootstrap uses `{BOOTSTRAPS}` contract-level resamples.
- `bootstrap P(EV>0)` estimates how often a parameter set remains profitable under resampling.
- `bootstrap winner frequency` estimates how often a parameter set is the raw best in resampled datasets.
- Local stability compares neighboring parameters within `±60s` and `±0.05` on both band endpoints.

## Visualizations

### 3D Objective Scatter

![3D objective scatter]({plots["scatter3d"].relative_to(ROOT)})

Axes are `T`, band low, and band high. Color is after-fee EV per available contract. Marker size is trade rate.

### 3D Width View

![3D width view]({plots["width3d"].relative_to(ROOT)})

This plot slices by selected band widths and shows how the objective changes with `T` and band center.

### Profit Heatmap

![Profit heatmap]({plots["heatmap_ev"].relative_to(ROOT)})

Only the 35 most active bands are shown to keep labels readable. Values are after-fee EV per available contract.

### Lower Confidence Bound Heatmap

![Lower confidence heatmap]({plots["heatmap_lci"].relative_to(ROOT)})

This shows the 95% lower confidence bound on the objective. It is more conservative than the point estimate.

### Bootstrap Winners

![Bootstrap winners]({plots["bootstrap"].relative_to(ROOT)})

A stable optimum should win frequently or at least appear near other positive, high-confidence parameter sets. A low winner frequency means the top point estimate is fragile.

### Best Band At Each T

![Best by T]({plots["best_by_t"].relative_to(ROOT)})

This shows the best point-estimate band at each `T`, with the 95% normal confidence interval and trade count.

## Top Parameter Sets By Point Estimate

{table(top_by_ev, top_cols, limit=15)}

## Top Parameter Sets By 95% Lower Confidence Bound

{table(top_by_lower, top_cols, limit=15)}

## Top Stability-Eligible Parameter Sets

These rows have at least `{MIN_TRADES_FOR_STABLE}` trades, a locally positive neighborhood, and no negative local neighbor under the `±60s` / `±0.05` endpoint perturbation rule.

{table(top_by_stability, stable_cols, limit=15)}

## Best Band At Each T

{table(best_by_t, by_t_cols, limit=len(best_by_t))}

## Interpretation

The optimization surface is not smooth enough to trust a single point estimate blindly. The profitable area is concentrated in moderate-confidence bands, not in the highest-confidence `0.9-1.0` region. This is consistent with the earlier backtest: very high confidence is directionally accurate, but the ask price is usually too expensive after fees.

The raw best, `T=720s [0.50,0.80)`, has strong average EV and wins the most bootstrap resamples, but its local minimum is negative. The stable recommendation, `T=630s [0.55,0.80)`, has lower point-estimate EV but a positive local minimum and a 100% locally positive neighborhood in this grid.

The objective definition changes the ranking materially. A high per-traded-contract EV band can still be weak if it trades rarely. Conversely, a slightly lower per-trade edge can be superior if it appears across many contracts.

The stability-adjusted candidate is preferable to the raw winner because it has:

- positive point-estimate EV per available contract,
- a positive or less fragile lower confidence bound,
- enough trades to avoid tiny-sample artifacts,
- a positive local neighborhood rather than a single isolated spike.

## Recommendation

Use `{stable_best.label}` as the first candidate for paper/live shadow validation, not the raw winner. Keep the current `T=600s [0.60,0.80)` rule as a baseline because it remains profitable after fees in this dataset and is simpler, but the optimizer suggests that a slightly earlier entry and broader lower band may improve all-contract EV.

Before increasing size:

1. Run the Kalshi trader in dry-run/shadow mode and compare realized fills to backtest assumed best asks.
2. Track realized slippage and rejected orderbooks separately.
3. Re-run this optimizer daily as new contracts are added.
4. Require the selected parameter set to remain positive on the all-contract after-fee objective for multiple non-overlapping date blocks.
5. Keep `--contracts 2` until the live fill distribution confirms enough liquidity at the selected band.

## Artifacts

- Grid CSV: `kalshi_strategy_optimization_grid.csv`
- Summary JSON: `kalshi_strategy_optimization_summary.json`
- Plots: `plots/kalshi_strategy_*`
"""
    REPORT_MD.write_text(report, encoding="utf-8")

    summary = {
        "n_contracts": n_contracts,
        "raw_rows": sum(int(m.get("rows", 0)) for m in contract_meta),
        "valid_entry_observations": int(len(entries_df)),
        "objective": "after-fee EV per available contract; skipped contracts count as 0",
        "raw_best": raw_best.to_dict(),
        "best_lower_ci": lower_best.to_dict(),
        "stable_recommendation": stable_best.to_dict(),
        "current_T600_060_080": current.to_dict(),
        "bootstraps": BOOTSTRAPS,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")


def main() -> None:
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    contract_ids: list[str] = []
    contract_meta: list[dict[str, object]] = []
    entries: list[Entry] = []
    for path in sorted(DATA_DIR.glob("*.csv")):
        contract_id, file_entries, meta = load_contract_entries(path)
        contract_ids.append(contract_id)
        contract_meta.append(meta)
        entries.extend(file_entries)

    entries_df = pd.DataFrame([entry.__dict__ for entry in entries])
    if entries_df.empty:
        raise RuntimeError("No valid entries found")

    grid, pnl_matrix = build_grid(entries_df, contract_ids)
    grid = add_local_stability(grid)
    grid = bootstrap_grid(grid, pnl_matrix)
    grid.to_csv(GRID_CSV, index=False)

    plots = {
        "scatter3d": PLOTS_DIR / "kalshi_strategy_profit_3d_scatter.png",
        "width3d": PLOTS_DIR / "kalshi_strategy_profit_width_3d.png",
        "heatmap_ev": PLOTS_DIR / "kalshi_strategy_profit_heatmap.png",
        "heatmap_lci": PLOTS_DIR / "kalshi_strategy_lower_ci_heatmap.png",
        "bootstrap": PLOTS_DIR / "kalshi_strategy_bootstrap_winners.png",
        "best_by_t": PLOTS_DIR / "kalshi_strategy_best_by_t.png",
    }
    scatter_3d_plot(grid, plots["scatter3d"])
    width_surface_plot(grid, plots["width3d"])
    heatmap_plot(
        grid,
        "ev_per_available_contract_after_fee",
        plots["heatmap_ev"],
        "After-fee EV per available contract",
    )
    heatmap_plot(
        grid,
        "ci95_low_ev_all",
        plots["heatmap_lci"],
        "95% lower bound for after-fee EV per available contract",
    )
    bootstrap_plot(grid, plots["bootstrap"])
    trade_count_plot(grid, plots["best_by_t"])
    write_report(grid, entries_df, contract_meta, plots)

    raw_best = grid.sort_values("ev_per_available_contract_after_fee", ascending=False).iloc[0]
    stable = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))["stable_recommendation"]
    print(f"wrote {REPORT_MD}")
    print(f"raw_best {raw_best['label']} ev_all={raw_best['ev_per_available_contract_after_fee']:.6f}")
    print(f"stable {stable['label']} ev_all={stable['ev_per_available_contract_after_fee']:.6f}")


if __name__ == "__main__":
    main()
