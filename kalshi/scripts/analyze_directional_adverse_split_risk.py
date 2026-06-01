#!/usr/bin/env python3
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
HORIZON_DIR = ROOT / "kp-0529-research" / "horizon_models"
TRADE_PATH = HORIZON_DIR / "profit_margin_latch_2m_1m_poly_price_floor_trades.csv"
LABEL_PATH = HORIZON_DIR / "horizon_contract_labels.csv"
OUT_CSV = HORIZON_DIR / "directional_adverse_split_risk.csv"
OUT_REPORT = HORIZON_DIR / "directional_adverse_split_risk_report.md"

PROFIT_MARGIN = 0.18
FLOOR_LABEL = "33c"


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return math.nan, math.nan
    phat = successes / total
    denom = 1.0 + z * z / total
    center = (phat + z * z / (2.0 * total)) / denom
    half = z * math.sqrt((phat * (1.0 - phat) + z * z / (4.0 * total)) / total) / denom
    return max(0.0, center - half), min(1.0, center + half)


def comb(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


def fisher_two_sided(a: int, b: int, c: int, d: int) -> float:
    row1 = a + b
    row2 = c + d
    col1 = a + c
    total = row1 + row2
    denom = comb(total, row1)
    if denom == 0:
        return math.nan

    def prob(x: int) -> float:
        return comb(col1, x) * comb(total - col1, row1 - x) / denom

    low = max(0, row1 - (total - col1))
    high = min(row1, col1)
    observed = prob(a)
    return float(sum(prob(x) for x in range(low, high + 1) if prob(x) <= observed + 1e-15))


def md_table(df: pd.DataFrame, floatfmt: str = ".4f") -> str:
    tmp = df.copy()
    for col in tmp.columns:
        if pd.api.types.is_float_dtype(tmp[col]):
            tmp[col] = tmp[col].map(lambda x: "" if pd.isna(x) else format(x, floatfmt))
        else:
            tmp[col] = tmp[col].map(lambda x: "" if pd.isna(x) else str(x))
    header = "| " + " | ".join(tmp.columns) + " |"
    sep = "| " + " | ".join("---" for _ in tmp.columns) + " |"
    rows = ["| " + " | ".join(str(v).replace("|", "\\|") for v in row) + " |" for row in tmp.values]
    return "\n".join([header, sep, *rows])


def annotate_directional_outcomes(trades: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    label_cols = [
        "contract_id",
        "kalshi_settle_yes",
        "polymarket_settle_yes",
        "kalshi_distance_final",
        "polymarket_distance_final",
    ]
    out = trades.merge(labels[label_cols], on="contract_id", how="left")
    out["diverge_bool"] = out["diverge"].astype(int).eq(1)
    out["favorable_split"] = False
    out["adverse_split"] = False

    k_plus_favorable = (
        out["direction"].eq("K+NP")
        & out["kalshi_settle_yes"].eq(True)
        & out["polymarket_settle_yes"].eq(False)
    )
    k_plus_adverse = (
        out["direction"].eq("K+NP")
        & out["kalshi_settle_yes"].eq(False)
        & out["polymarket_settle_yes"].eq(True)
    )
    nk_plus_favorable = (
        out["direction"].eq("NK+P")
        & out["kalshi_settle_yes"].eq(False)
        & out["polymarket_settle_yes"].eq(True)
    )
    nk_plus_adverse = (
        out["direction"].eq("NK+P")
        & out["kalshi_settle_yes"].eq(True)
        & out["polymarket_settle_yes"].eq(False)
    )
    out.loc[k_plus_favorable | nk_plus_favorable, "favorable_split"] = True
    out.loc[k_plus_adverse | nk_plus_adverse, "adverse_split"] = True
    return out


def summarize(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, object]] = []
    tests: list[dict[str, object]] = []
    for sample, sample_frame in frame.groupby("sample", sort=False):
        stats: dict[str, dict[str, int]] = {}
        for direction in ["K+NP", "NK+P"]:
            subset = sample_frame[sample_frame["direction"].eq(direction)]
            trades = int(len(subset))
            adverse = int(subset["adverse_split"].sum())
            favorable = int(subset["favorable_split"].sum())
            divergences = int(subset["diverge_bool"].sum())
            ci_low, ci_high = wilson_interval(adverse, trades)
            stats[direction] = {"adverse": adverse, "non_adverse": trades - adverse}
            rows.append(
                {
                    "sample": sample,
                    "direction": direction,
                    "trades": trades,
                    "divergences": divergences,
                    "favorable_splits": favorable,
                    "adverse_splits": adverse,
                    "adverse_split_rate": adverse / trades if trades else math.nan,
                    "adverse_rate_ci_low": ci_low,
                    "adverse_rate_ci_high": ci_high,
                    "mean_all_in_cost": float(subset["all_in_cost"].mean()) if trades else math.nan,
                    "mean_polymarket_price": float(subset["polymarket_price"].mean()) if trades else math.nan,
                }
            )
        k = stats["K+NP"]
        nk = stats["NK+P"]
        k_total = k["adverse"] + k["non_adverse"]
        nk_total = nk["adverse"] + nk["non_adverse"]
        k_rate = k["adverse"] / k_total if k_total else math.nan
        nk_rate = nk["adverse"] / nk_total if nk_total else math.nan
        tests.append(
            {
                "sample": sample,
                "k_plus_np_adverse_rate": k_rate,
                "nk_plus_p_adverse_rate": nk_rate,
                "rate_difference_nk_minus_k": nk_rate - k_rate,
                "fisher_exact_p_value": fisher_two_sided(
                    k["adverse"],
                    k["non_adverse"],
                    nk["adverse"],
                    nk["non_adverse"],
                ),
            }
        )
    return pd.DataFrame(rows), pd.DataFrame(tests)


def main() -> None:
    trades = pd.read_csv(TRADE_PATH)
    labels = pd.read_csv(LABEL_PATH)
    selected = trades[
        trades["floor_label"].eq(FLOOR_LABEL)
        & trades["profit_margin"].sub(PROFIT_MARGIN).abs().lt(1e-12)
    ].copy()
    annotated = annotate_directional_outcomes(selected, labels)
    summary, tests = summarize(annotated)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(OUT_CSV, index=False)

    all_tests = tests[tests["sample"].eq("all")].iloc[0]
    conclusion = (
        "The two directional adverse-split rates are not statistically distinguishable in the full historical sample."
        if all_tests["fisher_exact_p_value"] >= 0.05
        else "The two directional adverse-split rates differ at the 5% level in the full historical sample."
    )
    report = [
        "# Directional Adverse Split Risk",
        "",
        "## Scope",
        "",
        f"- Data: `{TRADE_PATH.relative_to(ROOT)}` joined to `{LABEL_PATH.relative_to(ROOT)}`.",
        "- Strategy slice: `latch_2m_1m` first entry after latch.",
        f"- Profit margin: `{PROFIT_MARGIN:.2f}`.",
        f"- Polymarket leg price floor: `{FLOOR_LABEL}`.",
        "- `K+NP` means buy Kalshi YES and Polymarket NO.",
        "- `NK+P` means buy Kalshi NO and Polymarket YES.",
        "- Adverse split means both legs lose:",
        "  - `K+NP` adverse: Kalshi settles NO and Polymarket settles YES.",
        "  - `NK+P` adverse: Kalshi settles YES and Polymarket settles NO.",
        "",
        "## Directional Rates",
        "",
        md_table(summary),
        "",
        "## Difference Test",
        "",
        md_table(tests),
        "",
        "## Conclusion",
        "",
        conclusion,
        "",
        "In the `all` sample, `K+NP` adverse-split risk is "
        f"{all_tests['k_plus_np_adverse_rate']:.2%}, while `NK+P` adverse-split risk is "
        f"{all_tests['nk_plus_p_adverse_rate']:.2%}. The point estimates differ by "
        f"{all_tests['rate_difference_nk_minus_k']:.2%}, but the Fisher exact p-value is "
        f"{all_tests['fisher_exact_p_value']:.4f}.",
        "",
        "This supports training direction-aware models because the economic outcome differs by direction, "
        "but this specific historical slice does not prove that one direction has a reliably higher adverse-split base rate than the other.",
        "",
        "## Output",
        "",
        f"- CSV summary: `{OUT_CSV.relative_to(ROOT)}`",
    ]
    OUT_REPORT.write_text("\n".join(report) + "\n")
    print(f"Wrote {OUT_REPORT}")
    print(f"Wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
