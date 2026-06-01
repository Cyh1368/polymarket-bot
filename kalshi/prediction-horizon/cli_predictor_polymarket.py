#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cli_trader_v2 as trader


LOG_PATH = APP_DIR / "predictor_log.txt"
PREDICTIONS_CSV = APP_DIR / "predictions.csv"
DATA_DIR = APP_DIR / "data"
TRUTH_DIR = APP_DIR / "truth_tables"
HORIZONS = {
    "10m": 10 * 60,
    "5m": 5 * 60,
    "2m": 2 * 60,
}
MODEL_PREFIX = "polymarket"
DEFAULT_THRESHOLD = 0.5

PREDICTION_FIELDS = [
    "timestamp_utc",
    "contract_id",
    "close_time",
    "polymarket_ticker",
    "model",
    "horizon",
    "status",
    "prob_yes",
    "threshold",
    "predicted_label",
    "predicted_side",
    "selected_ask",
    "selected_ask_qty",
    "contracts",
    "dry_test",
    "order_status",
    "order_id",
    "order_error",
    "window_rows",
    "sampled_history_rows",
    "raw_history_rows",
    "asof_gap_seconds",
    "null_feature_count",
    "null_features_json",
]

TRUTH_FIELDS = [
    "timestamp_utc",
    "contract_id",
    "close_time",
    "polymarket_ticker",
    "model",
    "horizon",
    "status",
    "predicted_label",
    "predicted_side",
    "prob_yes",
    "actual_label",
    "actual_side",
    "correct",
    "polymarket_price",
    "polymarket_target",
    "polymarket_target_source",
    "selected_ask",
    "contracts",
    "dry_test",
    "order_status",
    "order_id",
]


def iso_utc(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append_log(message: str, *, prefix_timestamp: bool = True) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = f"{iso_utc()} | {message}" if prefix_timestamp else message
    with LOG_PATH.open("a", encoding="utf-8") as file_obj:
        file_obj.write(text.rstrip() + "\n")
    print(text, flush=True)


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return value


def finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def append_csv(path: Path, fieldnames: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fieldnames})


@dataclass
class PredictionModel:
    horizon: str
    seconds: int
    model_name: str
    feature_names: list[str]
    model: Any
    threshold: float = DEFAULT_THRESHOLD
    collection_started: bool = False
    evaluated: bool = False


@dataclass
class PredictionRecord:
    contract_id: str
    close_time: str
    polymarket_ticker: str
    model_name: str
    horizon: str
    status: str
    prob_yes: float | None = None
    predicted_label: int | None = None
    predicted_side: str = ""
    selected_ask: float | None = None
    selected_ask_qty: float | None = None
    contracts: int = 0
    dry_test: bool = True
    order_status: str = ""
    order_id: str = ""
    order_error: str = ""
    window_rows: float | None = None
    sampled_history_rows: float | None = None
    raw_history_rows: float | None = None
    asof_gap_seconds: float | None = None
    null_features: list[str] = field(default_factory=list)
    timestamp_utc: str = field(default_factory=iso_utc)
    outcome_recorded: bool = False

    def to_prediction_row(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc,
            "contract_id": self.contract_id,
            "close_time": self.close_time,
            "polymarket_ticker": self.polymarket_ticker,
            "model": self.model_name,
            "horizon": self.horizon,
            "status": self.status,
            "prob_yes": self.prob_yes if self.prob_yes is not None else "",
            "threshold": DEFAULT_THRESHOLD,
            "predicted_label": self.predicted_label if self.predicted_label is not None else "",
            "predicted_side": self.predicted_side,
            "selected_ask": self.selected_ask if self.selected_ask is not None else "",
            "selected_ask_qty": self.selected_ask_qty if self.selected_ask_qty is not None else "",
            "contracts": self.contracts,
            "dry_test": int(self.dry_test),
            "order_status": self.order_status,
            "order_id": self.order_id,
            "order_error": self.order_error,
            "window_rows": self.window_rows if self.window_rows is not None else "",
            "sampled_history_rows": self.sampled_history_rows if self.sampled_history_rows is not None else "",
            "raw_history_rows": self.raw_history_rows if self.raw_history_rows is not None else "",
            "asof_gap_seconds": self.asof_gap_seconds if self.asof_gap_seconds is not None else "",
            "null_feature_count": len(self.null_features),
            "null_features_json": json.dumps(self.null_features, separators=(",", ":")),
        }


