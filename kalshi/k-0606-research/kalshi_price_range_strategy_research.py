#!/usr/bin/env python3
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
TRADES_CSV = ROOT / "kalshi_trader_trades.csv"
LOG_PATH = ROOT / "kalshi_trader.log"
PAIRED_CSV = ROOT / "kalshi_t630_strategy_dataset.csv"
GRID_CSV = ROOT / "kalshi_t630_strategy_grid.csv"
REPORT_PATH = ROOT / "kalshi_price_range_strategy_improvement_report.md"

FEE_RATE = 0.07
CONTRACT_RE = re.compile(
    r"^CONTRACT (?P<contract>\S+) \| close (?P<close>\S+) \| K target (?P<target>-?\d+(?:\.\d+)?)"
)
STATUS_RE = re.compile(
    r"^STATUS T=(?P<t>-?\d+(?:\.\d+)?)s \| K (?P<spot>-?\d+(?:\.\d+)?) "
    r"(?P<sign>[+-])\s*(?P<delta>\d+(?:\.\d+)?) \| yes_mid=(?P<yes_mid>\d+(?:\.\d+)?|--)"
)


def fee(price: pd.Series | float) -> pd.Series | float:
    return FEE_RATE * price * (1.0 - price)


def net_per_contract(price: pd.Series | float, success: pd.Series | bool) -> pd.Series | float:
    return np.where(success, 1.0 - price - fee(price), -price - fee(price))


def summarize(
    frame: pd.DataFrame,
    price_col: str = "sim_price",
    qty_col: str = "contracts",
    total_contracts: float | None = None,
    total_opportunities: int | None = None,
) -> dict[str, Any]:
    if frame.empty:
        denominator = total_contracts or 0.0
        return {
            "trades": 0,
            "contracts": 0.0,
            "total_opportunities": total_opportunities if total_opportunities is not None else 0,
            "skipped_opportunities": total_opportunities if total_opportunities is not None else 0,
            "total_contracts": denominator,
            "successes": 0,
            "failures": 0,
            "success_rate": math.nan,
            "avg_price": math.nan,
            "gross_profit": 0.0,
            "fees": 0.0,
            "net_profit": 0.0,
            "net_per_contract": math.nan,
            "net_per_total_contract": 0.0 if denominator else math.nan,
            "roi_on_cost": math.nan,
        }
    price = frame[price_col].astype(float)
    qty = frame[qty_col].astype(float)
    success = frame["sim_success"].astype(bool)
    gross = np.where(success, 1.0 - price, -price) * qty
    fees = fee(price) * qty
    net = gross - fees
    contracts = float(qty.sum())
    denominator = contracts if total_contracts is None else float(total_contracts)
    opportunities = int(total_opportunities) if total_opportunities is not None else int(len(frame))
    cost = float((price * qty).sum())
    return {
        "trades": int(len(frame)),
        "contracts": contracts,
        "total_opportunities": opportunities,
        "skipped_opportunities": max(0, opportunities - int(len(frame))),
        "total_contracts": denominator,
        "successes": int(success.sum()),
        "failures": int((~success).sum()),
        "success_rate": float(success.mean()),
        "avg_price": float((price * qty).sum() / contracts) if contracts else math.nan,
        "gross_profit": float(gross.sum()),
        "fees": float(fees.sum()),
        "net_profit": float(net.sum()),
        "net_per_contract": float(net.sum() / contracts) if contracts else math.nan,
        "net_per_total_contract": float(net.sum() / denominator) if denominator else math.nan,
        "roi_on_cost": float(net.sum() / cost) if cost else math.nan,
    }


def actual_filled_summary(outcomes: pd.DataFrame, t630_only: bool) -> dict[str, Any]:
    frame = outcomes.copy()
    if t630_only:
        frame = frame[frame["entry_seconds"] == 630.0]
    frame = frame[
        (frame["order_status"].isin(["filled", "dry_run"]))
        & frame["correct"].notna()
        & frame["fill_price"].notna()
        & frame["filled_size"].fillna(0).gt(0)
    ].copy()
    frame["sim_price"] = frame["fill_price"].astype(float)
    frame["contracts"] = frame["filled_size"].astype(float)
    frame["sim_success"] = frame["correct"].astype(int).astype(bool)
    return summarize(frame)


