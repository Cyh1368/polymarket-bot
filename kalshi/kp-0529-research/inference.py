from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd


ARTIFACT_DIR = Path(__file__).resolve().parent
MODEL_PATH = ARTIFACT_DIR / "divergence_model.pkl"
FEATURE_LIST_PATH = ARTIFACT_DIR / "feature_list.json"
METADATA_PATH = ARTIFACT_DIR / "divergence_model_metadata.json"

CONTRACT_SECONDS = 15 * 60
KALSHI_FEE_RATE = 0.07
POLYMARKET_FEE_RATE = 0.05
CONTRACTS_PER_LEG = 1.0

_MODEL = None
_FEATURES: list[str] | None = None
_METADATA: dict[str, Any] | None = None
_HISTORY: dict[str, list[dict[str, Any]]] = {}


FRIENDLY_SIGNALS = {
    "feeds_on_same_side": "price feeds are on opposite sides of the target",
    "spread_vs_distance_ratio": "feed spread is large relative to distance from target",
    "price_spread_abs": "large BRTI vs RTDS price spread",
    "kalshi_distance_to_target": "Kalshi feed is close to the settlement boundary",
    "polymarket_distance_to_target": "Polymarket feed is close to the settlement boundary",
    "implied_prob_spread": "Kalshi and Polymarket implied probabilities disagree",
    "time_to_close_seconds": "late-contract timing",
    "elapsed_fraction": "late-contract timing",
    "arb_available": "live arbitrage signal is present",
    "k_plus_np": "Kalshi YES / Polymarket NO arb signal",
    "nk_plus_p": "Kalshi NO / Polymarket YES arb signal",
}


def _load_artifacts() -> tuple[Any, list[str], dict[str, Any]]:
    global _MODEL, _FEATURES, _METADATA
    if _MODEL is None:
        _MODEL = joblib.load(MODEL_PATH)
    if _FEATURES is None:
        _FEATURES = json.loads(FEATURE_LIST_PATH.read_text())
    if _METADATA is None:
        _METADATA = json.loads(METADATA_PATH.read_text()) if METADATA_PATH.exists() else {}
    return _MODEL, _FEATURES, _METADATA


def _to_float(value: Any) -> float:
    if value is None:
        return math.nan
    if isinstance(value, str) and value.strip() in {"", "nan", "NaN", "None", "null"}:
        return math.nan
    try:
        out = float(value)
    except (TypeError, ValueError):
        return math.nan
    return out if math.isfinite(out) else math.nan


def _kalshi_fee(price: float) -> float:
    return KALSHI_FEE_RATE * CONTRACTS_PER_LEG * price * (1.0 - price) if math.isfinite(price) else math.nan


def _polymarket_fee(price: float) -> float:
    return POLYMARKET_FEE_RATE * CONTRACTS_PER_LEG * price * (1.0 - price) if math.isfinite(price) else math.nan


def _parse_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce", format="mixed")
    if pd.isna(ts):
        return None
    return ts


def _contract_key(snapshot: dict[str, Any]) -> str:
    for key in ("kalshi_ticker", "contract_id", "polymarket_ticker", "source_file"):
        value = snapshot.get(key)
        if value:
            return str(value)
    close_time = snapshot.get("kalshi_close_time")
    return f"unknown-{close_time}" if close_time else "unknown"


def _safe_std(values: list[float]) -> float:
    vals = np.asarray([v for v in values if math.isfinite(v)], dtype=float)
    if len(vals) < 2:
        return math.nan
    return float(np.std(vals, ddof=1))


def _safe_mean(values: list[float]) -> float:
    vals = np.asarray([v for v in values if math.isfinite(v)], dtype=float)
    if len(vals) == 0:
        return math.nan
    return float(np.mean(vals))


