#!/usr/bin/env python3
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ENTRIES_CSV = ROOT / "kalshi_new_strategy_backtest_entries.csv"
RESULTS_CSV = ROOT / "kalshi_new_strategy_backtest_results.csv"
REPORT_MD = ROOT / "kalshi_new_strategy_backtest_report.md"

FEE_RATE = 0.07
CONTRACTS_PER_TRADE = 2
ENTRY_TOLERANCE_SECONDS = 45.0


def finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def first_valid(series: pd.Series) -> Any | None:
    valid = series.dropna()
    return None if valid.empty else valid.iloc[0]


def last_valid(series: pd.Series) -> Any | None:
    valid = series.dropna()
    return None if valid.empty else valid.iloc[-1]


def money(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "--"
    return f"${value:,.4f}"


def pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "--"
    return f"{value * 100:.2f}%"


def price(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "--"
    return f"{value:.4f}"


def fee(cost: float) -> float:
    return FEE_RATE * cost * (1.0 - cost)


def valid_kalshi_quote(row: pd.Series) -> bool:
    yes_bid = finite_float(row.get("kalshi_yes_bid"))
    no_bid = finite_float(row.get("kalshi_no_bid"))
    yes_ask = finite_float(row.get("kalshi_yes_ask"))
    no_ask = finite_float(row.get("kalshi_no_ask"))
    yes_mid = finite_float(row.get("kalshi_yes_mid"))
    if any(value is None for value in [yes_bid, no_bid, yes_ask, no_ask, yes_mid]):
        return False
    assert yes_bid is not None and no_bid is not None
    assert yes_ask is not None and no_ask is not None and yes_mid is not None
    if not (0.0 <= yes_bid <= 1.0 and 0.0 <= no_bid <= 1.0):
        return False
    if not (0.0 < yes_ask < 1.0 and 0.0 < no_ask < 1.0):
        return False
    if not (0.0 <= yes_mid <= 1.0):
        return False
    if yes_bid + no_bid > 1.0 + 1e-9:
        return False
    return True


def selected_side(row: pd.Series) -> tuple[str, int, float, float, float] | None:
    yes_mid = finite_float(row.get("kalshi_yes_mid"))
    if yes_mid is None:
        return None
    buy_yes = yes_mid >= 0.5
    if buy_yes:
        side = "YES"
        label = 1
        selected_mid = yes_mid
        selected_ask = finite_float(row.get("kalshi_yes_ask"))
        selected_ask_qty = finite_float(row.get("kalshi_best_no_bid_qty"))
    else:
        side = "NO"
        label = 0
        selected_mid = 1.0 - yes_mid
        selected_ask = finite_float(row.get("kalshi_no_ask"))
        selected_ask_qty = finite_float(row.get("kalshi_best_yes_bid_qty"))
    if selected_ask is None or not 0.0 < selected_ask < 1.0:
        return None
    if selected_ask_qty is None or selected_ask_qty < CONTRACTS_PER_TRADE:
        return None
    return side, label, selected_mid, selected_ask, selected_ask_qty


def load_entry(path: Path, t_seconds: int) -> dict[str, Any] | None:
    df = pd.read_csv(path)
    if df.empty:
        return None

    contract_id = path.stem.replace("cli_predictor_polymarket_", "")
    close_value = first_valid(df.get("kalshi_close_time", pd.Series(dtype=object)))
    close_time = pd.to_datetime(close_value, utc=True, errors="coerce")
    if pd.isna(close_time):
        return None

    final_price = finite_float(last_valid(df.get("kalshi_btc_price", pd.Series(dtype=object))))
    final_target = finite_float(last_valid(df.get("kalshi_btc_target", pd.Series(dtype=object))))
    if final_price is None or final_target is None:
        return None
    actual_label = int(final_price > final_target)

    frame = df.copy()
    frame["_timestamp"] = pd.to_datetime(frame["timestamp_utc"], utc=True, errors="coerce")
    frame["_remaining"] = (close_time - frame["_timestamp"]).dt.total_seconds()
    candidates = frame[
        frame["_remaining"].notna()
        & frame["_remaining"].ge(0)
        & frame["_remaining"].sub(t_seconds).abs().le(ENTRY_TOLERANCE_SECONDS)
    ]
    if candidates.empty:
        return None
    row = candidates.loc[candidates["_remaining"].sub(t_seconds).abs().idxmin()]
    if not valid_kalshi_quote(row):
        return None

    selected = selected_side(row)
    if selected is None:
        return None
    side, predicted_label, selected_mid, selected_ask, selected_ask_qty = selected
    spot = finite_float(row.get("kalshi_btc_price"))
    target = finite_float(row.get("kalshi_btc_target"))
    spot_delta = None if spot is None or target is None else spot - target
    spot_agrees = None
    if spot_delta is not None:
        spot_agrees = spot_delta > 0 if side == "YES" else spot_delta < 0

    did_win = predicted_label == actual_label
    trade_fee = fee(selected_ask)
    pnl = (1.0 - selected_ask - trade_fee) if did_win else (-selected_ask - trade_fee)
    return {
        "contract_id": contract_id,
        "file": path.name,
        "t_seconds": t_seconds,
        "timestamp_utc": row["timestamp_utc"],
        "close_time": close_time.isoformat(),
        "remaining_seconds": float(row["_remaining"]),
        "yes_mid": finite_float(row.get("kalshi_yes_mid")),
        "selected_side": side,
        "selected_label": predicted_label,
        "selected_mid": selected_mid,
        "selected_ask": selected_ask,
        "selected_ask_qty": selected_ask_qty,
        "entry_spot": spot,
        "entry_target": target,
        "entry_delta": spot_delta,
        "spot_agrees": spot_agrees,
        "actual_label": actual_label,
        "actual_side": "YES" if actual_label else "NO",
        "success": did_win,
        "fee": trade_fee,
        "net_pnl": pnl,
        "final_price": final_price,
        "final_target": final_target,
    }


def build_entries() -> tuple[pd.DataFrame, int]:
    all_paths = sorted(DATA_DIR.glob("*.csv"))
    rows: list[dict[str, Any]] = []
    for path in all_paths:
        for t_seconds in (600, 630):
            entry = load_entry(path, t_seconds)
            if entry is not None:
                rows.append(entry)
    frame = pd.DataFrame(rows)
    frame.to_csv(ENTRIES_CSV, index=False)
    return frame, len(all_paths)


def summarize(name: str, frame: pd.DataFrame, mask: pd.Series, total_contracts: int) -> dict[str, Any]:
    selected = frame[mask].copy()
    successes = int(selected["success"].sum()) if not selected.empty else 0
    failures = int((~selected["success"].astype(bool)).sum()) if not selected.empty else 0
    total_net = float(selected["net_pnl"].sum()) if not selected.empty else 0.0
    return {
        "rule": name,
        "available_contracts": int(total_contracts),
        "usable_entry_rows": int(len(frame)),
        "traded": int(len(selected)),
        "successful": successes,
        "unsuccessful": failures,
        "skipped": int(total_contracts - len(selected)),
        "win_rate": float(successes / len(selected)) if len(selected) else math.nan,
        "avg_mid": float(selected["selected_mid"].mean()) if len(selected) else math.nan,
        "avg_ask": float(selected["selected_ask"].mean()) if len(selected) else math.nan,
        "avg_fee": float(selected["fee"].mean()) if len(selected) else math.nan,
        "spot_agree_rate": float(selected["spot_agrees"].mean()) if len(selected) else math.nan,
        "total_net_pnl": total_net,
        "net_per_available_contract": total_net / total_contracts if total_contracts else math.nan,
        "net_per_traded_contract": total_net / len(selected) if len(selected) else math.nan,
    }


def result_row(row: dict[str, Any] | pd.Series) -> dict[str, str]:
    return {
        "Rule": str(row["rule"]),
        "Traded": str(int(row["traded"])),
        "S/U/K": f"{int(row['successful'])}/{int(row['unsuccessful'])}/{int(row['skipped'])}",
        "Win %": pct(float(row["win_rate"])),
        "Avg ask": price(float(row["avg_ask"])),
        "Net": money(float(row["total_net_pnl"])),
        "Net/available": money(float(row["net_per_available_contract"])),
        "Net/traded": money(float(row["net_per_traded_contract"])),
    }


def md_table(rows: list[dict[str, str]], columns: list[str]) -> str:
    out = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows:
        out.append("|" + "|".join(row.get(col, "") for col in columns) + "|")
    return "\n".join(out)


def daily_rows(frame: pd.DataFrame, old_mask: pd.Series, new_mask: pd.Series) -> pd.DataFrame:
    tmp = frame.copy()
    tmp["date"] = pd.to_datetime(tmp["close_time"], utc=True, errors="coerce").dt.date.astype(str)
    rows: list[dict[str, Any]] = []
    for date in sorted(tmp["date"].dropna().unique()):
        day_idx = tmp.index[tmp["date"].eq(date)]
        day_contracts = int(tmp.loc[day_idx, "contract_id"].nunique())
        if day_contracts == 0:
            continue
        rows.append({"date": date, **summarize("existing", tmp.loc[day_idx], old_mask.loc[day_idx], day_contracts)})
        rows.append({"date": date, **summarize("new", tmp.loc[day_idx], new_mask.loc[day_idx], day_contracts)})
    return pd.DataFrame(rows)


def write_report(results: pd.DataFrame, daily: pd.DataFrame, entries: pd.DataFrame, total_contracts: int) -> None:
    columns = ["Rule", "Traded", "S/U/K", "Win %", "Avg ask", "Net", "Net/available", "Net/traded"]
    primary = results[results["rule"].isin(["Existing T=630 mid [0.55,0.80)", "New T=630 ask (0.50,0.78) + spot"])]
    rows = [result_row(row) for _, row in primary.iterrows()]
    component_rows = [result_row(row) for _, row in results.iterrows()]

    daily_lines = []
    for _, row in daily.iterrows():
        daily_lines.append(
            {
                "Date": str(row["date"]),
                "Rule": str(row["rule"]),
                "Traded": str(int(row["traded"])),
                "S/U/K": f"{int(row['successful'])}/{int(row['unsuccessful'])}/{int(row['skipped'])}",
                "Net": money(float(row["total_net_pnl"])),
                "Net/available": money(float(row["net_per_available_contract"])),
            }
        )

    old = results[results["rule"] == "Existing T=630 mid [0.55,0.80)"].iloc[0]
    new = results[results["rule"] == "New T=630 ask (0.50,0.78) + spot"].iloc[0]
    old_spot = results[results["rule"] == "Existing T=630 mid [0.55,0.80) + spot"].iloc[0]
    delta = float(new["total_net_pnl"] - old["total_net_pnl"])

    report = [
        "# Kalshi New Strategy Backtest On p-0604 Data",
        "",
        "## Setup",
        "",
        f"- Source: `p-0604-research/data/*.csv`",
        f"- Contract files: `{total_contracts}`",
        f"- Usable `T=630` entries: `{int((entries['t_seconds'] == 630).sum())}`",
        f"- Usable `T=600` entries: `{int((entries['t_seconds'] == 600).sum())}`",
        f"- Entry row: closest row to target `T` within `{ENTRY_TOLERANCE_SECONDS:.0f}` seconds.",
        "- Side: more-likely side from `kalshi_yes_mid >= 0.5`.",
        "- Fee model: `0.07 * ask * (1 - ask)` per contract.",
        "- Objective: net PnL per available contract; skipped contracts contribute zero.",
        "",
        "## Primary Benchmark",
        "",
        md_table(rows, columns),
        "",
        f"Delta, new minus existing: `{money(delta)}` total net, `{money(float(new['net_per_available_contract'] - old['net_per_available_contract']))}` per available contract.",
        "",
        "The new rule is better per traded contract, but it is slightly worse on the requested total-opportunity objective in this dataset. It trades fewer contracts and gives up enough profitable old-rule trades to almost exactly offset its cleaner lower-price entries.",
        "",
        "## Component Breakdown",
        "",
        md_table(component_rows, columns),
        "",
        "The spot-agreement filter itself is positive on this dataset: applying it to the existing midpoint band improves the benchmark. The ask-price cutoff is the part that loses edge here, because several `ask >= 0.78` entries in this older dataset were successful.",
        "",
        f"Best direct rule in this comparison: `{old_spot['rule']}`, net `{money(float(old_spot['total_net_pnl']))}`, net/available `{money(float(old_spot['net_per_available_contract']))}`.",
        "",
        "## Daily Split",
        "",
        md_table(daily_lines, ["Date", "Rule", "Traded", "S/U/K", "Net", "Net/available"]),
        "",
        "## Artifacts",
        "",
        f"- Entries: `{ENTRIES_CSV.name}`",
        f"- Results: `{RESULTS_CSV.name}`",
        f"- Report: `{REPORT_MD.name}`",
    ]
    REPORT_MD.write_text("\n".join(report) + "\n", encoding="utf-8")


def main() -> None:
    entries, total_contracts = build_entries()
    t630 = entries[entries["t_seconds"] == 630].copy()
    t600 = entries[entries["t_seconds"] == 600].copy()

    old_mask = t630["selected_mid"].ge(0.55) & t630["selected_mid"].lt(0.80)
    new_mask = t630["selected_ask"].gt(0.50) & t630["selected_ask"].lt(0.78) & t630["spot_agrees"].fillna(False)
    old_spot_mask = old_mask & t630["spot_agrees"].fillna(False)
    ask_only_mask = t630["selected_ask"].gt(0.50) & t630["selected_ask"].lt(0.78)
    old_live_mask = t600["selected_mid"].ge(0.60) & t600["selected_mid"].lt(0.80)

    results = pd.DataFrame(
        [
            summarize("Existing T=630 mid [0.55,0.80)", t630, old_mask, total_contracts),
            summarize("New T=630 ask (0.50,0.78) + spot", t630, new_mask, total_contracts),
            summarize("Existing T=630 mid [0.55,0.80) + spot", t630, old_spot_mask, total_contracts),
            summarize("Ask (0.50,0.78), no spot filter", t630, ask_only_mask, total_contracts),
            summarize("Old live-like T=600 mid [0.60,0.80)", t600, old_live_mask, total_contracts),
        ]
    )
    results.to_csv(RESULTS_CSV, index=False)
    daily = daily_rows(t630, old_mask, new_mask)
    write_report(results, daily, entries, total_contracts)
    print(f"entries -> {ENTRIES_CSV}")
    print(f"results -> {RESULTS_CSV}")
    print(f"report -> {REPORT_MD}")
    print(results[["rule", "traded", "successful", "unsuccessful", "skipped", "total_net_pnl", "net_per_available_contract", "net_per_traded_contract"]].to_string(index=False))


if __name__ == "__main__":
    main()