@dataclass
class ContractRuntime:
    ticker: str
    close_time: str
    polymarket_ticker: str
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1200))
    predictions: dict[str, PredictionRecord] = field(default_factory=dict)
    tradable: bool = False
    latch_horizon: str = ""
    latch_action: str = ""
    last_sample_bucket: int | None = None
    last_status_log_at: float = 0.0
    last_csv_save_at: float = 0.0
    outcome_logged: bool = False

    def last_model_decision(self) -> dict[str, Any]:
        ordered = [self.predictions[name] for name in HORIZONS if name in self.predictions]
        if not ordered:
            return {}
        last = ordered[-1]
        return {
            "horizon": last.horizon,
            "diverge_prob": "",
            "threshold": "",
        }


def load_models(model_dir: Path) -> dict[str, PredictionModel]:
    loaded: dict[str, PredictionModel] = {}
    for horizon, seconds in HORIZONS.items():
        stem = f"{MODEL_PREFIX}_{horizon}_horizon_prediction"
        model_path = model_dir / f"{stem}_model.pkl"
        feature_path = model_dir / f"{stem}_feature_list.json"
        metadata_path = model_dir / f"{stem}_metadata.json"
        if not model_path.exists() or not feature_path.exists():
            raise RuntimeError(f"Missing model artifacts for {horizon}: {model_path} / {feature_path}")
        threshold = DEFAULT_THRESHOLD
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text())
            threshold = float(metadata.get("decision_threshold", DEFAULT_THRESHOLD))
        loaded[horizon] = PredictionModel(
            horizon=horizon,
            seconds=seconds,
            model_name=stem,
            feature_names=json.loads(feature_path.read_text()),
            model=joblib.load(model_path),
            threshold=threshold,
        )
    return loaded


def csv_path_for_contract(ticker: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in ticker)
    return DATA_DIR / f"cli_predictor_polymarket_{safe}.csv"


def build_runtime_row(
    runtime: ContractRuntime,
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    source_snapshot: dict[str, Any],
    contracts: int,
) -> dict[str, Any]:
    return trader.build_csv_row(
        kalshi_snapshot,
        polymarket_snapshot,
        source_snapshot,
        runtime,  # ContractRuntime has the fields build_csv_row reads.
        0.0,
        contracts,
        None,
    )


def model_feature_debug_message(
    runtime: ContractRuntime,
    model: PredictionModel,
    feature_row: dict[str, Any],
    status: str,
    null_features: list[str],
) -> str:
    features = {name: json_safe(feature_row.get(name, math.nan)) for name in model.feature_names}
    payload = {
        "ticker": runtime.ticker,
        "polymarket_ticker": runtime.polymarket_ticker,
        "horizon": model.horizon,
        "model": model.model_name,
        "status": status,
        "threshold": model.threshold,
        "sampling": {
            "mode": feature_row.get("model_sampling_mode", "in_memory_previous_tick_2s_grid"),
            "sample_interval_seconds": json_safe(feature_row.get("model_sample_interval_seconds")),
            "expected_window_rows": json_safe(feature_row.get("model_expected_window_rows")),
            "warmup_sample_rows": json_safe(feature_row.get("model_warmup_sample_rows")),
            "raw_history_rows": json_safe(feature_row.get("model_raw_history_rows")),
            "sampled_history_rows": json_safe(feature_row.get("model_sampled_history_rows")),
            "actual_window_rows": json_safe(feature_row.get("window_rows")),
            "window_actual_seconds": json_safe(feature_row.get("window_actual_seconds")),
            "asof_gap_seconds": json_safe(feature_row.get("asof_gap_seconds")),
        },
        "feature_count": len(model.feature_names),
        "null_feature_count": len(null_features),
        "null_features": null_features,
        "features": features,
    }
    return f"MODEL_FEATURES {model.horizon} {runtime.ticker} | {json.dumps(payload, separators=(',', ':'), sort_keys=False)}"


