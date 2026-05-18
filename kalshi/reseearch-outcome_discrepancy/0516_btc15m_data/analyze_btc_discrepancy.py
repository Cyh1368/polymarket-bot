#!/usr/bin/env python3
import csv
import glob
import math
import os
from collections import defaultdict
from datetime import datetime, timezone


DATA_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(DATA_DIR, "analysis_outputs")
SOURCE_GAP_THRESHOLD = 100.0


def parse_dt(value):
    if not value:
        return None
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def f(row, key, default=math.nan):
    value = row.get(key, "")
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def finite(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


def target_for(rows, key, close_time):
    before_close = [f(r, key) for r in rows if r["timestamp_dt"] <= close_time and finite(f(r, key))]
    if before_close:
        return before_close[-1]
    any_target = [f(r, key) for r in rows if finite(f(r, key))]
    return any_target[0] if any_target else math.nan


def clean_rows(path):
    with open(path, newline="") as fp:
        for row in csv.DictReader(fp):
            if row.get("polymarket_error"):
                continue
            row["timestamp_dt"] = parse_dt(row["timestamp_utc"])
            row["kalshi_ts_dt"] = parse_dt(row["kalshi_timestamp_utc"])
            row["close_dt"] = parse_dt(row["kalshi_close_time"])
            yield row


def contract_id_from_path(path):
    base = os.path.basename(path)
    return base.removeprefix("combined_").removesuffix(".csv")


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {}
            for key in fieldnames:
                value = row.get(key, "")
                if isinstance(value, datetime):
                    value = value.isoformat().replace("+00:00", "Z")
                out[key] = value
            writer.writerow(out)


def payout(arb_type, kalshi_outcome, poly_outcome):
    if "UNKNOWN" in (kalshi_outcome, poly_outcome):
        return math.nan
    if kalshi_outcome == poly_outcome:
        return 1.0
    if arb_type == "K+NP":
        return 1.0 if kalshi_outcome == "YES" and poly_outcome == "NO" else 0.0
    return 1.0 if kalshi_outcome == "NO" and poly_outcome == "YES" else 0.0


def payout_after_winner_fee(normalized_payout, fee_rate=0.02):
    if not finite(normalized_payout):
        return math.nan
    return max(0.0, normalized_payout - fee_rate) if normalized_payout > 0 else 0.0


def outcome(price, target):
    if not finite(price) or not finite(target):
        return "UNKNOWN"
    return "YES" if price > target else "NO"


def add_features(row, close_time, open_time):
    ts = row["timestamp_dt"]
    k_price = f(row, "kalshi_btc_price")
    p_price = f(row, "polymarket_btc_price")
    k_target = f(row, "kalshi_btc_target")
    p_target = f(row, "polymarket_btc_target")
    k_dist = abs(k_price - k_target)
    p_dist = abs(p_price - p_target)
    min_dist = min(k_dist, p_dist)
    gap = abs(k_price - p_price)
    seconds_to_expiry = max(0.0, (close_time - ts).total_seconds())
    total_window = max(1.0, (close_time - open_time).total_seconds())
    kalshi_yes_bid = f(row, "kalshi_yes_bid")
    kalshi_yes_ask = f(row, "kalshi_yes_ask")
    poly_yes_bid = f(row, "polymarket_yes_bid")
    poly_yes_ask = f(row, "polymarket_yes_ask")
    row.update(
        {
            "kalshi_btc_distance_from_target": k_dist,
            "polymarket_btc_distance_from_target": p_dist,
            "min_distance_from_target": min_dist,
            "source_price_gap": gap,
            "source_price_gap_relative_to_target_distance": math.inf if min_dist == 0 else gap / min_dist,
            "seconds_to_expiry": seconds_to_expiry,
            "fraction_of_window_elapsed": 1.0 - seconds_to_expiry / total_window,
            "kalshi_spread": kalshi_yes_ask - kalshi_yes_bid,
            "polymarket_spread": poly_yes_ask - poly_yes_bid,
            "kalshi_yes_mid_calc": (kalshi_yes_ask + kalshi_yes_bid) / 2.0,
            "polymarket_yes_mid_calc": (poly_yes_ask + poly_yes_bid) / 2.0,
            "price_direction_agreement": (k_price > k_target) == (p_price > p_target),
            "kalshi_60sma_vs_target": f(row, "kalshi_btc_60_sma") - k_target,
            "sma_sample_count": f(row, "kalshi_btc_60_sma_sample_count"),
        }
    )
    row["kalshi_current_closer_than_60sma"] = (
        abs(row["kalshi_btc_distance_from_target"]) < abs(row["kalshi_60sma_vs_target"])
        if finite(row["kalshi_btc_distance_from_target"]) and finite(row["kalshi_60sma_vs_target"])
        else False
    )
    return row


def fmt(x, nd=4):
    if isinstance(x, float):
        if math.isinf(x):
            return "inf"
        if math.isnan(x):
            return ""
        return round(x, nd)
    return x


def scan_thresholds(arb_rows, feature, direction):
    labeled = [r for r in arb_rows if r["dangerous"] is not None]
    safe_total = sum(not r["dangerous"] for r in labeled)
    danger_total = sum(r["dangerous"] for r in labeled)
    values = sorted({r[feature] for r in arb_rows if isinstance(r.get(feature), (int, float)) and math.isfinite(r[feature])})
    if not values:
        return None, []
    candidates = []
    if len(values) > 60:
        for i in range(0, 101, 5):
            candidates.append(values[min(len(values) - 1, int((len(values) - 1) * i / 100))])
    else:
        candidates = values
    rows = []
    best = None
    for t in candidates:
        def allow(v):
            return v <= t if direction == "max" else v >= t
        allowed = [r for r in labeled if allow(r[feature])]
        blocked = [r for r in labeled if not allow(r[feature])]
        allowed_danger = sum(r["dangerous"] for r in allowed)
        blocked_safe = sum(not r["dangerous"] for r in blocked)
        fpr = allowed_danger / danger_total if danger_total else 0.0
        fnr = blocked_safe / safe_total if safe_total else 0.0
        score = fpr + fnr
        row = {
            "feature": feature,
            "allow_rule": f"{feature} {'<=' if direction == 'max' else '>='} {t:.6g}",
            "threshold": t,
            "allowed_dangerous": allowed_danger,
            "blocked_dangerous": danger_total - allowed_danger,
            "blocked_safe": blocked_safe,
            "allowed_safe": safe_total - blocked_safe,
            "false_positive_rate_allowed_danger": fpr,
            "false_negative_rate_blocked_safe": fnr,
            "score": score,
        }
        rows.append(row)
        if best is None or score < best["score"]:
            best = row
    return best, rows


def evaluate_filter(rows, name, predicate):
    labeled = [r for r in rows if r["dangerous"] is not None and finite(r["actual_pnl"])]
    safe_total = sum(not r["dangerous"] for r in labeled)
    danger_total = sum(r["dangerous"] for r in labeled)
    base_pnl = sum(r["actual_pnl"] for r in labeled)
    allowed = [r for r in labeled if predicate(r)]
    blocked = [r for r in labeled if not predicate(r)]
    return {
        "filter": name,
        "total_ticks": len(rows),
        "allowed_ticks": len(allowed),
        "blocked_ticks": len(blocked),
        "dangerous_total": danger_total,
        "dangerous_blocked": sum(r["dangerous"] for r in blocked),
        "dangerous_allowed": sum(r["dangerous"] for r in allowed),
        "safe_total": safe_total,
        "safe_blocked": sum(not r["dangerous"] for r in blocked),
        "safe_allowed": sum(not r["dangerous"] for r in allowed),
        "base_total_pnl": base_pnl,
        "filtered_total_pnl": sum(r["actual_pnl"] for r in allowed),
        "ev_improvement": sum(r["actual_pnl"] for r in allowed) - base_pnl,
        "avg_allowed_pnl": sum(r["actual_pnl"] for r in allowed) / len(allowed) if allowed else 0.0,
    }


def evaluate_contract_filter(rows, name, predicate):
    by_contract = defaultdict(list)
    for row in rows:
        if row["dangerous"] is None or not finite(row["actual_pnl"]):
            continue
        by_contract[row["contract_id"]].append(row)

    entries = []
    blocked_contracts = 0
    for cid, contract_rows in sorted(by_contract.items()):
        ordered = sorted(contract_rows, key=lambda r: (r["timestamp_dt"], r["arb_type"]))
        allowed = [r for r in ordered if predicate(r)]
        if not allowed:
            blocked_contracts += 1
            continue
        entry = allowed[0]
        entries.append(
            {
                "filter": name,
                "contract_id": cid,
                "entry_timestamp_utc": entry["timestamp_utc"],
                "arb_type": entry["arb_type"],
                "arb_cost": entry["arb_cost"],
                "theoretical_profit": entry["theoretical_profit"],
                "actual_payout": entry["actual_payout"],
                "actual_pnl": entry["actual_pnl"],
                "fee_adjusted_payout": entry["fee_adjusted_payout"],
                "fee_adjusted_pnl": entry["fee_adjusted_pnl"],
                "dangerous_contract_entry": entry["dangerous"],
                "kalshi_outcome": entry["kalshi_outcome"],
                "polymarket_outcome": entry["polymarket_outcome"],
                "source_price_gap": entry["source_price_gap"],
                "min_distance_from_target": entry["min_distance_from_target"],
                "seconds_to_expiry": entry["seconds_to_expiry"],
                "price_direction_agreement": entry["price_direction_agreement"],
                "target_divergence": entry["target_divergence"],
                "abs_target_divergence": entry["abs_target_divergence"],
            }
        )

    safe_entries = [r for r in entries if not r["dangerous_contract_entry"]]
    dangerous_entries = [r for r in entries if r["dangerous_contract_entry"]]
    total_pnl = sum(r["actual_pnl"] for r in entries)
    fee_adjusted_total_pnl = sum(r["fee_adjusted_pnl"] for r in entries)
    avg_safe_gain = sum(r["actual_pnl"] for r in safe_entries) / len(safe_entries) if safe_entries else 0.0
    avg_danger_loss = sum(r["actual_pnl"] for r in dangerous_entries) / len(dangerous_entries) if dangerous_entries else 0.0
    fee_adjusted_avg_safe_gain = sum(r["fee_adjusted_pnl"] for r in safe_entries) / len(safe_entries) if safe_entries else 0.0
    fee_adjusted_avg_danger_loss = sum(r["fee_adjusted_pnl"] for r in dangerous_entries) / len(dangerous_entries) if dangerous_entries else 0.0
    breakeven_danger_rate = (
        avg_safe_gain / (avg_safe_gain + abs(avg_danger_loss))
        if avg_safe_gain > 0 and avg_danger_loss < 0
        else ""
    )
    fee_adjusted_breakeven_danger_rate = (
        fee_adjusted_avg_safe_gain / (fee_adjusted_avg_safe_gain + abs(fee_adjusted_avg_danger_loss))
        if fee_adjusted_avg_safe_gain > 0 and fee_adjusted_avg_danger_loss < 0
        else ""
    )
    summary = {
        "filter": name,
        "eligible_contracts": len(by_contract),
        "entered_contracts": len(entries),
        "blocked_contracts": blocked_contracts,
        "safe_entries": len(safe_entries),
        "dangerous_entries": len(dangerous_entries),
        "dangerous_entry_rate": len(dangerous_entries) / len(entries) if entries else 0.0,
        "win_rate": len(safe_entries) / len(entries) if entries else 0.0,
        "total_pnl": total_pnl,
        "avg_pnl_per_entry": total_pnl / len(entries) if entries else 0.0,
        "fee_adjusted_total_pnl": fee_adjusted_total_pnl,
        "fee_adjusted_avg_pnl_per_entry": fee_adjusted_total_pnl / len(entries) if entries else 0.0,
        "avg_safe_gain": avg_safe_gain,
        "avg_danger_loss": avg_danger_loss,
        "breakeven_danger_rate": breakeven_danger_rate,
        "fee_adjusted_avg_safe_gain": fee_adjusted_avg_safe_gain,
        "fee_adjusted_avg_danger_loss": fee_adjusted_avg_danger_loss,
        "fee_adjusted_breakeven_danger_rate": fee_adjusted_breakeven_danger_rate,
        "safe_entries_cost_ge_098": sum((not r["dangerous_contract_entry"]) and r["arb_cost"] >= 0.98 for r in entries),
        "safe_entries_cost_le_096": sum((not r["dangerous_contract_entry"]) and r["arb_cost"] <= 0.96 for r in entries),
    }
    return summary, entries


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    files = sorted(glob.glob(os.path.join(DATA_DIR, "combined_KXBTC15M-*.csv")))
    contract_rows = []
    discrepant_rows = []
    arb_tick_rows = []
    timeline_rows = []

    for path in files:
        cid = contract_id_from_path(path)
        rows = sorted(clean_rows(path), key=lambda r: r["timestamp_dt"])
        if not rows:
            continue
        close_time = rows[0]["close_dt"]
        open_time = min(r["timestamp_dt"] for r in rows)
        settlement_candidates = [r for r in rows if r["timestamp_dt"] <= close_time]
        final = settlement_candidates[-1] if settlement_candidates else rows[-1]
        k_final = f(final, "kalshi_btc_price")
        p_final = f(final, "polymarket_btc_price")
        k_target = target_for(rows, "kalshi_btc_target", close_time)
        p_target = target_for(rows, "polymarket_btc_target", close_time)
        k_outcome = outcome(k_final, k_target)
        p_outcome = outcome(p_final, p_target)
        agree = None if "UNKNOWN" in (k_outcome, p_outcome) else k_outcome == p_outcome
        active_rows = [add_features(r, close_time, open_time) for r in rows if r.get("kalshi_status") == "active"]

        had_arb = False
        had_danger = False
        worst_tick_loss = 0.0
        for r in active_rows:
            for arb_type, cost_key in (("K+NP", "k_plus_np"), ("NK+P", "nk_plus_p")):
                cost = f(r, cost_key)
                if not math.isfinite(cost) or cost >= 1.0:
                    continue
                had_arb = True
                pay = payout(arb_type, k_outcome, p_outcome)
                fee_pay = payout_after_winner_fee(pay)
                actual_pnl = pay - cost if finite(pay) else math.nan
                fee_adjusted_pnl = fee_pay - cost if finite(fee_pay) else math.nan
                danger = None if agree is None else ((not agree) and actual_pnl < 0)
                had_danger = had_danger or bool(danger)
                if finite(actual_pnl) and actual_pnl < worst_tick_loss:
                    worst_tick_loss = actual_pnl
                arb_tick_rows.append(
                    {
                        **r,
                        "timestamp_utc": r["timestamp_utc"],
                        "contract_id": cid,
                        "arb_type": arb_type,
                        "arb_cost": cost,
                        "theoretical_profit": 1.0 - cost,
                        "actual_payout": pay,
                        "actual_pnl": actual_pnl,
                        "fee_adjusted_payout": fee_pay,
                        "fee_adjusted_pnl": fee_adjusted_pnl,
                        "fee_adjusted_theoretical_profit": fee_pay - cost if finite(fee_pay) and fee_pay > 0 else math.nan,
                        "is_fee_viable_one_winner": cost <= 0.98,
                        "clears_four_cent_edge": cost <= 0.96,
                        "was_loss": actual_pnl < 0 if finite(actual_pnl) else None,
                        "fee_adjusted_was_loss": fee_adjusted_pnl < 0 if finite(fee_adjusted_pnl) else None,
                        "dangerous": danger,
                        "outcomes_agree": agree,
                        "kalshi_outcome": k_outcome,
                        "polymarket_outcome": p_outcome,
                        "target_divergence": k_target - p_target,
                        "abs_target_divergence": abs(k_target - p_target) if finite(k_target) and finite(p_target) else math.inf,
                    }
                )

        c_row = {
            "contract_id": cid,
            "close_time": close_time,
            "open_time": open_time,
            "duration_remaining_at_first_tick": (close_time - open_time).total_seconds(),
            "kalshi_final_price": k_final,
            "polymarket_final_price": p_final,
            "kalshi_btc_target": k_target,
            "polymarket_btc_target": p_target,
            "kalshi_outcome": k_outcome,
            "polymarket_outcome": p_outcome,
            "outcomes_agree": agree,
            "price_divergence_at_close": k_final - p_final,
            "abs_price_divergence_at_close": abs(k_final - p_final),
            "target_divergence": k_target - p_target,
            "kalshi_margin_to_flip": abs(k_final - k_target),
            "polymarket_margin_to_flip": abs(p_final - p_target),
            "had_arb_opportunity": had_arb,
            "had_dangerous_arb": had_danger,
            "worst_tick_loss": worst_tick_loss,
        }
        contract_rows.append(c_row)
        if agree is False:
            discrepant_rows.append(c_row)
            # 10 bucket pre-close timeline, using nearest row per bucket.
            for secs in (900, 600, 300, 180, 120, 90, 60, 30, 15, 5, 0):
                candidates = [r for r in active_rows if r["seconds_to_expiry"] >= secs]
                r = candidates[-1] if candidates else (active_rows[0] if active_rows else None)
                if r:
                    timeline_rows.append(
                        {
                            "contract_id": cid,
                            "seconds_to_expiry_bucket": secs,
                            "timestamp_utc": r["timestamp_utc"],
                            "source_price_gap": r["source_price_gap"],
                            "min_distance_from_target": r["min_distance_from_target"],
                            "gap_to_min_distance_ratio": r["source_price_gap_relative_to_target_distance"],
                            "price_direction_agreement": r["price_direction_agreement"],
                        }
                    )

    arb_tick_rows.sort(key=lambda r: (r["contract_id"], r["timestamp_utc"], r["arb_type"]))
    danger_by_contract = defaultdict(list)
    for r in arb_tick_rows:
        if r["dangerous"]:
            danger_by_contract[r["contract_id"]].append(r["actual_pnl"])
    worst_by_contract = [
        {
            "contract_id": cid,
            "dangerous_tick_count": len(vals),
            "worst_actual_pnl": min(vals),
        }
        for cid, vals in sorted(danger_by_contract.items())
    ]

    features = [
        ("kalshi_btc_distance_from_target", "min"),
        ("polymarket_btc_distance_from_target", "min"),
        ("min_distance_from_target", "min"),
        ("source_price_gap", "max"),
        ("source_price_gap_relative_to_target_distance", "max"),
        ("seconds_to_expiry", "min"),
        ("fraction_of_window_elapsed", "max"),
        ("kalshi_spread", "max"),
        ("polymarket_spread", "max"),
        ("kalshi_yes_mid_calc", "max"),
        ("polymarket_yes_mid_calc", "max"),
        ("kalshi_60sma_vs_target", "min"),
        ("sma_sample_count", "min"),
    ]
    scan_rows = []
    best_rows = []
    for feature, direction in features:
        best, rows = scan_thresholds(arb_tick_rows, feature, direction)
        scan_rows.extend(rows)
        if best:
            best_rows.append(best)

    filter_results = []
    filters = [
        ("baseline_first_arb_no_filter", lambda r: True),
        ("gap<=3 and min_distance>=10 and seconds_to_expiry>=120", lambda r: r["source_price_gap"] <= 3 and r["min_distance_from_target"] >= 10 and r["seconds_to_expiry"] >= 120),
        ("gap_ratio<=1 and min_distance>=5 and seconds_to_expiry>=60", lambda r: r["source_price_gap_relative_to_target_distance"] <= 1 and r["min_distance_from_target"] >= 5 and r["seconds_to_expiry"] >= 60),
        ("direction_agree and gap<=100 and min_distance>=10", lambda r: r["price_direction_agreement"] and r["source_price_gap"] <= SOURCE_GAP_THRESHOLD and r["min_distance_from_target"] >= 10),
        ("direction_agree and gap_ratio<=1 and seconds_to_expiry>=30", lambda r: r["price_direction_agreement"] and r["source_price_gap_relative_to_target_distance"] <= 1 and r["seconds_to_expiry"] >= 30),
        ("direction_agree and gap<=100 and min_distance>=10 and abs_target_divergence<=15", lambda r: r["price_direction_agreement"] and r["source_price_gap"] <= SOURCE_GAP_THRESHOLD and r["min_distance_from_target"] >= 10 and r["abs_target_divergence"] <= 15),
        ("time_scaled_distance and gap<=100 and target_divergence<=15", lambda r: r["price_direction_agreement"] and r["source_price_gap"] <= SOURCE_GAP_THRESHOLD and r["min_distance_from_target"] >= max(10.0, r["seconds_to_expiry"] * 0.05) and r["abs_target_divergence"] <= 15),
        ("time_scaled_distance and gap<=100 no_target_cap", lambda r: r["price_direction_agreement"] and r["source_price_gap"] <= SOURCE_GAP_THRESHOLD and r["min_distance_from_target"] >= max(10.0, r["seconds_to_expiry"] * 0.05)),
        ("time_scaled_distance and gap<=100 target_divergence<=30", lambda r: r["price_direction_agreement"] and r["source_price_gap"] <= SOURCE_GAP_THRESHOLD and r["min_distance_from_target"] >= max(10.0, r["seconds_to_expiry"] * 0.05) and r["abs_target_divergence"] <= 30),
        ("time_scaled_distance and gap<=100 target_divergence<=35", lambda r: r["price_direction_agreement"] and r["source_price_gap"] <= SOURCE_GAP_THRESHOLD and r["min_distance_from_target"] >= max(10.0, r["seconds_to_expiry"] * 0.05) and r["abs_target_divergence"] <= 35),
        ("canonical_gap100 + arb_cost<=0.98", lambda r: r["price_direction_agreement"] and r["source_price_gap"] <= SOURCE_GAP_THRESHOLD and r["min_distance_from_target"] >= max(10.0, r["seconds_to_expiry"] * 0.05) and r["abs_target_divergence"] <= 35 and r["arb_cost"] <= 0.98),
        ("canonical_gap100 + arb_cost<=0.96", lambda r: r["price_direction_agreement"] and r["source_price_gap"] <= SOURCE_GAP_THRESHOLD and r["min_distance_from_target"] >= max(10.0, r["seconds_to_expiry"] * 0.05) and r["abs_target_divergence"] <= 35 and r["arb_cost"] <= 0.96),
        ("canonical_gap100 + arb_cost<=0.95", lambda r: r["price_direction_agreement"] and r["source_price_gap"] <= SOURCE_GAP_THRESHOLD and r["min_distance_from_target"] >= max(10.0, r["seconds_to_expiry"] * 0.05) and r["abs_target_divergence"] <= 35 and r["arb_cost"] <= 0.95),
        ("time_scaled + target<=15 + not drifting_to_wire", lambda r: r["price_direction_agreement"] and r["source_price_gap"] <= SOURCE_GAP_THRESHOLD and r["min_distance_from_target"] >= max(10.0, r["seconds_to_expiry"] * 0.05) and r["abs_target_divergence"] <= 15 and not r["kalshi_current_closer_than_60sma"]),
        ("very_conservative: gap<=3 distance>=max(25,t*0.05) target<=10 not drifting", lambda r: r["price_direction_agreement"] and r["source_price_gap"] <= 3 and r["min_distance_from_target"] >= max(25.0, r["seconds_to_expiry"] * 0.05) and r["abs_target_divergence"] <= 10 and not r["kalshi_current_closer_than_60sma"]),
    ]
    contract_filter_results = []
    contract_entry_rows = []
    for name, pred in filters:
        filter_results.append(evaluate_filter(arb_tick_rows, name, pred))
        summary, entries = evaluate_contract_filter(arb_tick_rows, name, pred)
        contract_filter_results.append(summary)
        contract_entry_rows.extend(entries)

    write_csv(
        os.path.join(OUT_DIR, "contract_summary.csv"),
        contract_rows,
        [
            "contract_id", "close_time", "open_time", "duration_remaining_at_first_tick",
            "kalshi_outcome", "polymarket_outcome", "outcomes_agree",
            "kalshi_final_price", "polymarket_final_price", "kalshi_btc_target",
            "polymarket_btc_target", "price_divergence_at_close",
            "abs_price_divergence_at_close", "target_divergence",
            "had_arb_opportunity", "had_dangerous_arb", "worst_tick_loss",
        ],
    )
    write_csv(os.path.join(OUT_DIR, "discrepancies.csv"), discrepant_rows, [
        "contract_id", "close_time", "kalshi_outcome", "polymarket_outcome",
        "kalshi_final_price", "polymarket_final_price", "kalshi_btc_target",
        "polymarket_btc_target", "price_divergence_at_close", "target_divergence",
        "kalshi_margin_to_flip", "polymarket_margin_to_flip", "had_arb_opportunity",
        "had_dangerous_arb", "worst_tick_loss",
    ])
    write_csv(os.path.join(OUT_DIR, "arb_ticks.csv"), arb_tick_rows, [
        "timestamp_utc", "contract_id", "arb_type", "arb_cost", "theoretical_profit",
        "actual_payout", "actual_pnl", "fee_adjusted_payout", "fee_adjusted_pnl",
        "fee_adjusted_theoretical_profit", "is_fee_viable_one_winner",
        "clears_four_cent_edge", "was_loss", "fee_adjusted_was_loss", "dangerous", "kalshi_outcome",
        "polymarket_outcome", "source_price_gap", "min_distance_from_target",
        "kalshi_btc_distance_from_target", "polymarket_btc_distance_from_target",
        "source_price_gap_relative_to_target_distance", "seconds_to_expiry",
        "fraction_of_window_elapsed", "price_direction_agreement", "kalshi_spread",
        "polymarket_spread", "kalshi_yes_mid_calc", "polymarket_yes_mid_calc",
        "kalshi_60sma_vs_target", "kalshi_current_closer_than_60sma",
        "sma_sample_count", "target_divergence", "abs_target_divergence",
    ])
    write_csv(os.path.join(OUT_DIR, "worst_danger_by_contract.csv"), worst_by_contract, [
        "contract_id", "dangerous_tick_count", "worst_actual_pnl",
    ])
    write_csv(os.path.join(OUT_DIR, "threshold_scan_all.csv"), scan_rows, [
        "feature", "allow_rule", "threshold", "allowed_dangerous", "blocked_dangerous",
        "blocked_safe", "allowed_safe", "false_positive_rate_allowed_danger",
        "false_negative_rate_blocked_safe", "score",
    ])
    write_csv(os.path.join(OUT_DIR, "threshold_scan_best.csv"), best_rows, [
        "feature", "allow_rule", "threshold", "allowed_dangerous", "blocked_dangerous",
        "blocked_safe", "allowed_safe", "false_positive_rate_allowed_danger",
        "false_negative_rate_blocked_safe", "score",
    ])
    write_csv(os.path.join(OUT_DIR, "filter_eval.csv"), filter_results, [
        "filter", "total_ticks", "allowed_ticks", "blocked_ticks", "dangerous_total",
        "dangerous_blocked", "dangerous_allowed", "safe_total", "safe_blocked",
        "safe_allowed", "base_total_pnl", "filtered_total_pnl", "ev_improvement",
        "avg_allowed_pnl",
    ])
    write_csv(os.path.join(OUT_DIR, "contract_filter_eval.csv"), contract_filter_results, [
        "filter", "eligible_contracts", "entered_contracts", "blocked_contracts",
        "safe_entries", "dangerous_entries", "dangerous_entry_rate", "win_rate",
        "total_pnl", "avg_pnl_per_entry", "fee_adjusted_total_pnl",
        "fee_adjusted_avg_pnl_per_entry", "avg_safe_gain", "avg_danger_loss",
        "breakeven_danger_rate", "fee_adjusted_avg_safe_gain",
        "fee_adjusted_avg_danger_loss", "fee_adjusted_breakeven_danger_rate",
        "safe_entries_cost_ge_098", "safe_entries_cost_le_096",
    ])
    write_csv(os.path.join(OUT_DIR, "contract_entries.csv"), contract_entry_rows, [
        "filter", "contract_id", "entry_timestamp_utc", "arb_type", "arb_cost",
        "theoretical_profit", "actual_payout", "actual_pnl",
        "fee_adjusted_payout", "fee_adjusted_pnl",
        "dangerous_contract_entry", "kalshi_outcome", "polymarket_outcome",
        "source_price_gap", "min_distance_from_target", "seconds_to_expiry",
        "price_direction_agreement", "target_divergence", "abs_target_divergence",
    ])
    write_csv(os.path.join(OUT_DIR, "discrepant_timeline.csv"), timeline_rows, [
        "contract_id", "seconds_to_expiry_bucket", "timestamp_utc", "source_price_gap",
        "min_distance_from_target", "gap_to_min_distance_ratio", "price_direction_agreement",
    ])

    print(f"contracts={len(contract_rows)}")
    print(f"discrepancies={len(discrepant_rows)} fraction={len(discrepant_rows)/len(contract_rows):.4f}")
    print(f"arb_ticks={len(arb_tick_rows)} dangerous={sum(r['dangerous'] is True for r in arb_tick_rows)}")
    print(f"outputs={OUT_DIR}")


if __name__ == "__main__":
    main()