def _update_history(key: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
    history = _HISTORY.setdefault(key, [])
    ts = entry.get("timestamp")
    if ts is not None:
        history[:] = [item for item in history if item.get("timestamp") != ts]
    history.append(entry)
    history.sort(key=lambda item: item.get("timestamp") or pd.Timestamp.max.tz_localize("UTC"))
    if len(history) > 128:
        del history[:-128]
    if ts is None:
        return list(history)
    return [item for item in history if item.get("timestamp") is not None and item["timestamp"] <= ts]


def reset_history(contract_id: str | None = None) -> None:
    if contract_id is None:
        _HISTORY.clear()
    else:
        _HISTORY.pop(contract_id, None)


def _history_features(snapshot: dict[str, Any], basic: dict[str, float]) -> dict[str, float]:
    key = _contract_key(snapshot)
    ts = _parse_timestamp(snapshot.get("timestamp_utc"))
    entry = {
        "timestamp": ts,
        "kalshi_btc_price": basic["kalshi_btc_price"],
        "price_spread": basic["price_spread"],
        "implied_prob_spread": basic["implied_prob_spread"],
    }
    history = _update_history(key, entry)
    prices = [item["kalshi_btc_price"] for item in history]
    spreads = [item["price_spread"] for item in history]
    prob_spreads = [item["implied_prob_spread"] for item in history]

    lag5 = prices[-6] if len(prices) >= 6 and math.isfinite(prices[-6]) else math.nan
    lag10 = prices[-11] if len(prices) >= 11 and math.isfinite(prices[-11]) else math.nan
    current_price = basic["kalshi_btc_price"]

    return {
        "price_spread_roll10_std": _safe_std(spreads[-10:]),
        "kalshi_btc_price_roll10_mean": _safe_mean(prices[-10:]),
        "kalshi_btc_price_roll10_std": _safe_std(prices[-10:]),
        "kalshi_btc_price_lag5": lag5,
        "kalshi_btc_price_lag10": lag10,
        "kalshi_btc_price_momentum_5": current_price - lag5
        if math.isfinite(current_price) and math.isfinite(lag5)
        else math.nan,
        "kalshi_btc_price_momentum_10": current_price - lag10
        if math.isfinite(current_price) and math.isfinite(lag10)
        else math.nan,
        "implied_prob_spread_roll10_std": _safe_std(prob_spreads[-10:]),
        "kalshi_btc_price_history_mean30": _safe_mean(prices[-30:]),
    }


def _compute_features(snapshot: dict[str, Any]) -> dict[str, float]:
    kalshi_price = _to_float(snapshot.get("kalshi_btc_price"))
    poly_price = _to_float(snapshot.get("polymarket_btc_price"))
    kalshi_target = _to_float(snapshot.get("kalshi_btc_target"))
    kalshi_yes_bid = _to_float(snapshot.get("kalshi_yes_bid"))
    kalshi_yes_ask = _to_float(snapshot.get("kalshi_yes_ask"))
    kalshi_yes_mid = _to_float(snapshot.get("kalshi_yes_mid"))
    kalshi_last_price = _to_float(snapshot.get("kalshi_last_price"))
    kalshi_yes_qty = _to_float(snapshot.get("kalshi_best_yes_bid_qty"))
    kalshi_no_qty = _to_float(snapshot.get("kalshi_best_no_bid_qty"))
    poly_yes_bid = _to_float(snapshot.get("polymarket_yes_bid"))
    poly_yes_ask = _to_float(snapshot.get("polymarket_yes_ask"))
    poly_yes_mid = _to_float(snapshot.get("polymarket_yes_mid"))
    poly_yes_qty = _to_float(snapshot.get("polymarket_best_yes_bid_qty"))
    poly_no_qty = _to_float(snapshot.get("polymarket_best_no_bid_qty"))
    k_plus_np = _to_float(snapshot.get("k_plus_np"))
    nk_plus_p = _to_float(snapshot.get("nk_plus_p"))

    price_spread = kalshi_price - poly_price if math.isfinite(kalshi_price) and math.isfinite(poly_price) else math.nan
    implied_prob_spread = (
        kalshi_yes_mid - poly_yes_mid if math.isfinite(kalshi_yes_mid) and math.isfinite(poly_yes_mid) else math.nan
    )
    basic = {
        "kalshi_btc_price": kalshi_price,
        "price_spread": price_spread,
        "implied_prob_spread": implied_prob_spread,
    }
    history = _history_features(snapshot, basic)

    raw_sma = _to_float(snapshot.get("kalshi_btc_60_sma"))
    kalshi_sma = raw_sma if math.isfinite(raw_sma) else history["kalshi_btc_price_history_mean30"]
    if not math.isfinite(kalshi_sma):
        kalshi_sma = kalshi_price

    timestamp = _parse_timestamp(snapshot.get("timestamp_utc"))
    close_time = _parse_timestamp(snapshot.get("kalshi_close_time"))
    if timestamp is not None and close_time is not None:
        time_to_close = float((close_time - timestamp).total_seconds())
        start_time = close_time - pd.Timedelta(seconds=CONTRACT_SECONDS)
        elapsed_fraction = float((timestamp - start_time).total_seconds() / CONTRACT_SECONDS)
        elapsed_fraction = min(1.0, max(0.0, elapsed_fraction))
    else:
        time_to_close = math.nan
        elapsed_fraction = math.nan

    kalshi_distance = (
        kalshi_sma - kalshi_target if math.isfinite(kalshi_sma) and math.isfinite(kalshi_target) else math.nan
    )
    poly_distance = (
        poly_price - kalshi_target if math.isfinite(poly_price) and math.isfinite(kalshi_target) else math.nan
    )
    if math.isfinite(kalshi_distance) and math.isfinite(poly_distance):
        feeds_on_same_side = float(
            (kalshi_distance > 0 and poly_distance > 0) or (kalshi_distance < 0 and poly_distance < 0)
        )
    else:
        feeds_on_same_side = math.nan

    price_spread_abs = abs(price_spread) if math.isfinite(price_spread) else math.nan
    ratio = (
        min(price_spread_abs / (abs(kalshi_distance) + 1e-6), 1_000_000.0)
        if math.isfinite(price_spread_abs) and math.isfinite(kalshi_distance)
        else math.nan
    )

    features = {
        "price_spread": price_spread,
        "price_spread_abs": price_spread_abs,
        "kalshi_distance_to_target": kalshi_distance,
        "polymarket_distance_to_target": poly_distance,
        "spread_vs_distance_ratio": ratio,
        "feeds_on_same_side": feeds_on_same_side,
        "elapsed_fraction": elapsed_fraction,
        "time_to_close_seconds": time_to_close,
        "kalshi_bid_ask_spread_yes": kalshi_yes_ask - kalshi_yes_bid
        if math.isfinite(kalshi_yes_ask) and math.isfinite(kalshi_yes_bid)
        else math.nan,
        "kalshi_order_book_imbalance": (kalshi_yes_qty - kalshi_no_qty) / (kalshi_yes_qty + kalshi_no_qty + 1e-6)
        if math.isfinite(kalshi_yes_qty) and math.isfinite(kalshi_no_qty)
        else math.nan,
        "kalshi_yes_mid": kalshi_yes_mid,
        "kalshi_last_price": kalshi_last_price,
        "polymarket_bid_ask_spread_yes": poly_yes_ask - poly_yes_bid
        if math.isfinite(poly_yes_ask) and math.isfinite(poly_yes_bid)
        else math.nan,
        "polymarket_order_book_imbalance": (poly_yes_qty - poly_no_qty) / (poly_yes_qty + poly_no_qty + 1e-6)
        if math.isfinite(poly_yes_qty) and math.isfinite(poly_no_qty)
        else math.nan,
        "polymarket_yes_mid": poly_yes_mid,
        "implied_prob_spread": implied_prob_spread,
        "k_plus_np": k_plus_np,
        "nk_plus_p": nk_plus_p,
        "arb_available": float(
            (math.isfinite(k_plus_np) and k_plus_np > 1.0)
            or (math.isfinite(nk_plus_p) and nk_plus_p > 1.0)
        ),
        "polymarket_error_flag": 0.0 if pd.isna(snapshot.get("polymarket_error")) else 1.0,
    }
    features["price_spread_abs_x_elapsed_fraction"] = (
        price_spread_abs * elapsed_fraction
        if math.isfinite(price_spread_abs) and math.isfinite(elapsed_fraction)
        else math.nan
    )
    features["spread_vs_distance_ratio_x_elapsed_fraction"] = (
        ratio * elapsed_fraction if math.isfinite(ratio) and math.isfinite(elapsed_fraction) else math.nan
    )
    features["feeds_on_same_side_x_elapsed_fraction"] = (
        feeds_on_same_side * elapsed_fraction
        if math.isfinite(feeds_on_same_side) and math.isfinite(elapsed_fraction)
        else math.nan
    )
    features.update({key: value for key, value in history.items() if key != "kalshi_btc_price_history_mean30"})
    return features


def _dominant_signal(features: dict[str, float], metadata: dict[str, Any]) -> str:
    if features.get("feeds_on_same_side") == 0.0:
        return FRIENDLY_SIGNALS["feeds_on_same_side"]

    stats = metadata.get("feature_stats", {})
    best_feature = None
    best_score = -1.0
    for feature, feature_stats in stats.items():
        value = features.get(feature, math.nan)
        if not math.isfinite(value):
            continue
        importance = float(feature_stats.get("importance_normalized", 0.0))
        std = float(feature_stats.get("std", 0.0))
        mean = float(feature_stats.get("mean", 0.0))
        z_score = abs((value - mean) / std) if std > 1e-9 else abs(value)
        score = importance * max(z_score, 0.1)
        if score > best_score:
            best_score = score
            best_feature = feature

    if best_feature is None:
        return "insufficient live feature signal"
    return FRIENDLY_SIGNALS.get(best_feature, best_feature)


def _confidence(prob: float, elapsed_fraction: float) -> str:
    uncertainty = 1.0 - min(1.0, abs(prob - 0.5) * 2.0)
    if not math.isfinite(elapsed_fraction):
        return "low" if uncertainty > 0.55 else "medium"
    if elapsed_fraction < 0.20:
        return "medium" if uncertainty < 0.25 else "low"
    if elapsed_fraction >= 0.60 and uncertainty < 0.40:
        return "high"
    if uncertainty < 0.65:
        return "medium"
    return "low"


def predict_divergence(snapshot: dict) -> dict:
    model, feature_names, metadata = _load_artifacts()
    features = _compute_features(snapshot)
    row = pd.DataFrame([[features.get(name, math.nan) for name in feature_names]], columns=feature_names)
    prob = float(model.predict_proba(row)[0, 1])
    feeds_same = features.get("feeds_on_same_side")
    elapsed = features.get("elapsed_fraction", math.nan)
    return {
        "diverge_prob": prob,
        "confidence": _confidence(prob, elapsed),
        "dominant_signal": _dominant_signal(features, metadata),
        "feeds_on_same_side": bool(feeds_same) if math.isfinite(feeds_same) else None,
        "elapsed_fraction": elapsed if math.isfinite(elapsed) else None,
        "model_name": metadata.get("model_name"),
    }


def should_trade(snapshot: dict, min_arb_return: float = 0.02) -> dict:
    prediction = predict_divergence(snapshot)
    _model, _feature_names, metadata = _load_artifacts()
    threshold = float(metadata.get("recommended_diverge_prob_threshold", 0.05))

    kalshi_yes_ask = _to_float(snapshot.get("kalshi_yes_ask"))
    kalshi_no_ask = _to_float(snapshot.get("kalshi_no_ask"))
    poly_yes_ask = _to_float(snapshot.get("polymarket_yes_ask"))
    poly_no_ask = _to_float(snapshot.get("polymarket_no_ask"))

    k_yes_p_no_raw_cost = kalshi_yes_ask + poly_no_ask
    k_yes_p_no_fee = _kalshi_fee(kalshi_yes_ask) + _polymarket_fee(poly_no_ask)
    k_yes_p_no_all_in = k_yes_p_no_raw_cost + k_yes_p_no_fee
    k_yes_p_no_edge = 1.0 - k_yes_p_no_all_in if math.isfinite(k_yes_p_no_all_in) else math.nan

    k_no_p_yes_raw_cost = kalshi_no_ask + poly_yes_ask
    k_no_p_yes_fee = _kalshi_fee(kalshi_no_ask) + _polymarket_fee(poly_yes_ask)
    k_no_p_yes_all_in = k_no_p_yes_raw_cost + k_no_p_yes_fee
    k_no_p_yes_edge = 1.0 - k_no_p_yes_all_in if math.isfinite(k_no_p_yes_all_in) else math.nan

    candidates = [
        ("KALSHI_YES_POLYMARKET_NO", k_yes_p_no_edge, k_yes_p_no_raw_cost, k_yes_p_no_fee, k_yes_p_no_all_in),
        ("KALSHI_NO_POLYMARKET_YES", k_no_p_yes_edge, k_no_p_yes_raw_cost, k_no_p_yes_fee, k_no_p_yes_all_in),
    ]
    direction, arb_return, raw_entry_cost, total_fees, all_in_cost = max(
        candidates,
        key=lambda item: item[1] if math.isfinite(item[1]) else -math.inf,
    )
    raw_arb_available = math.isfinite(all_in_cost) and all_in_cost < 1.0
    meets_min_return = math.isfinite(arb_return) and arb_return >= min_arb_return
    divergence_ok = prediction["diverge_prob"] < threshold
    recommend = bool(raw_arb_available and meets_min_return and divergence_ok)

    if not raw_arb_available:
        reason = "no fee-adjusted buy-side arb has positive edge"
    elif not meets_min_return:
        reason = "fee-adjusted edge is below min_arb_return"
    elif not divergence_ok:
        reason = "divergence probability is above threshold"
    else:
        reason = "fee-adjusted edge and divergence risk are acceptable"

    return {
        "recommend_trade": recommend,
        "direction": direction if recommend else None,
        "reason": reason,
        "arb_available": bool(raw_arb_available),
        "arb_return": arb_return if math.isfinite(arb_return) else None,
        "raw_entry_cost": raw_entry_cost if math.isfinite(raw_entry_cost) else None,
        "total_fees": total_fees if math.isfinite(total_fees) else None,
        "all_in_cost": all_in_cost if math.isfinite(all_in_cost) else None,
        "entry_cost": all_in_cost if math.isfinite(all_in_cost) else None,
        "min_arb_return": float(min_arb_return),
        "diverge_prob": prediction["diverge_prob"],
        "diverge_threshold": threshold,
        "confidence": prediction["confidence"],
        "dominant_signal": prediction["dominant_signal"],
        "feeds_on_same_side": prediction["feeds_on_same_side"],
    }