def selected_side_snapshot(polymarket_snapshot: dict[str, Any], predicted_label: int) -> tuple[str, float | None, float | None]:
    if predicted_label == 1:
        return (
            "YES",
            finite_float(polymarket_snapshot.get("yes_ask")),
            finite_float(polymarket_snapshot.get("best_yes_ask_qty")),
        )
    return (
        "NO",
        finite_float(polymarket_snapshot.get("no_ask")),
        finite_float(polymarket_snapshot.get("best_no_ask_qty")),
    )


async def place_prediction_order(
    polymarket_market: dict[str, Any],
    side: str,
    price: float | None,
    available_qty: float | None,
    contracts: int,
    dry_test: bool,
    order_type: str,
) -> tuple[str, str, str]:
    if price is None or not 0.0 < price < 1.0:
        return "skip", "", f"invalid {side} ask {price}"
    if available_qty is not None and available_qty < contracts:
        return "skip", "", f"{side} ask liquidity {available_qty:g} < requested {contracts:g}"
    notional = price * contracts
    if notional < trader.POLYMARKET_MIN_ORDER_NOTIONAL:
        return "skip", "", (
            f"notional {trader.fmt_money(notional)} < "
            f"{trader.fmt_money(trader.POLYMARKET_MIN_ORDER_NOTIONAL)} minimum"
        )
    if dry_test:
        return "dry_test", "", f"would buy {contracts:g} {side} at {trader.fmt_cents(price)}"
    try:
        response = await asyncio.to_thread(
            trader.polymarket_post_order,
            polymarket_market,
            side,
            price,
            contracts,
            None,
            order_type,
        )
    except Exception as exc:
        return "error", "", f"{type(exc).__name__}: {exc}"
    order_id = trader.response_order_id(response)
    filled, fill_price, filled_size = trader.polymarket_fill_summary(response, price, contracts)
    if not filled:
        return "unfilled", order_id, f"posted but not filled: {response}"
    return "filled", order_id, f"filled {filled_size:g} at {trader.fmt_cents(fill_price)}"


async def evaluate_model(
    runtime: ContractRuntime,
    model: PredictionModel,
    polymarket_market: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    args: argparse.Namespace,
) -> PredictionRecord:
    feature_row, status = trader.aggregate_horizon_features(runtime.history, model.horizon, model.seconds)
    null_features: list[str] = []
    for feature in model.feature_names:
        value = feature_row.get(feature, math.nan)
        number = finite_float(value)
        if number is None:
            null_features.append(feature)
        feature_row.setdefault(feature, math.nan)

    if args.debug_model_features:
        append_log(model_feature_debug_message(runtime, model, feature_row, status, null_features))

    base = {
        "contract_id": runtime.ticker,
        "close_time": runtime.close_time,
        "polymarket_ticker": runtime.polymarket_ticker,
        "model_name": model.model_name,
        "horizon": model.horizon,
        "status": status,
        "contracts": args.contracts,
        "dry_test": args.dry_test,
        "window_rows": finite_float(feature_row.get("window_rows")),
        "sampled_history_rows": finite_float(feature_row.get("model_sampled_history_rows")),
        "raw_history_rows": finite_float(feature_row.get("model_raw_history_rows")),
        "asof_gap_seconds": finite_float(feature_row.get("asof_gap_seconds")),
        "null_features": null_features,
    }
    if status != "ok":
        record = PredictionRecord(**base)
        record.order_status = "skip"
        record.order_error = status
        return record

    if null_features and not args.allow_imputed_features:
        record = PredictionRecord(**{**base, "status": "invalid_model_features"})
        record.order_status = "skip"
        record.order_error = f"{len(null_features)} null model features"
        return record

    x = pd.DataFrame([{feature: feature_row.get(feature, math.nan) for feature in model.feature_names}])
    prob_yes = float(model.model.predict_proba(x)[0, 1])
    predicted_label = int(prob_yes >= model.threshold)
    predicted_side, selected_ask, selected_qty = selected_side_snapshot(polymarket_snapshot, predicted_label)
    order_status, order_id, order_error = await place_prediction_order(
        polymarket_market,
        predicted_side,
        selected_ask,
        selected_qty,
        args.contracts,
        args.dry_test,
        args.order_type,
    )
    record = PredictionRecord(
        **base,
        prob_yes=prob_yes,
        predicted_label=predicted_label,
        predicted_side=predicted_side,
        selected_ask=selected_ask,
        selected_ask_qty=selected_qty,
        order_status=order_status,
        order_id=order_id,
        order_error=order_error,
    )
    return record