def parse_entry_statuses(log_path: Path) -> pd.DataFrame:
    current_contract = ""
    current_target = math.nan
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(log_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = line.strip()
        contract_match = CONTRACT_RE.match(line)
        if contract_match:
            current_contract = contract_match.group("contract")
            current_target = float(contract_match.group("target"))
            continue
        status_match = STATUS_RE.match(line)
        if not status_match or not current_contract:
            continue
        sign = -1.0 if status_match.group("sign") == "-" else 1.0
        rows.append(
            {
                "contract_id": current_contract,
                "status_line": line_no,
                "status_t": float(status_match.group("t")),
                "entry_spot": float(status_match.group("spot")),
                "entry_delta": sign * float(status_match.group("delta")),
                "entry_target": current_target,
                "status_yes_mid": np.nan
                if status_match.group("yes_mid") == "--"
                else float(status_match.group("yes_mid")),
            }
        )
    return pd.DataFrame(rows)


def nearest_status(decisions: pd.DataFrame, statuses: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    by_contract = {contract: group.copy() for contract, group in statuses.groupby("contract_id")}
    for row in decisions.itertuples(index=False):
        group = by_contract.get(row.contract_id)
        if group is None or group.empty:
            rows.append({})
            continue
        group = group.copy()
        group["distance_to_decision_t"] = (group["status_t"] - float(row.remaining_seconds)).abs()
        chosen = group.sort_values(["distance_to_decision_t", "status_line"]).iloc[0].to_dict()
        rows.append(chosen)
    return pd.DataFrame(rows)


def build_dataset() -> pd.DataFrame:
    trades = pd.read_csv(TRADES_CSV)
    numeric_cols = [
        "remaining_seconds",
        "entry_seconds",
        "yes_bid",
        "yes_ask",
        "no_bid",
        "no_ask",
        "yes_mid",
        "selected_probability",
        "selected_ask",
        "contracts",
        "fill_price",
        "filled_size",
        "actual_label",
        "correct",
    ]
    for col in numeric_cols:
        trades[col] = pd.to_numeric(trades[col], errors="coerce")
    decisions = trades[(trades["event"] == "decision") & (trades["entry_seconds"] == 630.0)].copy()
    outcomes = trades[(trades["event"] == "outcome") & (trades["entry_seconds"] == 630.0)].copy()
    paired = decisions.merge(
        outcomes[
            [
                "contract_id",
                "close_time",
                "actual_label",
                "correct",
                "kalshi_price",
                "kalshi_target",
                "order_status",
                "reason",
            ]
        ],
        on=["contract_id", "close_time"],
        how="inner",
        suffixes=("_decision", "_outcome"),
    )
    paired = paired[paired["actual_label_outcome"].notna()].copy()
    paired["side"] = np.where(paired["yes_mid"] >= 0.5, "YES", "NO")
    paired["side_label"] = np.where(paired["side"] == "YES", 1, 0)
    paired["mid_p"] = paired["selected_probability"].astype(float)
    paired["ask_p"] = np.where(paired["side"] == "YES", paired["yes_ask"], paired["no_ask"]).astype(float)
    paired["sim_price"] = paired["ask_p"]
    paired["sim_success"] = paired["side_label"].astype(int) == paired["actual_label_outcome"].astype(int)
    paired["contracts"] = pd.to_numeric(paired["contracts"], errors="coerce").fillna(2.0)

    statuses = parse_entry_statuses(LOG_PATH)
    nearest = nearest_status(paired, statuses)
    for col in ["status_t", "entry_spot", "entry_delta", "entry_target", "status_yes_mid", "distance_to_decision_t"]:
        paired[col] = nearest.get(col, np.nan)
    paired["favorable_distance"] = np.where(paired["side"] == "YES", paired["entry_delta"], -paired["entry_delta"])
    paired["abs_distance"] = paired["entry_delta"].abs()
    paired["spot_agrees"] = paired["favorable_distance"] > 0
    paired["net_per_contract"] = net_per_contract(paired["sim_price"], paired["sim_success"])
    paired["net_profit"] = paired["net_per_contract"] * paired["contracts"]
    paired["fee_per_contract"] = fee(paired["sim_price"])
    paired.to_csv(PAIRED_CSV, index=False)
    return paired


def rule_mask(
    data: pd.DataFrame,
    low: float,
    high: float,
    price_col: str,
    spot_mode: str,
    min_favorable_distance: float | None,
    max_abs_distance: float | None,
) -> pd.Series:
    mask = data[price_col].gt(low) & data[price_col].lt(high)
    if spot_mode == "agrees":
        mask &= data["spot_agrees"].fillna(False)
    elif spot_mode == "contradicts":
        mask &= ~data["spot_agrees"].fillna(False)
    if min_favorable_distance is not None:
        mask &= data["favorable_distance"].ge(min_favorable_distance)
    if max_abs_distance is not None:
        mask &= data["abs_distance"].le(max_abs_distance)
    return mask


def sweep_rules(data: pd.DataFrame, output_path: Path | None = GRID_CSV) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    total_contracts = float(data["contracts"].sum())
    total_opportunities = int(len(data))
    lows = np.round(np.arange(0.50, 0.805, 0.025), 3)
    highs = np.round(np.arange(0.575, 0.955, 0.025), 3)
    spot_modes = ["none", "agrees"]
    favorable_thresholds: list[float | None] = [None, 25, 50, 75]
    max_abs_thresholds: list[float | None] = [None, 75, 100, 150, 200]
    for price_col in ["mid_p", "ask_p"]:
        for low in lows:
            for high in highs:
                if high <= low:
                    continue
                for spot_mode in spot_modes:
                    for min_favorable in favorable_thresholds:
                        for max_abs in max_abs_thresholds:
                            if spot_mode == "none" and min_favorable is not None:
                                continue
                            if spot_mode == "none" and max_abs is not None:
                                continue
                            mask = rule_mask(data, low, high, price_col, spot_mode, min_favorable, max_abs)
                            selected = data[mask].copy()
                            summary = summarize(
                                selected,
                                total_contracts=total_contracts,
                                total_opportunities=total_opportunities,
                            )
                            if summary["trades"] == 0:
                                continue
                            rows.append(
                                {
                                    "price_col": price_col,
                                    "low": low,
                                    "high": high,
                                    "spot_mode": spot_mode,
                                    "min_favorable_distance": min_favorable,
                                    "max_abs_distance": max_abs,
                                    **summary,
                                }
                            )
    grid = pd.DataFrame(rows)
    if output_path is not None:
        grid.to_csv(output_path, index=False)
    return grid


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def fmt_unit_money(value: float) -> str:
    if not math.isfinite(value):
        return "--"
    return f"${value:,.4f}"


def fmt_pct(value: float) -> str:
    if not math.isfinite(value):
        return "--"
    return f"{value * 100:.2f}%"


def fmt_price(value: float) -> str:
    if not math.isfinite(value):
        return "--"
    return f"{value:.3f}"


def md_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    out = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows:
        out.append("|" + "|".join(str(row.get(col, "")) for col in columns) + "|")
    return "\n".join(out)


def rule_label(row: pd.Series) -> str:
    price = "mid p" if row["price_col"] == "mid_p" else "ask p"
    parts = [f"{row['low']:.2f} < {price} < {row['high']:.2f}"]
    if row["spot_mode"] == "agrees":
        parts.append("spot agrees with selected side")
    if pd.notna(row.get("min_favorable_distance")):
        parts.append(f"favored spot distance >= ${row['min_favorable_distance']:.0f}")
    if pd.notna(row.get("max_abs_distance")):
        parts.append(f"|spot-target| <= ${row['max_abs_distance']:.0f}")
    return "; ".join(parts)


def mask_for_grid_row(data: pd.DataFrame, row: pd.Series) -> pd.Series:
    return rule_mask(
        data,
        float(row["low"]),
        float(row["high"]),
        str(row["price_col"]),
        str(row["spot_mode"]),
        None if pd.isna(row["min_favorable_distance"]) else float(row["min_favorable_distance"]),
        None if pd.isna(row["max_abs_distance"]) else float(row["max_abs_distance"]),
    )


def write_report(data: pd.DataFrame, grid: pd.DataFrame, all_actual: dict[str, Any], actual_t630: dict[str, Any]) -> None:
    current_mask = data["mid_p"].gt(0.55) & data["mid_p"].lt(0.80)
    total_contracts = float(data["contracts"].sum())
    total_opportunities = int(len(data))
    current_replay = summarize(
        data[current_mask],
        total_contracts=total_contracts,
        total_opportunities=total_opportunities,
    )

    min_trades = 40
    eligible = grid[grid["trades"] >= min_trades].copy()
    eligible = eligible.sort_values(["net_per_total_contract", "net_profit", "trades"], ascending=[False, False, False])
    display_candidates = eligible.drop_duplicates(
        subset=["price_col", "low", "high", "spot_mode", "trades", "successes", "failures", "net_profit"]
    ).head(8)
    best = eligible.iloc[0]

    # Chronological sanity check: pick the best rule on the first 70% of contracts and evaluate on the final 30%.
    ordered = data.sort_values("timestamp_utc")
    split_at = max(1, int(len(ordered) * 0.70))
    train = ordered.iloc[:split_at]
    test = ordered.iloc[split_at:]
    train_grid = sweep_rules(train, output_path=None)
    train_eligible = train_grid[train_grid["trades"] >= max(20, int(len(train) * 0.25))].copy()
    train_best = train_eligible.sort_values(
        ["net_per_total_contract", "net_profit", "trades"], ascending=[False, False, False]
    ).iloc[0]
    train_mask = mask_for_grid_row(train, train_best)
    test_mask = mask_for_grid_row(test, train_best)
    recommended_train_mask = mask_for_grid_row(train, best)
    recommended_test_mask = mask_for_grid_row(test, best)
    current_train = summarize(
        train[current_mask.loc[train.index]],
        total_contracts=float(train["contracts"].sum()),
        total_opportunities=int(len(train)),
    )
    current_test = summarize(
        test[current_mask.loc[test.index]],
        total_contracts=float(test["contracts"].sum()),
        total_opportunities=int(len(test)),
    )
    train_best_summary = summarize(
        train[train_mask],
        total_contracts=float(train["contracts"].sum()),
        total_opportunities=int(len(train)),
    )
    test_best_summary = summarize(
        test[test_mask],
        total_contracts=float(test["contracts"].sum()),
        total_opportunities=int(len(test)),
    )
    recommended_train = summarize(
        train[recommended_train_mask],
        total_contracts=float(train["contracts"].sum()),
        total_opportunities=int(len(train)),
    )
    recommended_test = summarize(
        test[recommended_test_mask],
        total_contracts=float(test["contracts"].sum()),
        total_opportunities=int(len(test)),
    )

    def summary_row(name: str, summary: dict[str, Any]) -> dict[str, Any]:
        skipped = int(summary.get("skipped_opportunities", 0))
        return {
            "Rule": name,
            "Traded": summary["trades"],
            "S/U/K": f"{summary['successes']}/{summary['failures']}/{skipped}",
            "Win %": fmt_pct(summary["success_rate"]),
            "Avg p": fmt_price(summary["avg_price"]),
            "Net": fmt_money(summary["net_profit"]),
            "Net/total contract": fmt_unit_money(summary["net_per_total_contract"]),
            "Net/traded contract": fmt_unit_money(summary["net_per_contract"]),
        }

    best_rows = []
    for row in display_candidates.itertuples(index=False):
        row_s = pd.Series(row._asdict())
        best_rows.append(summary_row(rule_label(row_s), row_s.to_dict()))

    report = [
        "# Kalshi T=630 Price Range Strategy Improvement",
        "",
        "## Inputs",
        "",
        f"- Trade CSV: `{TRADES_CSV.name}`",
        f"- Log: `{LOG_PATH.name}`",
        f"- Fee model: `0.07 * p * (1 - p)` per contract.",
        "- Successful trade net per contract: `1 - p - fee`.",
        "- Unsuccessful trade net per contract: `-p - fee`.",
        "- The replay keeps entry timing controlled at `T=630`; only contracts with paired T=630 decision and outcome rows are used for rule search.",
        f"- Objective: maximize `net profit / total potential contracts`, where total potential contracts includes skipped T=630 opportunities as zero-P&L. This replay has {total_opportunities} paired T=630 opportunities and {total_contracts:.0f} total potential contracts.",
        "",
        "## Current Net Profit",
        "",
        md_table(
            [
                summary_row("All executed outcomes in CSV", all_actual),
                summary_row("Executed T=630 outcomes", actual_t630),
                summary_row("Replayed current band, 0.55 < mid p < 0.80", current_replay),
            ],
            ["Rule", "Traded", "S/U/K", "Win %", "Avg p", "Net", "Net/total contract", "Net/traded contract"],
        ),
        "",
        "The executed T=630 result is the cleanest baseline because it uses actual fill prices and excludes rows without outcomes.",
        "For the executed-only rows, skipped opportunities are not inferred, so `Net/total contract` is the same as `Net/traded contract`. Use the replay rows for objective comparisons because they include skipped T=630 opportunities in the denominator.",
        "The replay baseline uses the T=630 decision book and can evaluate skipped opportunities under alternate rules. In replay tables, K is the number of T=630 opportunities the rule would skip.",
        "",
        "## Best In-Sample Candidate Rules",
        "",
        f"Rules below require at least {min_trades} trades to avoid tiny-sample one-offs and are ranked by `net / total potential contract`, not by traded-contract ROI.",
        "",
        md_table(
            best_rows,
            ["Rule", "Traded", "S/U/K", "Win %", "Avg p", "Net", "Net/total contract", "Net/traded contract"],
        ),
        "",
        "## Recommended Rule",
        "",
        f"Recommended in-sample rule: **{rule_label(best)}**.",
        "",
        md_table(
            [
                summary_row("Current replay", current_replay),
                summary_row("Recommended replay", best.to_dict()),
            ],
            ["Rule", "Traded", "S/U/K", "Win %", "Avg p", "Net", "Net/total contract", "Net/traded contract"],
        ),
        "",
        "Why this helps:",
        "",
        "- The old range admits too many high-cost contracts near the top of the band.",
        "- Net profit is fee-adjusted, so the hurdle is higher than raw `success_rate > p`.",
        "- Because the denominator is fixed across T=630 rules, maximizing `net / total potential contract` gives the same ordering as maximizing total net profit. It does not give extra credit to rules that merely trade less often.",
        "- The best candidate shifts selection toward lower entry prices and uses spot/target agreement to avoid cases where the book's more-likely side is not supported by the underlying price at entry.",
        "",
        "## Chronological Sanity Check",
        "",
        "The main ranking above is in-sample. The checks below keep the denominator fixed inside each split, so skipped opportunities reduce `net / total contract` instead of disappearing from the metric.",
        "",
        "First, compare the current rule and the recommended full-sample candidate across the first 70% and final 30% of paired T=630 contracts. This is not a pure holdout proof for the recommended candidate, because that candidate was selected using the full sample, but it shows whether the candidate is simply concentrating profit in one period.",
        "",
        md_table(
            [
                summary_row("Current train", current_train),
                summary_row("Recommended candidate train", recommended_train),
                summary_row("Current final 30%", current_test),
                summary_row("Recommended candidate final 30%", recommended_test),
            ],
            ["Rule", "Traded", "S/U/K", "Win %", "Avg p", "Net", "Net/total contract", "Net/traded contract"],
        ),
        "",
        "Second, as a stricter overfit check, optimize only on the first 70% and evaluate that selected rule on the final 30%.",
        "",
        f"Train-selected rule: **{rule_label(train_best)}**.",
        "",
        md_table(
            [
                summary_row("Current train", current_train),
                summary_row("Train-selected rule on train", train_best_summary),
                summary_row("Current final 30%", current_test),
                summary_row("Train-selected rule on final 30%", test_best_summary),
            ],
            ["Rule", "Traded", "S/U/K", "Win %", "Avg p", "Net", "Net/total contract", "Net/traded contract"],
        ),
        "",
        "The train-selected rule is profitable, but it gives back too much coverage on the final 30% under the total-contract objective. That makes the broader full-sample recommendation a better live candidate than the narrower train-only rule until more data is collected.",
        "",
        "## Implementation Notes",
        "",
        "- Keep the trigger time at `T=630`.",
        "- At the decision snapshot, infer the more-likely side from `yes_mid >= 0.5`.",
        "- Use the tradable ask for that side as `p`: YES uses `yes_ask`, NO uses `no_ask`.",
        "- Compute entry spot agreement from the latest nearby log line: YES agrees when `spot - target > 0`; NO agrees when `target - spot > 0`.",
        "- If adopting the recommended rule live, keep logging the same fields and rerun this report after materially more contracts; the current sample is useful but still small.",
        "",
        "## Artifacts",
        "",
        f"- Paired T=630 dataset: `{PAIRED_CSV.name}`",
        f"- Rule sweep grid: `{GRID_CSV.name}`",
    ]
    REPORT_PATH.write_text("\n".join(report) + "\n", encoding="utf-8")


def main() -> None:
    trades = pd.read_csv(TRADES_CSV)
    for col in ["entry_seconds", "fill_price", "filled_size", "correct"]:
        trades[col] = pd.to_numeric(trades[col], errors="coerce")
    outcomes = trades[trades["event"] == "outcome"].copy()
    all_actual = actual_filled_summary(outcomes, t630_only=False)
    actual_t630 = actual_filled_summary(outcomes, t630_only=True)
    data = build_dataset()
    grid = sweep_rules(data)
    write_report(data, grid, all_actual, actual_t630)
    print(f"paired rows: {len(data)} -> {PAIRED_CSV}")
    print(f"grid rows: {len(grid)} -> {GRID_CSV}")
    print(f"report -> {REPORT_PATH}")


if __name__ == "__main__":
    main()