def prediction_line(record: PredictionRecord) -> str:
    prob = trader.fmt_price(record.prob_yes, 4)
    price = trader.fmt_cents(record.selected_ask)
    feature_note = (
        f" null_features={len(record.null_features)}"
        if record.null_features
        else " null_features=0"
    )
    return (
        f"PREDICT {record.horizon:<3} {record.contract_id} | "
        f"status={record.status} prob_yes={prob} pred={record.predicted_side or '--'} "
        f"ask={price} order={record.order_status}"
        f"{feature_note} {record.order_error}"
    ).rstrip()


def polymarket_target_for_truth(
    runtime: ContractRuntime,
    source_snapshot: dict[str, Any],
) -> tuple[float | None, str]:
    target = finite_float(source_snapshot.get("polymarket_target"))
    if target is not None:
        return target, "observed_source_snapshot"
    history = trader.history_dataframe(runtime.history)
    if "polymarket_btc_target" in history:
        observed = history["polymarket_btc_target"].dropna()
        if not observed.empty:
            return float(observed.iloc[-1]), "observed_history"
    if "polymarket_btc_price" in history:
        prices = history["polymarket_btc_price"].dropna()
        if not prices.empty:
            return float(prices.iloc[0]), "inferred_from_opening_rtds"
    return None, "missing"


def outcome_from_source(
    runtime: ContractRuntime,
    source_snapshot: dict[str, Any],
) -> tuple[int | None, str, float | None, float | None, str]:
    price = finite_float(source_snapshot.get("polymarket_price"))
    target, target_source = polymarket_target_for_truth(runtime, source_snapshot)
    if price is None or target is None:
        return None, "MISSING", price, target, target_source
    label = int(price > target)
    return label, "YES" if label else "NO", price, target, target_source


def truth_path(model_name: str) -> Path:
    return TRUTH_DIR / f"{model_name}_truth_rows.csv"


def load_truth_rows(model_name: str) -> pd.DataFrame:
    path = truth_path(model_name)
    if not path.exists():
        return pd.DataFrame(columns=TRUTH_FIELDS)
    return pd.read_csv(path)


def truth_summary(model_name: str) -> dict[str, Any]:
    rows = load_truth_rows(model_name)
    if rows.empty:
        summary = {"model": model_name, "total": 0, "tp": 0, "tn": 0, "fp": 0, "fn": 0, "no_prediction": 0, "accuracy": math.nan}
    else:
        valid = rows[rows["correct"].isin([0, 1, "0", "1", True, False])].copy()
        valid["predicted_label"] = pd.to_numeric(valid["predicted_label"], errors="coerce")
        valid["actual_label"] = pd.to_numeric(valid["actual_label"], errors="coerce")
        valid = valid.dropna(subset=["predicted_label", "actual_label"])
        pred = valid["predicted_label"].astype(int)
        actual = valid["actual_label"].astype(int)
        tp = int(((pred == 1) & (actual == 1)).sum())
        tn = int(((pred == 0) & (actual == 0)).sum())
        fp = int(((pred == 1) & (actual == 0)).sum())
        fn = int(((pred == 0) & (actual == 1)).sum())
        total = tp + tn + fp + fn
        summary = {
            "model": model_name,
            "total": total,
            "tp": tp,
            "tn": tn,
            "fp": fp,
            "fn": fn,
            "no_prediction": int(len(rows) - total),
            "accuracy": (tp + tn) / total if total else math.nan,
        }
    (TRUTH_DIR / f"{model_name}_truth_table.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def truth_table_line(summary: dict[str, Any], latest: str) -> str:
    acc = summary.get("accuracy")
    acc_text = "--" if acc is None or (isinstance(acc, float) and not math.isfinite(acc)) else f"{acc:.4f}"
    return (
        f"TRUTH_TABLE {summary['model']} | "
        f"TP={summary['tp']} TN={summary['tn']} FP={summary['fp']} FN={summary['fn']} "
        f"no_pred={summary['no_prediction']} acc={acc_text} latest={latest}"
    )


def record_outcome(runtime: ContractRuntime, source_snapshot: dict[str, Any], models: dict[str, PredictionModel]) -> None:
    actual_label, actual_side, price, target, target_source = outcome_from_source(runtime, source_snapshot)
    append_log(
        f"OUTCOME {runtime.ticker} | P price={trader.fmt_price(price, 2)} "
        f"target={trader.fmt_price(target, 2)} target_source={target_source} actual={actual_side}",
        prefix_timestamp=False,
    )
    for horizon, model in models.items():
        record = runtime.predictions.get(horizon)
        latest = "no_prediction"
        if record is not None and not record.outcome_recorded:
            correct: int | str = ""
            if actual_label is not None and record.predicted_label is not None:
                correct = int(record.predicted_label == actual_label)
                latest = "correct" if correct else "wrong"
            row = {
                "timestamp_utc": iso_utc(),
                "contract_id": runtime.ticker,
                "close_time": runtime.close_time,
                "polymarket_ticker": runtime.polymarket_ticker,
                "model": model.model_name,
                "horizon": horizon,
                "status": record.status,
                "predicted_label": record.predicted_label if record.predicted_label is not None else "",
                "predicted_side": record.predicted_side,
                "prob_yes": record.prob_yes if record.prob_yes is not None else "",
                "actual_label": actual_label if actual_label is not None else "",
                "actual_side": actual_side,
                "correct": correct,
                "polymarket_price": price if price is not None else "",
                "polymarket_target": target if target is not None else "",
                "polymarket_target_source": target_source,
                "selected_ask": record.selected_ask if record.selected_ask is not None else "",
                "contracts": record.contracts,
                "dry_test": int(record.dry_test),
                "order_status": record.order_status,
                "order_id": record.order_id,
            }
            append_csv(truth_path(model.model_name), TRUTH_FIELDS, row)
            record.outcome_recorded = True
            append_log(
                f"EVAL {horizon:<3} {runtime.ticker} | pred={record.predicted_side or '--'} "
                f"actual={actual_side} correct={correct if correct != '' else '--'} "
                f"prob_yes={trader.fmt_price(record.prob_yes, 4)}",
                prefix_timestamp=False,
            )
        summary = truth_summary(model.model_name)
        append_log(truth_table_line(summary, latest), prefix_timestamp=False)
    runtime.outcome_logged = True


async def start_context_with_backoff(logger: Any, startup_timeout: float) -> trader.AsyncMarketContext:
    while True:
        context = trader.AsyncMarketContext(trader.fetch_market_state, logger=logger)
        try:
            await asyncio.wait_for(context.start(), timeout=startup_timeout)
            return context
        except TimeoutError as exc:
            await context.stop()
            append_log(
                f"MARKET REFRESH WAIT startup: timeout after {startup_timeout:g}s; retrying in "
                f"{trader.ACTIVE_MARKET_REFRESH_MIN_INTERVAL_SECONDS:g}s"
            )
            await asyncio.sleep(trader.ACTIVE_MARKET_REFRESH_MIN_INTERVAL_SECONDS)
        except Exception as exc:
            await context.stop()
            if not (
                trader.is_active_market_rate_limit_error(exc)
                or "No open market found" in str(exc)
                or "No matching open Polymarket market found" in str(exc)
            ):
                raise
            append_log(
                f"MARKET REFRESH WAIT startup: {type(exc).__name__}: {exc}; "
                f"retrying in {trader.ACTIVE_MARKET_REFRESH_MIN_INTERVAL_SECONDS:g}s"
            )
            await asyncio.sleep(trader.ACTIVE_MARKET_REFRESH_MIN_INTERVAL_SECONDS)


async def fetch_state_polling(fetch_timeout: float) -> trader.MarketState | None:
    try:
        return await asyncio.to_thread(trader.fetch_market_state)
    except Exception as exc:
        append_log(f"POLL FETCH ERROR {type(exc).__name__}: {exc}")
    return None


def sample_runtime(
    runtime: ContractRuntime,
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    source_snapshot: dict[str, Any],
    contracts: int,
    csv_save_interval: float,
) -> None:
    sample_bucket = int(datetime.now(timezone.utc).timestamp() // trader.MODEL_SAMPLE_INTERVAL_SECONDS)
    if runtime.last_sample_bucket == sample_bucket:
        return
    row = build_runtime_row(runtime, kalshi_snapshot, polymarket_snapshot, source_snapshot, contracts)
    runtime.history.append(row)
    runtime.last_sample_bucket = sample_bucket
    now = time.monotonic()
    if now - runtime.last_csv_save_at >= csv_save_interval:
        runtime.last_csv_save_at = now
        trader.append_csv_row(csv_path_for_contract(runtime.ticker), row)


def status_line(runtime: ContractRuntime, source_snapshot: dict[str, Any], remaining: float | None) -> str:
    remaining_text = f"{remaining:.1f}" if remaining is not None else "--"
    decisions = []
    for horizon in HORIZONS:
        record = runtime.predictions.get(horizon)
        if record is None:
            decisions.append(f"{horizon}=--")
        else:
            decisions.append(
                f"{horizon}={record.predicted_side or record.status}:{trader.fmt_price(record.prob_yes, 4)}"
            )
    target, target_source = polymarket_target_for_truth(runtime, source_snapshot)
    return (
        f"STATUS T={remaining_text}s | "
        f"RTDS {trader.fmt_price(source_snapshot.get('polymarket_price'), 2)} "
        f"{trader.fmt_price_delta(source_snapshot.get('polymarket_price'), target)} "
        f"target_source={target_source} | "
        + " ".join(decisions)
    )


async def run() -> None:
    args = parse_args()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    models = load_models(args.model_dir)
    append_log(
        f"START cli_predictor_polymarket contracts={args.contracts} dry_test={args.dry_test} "
        f"debug_model_features={args.debug_model_features} allow_imputed_features={args.allow_imputed_features} "
        f"models={','.join(model.model_name for model in models.values())}"
    )

    context = None if args.poll_only else await start_context_with_backoff(lambda line: append_log(line), args.startup_timeout)
    runtime: ContractRuntime | None = None
    completed_contracts = 0
    started_at = time.monotonic()

    try:
        while True:
            if args.max_seconds and time.monotonic() - started_at >= args.max_seconds:
                append_log(f"STOP max_seconds={args.max_seconds:g} reached")
                return
            if args.poll_only:
                state = await fetch_state_polling(args.fetch_timeout)
                if state is None:
                    await asyncio.sleep(args.poll_interval)
                    continue
                kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot = state
                await asyncio.sleep(max(0.1, args.poll_interval))
            else:
                if context is None:
                    context = await start_context_with_backoff(lambda line: append_log(line), args.startup_timeout)
                kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot = await context.wait_for_update(timeout=0.5)
            ticker = str(kalshi_snapshot.get("ticker") or kalshi_market.get("ticker") or "")
            close_time = str(kalshi_snapshot.get("close_time") or kalshi_market.get("close_time") or "")
            polymarket_ticker = str(polymarket_snapshot.get("ticker") or polymarket_market.get("slug") or "")
            remaining = trader.seconds_to_expiry(kalshi_snapshot)

            if runtime is None or runtime.ticker != ticker:
                runtime = ContractRuntime(ticker=ticker, close_time=close_time, polymarket_ticker=polymarket_ticker)
                for model in models.values():
                    model.collection_started = False
                    model.evaluated = False
                append_log("", prefix_timestamp=False)
                append_log(
                    f"CONTRACT {ticker} | close {close_time} | Polymarket {polymarket_ticker} | "
                    f"P target {trader.fmt_price(source_snapshot.get('polymarket_target'), 2)}",
                    prefix_timestamp=False,
                )

            sample_runtime(
                runtime,
                kalshi_snapshot,
                polymarket_snapshot,
                source_snapshot,
                args.contracts,
                args.csv_save_interval,
            )

            if remaining is not None:
                for model in models.values():
                    if not model.collection_started and remaining <= model.seconds + trader.WINDOW_SECONDS:
                        model.collection_started = True
                        append_log(
                            f"COLLECT {model.horizon} window started for {ticker}; "
                            f"evaluation at T={model.seconds}s"
                        )
                    if model.evaluated or remaining > model.seconds:
                        continue
                    record = await evaluate_model(runtime, model, polymarket_market, polymarket_snapshot, args)
                    runtime.predictions[model.horizon] = record
                    model.evaluated = True
                    append_csv(PREDICTIONS_CSV, PREDICTION_FIELDS, record.to_prediction_row())
                    append_log(prediction_line(record), prefix_timestamp=False)

            now = time.monotonic()
            if now - runtime.last_status_log_at >= args.status_interval:
                append_log(status_line(runtime, source_snapshot, remaining), prefix_timestamp=False)
                runtime.last_status_log_at = now

            if remaining is not None and remaining <= args.outcome_delay_seconds and not runtime.outcome_logged:
                record_outcome(runtime, source_snapshot, models)
                completed_contracts += 1
                if args.max_contracts and completed_contracts >= args.max_contracts:
                    append_log(f"STOP max_contracts={args.max_contracts:g} reached")
                    return
                trader.KALSHI_MARKET_CACHE.pop(trader.SERIES_TICKER, None)
                if not args.poll_only:
                    old_context = context
                    context = await start_context_with_backoff(lambda line: append_log(line), args.startup_timeout)
                    if old_context is not None:
                        await old_context.stop()
                runtime = None
    finally:
        if context is not None:
            await context.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live/dry Polymarket outcome predictor for BTC 15m horizon models.")
    parser.add_argument("--contracts", type=int, default=2, help="Polymarket contracts to buy per model prediction. Default: 2.")
    parser.add_argument("--model-dir", type=Path, default=APP_DIR, help=f"Directory containing copied model artifacts. Default: {APP_DIR}.")
    parser.add_argument("--dry-test", "--dry-run", action="store_true", dest="dry_test", help="Record would-be orders without placing real Polymarket orders.")
    parser.add_argument("--debug-model-features", action="store_true", help="Log the full feature payload sent to each model.")
    parser.add_argument("--allow-imputed-features", action="store_true", help="Allow model prediction when live features contain NaN; the model imputer will fill them.")
    parser.add_argument("--order-type", default="FOK", help="Polymarket order type for live orders. Default: FOK.")
    parser.add_argument("--poll-only", action="store_true", help="Use HTTP polling instead of websocket market context.")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between HTTP polls in --poll-only mode. Default: 2.")
    parser.add_argument("--fetch-timeout", type=float, default=20.0, help="Seconds before one poll fetch times out. Default: 20.")
    parser.add_argument("--startup-timeout", type=float, default=25.0, help="Seconds before websocket startup is retried. Default: 25.")
    parser.add_argument("--csv-save-interval", type=float, default=2.0, help="Seconds between raw contract CSV row writes. Default: 2.")
    parser.add_argument("--status-interval", type=float, default=10.0, help="Seconds between status log lines. Default: 10.")
    parser.add_argument("--outcome-delay-seconds", type=float, default=-2.0, help="Evaluate outcome this many seconds relative to close. Default: -2.")
    parser.add_argument("--max-contracts", type=int, default=0, help="Stop after this many contract outcomes. 0 means run forever.")
    parser.add_argument("--max-seconds", type=float, default=0.0, help="Stop after this many wall-clock seconds. 0 means no limit.")
    return parser.parse_args()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        append_log("STOP cli_predictor_polymarket interrupted")
    except Exception as exc:
        append_log(f"FATAL {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        raise


if __name__ == "__main__":
    main()
