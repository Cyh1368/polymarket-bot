#!/usr/bin/env python3
import argparse
import asyncio
import concurrent.futures
import json
import os
import time
import traceback
import uuid
from collections import deque
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cli_server as cli
import kalshi_btc15_server as btc
from logic.exits import LimitWalkResult, LimitWalker
from market_interface import AsyncMarketContext


class FatalTradeError(RuntimeError):
    pass


class PartialEntryError(RuntimeError):
    def __init__(self, message: str, position: dict[str, Any]) -> None:
        super().__init__(message)
        self.position = position


class KalshiOrderStateUnknown(RuntimeError):
    def __init__(self, message: str, order: dict[str, Any]) -> None:
        super().__init__(message)
        self.order = order


class TradeResult(str):
    def __new__(
        cls,
        text: str,
        entry_cost: float | None = None,
        kalshi_fill_price: float | None = None,
        polymarket_fill_price: float | None = None,
        contracts: int | None = None,
    ) -> "TradeResult":
        obj = str.__new__(cls, text)
        obj.entry_cost = entry_cost
        obj.kalshi_fill_price = kalshi_fill_price
        obj.polymarket_fill_price = polymarket_fill_price
        obj.contracts = contracts
        return obj


POLYMARKET_MARKET_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
KALSHI_MARKET_CACHE: dict[str, dict[str, Any]] = {}
SETTLEMENT_PAYOUT_AFTER_FEES = 0.98
CONTRACT_WINDOW_SECONDS = 15 * 60
CONTRACT_BOUNDARY_NO_TRADE_SECONDS = 60.0
EXIT_LIMIT_DEVIATION = 0.01
POLYMARKET_MIN_ORDER_NOTIONAL = 1.0
KALSHI_ORDER_VERIFY_ATTEMPTS = 5
KALSHI_ORDER_VERIFY_DELAY_SECONDS = 0.25
POLYMARKET_SELL_BALANCE_ATTEMPTS = 8
POLYMARKET_SELL_BALANCE_DELAY_SECONDS = 0.75
CONTRACT_FAILURE_COOLDOWN_SECONDS = 60.0
EDGE_RECHECK_COOLDOWN_SECONDS = 30.0
EXECUTION_FAILURE_COOLDOWN_SECONDS = 5 * 60.0
PROFIT_CAPTURE_MIN_EDGE = 0.07
EXIT_CUSHION = 0.03
ONE_WINNER_NEGATIVE_EXIT_DISTANCE = 3.0
ONE_WINNER_NEGATIVE_EXIT_SECONDS = 45.0
TWO_WINNER_PROFIT_EXIT_SECONDS = 90.0
TWO_WINNER_PROFIT_EXIT_DISTANCE = 20.0
CHASE_INTERVAL = 2.0
CHASE_MAX_STEPS = 6
WEBSOCKET_REPORT_INTERVAL = 2.0
WEBSOCKET_STALE_SECONDS = 5.0


def http_json(
    method: str,
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
    auth: bool = False,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "User-Agent": "btc15m-cli-trader/1.0"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if auth:
        headers.update(btc.auth_headers(method, path))
    req = Request(f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code} {detail}") from exc


def as_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def cents(value: float) -> int:
    return max(1, min(99, int(round(value * 100))))


def dollars(value: float) -> str:
    return f"{value:.4f}"


def finite_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def format_balance_value(value: Any) -> str:
    number = as_float(value)
    if number >= 100:
        number /= 100.0
    return cli.fmt_money(number)


def format_usdc_base_units(value: Any) -> str:
    return cli.fmt_money(as_float(value) / 1_000_000.0)


def kalshi_balance_dollars(value: Any, key: str) -> float:
    number = as_float(value)
    if "dollars" in key:
        return number
    if number >= 100:
        return number / 100.0
    return number


def kalshi_balance_amounts() -> tuple[float, float | None]:
    data = http_json("GET", btc.BASE_URL, "/portfolio/balance", auth=True)
    balance = data.get("balance") if isinstance(data, dict) else None
    if isinstance(balance, dict):
        cash_key = next(
            (
                key
                for key in (
                    "cash_balance_dollars",
                    "cash_balance",
                    "balance_dollars",
                    "balance",
                )
                if balance.get(key) not in (None, "")
            ),
            "balance",
        )
        cash = (
            balance.get("cash_balance_dollars")
            or balance.get("cash_balance")
            or balance.get("balance_dollars")
            or balance.get("balance")
        )
        available_key = next(
            (
                key
                for key in (
                    "available_balance_dollars",
                    "available_balance",
                    "cash_available_dollars",
                    "cash_available",
                )
                if balance.get(key) not in (None, "")
            ),
            "",
        )
        available = (
            balance.get("available_balance_dollars")
            or balance.get("available_balance")
            or balance.get("cash_available_dollars")
            or balance.get("cash_available")
        )
    else:
        cash_key = "balance"
        cash = data.get("balance") if isinstance(data, dict) else None
        available_key = "available_balance"
        available = data.get("available_balance") if isinstance(data, dict) else None
    cash_dollars = kalshi_balance_dollars(cash, cash_key)
    available_dollars = (
        kalshi_balance_dollars(available, available_key)
        if available not in (None, "")
        else None
    )
    return cash_dollars, available_dollars


def kalshi_balance_summary() -> str:
    cash, available = kalshi_balance_amounts()
    if available not in (None, ""):
        return f"Kalshi balance {cli.fmt_money(cash)} available {cli.fmt_money(available)}"
    return f"Kalshi balance {cli.fmt_money(cash)}"


def polymarket_balance_amounts() -> tuple[float, float]:
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    client = polymarket_client_v2()
    data = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    if not isinstance(data, dict):
        raise RuntimeError(f"Polymarket balance response {data}")
    balance = data.get("balance") or data.get("usdc_balance") or data.get("collateral")
    allowances = data.get("allowances")
    if isinstance(allowances, dict) and allowances:
        allowance = max(as_float(value) for value in allowances.values())
    else:
        allowance = data.get("allowance") or data.get("usdc_allowance")
    return as_float(balance) / 1_000_000.0, as_float(allowance) / 1_000_000.0


def polymarket_conditional_balance_amounts(token_id: str) -> tuple[float, float]:
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    client = polymarket_client_v2()
    params_kwargs = {"asset_type": AssetType.CONDITIONAL, "token_id": token_id}
    try:
        params = BalanceAllowanceParams(**params_kwargs)
    except TypeError:
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, asset_id=token_id)
    data = client.get_balance_allowance(params)
    if not isinstance(data, dict):
        raise RuntimeError(f"Polymarket conditional balance response {data}")
    balance = (
        data.get("balance")
        or data.get("conditional_balance")
        or data.get("token_balance")
        or data.get("asset_balance")
    )
    allowances = data.get("allowances")
    if isinstance(allowances, dict) and allowances:
        allowance = max(as_float(value) for value in allowances.values())
    else:
        allowance = data.get("allowance") or data.get("conditional_allowance") or data.get("token_allowance")
    allowance_amount = float("inf") if allowance in (None, "") else as_float(allowance) / 1_000_000.0
    return as_float(balance) / 1_000_000.0, allowance_amount


def polymarket_balance_summary() -> str:
    balance, allowance = polymarket_balance_amounts()
    return (
        f"Polymarket USDC balance {cli.fmt_money(balance)} "
        f"allowance {cli.fmt_money(allowance)}"
    )


def combined_balance_line() -> str:
    parts = []
    total = 0.0
    try:
        kalshi_cash, kalshi_available = kalshi_balance_amounts()
        total += kalshi_cash
        available_text = (
            f" available {cli.fmt_money(kalshi_available)}"
            if kalshi_available is not None
            else ""
        )
        parts.append(f"Kalshi {cli.fmt_money(kalshi_cash)}{available_text}")
    except Exception as exc:
        parts.append(f"Kalshi ERROR {type(exc).__name__}: {exc}")
    try:
        polymarket_cash, _polymarket_allowance = polymarket_balance_amounts()
        total += polymarket_cash
        parts.append(f"Polymarket {cli.fmt_money(polymarket_cash)}")
    except Exception as exc:
        parts.append(f"Polymarket ERROR {type(exc).__name__}: {exc}")
    return f"BALANCE {' | '.join(parts)} | {cli.yellow_text(f'total {cli.fmt_money(total)}')}"


def print_startup_banner() -> None:
    timestamp = btc.iso_utc().replace("T", " ")[:19]
    for line in (
        "",
        "==============================================================",
        f"========== CLI TRADER INITIATED {timestamp} ==========",
        "==============================================================",
        "",
    ):
        cli.print_line(line, force_concise=True)


def print_startup_balances() -> None:
    try:
        cli.print_line(kalshi_balance_summary())
    except Exception as exc:
        cli.print_line(f"Kalshi balance ERROR {type(exc).__name__}: {exc}")
    try:
        cli.print_line(polymarket_balance_summary())
    except Exception as exc:
        cli.print_line(f"Polymarket balance ERROR {type(exc).__name__}: {exc}")


def fill_count(order: dict[str, Any]) -> float:
    return as_float(order.get("fill_count_fp") or order.get("fill_count") or order.get("filled_count"))


def filled_price(order: dict[str, Any], side: str, action: str = "buy") -> float:
    filled = fill_count(order)
    cost = as_float(
        order.get("taker_fill_cost_dollars")
        or order.get("maker_fill_cost_dollars")
        or order.get("cost_dollars")
    )
    reference = finite_float(order.get("limit_price") or order.get("best_bid") or order.get("best_ask"))
    if filled and cost:
        unit_cost = cost / filled
        if action == "sell":
            complement = round(1.0 - unit_cost, 10)
            if reference is not None:
                return min((unit_cost, complement), key=lambda price: abs(price - reference))
            return complement
        return unit_cost
    return as_float(order.get(f"{side}_price_dollars")) or as_float(order.get(f"{side}_price")) / 100.0


def level_price(level: Any) -> float | None:
    if not isinstance(level, (list, tuple)) or not level:
        return None
    return btc.normalize_price(level[0])


def level_quantity(level: Any) -> float:
    if not isinstance(level, (list, tuple)) or len(level) < 2:
        return 0.0
    return as_float(level[1])


def kalshi_liquidity_plan_for_buy(ticker: str, side: str, price: float) -> dict[str, Any]:
    orderbook = btc.kalshi_get(f"/markets/{ticker}/orderbook", {"depth": btc.ORDERBOOK_DEPTH})
    yes_levels, no_levels = btc.orderbook_levels(orderbook)
    opposite_levels = no_levels if side == "yes" else yes_levels
    min_opposite_bid = round(1.0 - price, 10)
    priced_levels = [
        (opposite_bid, level_quantity(level))
        for level in opposite_levels
        if (opposite_bid := level_price(level)) is not None
    ]
    priced_levels.sort(key=lambda item: item[0], reverse=True)

    cumulative = 0.0
    levels = []
    for opposite_bid, quantity in priced_levels:
        executable = opposite_bid >= min_opposite_bid
        if executable:
            cumulative += quantity
        levels.append(
            {
                "opposite_bid": opposite_bid,
                "buy_price": round(1.0 - opposite_bid, 10),
                "quantity": quantity,
                "cumulative": cumulative if executable else None,
                "executable": executable,
            }
        )

    return {
        "side": side,
        "limit_price": price,
        "min_opposite_bid": min_opposite_bid,
        "liquidity": cumulative,
        "levels": levels,
        "source": "http",
    }


def kalshi_resting_volume_for_buy(ticker: str, side: str, price: float) -> float:
    return as_float(kalshi_liquidity_plan_for_buy(ticker, side, price).get("liquidity"))


def kalshi_post_order(
    ticker: str,
    side: str,
    price: float,
    contracts: int,
    client_order_id: str,
    action: str = "buy",
    time_in_force: str = "fill_or_kill",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ticker": ticker,
        "side": side,
        "action": action,
        "client_order_id": client_order_id,
        "count": contracts,
        "time_in_force": time_in_force,
    }
    payload[f"{side}_price"] = cents(price)
    response = http_json("POST", btc.BASE_URL, "/portfolio/orders", payload=payload, auth=True)
    order = response.get("order") or response
    order_id = order.get("order_id")
    if order_id:
        last_error: Exception | None = None
        for attempt in range(KALSHI_ORDER_VERIFY_ATTEMPTS):
            try:
                verified = http_json("GET", btc.BASE_URL, f"/portfolio/orders/{order_id}", auth=True)
                return verified.get("order") or verified
            except Exception as exc:
                last_error = exc
                if "HTTP 404" not in str(exc) or attempt == KALSHI_ORDER_VERIFY_ATTEMPTS - 1:
                    break
                time.sleep(KALSHI_ORDER_VERIFY_DELAY_SECONDS * (attempt + 1))
        order["verification_error"] = f"{type(last_error).__name__}: {last_error}"
        if fill_count(order) > 0:
            return order
        if last_error and "HTTP 404" in str(last_error):
            raise KalshiOrderStateUnknown(
                f"Kalshi order state unknown after POST; order_id={order_id}; "
                f"client_order_id={client_order_id}; verification failed: {last_error}",
                order,
            ) from last_error
        if last_error:
            raise last_error
    return order


def kalshi_cancel_order(order_id: str) -> dict[str, Any]:
    return http_json("DELETE", btc.BASE_URL, f"/portfolio/orders/{order_id}", auth=True)


def kalshi_get_order(order_id: str) -> dict[str, Any]:
    response = http_json("GET", btc.BASE_URL, f"/portfolio/orders/{order_id}", auth=True)
    return response.get("order") or response


def is_kalshi_fok_liquidity_error(exc: Exception) -> bool:
    text = str(exc)
    return (
        "fill_or_kill_insufficient_resting_volume" in text
        or "insufficient resting volume" in text.lower()
    )


def kalshi_current_bid(ticker: str, side: str) -> float | None:
    orderbook = btc.kalshi_get(f"/markets/{ticker}/orderbook", {"depth": btc.ORDERBOOK_DEPTH})
    yes_levels, no_levels = btc.orderbook_levels(orderbook)
    best_yes_bid, _best_yes_qty = btc.best_level(yes_levels)
    best_no_bid, _best_no_qty = btc.best_level(no_levels)
    return best_yes_bid if side == "yes" else best_no_bid


def kalshi_exit_plan(ticker: str, side: str, contracts: int) -> tuple[float | None, float, dict[str, Any]]:
    orderbook = btc.kalshi_get(f"/markets/{ticker}/orderbook", {"depth": btc.ORDERBOOK_DEPTH})
    yes_levels, no_levels = btc.orderbook_levels(orderbook)
    bid_levels = yes_levels if side == "yes" else no_levels
    priced_levels = [
        (price, level_quantity(level))
        for level in bid_levels
        if (price := level_price(level)) is not None
    ]
    priced_levels.sort(key=lambda item: item[0], reverse=True)
    cumulative = 0.0
    cumulative_value = 0.0
    levels = []
    selected_price: float | None = None
    for price, quantity in priced_levels:
        remaining = max(0.0, contracts - cumulative)
        used = min(quantity, remaining)
        cumulative += quantity
        cumulative_value += used * price
        selected = selected_price is None and cumulative >= contracts
        if selected:
            selected_price = price
        levels.append(
            {
                "price": price,
                "quantity": quantity,
                "used": used,
                "cumulative": cumulative,
                "selected": selected,
            }
        )
        if selected:
            break
    vwap_price = cumulative_value / contracts if selected_price is not None and contracts else None
    plan = {
        "side": side,
        "liquidity": cumulative,
        "levels": levels,
        "vwap_price": vwap_price,
        "worst_executable_price": selected_price,
    }
    if selected_price is not None:
        return selected_price, cumulative, plan
    for price, quantity in priced_levels[len(levels):]:
        cumulative += quantity
        levels.append(
            {
                "price": price,
                "quantity": quantity,
                "used": 0.0,
                "cumulative": cumulative,
                "selected": False,
            }
        )
    plan["liquidity"] = cumulative
    return None, cumulative, plan


def opposite_side(side: str) -> str:
    return "no" if side == "yes" else "yes"


def kalshi_current_ask(ticker: str, side: str) -> float | None:
    opposite_bid = kalshi_current_bid(ticker, opposite_side(side))
    if opposite_bid is None:
        return None
    return round(1.0 - opposite_bid, 10)


def exit_sell_limit(best_bid: float, deviation: float = EXIT_LIMIT_DEVIATION) -> float:
    return max(0.01, round(best_bid - deviation, 10))


def exit_buy_limit(best_ask: float, deviation: float = EXIT_LIMIT_DEVIATION) -> float:
    return min(0.99, round(best_ask + deviation, 10))


def kalshi_exit_position(
    ticker: str,
    side: str,
    contracts: int,
    deviation: float = EXIT_LIMIT_DEVIATION,
) -> dict[str, Any]:
    bid, liquidity, _plan = kalshi_exit_plan(ticker, side, contracts)
    if bid is None:
        sell_error = RuntimeError(
            f"Kalshi {side.upper()} exit liquidity {liquidity:g} < {contracts:g} for {ticker}"
        )
    else:
        sell_limit = exit_sell_limit(bid, deviation)
        try:
            exit_order = kalshi_post_order(
                ticker,
                side,
                sell_limit,
                contracts,
                f"btc15-exit-{uuid.uuid4().hex[:19]}",
                action="sell",
            )
            if fill_count(exit_order) > 0:
                exit_order["exit_method"] = "sell"
                exit_order["best_bid"] = bid
                exit_order["limit_price"] = sell_limit
                return exit_order
            sell_error = RuntimeError(f"Kalshi sell exit had no fill: {exit_order}")
        except Exception as exc:
            sell_error = exc

    hedge_side = opposite_side(side)
    hedge_ask = kalshi_current_ask(ticker, hedge_side)
    if hedge_ask is None:
        raise RuntimeError(
            f"Kalshi sell exit failed ({sell_error}); no {hedge_side.upper()} ask available to hedge"
        )
    hedge_limit = exit_buy_limit(hedge_ask, deviation)
    try:
        hedge_order = kalshi_post_order(
            ticker,
            hedge_side,
            hedge_limit,
            contracts,
            f"btc15-hedge-{uuid.uuid4().hex[:18]}",
            action="buy",
        )
    except Exception as hedge_exc:
        raise RuntimeError(
            f"Kalshi sell exit failed ({sell_error}); "
            f"opposite hedge {hedge_side.upper()} buy @ {cli.fmt_display_cents(hedge_ask)}c failed: "
            f"{type(hedge_exc).__name__}: {hedge_exc}"
        ) from hedge_exc
    if fill_count(hedge_order) <= 0:
        raise RuntimeError(
            f"Kalshi sell exit failed ({sell_error}); opposite hedge had no fill: {hedge_order}"
        )
    hedge_order["exit_method"] = f"buy_{hedge_side}"
    hedge_order["best_ask"] = hedge_ask
    hedge_order["limit_price"] = hedge_limit
    return hedge_order


def token_ids_by_contract(market: dict[str, Any]) -> dict[str, str]:
    token_ids = btc.parse_json_list(market.get("clobTokenIds"))
    outcomes = [str(outcome).lower() for outcome in btc.parse_json_list(market.get("outcomes"))]
    if len(token_ids) < 2:
        raise RuntimeError("Polymarket market has no CLOB token ids")
    up_index = outcomes.index("up") if "up" in outcomes else 0
    down_index = outcomes.index("down") if "down" in outcomes else 1
    return {"YES": str(token_ids[up_index]), "NO": str(token_ids[down_index])}


def polymarket_token_ref(market: dict[str, Any], contract: str) -> str:
    try:
        token_id = token_ids_by_contract(market)[contract]
    except Exception:
        token_id = "--"
    slug = market.get("slug") or market.get("conditionId") or market.get("id") or "--"
    return f"token {token_id} market {slug}"


def response_order_id(response: dict[str, Any] | None) -> str:
    if not isinstance(response, dict):
        return "--"
    verified = response.get("verified_order")
    candidates: list[Any] = [
        response.get("order_id"),
        response.get("id"),
        response.get("orderID"),
        response.get("orderId"),
    ]
    if isinstance(verified, dict):
        candidates.extend(
            [
                verified.get("order_id"),
                verified.get("id"),
                verified.get("orderID"),
                verified.get("orderId"),
            ]
        )
    for candidate in candidates:
        if candidate not in (None, ""):
            return str(candidate)
    return "--"


def response_status(response: dict[str, Any] | None) -> str:
    if not isinstance(response, dict):
        return "--"
    verified = response.get("verified_order")
    candidates: list[Any] = [
        response.get("status"),
        response.get("state"),
    ]
    if isinstance(verified, dict):
        candidates.extend([verified.get("status"), verified.get("state")])
    for candidate in candidates:
        if candidate not in (None, ""):
            return str(candidate)
    return "--"


def kalshi_order_ref(order: dict[str, Any] | None) -> str:
    if not isinstance(order, dict):
        return "order -- client -- status --"
    return (
        f"order {order.get('order_id') or '--'} "
        f"client {order.get('client_order_id') or '--'} "
        f"status {order.get('status') or order.get('state') or '--'}"
    )


def polymarket_order_ref(response: dict[str, Any] | None) -> str:
    return f"order {response_order_id(response)} status {response_status(response)}"


def polymarket_resting_volume_for_buy(
    market: dict[str, Any],
    contract: str,
    limit_price: float,
) -> float:
    token_id = token_ids_by_contract(market)[contract]
    orderbook = btc.clob_get("/book", {"token_id": token_id})
    _bid_levels, ask_levels = btc.polymarket_book_levels(orderbook)
    return sum(
        level_quantity(level)
        for level in ask_levels
        if (level_price(level) or 1.0) <= limit_price
    )


def polymarket_execution_plan(
    market: dict[str, Any],
    contract: str,
    contracts: int,
) -> tuple[float | None, float, dict[str, Any]]:
    token_id = token_ids_by_contract(market)[contract]
    orderbook = btc.clob_get("/book", {"token_id": token_id})
    _bid_levels, ask_levels = btc.polymarket_book_levels(orderbook)
    priced_levels = [
        (price, level_quantity(level))
        for level in ask_levels
        if (price := level_price(level)) is not None
    ]
    priced_levels.sort(key=lambda item: item[0])
    cumulative = 0.0
    cumulative_cost = 0.0
    levels = []
    selected_price: float | None = None
    for price, quantity in priced_levels:
        remaining = max(0.0, contracts - cumulative)
        used = min(quantity, remaining)
        cumulative += quantity
        cumulative_cost += used * price
        selected = selected_price is None and cumulative >= contracts
        if selected:
            selected_price = price
        levels.append(
            {
                "price": price,
                "quantity": quantity,
                "used": used,
                "cumulative": cumulative,
                "selected": selected,
            }
        )
        if selected:
            break
    vwap_price = cumulative_cost / contracts if selected_price is not None and contracts else None
    plan = {
        "contract": contract,
        "token_id": token_id,
        "liquidity": cumulative,
        "levels": levels,
        "vwap_price": vwap_price,
    }
    if selected_price is not None:
        return selected_price, cumulative, plan
    for price, quantity in priced_levels[len(levels):]:
        cumulative += quantity
        levels.append(
            {
                "price": price,
                "quantity": quantity,
                "used": 0.0,
                "cumulative": cumulative,
                "selected": False,
            }
        )
    plan["liquidity"] = cumulative
    return None, cumulative, plan


def polymarket_exit_plan(
    market: dict[str, Any],
    contract: str,
    contracts: int,
) -> tuple[float | None, float, dict[str, Any]]:
    token_id = token_ids_by_contract(market)[contract]
    orderbook = btc.clob_get("/book", {"token_id": token_id})
    bid_levels, _ask_levels = btc.polymarket_book_levels(orderbook)
    priced_levels = [
        (price, level_quantity(level))
        for level in bid_levels
        if (price := level_price(level)) is not None
    ]
    priced_levels.sort(key=lambda item: item[0], reverse=True)
    cumulative = 0.0
    cumulative_value = 0.0
    levels = []
    selected_price: float | None = None
    for price, quantity in priced_levels:
        remaining = max(0.0, contracts - cumulative)
        used = min(quantity, remaining)
        cumulative += quantity
        cumulative_value += used * price
        selected = selected_price is None and cumulative >= contracts
        if selected:
            selected_price = price
        levels.append(
            {
                "price": price,
                "quantity": quantity,
                "used": used,
                "cumulative": cumulative,
                "selected": selected,
            }
        )
        if selected:
            break
    vwap_price = cumulative_value / contracts if selected_price is not None and contracts else None
    plan = {
        "contract": contract,
        "token_id": token_id,
        "liquidity": cumulative,
        "levels": levels,
        "vwap_price": vwap_price,
        "worst_executable_price": selected_price,
    }
    if selected_price is not None:
        return selected_price, cumulative, plan
    for price, quantity in priced_levels[len(levels):]:
        cumulative += quantity
        levels.append(
            {
                "price": price,
                "quantity": quantity,
                "used": 0.0,
                "cumulative": cumulative,
                "selected": False,
            }
        )
    plan["liquidity"] = cumulative
    return None, cumulative, plan


def polymarket_current_bid(market: dict[str, Any], contract: str) -> float | None:
    token_id = token_ids_by_contract(market)[contract]
    orderbook = btc.clob_get("/book", {"token_id": token_id})
    bid_levels, _ask_levels = btc.polymarket_book_levels(orderbook)
    best_bid, _best_qty = btc.best_level(bid_levels)
    return best_bid


def polymarket_client_v2() -> Any:
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2
    except ImportError as exc:
        raise RuntimeError(
            "Polymarket trading requires py-clob-client-v2. Install with: "
            "python3 -m pip install py-clob-client-v2"
        ) from exc

    key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PK")
    if not key:
        raise RuntimeError("Missing POLYMARKET_PRIVATE_KEY in .env")
    host = os.getenv("POLYMARKET_CLOB_URL", btc.POLYMARKET_CLOB_URL)
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
    kwargs: dict[str, Any] = {"host": host, "chain_id": chain_id, "key": key}
    signature_type = os.getenv("POLYMARKET_SIGNATURE_TYPE")
    funder = (
        os.getenv("POLYMARET_ADDRESS")
        or os.getenv("POLYMARKET_ADDRESS")
        or os.getenv("POLYMARKET_FUNDER")
        or os.getenv("POLYMARKET_PROXY_FUNDER")
    )
    if funder and not signature_type:
        signature_type = str(int(SignatureTypeV2.POLY_1271))
    if signature_type:
        kwargs["signature_type"] = int(signature_type)
    if funder:
        kwargs["funder"] = funder

    if os.getenv("CLOB_API_KEY") and os.getenv("CLOB_SECRET") and os.getenv("CLOB_PASS_PHRASE"):
        creds = ApiCreds(
            api_key=os.environ["CLOB_API_KEY"],
            api_secret=os.environ["CLOB_SECRET"],
            api_passphrase=os.environ["CLOB_PASS_PHRASE"],
        )
    else:
        auth_client = ClobClient(**kwargs)
        try:
            creds = auth_client.derive_api_key()
        except Exception:
            creds = auth_client.create_api_key()
    if not all(
        getattr(creds, field, None) for field in ("api_key", "api_secret", "api_passphrase")
    ):
        raise RuntimeError("Polymarket API credentials are incomplete")
    return ClobClient(**kwargs, creds=creds)


def polymarket_post_order(
    market: dict[str, Any],
    contract: str,
    price: float,
    contracts: int,
    side: Any = None,
    order_type_name: str = "FOK",
) -> dict[str, Any]:
    from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

    token_id = token_ids_by_contract(market)[contract]
    client = polymarket_client_v2()
    order_side = side or Side.BUY
    order_type = getattr(OrderType, order_type_name.upper(), OrderType.FOK)
    response = client.create_and_post_order(
        order_args=OrderArgs(
            token_id=token_id,
            price=price,
            side=order_side,
            size=float(contracts),
        ),
        options=PartialCreateOrderOptions(tick_size="0.01"),
        order_type=order_type,
    )
    if not isinstance(response, dict):
        return {"response": response}
    order_id = (
        response.get("id")
        or response.get("order_id")
        or response.get("orderID")
        or response.get("orderId")
    )
    if order_id:
        try:
            verified = client.get_order(str(order_id))
        except Exception as exc:
            response["verification_error"] = f"{type(exc).__name__}: {exc}"
        else:
            response["verified_order"] = verified
    return response


def polymarket_cancel_order(order_id: str) -> Any:
    from py_clob_client_v2.clob_types import OrderPayload

    client = polymarket_client_v2()
    cancel_method = getattr(client, "cancel", None)
    if cancel_method is not None:
        return cancel_method(str(order_id))
    cancel_order_method = getattr(client, "cancel_order", None)
    if cancel_order_method is not None:
        return cancel_order_method(OrderPayload(orderID=str(order_id)))
    raise RuntimeError("Polymarket client has no cancel/cancel_order method")


def polymarket_get_order(order_id: str) -> dict[str, Any]:
    client = polymarket_client_v2()
    response = client.get_order(str(order_id))
    return response if isinstance(response, dict) else {"response": response}


def polymarket_exit_position(
    market: dict[str, Any],
    contract: str,
    contracts: int,
    deviation: float = EXIT_LIMIT_DEVIATION,
) -> tuple[dict[str, Any], float]:
    from py_clob_client_v2 import Side

    token_id = token_ids_by_contract(market)[contract]
    last_balance_error: Exception | None = None
    checked_balance = False
    balance_ready = False
    for attempt in range(POLYMARKET_SELL_BALANCE_ATTEMPTS):
        try:
            balance, allowance = polymarket_conditional_balance_amounts(token_id)
            checked_balance = True
            if balance >= contracts and allowance >= contracts:
                balance_ready = True
                break
            last_balance_error = RuntimeError(
                "Polymarket conditional balance/allowance below cleanup size: "
                f"balance {balance:g}, allowance {allowance:g}, need {contracts:g}"
            )
        except Exception as exc:
            last_balance_error = exc
            break
        time.sleep(POLYMARKET_SELL_BALANCE_DELAY_SECONDS)
    if checked_balance and not balance_ready:
        raise RuntimeError(str(last_balance_error))

    bid, liquidity, _plan = polymarket_exit_plan(market, contract, contracts)
    if bid is None:
        raise RuntimeError(f"Polymarket {contract} exit liquidity {liquidity:g} < {contracts:g}")
    sell_limit = exit_sell_limit(bid, deviation)
    response = polymarket_post_order(market, contract, sell_limit, contracts, side=Side.SELL)
    filled, fill_price = polymarket_fill_summary(response, sell_limit)
    if not filled:
        balance_text = (
            f"; prior balance check: {type(last_balance_error).__name__}: {last_balance_error}"
            if last_balance_error is not None
            else ""
        )
        raise RuntimeError(f"Polymarket sell exit had no fill: {response}{balance_text}")
    response["exit_method"] = "sell"
    response["best_bid"] = bid
    response["limit_price"] = sell_limit
    return response, fill_price


def polymarket_fill_summary(response: dict[str, Any], expected_price: float) -> tuple[bool, float]:
    verified = response.get("verified_order")
    if isinstance(verified, dict):
        filled_size = as_float(
            verified.get("filled_size")
            or verified.get("matched_size")
            or verified.get("size_matched")
            or verified.get("fill_count")
        )
        average_price = as_float(
            verified.get("average_price")
            or verified.get("avg_price")
            or verified.get("price")
        )
        status = str(verified.get("status") or verified.get("state") or "").lower()
        if filled_size > 0 or "filled" in status or "matched" in status:
            return True, average_price or expected_price

    executions = response.get("executions")
    if isinstance(executions, list) and executions:
        total_size = sum(as_float(item.get("quantity") or item.get("size")) for item in executions)
        total_cost = sum(
            as_float(item.get("price", {}).get("value") if isinstance(item.get("price"), dict) else item.get("price"))
            * as_float(item.get("quantity") or item.get("size"))
            for item in executions
        )
        if total_size:
            return True, total_cost / total_size

    status_text = " ".join(str(value).lower() for value in response.values())
    filled = any(word in status_text for word in ("filled", "matched"))
    return filled, expected_price


def cached_active_kalshi_market() -> dict[str, Any] | None:
    cached = KALSHI_MARKET_CACHE.get(btc.SERIES_TICKER)
    if cached:
        close_ts = btc.parse_ts(cached.get("close_time") or cached.get("close_ts"))
        if not close_ts or close_ts > time.time() + 5:
            return cached
    market = btc.discover_active_market()
    if market:
        KALSHI_MARKET_CACHE[btc.SERIES_TICKER] = market
    return market


def fetch_market_state() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    kalshi_market = cached_active_kalshi_market()
    if not kalshi_market:
        raise RuntimeError(f"No open market found for {btc.SERIES_TICKER}")
    cache_key = (
        str(kalshi_market.get("ticker") or ""),
        str(kalshi_market.get("close_time") or kalshi_market.get("close_ts") or ""),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        kalshi_orderbook_future = executor.submit(
            btc.kalshi_get,
            f"/markets/{kalshi_market['ticker']}/orderbook",
            {"depth": btc.ORDERBOOK_DEPTH},
        )
        polymarket_market = POLYMARKET_MARKET_CACHE.get(cache_key)
        polymarket_market_future = (
            None
            if polymarket_market is not None
            else executor.submit(btc.discover_polymarket_market, kalshi_market)
        )
        kalshi_orderbook = kalshi_orderbook_future.result()
        if polymarket_market_future is not None:
            polymarket_market = polymarket_market_future.result()
            if polymarket_market is not None:
                POLYMARKET_MARKET_CACHE[cache_key] = polymarket_market
    kalshi_snapshot = btc.make_snapshot(kalshi_market, kalshi_orderbook)

    if not polymarket_market:
        raise RuntimeError("No matching open Polymarket market found")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        polymarket_orderbook_future = executor.submit(
            btc.polymarket_clob_orderbooks,
            polymarket_market,
        )
        source_snapshot_future = executor.submit(
            btc.source_price_snapshot,
            kalshi_market,
            polymarket_market,
        )
        polymarket_orderbook = polymarket_orderbook_future.result()
        source_snapshot = source_snapshot_future.result()
    polymarket_snapshot = btc.make_polymarket_snapshot(polymarket_market, polymarket_orderbook)
    return kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot, source_snapshot


def polymarket_execution_price(price: float) -> float:
    return min(0.99, round(price + 0.01, 2))


def execution_expected_profit(arbitrage: dict[str, Any]) -> float:
    total_cost = arbitrage["kalshi_price"] + polymarket_execution_price(
        arbitrage["polymarket_price"]
    )
    if total_cost <= 0 or total_cost >= 1:
        return 0.0
    return (1.0 / total_cost) - 1.0


def seconds_to_expiry(kalshi_snapshot: dict[str, Any]) -> float | None:
    close_ts = btc.parse_ts(kalshi_snapshot.get("close_time"))
    if close_ts is None:
        return None
    return max(0.0, close_ts - time.time())


def no_trade_boundary_reason(kalshi_snapshot: dict[str, Any]) -> str | None:
    remaining = seconds_to_expiry(kalshi_snapshot)
    if remaining is None:
        return None
    if remaining <= CONTRACT_BOUNDARY_NO_TRADE_SECONDS:
        return f"last {CONTRACT_BOUNDARY_NO_TRADE_SECONDS:.0f}s before expiry ({remaining:.1f}s left)"
    if remaining >= CONTRACT_WINDOW_SECONDS - CONTRACT_BOUNDARY_NO_TRADE_SECONDS:
        elapsed = max(0.0, CONTRACT_WINDOW_SECONDS - remaining)
        return f"first {CONTRACT_BOUNDARY_NO_TRADE_SECONDS:.0f}s of contract ({elapsed:.1f}s elapsed)"
    return None


def position_expired(position: dict[str, Any]) -> bool:
    close_ts = btc.parse_ts(position.get("close_time"))
    return close_ts is not None and time.time() >= close_ts


def pending_exit_clear_reason(position: dict[str, Any], current_contract_key: str | None) -> str | None:
    if not position_has_pending_exit(position):
        return None
    if position_expired(position):
        return "pending exit contract reached expiry"
    position_ticker = str(position.get("ticker") or "")
    if current_contract_key and position_ticker and current_contract_key != position_ticker:
        return f"current contract is {current_contract_key}"
    return None


def mark_contract_cooldown(
    cooldowns: dict[str, tuple[float, str]],
    ticker: str | None,
    reason: str,
    seconds: float = CONTRACT_FAILURE_COOLDOWN_SECONDS,
) -> None:
    if ticker:
        cooldowns[ticker] = (time.time() + seconds, reason)


def contract_cooldown_reason(cooldowns: dict[str, tuple[float, str]], ticker: str | None) -> str | None:
    if not ticker:
        return None
    item = cooldowns.get(ticker)
    if not item:
        return None
    until, reason = item
    remaining = until - time.time()
    if remaining <= 0:
        cooldowns.pop(ticker, None)
        return None
    return f"{reason}; cooldown {remaining:.1f}s left"


def entry_prefilter_reason(
    open_position: dict[str, Any] | None,
    boundary_reason: str | None,
    arbitrage: dict[str, Any] | None,
    min_profit: float,
    trades_done: int,
    max_trades: int,
    cooldown_reason: str | None,
) -> str | None:
    checks = (
        ("open_position", open_position is None, "position already open"),
        ("boundary", boundary_reason is None, boundary_reason or "outside no-trade boundary"),
        ("arbitrage", arbitrage is not None, "no arbitrage candidate"),
        (
            "expected_profit",
            bool(arbitrage and arbitrage["expected_profit"] > min_profit),
            (
                f"{cli.fmt_money(arbitrage['expected_profit'])} <= {cli.fmt_money(min_profit)}"
                if arbitrage
                else "no arbitrage candidate"
            ),
        ),
        ("trade_limit", trades_done < max_trades, f"{trades_done:g}/{max_trades:g} trades used"),
        ("cooldown", cooldown_reason is None, cooldown_reason or "no cooldown"),
    )
    for _name, passed, reason in checks:
        if not passed:
            return reason
    return None


def source_filter_metrics(
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    source_snapshot: dict[str, Any],
    arb_cost: float | None,
    kalshi_direction_price: float | None = None,
) -> dict[str, Any]:
    kalshi_price = finite_float(source_snapshot.get("kalshi_price"))
    poly_price = finite_float(source_snapshot.get("polymarket_price"))
    kalshi_target = finite_float(source_snapshot.get("kalshi_target"))
    poly_target = finite_float(source_snapshot.get("polymarket_target"))
    remaining = seconds_to_expiry(kalshi_snapshot)

    source_gap = (
        abs(kalshi_price - poly_price)
        if kalshi_price is not None and poly_price is not None
        else None
    )
    target_divergence = (
        abs(kalshi_target - poly_target)
        if kalshi_target is not None and poly_target is not None
        else None
    )
    kalshi_distance = (
        abs(kalshi_direction_price - kalshi_target)
        if kalshi_direction_price is not None and kalshi_target is not None
        else None
    )
    poly_distance = (
        abs(poly_price - poly_target)
        if poly_price is not None and poly_target is not None
        else None
    )
    min_distance = (
        min(kalshi_distance, poly_distance)
        if kalshi_distance is not None and poly_distance is not None
        else None
    )
    direction_agreement = (
        (kalshi_direction_price > kalshi_target) == (poly_price > poly_target)
        if (
            kalshi_direction_price is not None
            and poly_price is not None
            and kalshi_target is not None
            and poly_target is not None
        )
        else None
    )
    entry_required_distance = (
        max(10.0, remaining * 0.05) if remaining is not None else None
    )
    profit_after_fees = (
        SETTLEMENT_PAYOUT_AFTER_FEES - arb_cost
        if arb_cost is not None
        else None
    )
    return {
        "kalshi_price": kalshi_price,
        "polymarket_price": poly_price,
        "kalshi_target": kalshi_target,
        "polymarket_target": poly_target,
        "seconds_to_expiry": remaining,
        "source_gap": source_gap,
        "target_divergence": target_divergence,
        "kalshi_distance": kalshi_distance,
        "polymarket_distance": poly_distance,
        "min_distance": min_distance,
        "direction_agreement": direction_agreement,
        "kalshi_direction_price": kalshi_direction_price,
        "entry_required_distance": entry_required_distance,
        "arb_cost": arb_cost,
        "profit_after_fees": profit_after_fees,
        "kalshi_status": kalshi_snapshot.get("status"),
        "polymarket_error": polymarket_snapshot.get("error") or source_snapshot.get("error"),
    }


def fmt_optional(value: Any, places: int = 3) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:.{places}f}"
    return "--"


def fmt_signed(value: Any, places: int = 2) -> str:
    if isinstance(value, (int, float)):
        return f"{value:+.{places}f}"
    return "--"


def kalshi_sma_window_size(interval: float) -> int:
    return max(1, int(round(60.0 / max(interval, 0.1))))


def reference_delta_suffix(
    source_snapshot: dict[str, Any],
    kalshi_brtis: deque[float],
    sma_window_size: int,
) -> tuple[str, float | None]:
    kalshi_price = finite_float(source_snapshot.get("kalshi_price"))
    if kalshi_price is not None:
        kalshi_brtis.append(kalshi_price)

    kalshi_target = finite_float(source_snapshot.get("kalshi_target"))
    poly_price = finite_float(source_snapshot.get("polymarket_price"))
    poly_target = finite_float(source_snapshot.get("polymarket_target"))

    kalshi_delta = None
    if kalshi_brtis and kalshi_target is not None:
        kalshi_delta = (sum(kalshi_brtis) / len(kalshi_brtis)) - kalshi_target
    poly_delta = (
        poly_price - poly_target
        if poly_price is not None and poly_target is not None
        else None
    )
    return (
        (
            f"ΔK {fmt_signed(kalshi_delta)} "
            f"({len(kalshi_brtis)}/{sma_window_size}) | "
            f"ΔP {fmt_signed(poly_delta)}"
        ),
        (kalshi_delta + kalshi_target if kalshi_delta is not None and kalshi_target is not None else None),
    )


def decision_line(label: str, passed: bool, detail: str) -> str:
    return f"{label} {'PASS' if passed else 'FAIL'} {detail}"


def evaluate_entry_filter(
    metrics: dict[str, Any],
    source_gap_threshold: float,
    target_divergence_threshold: float,
    min_profit_after_fees: float,
) -> dict[str, Any]:
    checks: list[tuple[str, bool, str]] = []

    poly_ok = not metrics.get("polymarket_error")
    checks.append(("polymarket_data", poly_ok, "error empty" if poly_ok else str(metrics.get("polymarket_error"))))

    status = str(metrics.get("kalshi_status") or "").lower()
    kalshi_active = status == "active"
    checks.append(("kalshi_status", kalshi_active, f"status={status or '--'}"))

    kalshi_target_ok = metrics.get("kalshi_target") is not None
    checks.append(("kalshi_target", kalshi_target_ok, f"value={fmt_optional(metrics.get('kalshi_target'), 2)}"))

    poly_target_ok = metrics.get("polymarket_target") is not None
    checks.append(("polymarket_target", poly_target_ok, f"value={fmt_optional(metrics.get('polymarket_target'), 2)}"))

    direction = metrics.get("direction_agreement")
    checks.append(("direction_agreement", direction is True, f"value={direction}"))

    source_gap = metrics.get("source_gap")
    source_gap_ok = source_gap is not None and source_gap <= source_gap_threshold
    checks.append((
        "source_gap",
        source_gap_ok,
        f"{fmt_optional(source_gap)} <= {source_gap_threshold:.3f}",
    ))

    min_distance = metrics.get("min_distance")
    required_distance = metrics.get("entry_required_distance")
    distance_ok = (
        min_distance is not None
        and required_distance is not None
        and min_distance >= required_distance
    )
    checks.append((
        "entry_distance",
        distance_ok,
        f"{fmt_optional(min_distance)} >= {fmt_optional(required_distance)}",
    ))

    target_divergence = metrics.get("target_divergence")
    target_ok = target_divergence is not None and target_divergence <= target_divergence_threshold
    checks.append((
        "target_divergence",
        target_ok,
        f"{fmt_optional(target_divergence)} <= {target_divergence_threshold:.3f}",
    ))

    profit_after_fees = metrics.get("profit_after_fees")
    profit_ok = profit_after_fees is not None and profit_after_fees >= min_profit_after_fees
    checks.append((
        "profit_after_fees",
        profit_ok,
        f"{fmt_optional(profit_after_fees, 4)} >= {min_profit_after_fees:.4f}",
    ))

    passed = all(item[1] for item in checks)
    return {
        "decision": "ENTER" if passed else "SKIP",
        "passed": passed,
        "checks": checks,
        "reasons": [f"{name}: {detail}" for name, ok, detail in checks if not ok],
    }


def evaluate_hold_filter(
    metrics: dict[str, Any],
    source_gap_threshold: float,
    target_divergence_threshold: float,
    distance_multiplier: float,
) -> dict[str, Any]:
    checks: list[tuple[str, bool, str]] = []

    poly_ok = not metrics.get("polymarket_error")
    checks.append(("polymarket_data", poly_ok, "error empty" if poly_ok else str(metrics.get("polymarket_error"))))

    kalshi_target_ok = metrics.get("kalshi_target") is not None
    checks.append(("kalshi_target", kalshi_target_ok, f"value={fmt_optional(metrics.get('kalshi_target'), 2)}"))

    poly_target_ok = metrics.get("polymarket_target") is not None
    checks.append(("polymarket_target", poly_target_ok, f"value={fmt_optional(metrics.get('polymarket_target'), 2)}"))

    direction = metrics.get("direction_agreement")
    checks.append(("direction_agreement", direction is True, f"value={direction}"))

    source_gap = metrics.get("source_gap")
    source_gap_ok = source_gap is not None and source_gap <= source_gap_threshold
    checks.append((
        "source_gap",
        source_gap_ok,
        f"{fmt_optional(source_gap)} <= {source_gap_threshold:.3f}",
    ))

    min_distance = metrics.get("min_distance")
    entry_required_distance = metrics.get("entry_required_distance")
    hold_required_distance = (
        distance_multiplier * entry_required_distance
        if entry_required_distance is not None
        else None
    )
    distance_ok = (
        min_distance is not None
        and hold_required_distance is not None
        and min_distance >= hold_required_distance
    )
    checks.append((
        "hold_distance",
        distance_ok,
        f"{fmt_optional(min_distance)} >= {fmt_optional(hold_required_distance)} "
        f"({distance_multiplier:.2f}x entry distance)",
    ))

    target_divergence = metrics.get("target_divergence")
    target_ok = target_divergence is not None and target_divergence <= target_divergence_threshold
    checks.append((
        "target_divergence",
        target_ok,
        f"{fmt_optional(target_divergence)} <= {target_divergence_threshold:.3f}",
    ))

    passed = all(item[1] for item in checks)
    return {
        "decision": "HOLD" if passed else "EXIT_REVIEW",
        "passed": passed,
        "checks": checks,
        "reasons": [f"{name}: {detail}" for name, ok, detail in checks if not ok],
    }


def format_filter_decision(prefix: str, decision: dict[str, Any]) -> list[str]:
    lines = [f"{prefix} {decision['decision']}"]
    for name, passed, detail in decision["checks"]:
        lines.append(f"{prefix} {decision_line(name, passed, detail)}")
    if decision["reasons"]:
        lines.append(f"{prefix} reasons: " + "; ".join(decision["reasons"]))
    return lines


def format_entry_skip(decision: dict[str, Any]) -> str:
    labels = {
        "polymarket_data": "polymarket_data",
        "kalshi_status": "kalshi_status",
        "kalshi_target": "kalshi_target",
        "polymarket_target": "polymarket_target",
        "direction_agreement": "direction",
        "source_gap": "source_gap",
        "entry_distance": "entry_dist",
        "target_divergence": "target_div",
        "profit_after_fees": "profit_after_fees",
    }
    parts = []
    for name, passed, detail in decision["checks"]:
        if passed:
            continue
        if detail.startswith("value="):
            detail = detail.removeprefix("value=")
        parts.append(f"{labels.get(name, name)}: {detail}")
    return "ENTRY SKIP " + "; ".join(parts)


def format_hold_decision(decision: dict[str, Any]) -> str:
    labels = {
        "polymarket_data": "polymarket_data",
        "kalshi_target": "kalshi_target",
        "polymarket_target": "polymarket_target",
        "direction_agreement": "direction",
        "source_gap": "source_gap",
        "hold_distance": "hold_dist",
        "target_divergence": "target_div",
    }
    if decision["passed"]:
        return "HOLD continue"
    parts = []
    for name, passed, detail in decision["checks"]:
        if passed:
            continue
        if detail.startswith("value="):
            detail = detail.removeprefix("value=")
        parts.append(f"{labels.get(name, name)}: {detail}")
    return "HOLD EXIT_REVIEW " + "; ".join(parts)


def filter_check_passed(decision: dict[str, Any], check_name: str) -> bool:
    for name, passed, _detail in decision["checks"]:
        if name == check_name:
            return passed
    return False


def format_contract_start(
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    source_snapshot: dict[str, Any],
) -> str:
    close_time = kalshi_snapshot.get("close_time") or "--"
    title = kalshi_snapshot.get("title") or "BTC 15m"
    return (
        "CONTRACT "
        f"{kalshi_snapshot.get('ticker') or '--'} | {title} | "
        f"expires {close_time} | "
        f"K target {fmt_optional(finite_float(source_snapshot.get('kalshi_target')), 2)} | "
        f"P target {fmt_optional(finite_float(source_snapshot.get('polymarket_target')), 2)} | "
        f"P market {polymarket_snapshot.get('ticker') or '--'}"
    )


def format_position_review(
    position: dict[str, Any],
    reason: str,
    liquidation: float | None = None,
) -> str:
    text = (
        f"POSITION REVIEW {position.get('ticker') or '--'} | {reason} | "
        f"K {str(position.get('kalshi_side') or '--').upper()} + "
        f"P {position.get('polymarket_contract') or '--'} | "
        f"size {position.get('contracts') or '--'} | "
        f"entry {cli.fmt_display_cents(position.get('entry_cost'))}c | "
        f"expiry {position.get('close_time') or '--'}"
    )
    if liquidation is not None:
        text += (
            f" | liquidation {cli.fmt_display_cents(liquidation)}c "
            f"({cli.fmt_money(liquidation - as_float(position.get('entry_cost')))} before exit fees)"
        )
    return text


def format_position_clear(position: dict[str, Any], reason: str) -> str:
    return (
        f"POSITION CLEAR {position.get('ticker') or '--'} | {reason} | "
        f"K {str(position.get('kalshi_side') or '--').upper()} + "
        f"P {position.get('polymarket_contract') or '--'} | "
        f"size {position.get('contracts') or '--'} | "
        f"entry {cli.fmt_display_cents(position.get('entry_cost'))}c | "
        f"expiry {position.get('close_time') or '--'} | internal tracking cleared"
    )


def format_pending_exit_clear(position: dict[str, Any], reason: str) -> str:
    contracts = int(position.get("contracts") or 0)
    kalshi_side = str(position.get("kalshi_side") or "--").upper()
    polymarket_contract = str(position.get("polymarket_contract") or "--")
    remaining = []
    if not position.get("kalshi_absent") and not position.get("kalshi_exited"):
        kalshi_contracts = position_leg_contracts(position, "kalshi", contracts)
        remaining.append(f"Kalshi {kalshi_side} size {kalshi_contracts:g}")
    if not position.get("polymarket_absent") and not position.get("polymarket_exited"):
        poly_contracts = position_leg_contracts(position, "polymarket", contracts)
        remaining.append(f"Polymarket {polymarket_contract} size {poly_contracts:g}")
    remaining_text = ", ".join(remaining) if remaining else "none"
    return (
        f"POSITION CLEAR {position.get('ticker') or '--'} | {reason} | "
        f"pending exit no longer actionable | remaining {remaining_text} left to settlement | "
        f"K {kalshi_side} + P {polymarket_contract} | "
        f"size {contracts or '--'} | "
        f"entry {cli.fmt_display_cents(position.get('entry_cost'))}c | "
        f"expiry {position.get('close_time') or '--'} | internal tracking cleared"
    )


def record_position_state(
    position: dict[str, Any],
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    source_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if str(position.get("ticker") or "") != str(kalshi_snapshot.get("ticker") or ""):
        return None
    state = position_state_metrics(
        position,
        kalshi_snapshot,
        polymarket_snapshot,
        source_snapshot,
        polymarket_market,
    )
    position["last_state_metrics"] = state
    position["last_state_time"] = kalshi_snapshot.get("timestamp_utc") or source_snapshot.get("timestamp_utc")
    return state


def format_dry_settlement(position: dict[str, Any], reason: str) -> str:
    state = position.get("last_state_metrics") or {}
    observed_at = position.get("last_state_time") or "--"
    held_winners = state.get("held_winners")
    base = (
        f"SETTLED DRY RUN {position.get('ticker') or '--'} | {reason} | "
        f"observed {observed_at} | "
        f"K {str(position.get('kalshi_side') or '--').upper()} final {state.get('kalshi_outcome') or '--'} + "
        f"P {position.get('polymarket_contract') or '--'} final {state.get('polymarket_outcome') or '--'} | "
        f"size {position.get('contracts') or '--'} | "
        f"entry {cli.fmt_display_cents(position.get('entry_cost'))}c"
    )
    if held_winners is None:
        return f"{base} | actual PnL unavailable; final outcome was not observed"
    entry_cost = as_float(position.get("entry_cost"))
    contracts = int(position.get("contracts") or 1)
    settlement_value = SETTLEMENT_PAYOUT_AFTER_FEES * int(held_winners)
    pnl_per_pair = settlement_value - entry_cost
    total_pnl = pnl_per_pair * contracts
    return (
        f"{base} | winners {held_winners}/2 | "
        f"settlement {cli.fmt_display_cents(settlement_value)}c - "
        f"entry {cli.fmt_display_cents(entry_cost)}c = "
        f"{cli.fmt_money(pnl_per_pair)} per pair | total {cli.fmt_money(total_pnl)}"
    )


def liquidation_bid_value(
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    kalshi_side: str,
    polymarket_contract: str,
) -> float | None:
    kalshi_bid = finite_float(kalshi_snapshot.get(f"{kalshi_side.lower()}_bid"))
    poly_bid = finite_float(polymarket_snapshot.get(f"{polymarket_contract.lower()}_bid"))
    if kalshi_bid is None or poly_bid is None:
        return None
    return kalshi_bid + poly_bid


def executable_liquidation_value(
    position: dict[str, Any],
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any] | None,
) -> tuple[float | None, dict[str, Any]]:
    contracts = int(position.get("contracts") or 0)
    ticker = str(position.get("ticker") or kalshi_snapshot.get("ticker") or "")
    kalshi_side = str(position.get("kalshi_side") or "").lower()
    polymarket_contract = str(position.get("polymarket_contract") or "")
    total = 0.0
    plans: dict[str, Any] = {}

    if not position.get("kalshi_absent") and not position.get("kalshi_exited"):
        kalshi_contracts = position_leg_contracts(position, "kalshi", contracts)
        if not ticker or kalshi_side not in ("yes", "no") or kalshi_contracts <= 0:
            return None, plans
        price, liquidity, plan = kalshi_exit_plan(ticker, kalshi_side, kalshi_contracts)
        plans["kalshi"] = plan
        if price is None or liquidity < kalshi_contracts:
            return None, plans
        total += price
    elif position.get("kalshi_exited") and not position.get("kalshi_absent"):
        total += as_float(position.get("kalshi_exit_price"))

    if not position.get("polymarket_absent") and not position.get("polymarket_exited"):
        poly_contracts = position_leg_contracts(position, "polymarket", contracts)
        if not polymarket_contract or poly_contracts <= 0:
            return None, plans
        if polymarket_market:
            price, liquidity, plan = polymarket_exit_plan(
                polymarket_market,
                polymarket_contract,
                poly_contracts,
            )
            plans["polymarket"] = plan
            if price is None or liquidity < poly_contracts:
                return None, plans
            total += price
        else:
            poly_bid = finite_float(polymarket_snapshot.get(f"{polymarket_contract.lower()}_bid"))
            if poly_bid is None:
                return None, plans
            total += poly_bid
    elif position.get("polymarket_exited") and not position.get("polymarket_absent"):
        total += as_float(position.get("polymarket_exit_price"))

    return total, plans


def source_outcome(price: float | None, target: float | None) -> str | None:
    if price is None or target is None:
        return None
    return "YES" if price > target else "NO"


def position_state_metrics(
    position: dict[str, Any],
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    source_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kalshi_side = str(position.get("kalshi_side") or "").upper()
    polymarket_side = str(position.get("polymarket_contract") or "").upper()
    kalshi_price = finite_float(source_snapshot.get("kalshi_price"))
    polymarket_price = finite_float(source_snapshot.get("polymarket_price"))
    kalshi_target = finite_float(source_snapshot.get("kalshi_target"))
    polymarket_target = finite_float(source_snapshot.get("polymarket_target"))
    kalshi_outcome = source_outcome(kalshi_price, kalshi_target)
    polymarket_outcome = source_outcome(polymarket_price, polymarket_target)
    kalshi_distance = (
        abs(kalshi_price - kalshi_target)
        if kalshi_price is not None and kalshi_target is not None
        else None
    )
    polymarket_distance = (
        abs(polymarket_price - polymarket_target)
        if polymarket_price is not None and polymarket_target is not None
        else None
    )
    min_distance = (
        min(kalshi_distance, polymarket_distance)
        if kalshi_distance is not None and polymarket_distance is not None
        else None
    )
    source_gap = (
        abs(kalshi_price - polymarket_price)
        if kalshi_price is not None and polymarket_price is not None
        else None
    )
    held_winners = None
    if kalshi_outcome is not None and polymarket_outcome is not None:
        held_winners = int(kalshi_outcome == kalshi_side) + int(polymarket_outcome == polymarket_side)
    liquidation, liquidation_plans = executable_liquidation_value(
        position,
        kalshi_snapshot,
        polymarket_snapshot,
        polymarket_market,
    )
    entry_cost = as_float(position.get("entry_cost"))
    liquidation_pnl = liquidation - entry_cost if liquidation is not None else None
    expected_settlement_pnl = (
        SETTLEMENT_PAYOUT_AFTER_FEES * held_winners - entry_cost
        if held_winners is not None
        else None
    )
    return {
        "kalshi_outcome": kalshi_outcome,
        "polymarket_outcome": polymarket_outcome,
        "held_winners": held_winners,
        "kalshi_distance": kalshi_distance,
        "polymarket_distance": polymarket_distance,
        "min_distance": min_distance,
        "source_gap": source_gap,
        "seconds_to_expiry": seconds_to_expiry(kalshi_snapshot),
        "liquidation": liquidation,
        "liquidation_plans": liquidation_plans,
        "liquidation_pnl": liquidation_pnl,
        "expected_settlement_pnl": expected_settlement_pnl,
    }


def format_position_state(state: dict[str, Any]) -> str:
    return (
        f"state winners={fmt_optional(state.get('held_winners'), 0)} "
        f"K->{state.get('kalshi_outcome') or '--'} "
        f"P->{state.get('polymarket_outcome') or '--'} "
        f"min_dist {fmt_optional(state.get('min_distance'), 2)} "
        f"gap {fmt_optional(state.get('source_gap'), 2)} "
        f"liq {cli.fmt_display_cents(state.get('liquidation'))}c "
        f"liq_pnl {cli.fmt_money(state.get('liquidation_pnl'))}"
    )


def exit_strategy_decision(
    position: dict[str, Any],
    state: dict[str, Any],
    hold_decision: dict[str, Any],
    take_profit_exit_value: float,
    profit_capture_min_edge: float,
    exit_cushion: float,
) -> dict[str, Any]:
    held_winners = state.get("held_winners")
    remaining = state.get("seconds_to_expiry")
    min_distance = state.get("min_distance")
    liquidation = state.get("liquidation")
    liquidation_pnl = state.get("liquidation_pnl")
    entry_cost = as_float(position.get("entry_cost"))

    if held_winners is None:
        if (
            not hold_decision["passed"]
            and liquidation_pnl is not None
            and liquidation_pnl >= exit_cushion
        ):
            return {"action": "EXIT", "reason": "data incomplete and cushioned hold-review unwind available"}
        return {"action": "HOLD", "reason": "data incomplete; no state-aware exit"}

    if held_winners <= 0:
        return {"action": "EXIT", "reason": "EMERGENCY held_winners=0; both held legs currently losing"}

    required_profit = max(profit_capture_min_edge, exit_cushion)
    if liquidation_pnl is not None and liquidation_pnl >= required_profit:
        return {
            "action": "EXIT",
            "reason": (
                f"TAKE_PROFIT liquidation edge {cli.fmt_money(liquidation_pnl)} "
                f">= {cli.fmt_money(required_profit)}"
            ),
        }

    if (
        liquidation is not None
        and liquidation >= take_profit_exit_value
        and liquidation_pnl is not None
        and liquidation_pnl >= exit_cushion
    ):
        return {
            "action": "EXIT",
            "reason": (
                f"TAKE_PROFIT liquidation {cli.fmt_display_cents(liquidation)}c "
                f">= {cli.fmt_display_cents(take_profit_exit_value)}c; "
                f"edge {cli.fmt_money(liquidation_pnl)} >= cushion {cli.fmt_money(exit_cushion)}"
            ),
        }

    if held_winners >= 2:
        near_expiry = isinstance(remaining, (int, float)) and remaining <= TWO_WINNER_PROFIT_EXIT_SECONDS
        near_target = isinstance(min_distance, (int, float)) and min_distance < TWO_WINNER_PROFIT_EXIT_DISTANCE
        if near_expiry and near_target and liquidation_pnl is not None and liquidation_pnl >= exit_cushion:
            return {
                "action": "EXIT",
                "reason": (
                    "TAKE_PROFIT favorable discrepancy near expiry/target; "
                    f"{fmt_optional(remaining, 1)}s left, min_dist {fmt_optional(min_distance, 2)}"
                ),
            }
        return {"action": "HOLD", "reason": "held_winners=2 but no acceptable profit capture"}

    if not hold_decision["passed"]:
        negative_unwind = liquidation_pnl is not None and liquidation_pnl < 0
        if negative_unwind:
            urgent_time = isinstance(remaining, (int, float)) and remaining <= ONE_WINNER_NEGATIVE_EXIT_SECONDS
            urgent_distance = (
                isinstance(min_distance, (int, float))
                and min_distance <= ONE_WINNER_NEGATIVE_EXIT_DISTANCE
            )
            if urgent_time and urgent_distance:
                return {
                    "action": "EXIT",
                    "reason": (
                        "ONE_WINNER emergency negative unwind accepted; "
                        f"{fmt_optional(remaining, 1)}s left, min_dist {fmt_optional(min_distance, 2)}"
                    ),
                }
            return {
                "action": "HOLD",
                "reason": (
                    "BLOCK_NEGATIVE_EXIT held_winners=1; "
                    f"liquidation PnL {cli.fmt_money(liquidation_pnl)}"
                ),
            }
        if liquidation_pnl is not None and liquidation_pnl >= exit_cushion:
            return {
                "action": "EXIT",
                "reason": (
                    f"HOLD_FAIL cushioned unwind {cli.fmt_money(liquidation_pnl)} "
                    f">= {cli.fmt_money(exit_cushion)}"
                ),
            }
        if liquidation_pnl is not None:
            return {
                "action": "HOLD",
                "reason": (
                    f"HOLD_FAIL unwind edge {cli.fmt_money(liquidation_pnl)} "
                    f"< cushion {cli.fmt_money(exit_cushion)}"
                ),
            }
        return {"action": "HOLD", "reason": "hold failed but executable liquidation unavailable"}

    return {"action": "HOLD", "reason": "state-aware hold"}


def position_has_pending_exit(position: dict[str, Any]) -> bool:
    return bool(
        position.get("exit_started")
        or position.get("kalshi_exited")
        or position.get("polymarket_exited")
        or position.get("kalshi_absent")
        or position.get("polymarket_absent")
    )


def position_has_exit_progress(position: dict[str, Any]) -> bool:
    return bool(
        position.get("kalshi_exited")
        or position.get("polymarket_exited")
        or position.get("kalshi_absent")
        or position.get("polymarket_absent")
    )


def is_take_profit_exit(strategy_decision: dict[str, Any]) -> bool:
    return str(strategy_decision.get("reason") or "").startswith("TAKE_PROFIT")


def position_leg_contracts(position: dict[str, Any], leg: str, default: int) -> int:
    return int(position.get(f"{leg}_contracts") or position.get("contracts") or default)


def reconcile_unknown_kalshi_order(position: dict[str, Any]) -> str | None:
    order_id = position.get("kalshi_order_id")
    if not order_id:
        position["kalshi_order_unknown"] = False
        return "Kalshi unknown order had no order id; treating Kalshi leg as absent"
    try:
        response = http_json("GET", btc.BASE_URL, f"/portfolio/orders/{order_id}", auth=True)
    except Exception as exc:
        if "HTTP 404" not in str(exc):
            return f"Kalshi unknown order reconciliation failed: {type(exc).__name__}: {exc}"
        unknown_since = finite_float(position.get("kalshi_unknown_since")) or time.time()
        if time.time() - unknown_since < 5.0:
            return f"Kalshi order {order_id} still not queryable; holding cleanup until state is known"
        position["kalshi_order_unknown"] = False
        return f"Kalshi order {order_id} still 404 after reconciliation window; treating Kalshi leg as absent"
    order = response.get("order") or response
    filled = int(fill_count(order))
    if filled > 0:
        kalshi_side = str(position.get("kalshi_side") or "").lower()
        position["kalshi_order_unknown"] = False
        position["kalshi_absent"] = False
        position["kalshi_exited"] = False
        position["kalshi_contracts"] = filled
        position["kalshi_fill_price"] = filled_price(order, kalshi_side)
        position["entry_cost"] = as_float(position.get("entry_cost")) + as_float(position["kalshi_fill_price"])
        position["kalshi_order"] = order
        return f"Kalshi order {order_id} reconciled filled {filled:g}; exiting both legs"
    position["kalshi_order_unknown"] = False
    return f"Kalshi order {order_id} reconciled with no fill; exiting Polymarket leg only"


def kalshi_exit_summary(
    order: dict[str, Any],
    side: str,
) -> tuple[float, float, float | None, float | None, str]:
    filled = fill_count(order)
    exit_method = str(order.get("exit_method") or "")
    if exit_method == "sell":
        fill_price = filled_price(order, side, action="sell")
    elif exit_method.startswith("buy_"):
        hedge_side = exit_method.removeprefix("buy_")
        fill_price = round(1.0 - filled_price(order, hedge_side, action="buy"), 10)
    else:
        fill_price = filled_price(order, side)
    reference = finite_float(
        order.get("best_bid")
        if order.get("exit_method") == "sell"
        else order.get("best_ask")
    )
    limit = finite_float(order.get("limit_price"))
    limit_text = (
        f"bid {cli.fmt_display_cents(reference)}c, limit {cli.fmt_display_cents(limit)}c"
        if order.get("exit_method") == "sell"
        else f"ask {cli.fmt_display_cents(reference)}c, limit {cli.fmt_display_cents(limit)}c"
    )
    return filled, fill_price, reference, limit, limit_text


def execute_position_exit(
    position: dict[str, Any],
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any],
    live: bool,
) -> tuple[str, bool]:
    contracts = int(position.get("contracts") or 0)
    if contracts <= 0:
        return f"EXIT FAILED invalid position size {position.get('contracts')}", False

    ticker = str(position.get("ticker") or kalshi_snapshot.get("ticker") or "")
    kalshi_side = str(position.get("kalshi_side") or "").lower()
    polymarket_contract = str(position.get("polymarket_contract") or "")
    if not ticker or kalshi_side not in ("yes", "no") or not polymarket_contract:
        return f"EXIT FAILED incomplete position {position}", False

    exit_polymarket_market = position.setdefault("polymarket_market", polymarket_market)
    if not live:
        liquidation = liquidation_bid_value(
            kalshi_snapshot,
            polymarket_snapshot,
            kalshi_side,
            polymarket_contract,
        )
        kalshi_bid = finite_float(kalshi_snapshot.get(f"{kalshi_side}_bid"))
        kalshi_limit = exit_sell_limit(kalshi_bid) if kalshi_bid is not None else None
        poly_bid = finite_float(polymarket_snapshot.get(f"{polymarket_contract.lower()}_bid"))
        poly_limit = exit_sell_limit(poly_bid) if poly_bid is not None else None
        liquidation_text = (
            f" liquidation {cli.fmt_display_cents(liquidation)}c"
            if liquidation is not None
            else ""
        )
        return (
            "EXITED DRY RUN would sell "
            f"Kalshi {ticker} {kalshi_side.upper()} size {contracts} "
            f"bid {cli.fmt_display_cents(kalshi_bid)}c limit {cli.fmt_display_cents(kalshi_limit)}c and "
            f"Polymarket {polymarket_contract} size {contracts} "
            f"{polymarket_token_ref(exit_polymarket_market, polymarket_contract)} "
            f"bid {cli.fmt_display_cents(poly_bid)}c limit {cli.fmt_display_cents(poly_limit)}c"
            f"{liquidation_text}; "
            "closing simulated position"
        ), True

    position["exit_started"] = True
    if position.get("kalshi_order_unknown"):
        reconcile_text = reconcile_unknown_kalshi_order(position)
        if position.get("kalshi_order_unknown"):
            return f"EXIT WAIT {reconcile_text}", False
        if reconcile_text:
            errors = [reconcile_text]
        else:
            errors = []
    else:
        errors = []
    poly_response = position.get("polymarket_exit_response")
    poly_fill_price = finite_float(position.get("polymarket_exit_price"))
    kalshi_order = position.get("kalshi_exit_order")
    kalshi_fill_price = finite_float(position.get("kalshi_exit_price"))

    attempted: list[str] = []
    poly_contracts = position_leg_contracts(position, "polymarket", contracts)
    kalshi_contracts = position_leg_contracts(position, "kalshi", contracts)
    need_poly = not position.get("polymarket_absent") and not position.get("polymarket_exited")
    need_kalshi = not position.get("kalshi_absent") and not position.get("kalshi_exited")

    if need_poly and need_kalshi:
        attempted.append("polymarket")
        try:
            poly_response, poly_fill_price = polymarket_exit_position(
                exit_polymarket_market,
                polymarket_contract,
                poly_contracts,
            )
            position["polymarket_exited"] = True
            position["polymarket_exit_response"] = poly_response
            position["polymarket_exit_price"] = poly_fill_price
        except Exception as exc:
            errors.append(f"polymarket failed: {type(exc).__name__}: {exc}")

        if position.get("polymarket_exited"):
            attempted.append("kalshi")
            try:
                kalshi_order = kalshi_exit_position(ticker, kalshi_side, kalshi_contracts)
                _filled, kalshi_fill_price, _reference, _limit, _limit_text = kalshi_exit_summary(
                    kalshi_order,
                    kalshi_side,
                )
                position["kalshi_exited"] = True
                position["kalshi_exit_order"] = kalshi_order
                position["kalshi_exit_price"] = kalshi_fill_price
            except Exception as exc:
                errors.append(f"kalshi failed: {type(exc).__name__}: {exc}")
    elif need_poly:
        attempted.append("polymarket")
        try:
            poly_response, poly_fill_price = polymarket_exit_position(
                exit_polymarket_market,
                polymarket_contract,
                poly_contracts,
            )
            position["polymarket_exited"] = True
            position["polymarket_exit_response"] = poly_response
            position["polymarket_exit_price"] = poly_fill_price
        except Exception as exc:
            errors.append(f"polymarket failed: {type(exc).__name__}: {exc}")
    elif need_kalshi:
        attempted.append("kalshi")
        try:
            kalshi_order = kalshi_exit_position(ticker, kalshi_side, kalshi_contracts)
            _filled, kalshi_fill_price, _reference, _limit, _limit_text = kalshi_exit_summary(
                kalshi_order,
                kalshi_side,
            )
            position["kalshi_exited"] = True
            position["kalshi_exit_order"] = kalshi_order
            position["kalshi_exit_price"] = kalshi_fill_price
        except Exception as exc:
            errors.append(f"kalshi failed: {type(exc).__name__}: {exc}")

    if need_kalshi and not position.get("kalshi_exited") or need_poly and not position.get("polymarket_exited"):
        remaining = []
        if need_kalshi and not position.get("kalshi_exited"):
            remaining.append(f"Kalshi {ticker} {kalshi_side.upper()} size {kalshi_contracts}")
        if need_poly and not position.get("polymarket_exited"):
            remaining.append(
                f"Polymarket {polymarket_contract} size {poly_contracts} "
                f"{polymarket_token_ref(exit_polymarket_market, polymarket_contract)}"
            )
        done = []
        if position.get("kalshi_exited"):
            done.append(
                f"Kalshi {ticker} {kalshi_side.upper()} @ "
                f"{cli.fmt_display_cents(kalshi_fill_price)}c {kalshi_order_ref(kalshi_order)}"
            )
        if position.get("polymarket_exited"):
            done.append(
                f"Polymarket {polymarket_contract} @ {cli.fmt_display_cents(poly_fill_price)}c "
                f"{polymarket_order_ref(poly_response)}"
            )
        done_text = f"; exited {' and '.join(done)}" if done else ""
        attempted_text = f"attempted {', '.join(attempted) or 'none'}"
        error_text = "; ".join(errors) if errors else "no fill"
        return (
            f"EXIT PARTIAL {attempted_text}{done_text}; "
            f"remaining {' and '.join(remaining)}; will retry next tick; {error_text}"
        ), False

    parts = []
    exit_value = 0.0
    if not position.get("kalshi_absent"):
        if not isinstance(kalshi_order, dict):
            return "EXIT FAILED missing recorded Kalshi exit order details", False
        kalshi_filled, kalshi_fill_price, _kalshi_reference, _kalshi_limit, kalshi_limit_text = kalshi_exit_summary(
            kalshi_order,
            kalshi_side,
        )
        exit_value += as_float(kalshi_fill_price)
        parts.append(
            f"Kalshi {kalshi_side.upper()} {kalshi_filled:g} @ {cli.fmt_display_cents(kalshi_fill_price)}c "
            f"({kalshi_limit_text}; {kalshi_order_ref(kalshi_order)})"
        )
    if not position.get("polymarket_absent"):
        if not isinstance(poly_response, dict):
            return "EXIT FAILED missing recorded Polymarket exit order details", False
        poly_best_bid = finite_float(poly_response.get("best_bid"))
        poly_limit = finite_float(poly_response.get("limit_price"))
        exit_value += as_float(poly_fill_price)
        parts.append(
            f"Polymarket {polymarket_contract} {poly_contracts} @ {cli.fmt_display_cents(poly_fill_price)}c "
            f"({polymarket_token_ref(exit_polymarket_market, polymarket_contract)}; "
            f"bid {cli.fmt_display_cents(poly_best_bid)}c, limit {cli.fmt_display_cents(poly_limit)}c; "
            f"{polymarket_order_ref(poly_response)})"
        )
    exit_cost = as_float(position.get("entry_cost"))
    return (
        "EXITED "
        f"{'; '.join(parts)}; "
        f"liquidation {cli.fmt_display_cents(exit_value)}c - "
        f"entry {cli.fmt_display_cents(exit_cost)}c = "
        f"{cli.fmt_money(exit_value - exit_cost)} before exit fees"
    ), True


async def execute_position_exit_async(
    position: dict[str, Any],
    kalshi_snapshot: dict[str, Any],
    polymarket_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any],
    live: bool,
    market_context: AsyncMarketContext | None,
    chase_interval: float,
    chase_max_steps: int,
) -> tuple[str, bool]:
    if not live or market_context is None:
        return await asyncio.to_thread(
            execute_position_exit,
            position,
            kalshi_snapshot,
            polymarket_snapshot,
            polymarket_market,
            live,
        )
    if market_context.failsafe_required():
        cli.print_line(
            f"{btc.iso_utc()} | WEBSOCKET FAILSAFE live position cleanup via HTTP fallback"
        )
        return await asyncio.to_thread(
            execute_position_exit,
            position,
            kalshi_snapshot,
            polymarket_snapshot,
            polymarket_market,
            live,
        )

    contracts = int(position.get("contracts") or 0)
    if contracts <= 0:
        return f"EXIT FAILED invalid position size {position.get('contracts')}", False

    ticker = str(position.get("ticker") or kalshi_snapshot.get("ticker") or "")
    kalshi_side = str(position.get("kalshi_side") or "").lower()
    polymarket_contract = str(position.get("polymarket_contract") or "")
    if not ticker or kalshi_side not in ("yes", "no") or not polymarket_contract:
        return f"EXIT FAILED incomplete position {position}", False

    exit_polymarket_market = position.setdefault("polymarket_market", polymarket_market)
    position["exit_started"] = True
    errors: list[str] = []
    if position.get("kalshi_order_unknown"):
        reconcile_text = await asyncio.to_thread(reconcile_unknown_kalshi_order, position)
        if position.get("kalshi_order_unknown"):
            return f"EXIT WAIT {reconcile_text}", False
        if reconcile_text:
            errors.append(reconcile_text)

    poly_response = position.get("polymarket_exit_response")
    poly_fill_price = finite_float(position.get("polymarket_exit_price"))
    kalshi_order = position.get("kalshi_exit_order")
    kalshi_fill_price = finite_float(position.get("kalshi_exit_price"))
    poly_contracts = position_leg_contracts(position, "polymarket", contracts)
    kalshi_contracts = position_leg_contracts(position, "kalshi", contracts)
    need_poly = not position.get("polymarket_absent") and not position.get("polymarket_exited")
    need_kalshi = not position.get("kalshi_absent") and not position.get("kalshi_exited")
    attempted: list[str] = []

    if need_poly:
        attempted.append("polymarket")
        result = await walk_polymarket_exit(
            market_context,
            exit_polymarket_market,
            polymarket_contract,
            poly_contracts,
            chase_interval,
            chase_max_steps,
        )
        if result.filled:
            poly_response = result.order or {}
            poly_response["exit_method"] = "sell"
            poly_response["limit_price"] = result.fill_price
            poly_response.setdefault("best_bid", result.fill_price)
            poly_fill_price = result.fill_price
            position["polymarket_exited"] = True
            position["polymarket_exit_response"] = poly_response
            position["polymarket_exit_price"] = poly_fill_price
        else:
            errors.append(f"polymarket failed: {result.reason}")

    if need_kalshi and (not need_poly or position.get("polymarket_exited")):
        attempted.append("kalshi")
        result = await walk_kalshi_exit(
            market_context,
            ticker,
            kalshi_side,
            kalshi_contracts,
            chase_interval,
            chase_max_steps,
        )
        if result.filled:
            kalshi_order = result.order or {}
            kalshi_order["exit_method"] = "sell"
            kalshi_order["limit_price"] = result.fill_price
            kalshi_order.setdefault("best_bid", result.fill_price)
            kalshi_fill_price = result.fill_price
            position["kalshi_exited"] = True
            position["kalshi_exit_order"] = kalshi_order
            position["kalshi_exit_price"] = kalshi_fill_price
        else:
            errors.append(f"kalshi failed: {result.reason}")

    if (need_kalshi and not position.get("kalshi_exited")) or (
        need_poly and not position.get("polymarket_exited")
    ):
        remaining = []
        if need_kalshi and not position.get("kalshi_exited"):
            remaining.append(f"Kalshi {ticker} {kalshi_side.upper()} size {kalshi_contracts}")
        if need_poly and not position.get("polymarket_exited"):
            remaining.append(
                f"Polymarket {polymarket_contract} size {poly_contracts} "
                f"{polymarket_token_ref(exit_polymarket_market, polymarket_contract)}"
            )
        done = []
        if position.get("kalshi_exited"):
            done.append(
                f"Kalshi {ticker} {kalshi_side.upper()} @ "
                f"{cli.fmt_display_cents(kalshi_fill_price)}c {kalshi_order_ref(kalshi_order)}"
            )
        if position.get("polymarket_exited"):
            done.append(
                f"Polymarket {polymarket_contract} @ {cli.fmt_display_cents(poly_fill_price)}c "
                f"{polymarket_order_ref(poly_response)}"
            )
        done_text = f"; exited {' and '.join(done)}" if done else ""
        attempted_text = f"attempted {', '.join(attempted) or 'none'}"
        error_text = "; ".join(errors) if errors else "no fill"
        return (
            f"EXIT PARTIAL {attempted_text}{done_text}; "
            f"remaining {' and '.join(remaining)}; will retry next tick; {error_text}"
        ), False

    return await asyncio.to_thread(
        execute_position_exit,
        position,
        kalshi_snapshot,
        polymarket_snapshot,
        polymarket_market,
        live,
    )


async def walk_kalshi_exit(
    market_context: AsyncMarketContext,
    ticker: str,
    side: str,
    contracts: int,
    chase_interval: float,
    chase_max_steps: int,
) -> LimitWalkResult:
    async def best_price() -> float | None:
        _km, snapshot, _pm, _ps, _ss = await market_context.snapshot()
        if str(snapshot.get("ticker") or "") == ticker:
            return finite_float(snapshot.get(f"{side}_bid"))
        return await asyncio.to_thread(kalshi_current_bid, ticker, side)

    async def place(price: float) -> dict[str, Any]:
        return await asyncio.to_thread(
            kalshi_post_order,
            ticker,
            side,
            price,
            contracts,
            f"btc15-walk-k-{uuid.uuid4().hex[:16]}",
            "sell",
            "good_till_canceled",
        )

    async def cancel(order: dict[str, Any]) -> None:
        order_id = order.get("order_id")
        if order_id:
            await asyncio.to_thread(kalshi_cancel_order, str(order_id))

    async def is_filled(order: dict[str, Any], expected_price: float) -> tuple[bool, float]:
        order_id = order.get("order_id")
        verified = await asyncio.to_thread(kalshi_get_order, str(order_id)) if order_id else order
        filled = fill_count(verified)
        if filled > 0:
            verified["exit_method"] = "sell"
            verified["best_bid"] = expected_price
            verified["limit_price"] = expected_price
            order.update(verified)
            return True, filled_price(verified, side, action="sell") or expected_price
        return False, expected_price

    walker = LimitWalker(
        venue="Kalshi",
        side="sell",
        contracts=contracts,
        tick_size=0.01,
        chase_interval=chase_interval,
        max_steps=chase_max_steps,
        logger=cli.print_line,
        best_price=best_price,
        place_order=place,
        cancel_order=cancel,
        is_filled=is_filled,
    )
    return await walker.run()


async def walk_polymarket_exit(
    market_context: AsyncMarketContext,
    market: dict[str, Any],
    contract: str,
    contracts: int,
    chase_interval: float,
    chase_max_steps: int,
) -> LimitWalkResult:
    from py_clob_client_v2 import Side

    token_id = token_ids_by_contract(market)[contract]

    async def best_price() -> float | None:
        _km, _ks, _pm, snapshot, _ss = await market_context.snapshot()
        return finite_float(snapshot.get(f"{contract.lower()}_bid"))

    async def place(price: float) -> dict[str, Any]:
        return await asyncio.to_thread(
            polymarket_post_order,
            market,
            contract,
            price,
            contracts,
            Side.SELL,
            "GTC",
        )

    async def cancel(order: dict[str, Any]) -> None:
        order_id = response_order_id(order)
        if order_id != "--":
            await asyncio.to_thread(polymarket_cancel_order, order_id)

    async def is_filled(order: dict[str, Any], expected_price: float) -> tuple[bool, float]:
        order_id = response_order_id(order)
        if order_id != "--":
            verified = await asyncio.to_thread(polymarket_get_order, order_id)
            order["verified_order"] = verified
        filled, fill_price = polymarket_fill_summary(order, expected_price)
        return filled, fill_price

    last_balance_error: Exception | None = None
    for attempt in range(POLYMARKET_SELL_BALANCE_ATTEMPTS):
        try:
            balance, allowance = await asyncio.to_thread(polymarket_conditional_balance_amounts, token_id)
            if balance >= contracts and allowance >= contracts:
                break
            last_balance_error = RuntimeError(
                "Polymarket conditional balance/allowance below cleanup size: "
                f"balance {balance:g}, allowance {allowance:g}, need {contracts:g}"
            )
        except Exception as exc:
            last_balance_error = exc
            break
        await asyncio.sleep(POLYMARKET_SELL_BALANCE_DELAY_SECONDS)
    else:
        return LimitWalkResult(False, reason=str(last_balance_error))
    if last_balance_error is not None and attempt == 0:
        return LimitWalkResult(False, reason=str(last_balance_error))

    walker = LimitWalker(
        venue="Polymarket",
        side="sell",
        contracts=contracts,
        tick_size=0.01,
        chase_interval=chase_interval,
        max_steps=chase_max_steps,
        logger=cli.print_line,
        best_price=best_price,
        place_order=place,
        cancel_order=cancel,
        is_filled=is_filled,
    )
    return await walker.run()


def trade_preflight(
    kalshi_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any],
    arbitrage: dict[str, Any],
    max_contracts: int,
    min_adjusted_profit: float,
    kalshi_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kalshi_side = arbitrage["kalshi_contract"].lower()
    polymarket_contract = arbitrage["polymarket_contract"]
    kalshi_price = arbitrage["kalshi_price"]
    fallback_poly_price = polymarket_execution_price(arbitrage["polymarket_price"])
    if kalshi_plan is not None:
        poly_price, poly_liquidity, poly_plan = polymarket_execution_plan(
            polymarket_market,
            polymarket_contract,
            max_contracts,
        )
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            kalshi_plan_future = executor.submit(
                kalshi_liquidity_plan_for_buy,
                str(kalshi_snapshot["ticker"]),
                kalshi_side,
                kalshi_price,
            )
            poly_plan_future = executor.submit(
                polymarket_execution_plan,
                polymarket_market,
                polymarket_contract,
                max_contracts,
            )
            kalshi_plan = kalshi_plan_future.result()
            poly_price, poly_liquidity, poly_plan = poly_plan_future.result()
    if kalshi_plan is None:
        raise RuntimeError("Kalshi preflight plan unavailable")
    kalshi_liquidity = as_float(kalshi_plan.get("liquidity"))
    executable_contracts = min(max_contracts, int(kalshi_liquidity), int(poly_liquidity))
    if executable_contracts > 0 and executable_contracts < max_contracts:
        poly_price, poly_liquidity, poly_plan = polymarket_execution_plan(
            polymarket_market,
            polymarket_contract,
            executable_contracts,
        )
    poly_order_price = poly_price or fallback_poly_price
    polymarket_notional = poly_order_price * executable_contracts
    total_cost = kalshi_price + poly_order_price
    adjusted_profit = (1.0 / total_cost) - 1.0 if 0 < total_cost < 1 else 0.0
    vwap_price = poly_plan.get("vwap_price")
    vwap_total_cost = kalshi_price + vwap_price if isinstance(vwap_price, float) else None
    vwap_profit = (
        (1.0 / vwap_total_cost) - 1.0
        if isinstance(vwap_total_cost, float) and 0 < vwap_total_cost < 1
        else None
    )

    decision = "PLACE"
    reason = f"sizing {executable_contracts:g}/{max_contracts:g} contracts"
    if executable_contracts <= 0:
        decision = "SKIP"
        if kalshi_liquidity < 1:
            reason = f"Kalshi liquidity {kalshi_liquidity:g} < 1"
        else:
            reason = f"Polymarket liquidity {poly_liquidity:g} < 1"
    elif poly_price is None:
        decision = "SKIP"
        reason = f"Polymarket liquidity {poly_liquidity:g} < {executable_contracts}"
    elif polymarket_notional < POLYMARKET_MIN_ORDER_NOTIONAL:
        decision = "SKIP"
        reason = (
            f"Polymarket notional {cli.fmt_money(polymarket_notional)} "
            f"< {cli.fmt_money(POLYMARKET_MIN_ORDER_NOTIONAL)} minimum"
        )
    elif adjusted_profit <= min_adjusted_profit:
        decision = "SKIP"
        reason = (
            f"adjusted profit {cli.fmt_money(adjusted_profit)} "
            f"<= {cli.fmt_money(min_adjusted_profit)}"
        )

    return {
        "decision": decision,
        "reason": reason,
        "contracts": executable_contracts,
        "max_contracts": max_contracts,
        "kalshi_side": kalshi_side,
        "kalshi_price": kalshi_price,
        "kalshi_liquidity": kalshi_liquidity,
        "kalshi_plan_source": kalshi_plan.get("source") or "http",
        "polymarket_contract": polymarket_contract,
        "polymarket_price": poly_order_price,
        "polymarket_notional": polymarket_notional,
        "polymarket_liquidity": poly_liquidity,
        "polymarket_vwap_price": vwap_price,
        "polymarket_vwap_profit": vwap_profit,
        "adjusted_profit": adjusted_profit,
        "kalshi_plan": kalshi_plan,
        "polymarket_plan": poly_plan,
    }


async def trade_preflight_async(
    kalshi_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any],
    arbitrage: dict[str, Any],
    max_contracts: int,
    min_adjusted_profit: float,
    market_context: AsyncMarketContext | None,
) -> dict[str, Any]:
    kalshi_plan = None
    if market_context is not None:
        try:
            kalshi_plan = await market_context.kalshi_liquidity_plan_for_buy(
                str(kalshi_snapshot["ticker"]),
                arbitrage["kalshi_contract"].lower(),
                arbitrage["kalshi_price"],
            )
        except Exception as exc:
            cli.print_line(
                f"{btc.iso_utc()} | WEBSOCKET Kalshi local preflight unavailable: "
                f"{type(exc).__name__}: {exc}"
            )
    if kalshi_plan is None:
        return await asyncio.to_thread(
            trade_preflight,
            kalshi_snapshot,
            polymarket_market,
            arbitrage,
            max_contracts,
            min_adjusted_profit,
        )
    return await asyncio.to_thread(
        trade_preflight,
        kalshi_snapshot,
        polymarket_market,
        arbitrage,
        max_contracts,
        min_adjusted_profit,
        kalshi_plan,
    )


def format_preflight(preflight: dict[str, Any]) -> str:
    return (
        f"CHECK {preflight['decision']} {preflight['reason']} | "
        f"size {preflight['contracts']:g}/{preflight['max_contracts']:g} | "
        f"K {preflight['kalshi_side'].upper()} @ {cli.fmt_display_cents(preflight['kalshi_price'])}c "
        f"liq {preflight['kalshi_liquidity']:g} ({preflight.get('kalshi_plan_source') or 'http'}) | "
        f"P {preflight['polymarket_contract']} @ {cli.fmt_display_cents(preflight['polymarket_price'])}c "
        f"liq {preflight['polymarket_liquidity']:g} | "
        f"adj profit {cli.fmt_money(preflight['adjusted_profit'])}"
    )


def format_depth_level(price: float, quantity: float, cumulative: Any | None = None) -> str:
    text = f"{cli.fmt_display_cents(price)}c x {quantity:g}"
    if isinstance(cumulative, (int, float)):
        text += f" cum {cumulative:g}"
    return text


def format_orderbook_debug(preflight: dict[str, Any], contracts: int, max_levels: int) -> list[str]:
    kalshi_plan = preflight["kalshi_plan"]
    poly_plan = preflight["polymarket_plan"]
    kalshi_side = preflight["kalshi_side"].upper()
    poly_contract = preflight["polymarket_contract"]
    max_levels = max(1, max_levels)

    kalshi_levels = [
        level for level in kalshi_plan.get("levels", []) if level.get("executable")
    ][:max_levels]
    if kalshi_levels:
        kalshi_text = ", ".join(
            format_depth_level(
                as_float(level.get("buy_price")),
                as_float(level.get("quantity")),
                level.get("cumulative"),
            )
            for level in kalshi_levels
        )
    else:
        kalshi_text = "none"

    poly_levels = poly_plan.get("levels", [])[:max_levels]
    if poly_levels:
        poly_text = ", ".join(
            (
                format_depth_level(
                    as_float(level.get("price")),
                    as_float(level.get("quantity")),
                    level.get("cumulative"),
                )
                + (" selected" if level.get("selected") else "")
            )
            for level in poly_levels
        )
    else:
        poly_text = "none"

    vwap_price = preflight.get("polymarket_vwap_price")
    vwap_profit = preflight.get("polymarket_vwap_profit")
    vwap_text = (
        f"book VWAP {cli.fmt_display_cents(vwap_price)}c profit {cli.fmt_money(vwap_profit)}"
        if isinstance(vwap_price, float) and isinstance(vwap_profit, float)
        else "book VWAP --"
    )

    return [
        (
            f"BOOK K {kalshi_side} buy limit {cli.fmt_display_cents(preflight['kalshi_price'])}c "
            f"source {kalshi_plan.get('source') or 'http'}; "
            f"needs opposite bid >= {cli.fmt_display_cents(kalshi_plan['min_opposite_bid'])}c; "
            f"executable {kalshi_plan['liquidity']:g}; levels {kalshi_text}"
        ),
        (
            f"BOOK P {poly_contract} buy size {preflight['contracts']:g}/{contracts:g}; "
            f"asks {poly_text}; selected limit "
            f"{cli.fmt_display_cents(preflight['polymarket_price'])}c; "
            f"available {preflight['polymarket_liquidity']:g}"
        ),
        (
            f"BOOK adjusted limit cost "
            f"{cli.fmt_display_cents(preflight['kalshi_price'])}c + "
            f"{cli.fmt_display_cents(preflight['polymarket_price'])}c = "
            f"{cli.fmt_display_cents(preflight['kalshi_price'] + preflight['polymarket_price'])}c; "
            f"limit profit {cli.fmt_money(preflight['adjusted_profit'])}; {vwap_text}"
        ),
    ]


def partial_entry_position(
    kalshi_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any],
    kalshi_side: str,
    polymarket_contract: str,
    poly_fill_price: float | None,
    kalshi_fill_price: float | None,
    trade_contracts: int,
    kalshi_contracts: int = 0,
    polymarket_contracts: int | None = None,
    kalshi_order_unknown: bool = False,
    kalshi_order: dict[str, Any] | None = None,
) -> dict[str, Any]:
    poly_contracts = trade_contracts if polymarket_contracts is None else polymarket_contracts
    entry_cost = 0.0
    if poly_contracts > 0:
        entry_cost += as_float(poly_fill_price)
    if kalshi_contracts > 0:
        entry_cost += as_float(kalshi_fill_price)
    return {
        "ticker": kalshi_snapshot.get("ticker"),
        "close_time": kalshi_snapshot.get("close_time"),
        "kalshi_side": kalshi_side,
        "polymarket_contract": polymarket_contract,
        "entry_cost": entry_cost,
        "entry_time": kalshi_snapshot.get("timestamp_utc"),
        "contracts": max(kalshi_contracts, poly_contracts, trade_contracts),
        "kalshi_contracts": kalshi_contracts,
        "polymarket_contracts": poly_contracts,
        "kalshi_absent": kalshi_contracts <= 0,
        "polymarket_absent": poly_contracts <= 0,
        "kalshi_exited": kalshi_contracts <= 0,
        "polymarket_exited": poly_contracts <= 0,
        "polymarket_market": polymarket_market,
        "exit_started": True,
        "boundary_review_logged": False,
        "kalshi_order_unknown": kalshi_order_unknown,
        "kalshi_order_id": (kalshi_order or {}).get("order_id"),
        "kalshi_client_order_id": (kalshi_order or {}).get("client_order_id"),
        "kalshi_unknown_since": time.time() if kalshi_order_unknown else None,
    }


def partial_entry_error(
    message: str,
    kalshi_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any],
    kalshi_side: str,
    polymarket_contract: str,
    poly_fill_price: float | None,
    kalshi_fill_price: float | None,
    trade_contracts: int,
    kalshi_contracts: int = 0,
    polymarket_contracts: int | None = None,
    kalshi_order_unknown: bool = False,
    kalshi_order: dict[str, Any] | None = None,
) -> PartialEntryError:
    return PartialEntryError(
        message,
        partial_entry_position(
            kalshi_snapshot,
            polymarket_market,
            kalshi_side,
            polymarket_contract,
            poly_fill_price,
            kalshi_fill_price,
            trade_contracts,
            kalshi_contracts,
            polymarket_contracts,
            kalshi_order_unknown,
            kalshi_order,
        ),
    )


def cleanup_partial_entry(
    message: str,
    position: dict[str, Any],
    kalshi_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any],
) -> str:
    exit_result, exit_complete = execute_position_exit(
        position,
        kalshi_snapshot,
        {},
        polymarket_market,
        True,
    )
    if not exit_complete:
        raise PartialEntryError(f"{message}; {exit_result}", position)
    return f"{message}; {exit_result}"


def cleanup_kalshi_first_partial_entry(
    message: str,
    kalshi_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any],
    kalshi_side: str,
    polymarket_contract: str,
    kalshi_fill_price: float | None,
    kalshi_contracts: int,
    kalshi_order: dict[str, Any],
) -> str:
    position = partial_entry_position(
        kalshi_snapshot,
        polymarket_market,
        kalshi_side,
        polymarket_contract,
        None,
        kalshi_fill_price,
        kalshi_contracts,
        kalshi_contracts,
        0,
        False,
        kalshi_order,
    )
    return cleanup_partial_entry(message, position, kalshi_snapshot, polymarket_market)


def execute_arbitrage(
    kalshi_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any],
    arbitrage: dict[str, Any],
    contracts: int,
    min_adjusted_profit: float,
    live: bool,
    preflight: dict[str, Any] | None = None,
    live_recheck_preflight: dict[str, Any] | None = None,
) -> str:
    kalshi_side = arbitrage["kalshi_contract"].lower()
    polymarket_contract = arbitrage["polymarket_contract"]
    kalshi_price = arbitrage["kalshi_price"]
    polymarket_price = arbitrage["polymarket_price"]
    if preflight is None:
        preflight = trade_preflight(
            kalshi_snapshot,
            polymarket_market,
            arbitrage,
            contracts,
            min_adjusted_profit,
        )
    poly_order_price = preflight["polymarket_price"]
    trade_contracts = int(preflight["contracts"])
    if not live:
        if preflight["decision"] != "PLACE":
            return f"DRY RUN would skip: {preflight['reason']}"
        return (
            "DRY RUN would place Kalshi first, then Polymarket after fill verification: "
            f"Kalshi BUY {kalshi_snapshot.get('ticker')} {kalshi_side.upper()} size {trade_contracts} "
            f"limit {cli.fmt_display_cents(kalshi_price)}c; then "
            f"Polymarket BUY {polymarket_contract} size {trade_contracts} "
            f"{polymarket_token_ref(polymarket_market, polymarket_contract)} "
            f"limit {cli.fmt_display_cents(poly_order_price)}c "
            f"({preflight['reason']})"
        )

    if preflight["decision"] != "PLACE":
        return (
            f"SKIP {preflight['reason']}"
        )

    live_preflight = (
        live_recheck_preflight
        if live_recheck_preflight is not None
        else trade_preflight(
            kalshi_snapshot,
            polymarket_market,
            arbitrage,
            contracts,
            min_adjusted_profit,
        )
    )
    if live_preflight["decision"] != "PLACE":
        return f"SKIP live recheck: {live_preflight['reason']}"
    trade_contracts = int(live_preflight["contracts"])

    client_order_id = f"btc15-arb-{uuid.uuid4().hex[:20]}"
    try:
        kalshi_order = kalshi_post_order(
            str(kalshi_snapshot["ticker"]),
            kalshi_side,
            kalshi_price,
            trade_contracts,
            client_order_id,
        )
    except KalshiOrderStateUnknown as exc:
        position = partial_entry_position(
            kalshi_snapshot,
            polymarket_market,
            kalshi_side,
            polymarket_contract,
            None,
            None,
            trade_contracts,
            0,
            0,
            True,
            exc.order,
        )
        raise PartialEntryError(
            "PARTIAL ENTRY Kalshi-first order state unknown before Polymarket; "
            f"Kalshi BUY {kalshi_snapshot.get('ticker')} {kalshi_side.upper()} "
            f"client {client_order_id} @ {cli.fmt_display_cents(kalshi_price)}c failed verification: {exc}; "
            "will reconcile before cleanup",
            position,
        ) from exc
    except Exception as exc:
        return (
            "SKIP Kalshi-first entry failed before Polymarket placement; "
            f"Kalshi BUY {kalshi_snapshot.get('ticker')} {kalshi_side.upper()} "
            f"client {client_order_id} @ {cli.fmt_display_cents(kalshi_price)}c failed: "
            f"{type(exc).__name__}: {exc}"
        )

    kalshi_filled = int(fill_count(kalshi_order))
    kalshi_fill_price = filled_price(kalshi_order, kalshi_side) if kalshi_filled > 0 else None
    kalshi_ref = (
        f"filled {kalshi_filled:g}/{trade_contracts:g} @ {cli.fmt_display_cents(kalshi_fill_price)}c "
        f"{kalshi_order_ref(kalshi_order)}"
        if kalshi_filled > 0
        else f"{kalshi_order_ref(kalshi_order)}"
    )
    if kalshi_filled <= 0:
        return (
            "SKIP Kalshi-first entry had no fill before Polymarket placement; "
            f"Kalshi BUY {kalshi_snapshot.get('ticker')} {kalshi_side.upper()} {kalshi_ref}"
        )
    if kalshi_filled != trade_contracts:
        message = (
            "SKIP Kalshi-first partial fill before Polymarket placement; "
            f"Kalshi BUY {kalshi_snapshot.get('ticker')} {kalshi_side.upper()} {kalshi_ref}; "
            "cleaning up instead of hedging mismatched size"
        )
        return cleanup_kalshi_first_partial_entry(
            message,
            kalshi_snapshot,
            polymarket_market,
            kalshi_side,
            polymarket_contract,
            kalshi_fill_price,
            kalshi_filled,
            kalshi_order,
        )

    poly_order_price, poly_liquidity, _poly_plan = polymarket_execution_plan(
        polymarket_market,
        polymarket_contract,
        kalshi_filled,
    )
    poly_notional = as_float(poly_order_price) * kalshi_filled if poly_order_price is not None else 0.0
    total_cost = as_float(kalshi_fill_price) + as_float(poly_order_price)
    adjusted_profit = (1.0 / total_cost) - 1.0 if 0 < total_cost < 1 else 0.0
    if (
        poly_order_price is None
        or poly_liquidity < kalshi_filled
        or poly_notional < POLYMARKET_MIN_ORDER_NOTIONAL
        or adjusted_profit <= min_adjusted_profit
    ):
        if poly_order_price is None or poly_liquidity < kalshi_filled:
            reason = f"Polymarket liquidity {poly_liquidity:g} < {kalshi_filled:g}"
        elif poly_notional < POLYMARKET_MIN_ORDER_NOTIONAL:
            reason = (
                f"Polymarket notional {cli.fmt_money(poly_notional)} "
                f"< {cli.fmt_money(POLYMARKET_MIN_ORDER_NOTIONAL)} minimum"
            )
        else:
            reason = (
                f"rechecked adjusted profit {cli.fmt_money(adjusted_profit)} "
                f"<= {cli.fmt_money(min_adjusted_profit)}"
            )
        message = (
            "SKIP Kalshi-first entry aborted before Polymarket placement; "
            f"{reason}; Kalshi BUY {kalshi_snapshot.get('ticker')} {kalshi_side.upper()} {kalshi_ref}"
        )
        return cleanup_kalshi_first_partial_entry(
            message,
            kalshi_snapshot,
            polymarket_market,
            kalshi_side,
            polymarket_contract,
            kalshi_fill_price,
            kalshi_filled,
            kalshi_order,
        )

    poly_response = None
    poly_exception = None
    try:
        poly_response = polymarket_post_order(
            polymarket_market,
            polymarket_contract,
            poly_order_price,
            kalshi_filled,
        )
    except Exception as exc:
        poly_exception = exc

    poly_filled = False
    poly_fill_price = None
    if poly_response is not None:
        poly_filled, poly_fill_price = polymarket_fill_summary(poly_response, poly_order_price)

    poly_ref = (
        f"fill @ {cli.fmt_display_cents(poly_fill_price)}c {polymarket_order_ref(poly_response)}"
        if poly_filled
        else (
            f"failed: {type(poly_exception).__name__}: {poly_exception}"
            if poly_exception is not None
            else f"not verified filled; {polymarket_order_ref(poly_response)}; response={poly_response}"
        )
    )

    if poly_filled:
        return TradeResult(
            (
                "TRADED Kalshi-first "
                f"Kalshi BUY {kalshi_snapshot.get('ticker')} {kalshi_side.upper()} "
                f"size {kalshi_filled:g} {kalshi_ref} "
                f"(limit {cli.fmt_display_cents(kalshi_price)}c); "
                f"Polymarket BUY {polymarket_contract} size {kalshi_filled:g} "
                f"{polymarket_token_ref(polymarket_market, polymarket_contract)} "
                f"{poly_ref} (limit {cli.fmt_display_cents(poly_order_price)}c); "
                f"entry {cli.fmt_display_cents(as_float(poly_fill_price) + as_float(kalshi_fill_price))}c"
            ),
            entry_cost=as_float(poly_fill_price) + as_float(kalshi_fill_price),
            kalshi_fill_price=kalshi_fill_price,
            polymarket_fill_price=poly_fill_price,
            contracts=int(kalshi_filled),
        )

    message = (
        "SKIP Kalshi-first entry incomplete; "
        f"Kalshi BUY {kalshi_snapshot.get('ticker')} {kalshi_side.upper()} {kalshi_ref}; "
        f"Polymarket BUY {polymarket_contract} "
        f"{polymarket_token_ref(polymarket_market, polymarket_contract)} {poly_ref}"
    )
    return cleanup_kalshi_first_partial_entry(
        message,
        kalshi_snapshot,
        polymarket_market,
        kalshi_side,
        polymarket_contract,
        kalshi_fill_price,
        kalshi_filled,
        kalshi_order,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print Kalshi/Polymarket BTC 15m arbitrage and optionally execute live trades."
    )
    parser.add_argument("--interval", type=float, default=btc.POLL_SECONDS)
    parser.add_argument(
        "--log-interval",
        type=float,
        default=10.0,
        help=(
            "Seconds between routine market snapshot log lines. "
            "Arbitrage/trade signals still log immediately. Default: 10."
        ),
    )
    parser.add_argument("--csv-dir", type=Path, default=btc.DATA_DIR)
    parser.add_argument("--flush-every", type=int, default=1)
    parser.add_argument(
        "--min-profit",
        type=float,
        default=0.0,
        help="Minimum raw displayed profit that triggers a preflight check.",
    )
    parser.add_argument(
        "--min-adjusted-profit",
        type=float,
        default=0.02,
        help="Minimum executable adjusted profit required before live or dry-run trade placement. Default: 0.02.",
    )
    parser.add_argument(
        "--min-profit-after-fees",
        type=float,
        default=0.05,
        help="Minimum per-contract profit after 2%% winner-payout fee. Default: 0.05.",
    )
    parser.add_argument(
        "--source-gap-threshold",
        type=float,
        default=100.0,
        help="Maximum absolute BTC source gap allowed for entry/hold. Default: 100.",
    )
    parser.add_argument(
        "--target-divergence-threshold",
        type=float,
        default=35.0,
        help="Maximum absolute Kalshi/Polymarket BTC target divergence. Default: 35.",
    )
    parser.add_argument(
        "--hold-distance-multiplier",
        type=float,
        default=0.25,
        help="Hold distance multiplier applied to max(10, seconds_to_expiry * 0.05). Default: 0.25.",
    )
    parser.add_argument(
        "--take-profit-exit-value",
        type=float,
        default=1.04,
        help="Exit an open matched position when executable liquidation value is at least this total. Default: 1.04.",
    )
    parser.add_argument(
        "--profit-capture-min-edge",
        type=float,
        default=PROFIT_CAPTURE_MIN_EDGE,
        help="Exit an open position when executable liquidation value exceeds entry by this amount. Default: 0.07.",
    )
    parser.add_argument(
        "--exit-cushion",
        type=float,
        default=EXIT_CUSHION,
        help="Minimum non-emergency executable exit edge over entry. Default: 0.03.",
    )
    parser.add_argument("--contracts", type=int, default=1, help="Maximum matched contracts/shares per leg.")
    parser.add_argument("--max-trades", type=int, default=1, help="Maximum live arbitrage executions.")
    parser.add_argument("--live", action="store_true", help="Actually submit orders. Omit for dry-run.")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit.")
    parser.add_argument(
        "--print-arb-orderbook",
        action="store_true",
        help="On profitable raw arbitrage, print book depth used to compute liquidity and adjusted profit.",
    )
    parser.add_argument(
        "--book-depth-levels",
        type=int,
        default=6,
        help="Maximum executable/book levels to print with --print-arb-orderbook.",
    )
    parser.add_argument(
        "--disable-websocket",
        action="store_true",
        help="Use the legacy HTTP polling loop instead of websocket-driven updates.",
    )
    parser.add_argument(
        "--ws-report-interval",
        type=float,
        default=WEBSOCKET_REPORT_INTERVAL,
        help="Print/refresh websocket state at least this often while waiting for market events. Default: 2.",
    )
    parser.add_argument(
        "--ws-stale-seconds",
        type=float,
        default=WEBSOCKET_STALE_SECONDS,
        help="Treat websocket books as stale after this many seconds and use HTTP cleanup for live exits.",
    )
    parser.add_argument(
        "--chase-interval",
        type=float,
        default=CHASE_INTERVAL,
        help="Seconds to wait for a passive exit fill before canceling and walking the limit. Default: 2.",
    )
    parser.add_argument(
        "--chase-max-steps",
        type=int,
        default=CHASE_MAX_STEPS,
        help="Maximum cancel/replace attempts for a walking limit exit. Default: 6.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    interval = max(0.1, args.interval)
    log_interval = max(0.0, args.log_interval)
    sma_window_size = kalshi_sma_window_size(interval)
    kalshi_brtis: deque[float] = deque(maxlen=sma_window_size)
    flush_every = max(1, args.flush_every)
    contracts = max(1, args.contracts)
    max_trades = max(0, args.max_trades)
    min_adjusted_profit = max(0.0, args.min_adjusted_profit)
    min_profit_after_fees = max(0.0, args.min_profit_after_fees)
    source_gap_threshold = max(0.0, args.source_gap_threshold)
    target_divergence_threshold = max(0.0, args.target_divergence_threshold)
    hold_distance_multiplier = max(0.0, args.hold_distance_multiplier)
    take_profit_exit_value = max(0.0, args.take_profit_exit_value)
    profit_capture_min_edge = max(0.0, args.profit_capture_min_edge)
    exit_cushion = max(0.0, args.exit_cushion)
    chase_interval = max(0.1, args.chase_interval)
    chase_max_steps = max(1, args.chase_max_steps)
    market_context: AsyncMarketContext | None = None
    pending_rows: dict[Path, list[dict[str, Any]]] = {}
    trades_done = 0
    open_position: dict[str, Any] | None = None
    last_contract_key: str | None = None
    last_snapshot_log_at = 0.0
    contract_cooldowns: dict[str, tuple[float, str]] = {}

    mode = "LIVE TRADING" if args.live else "DRY RUN"
    print_startup_banner()
    cli.print_line(
        f"{mode}; raw threshold > {cli.fmt_money(args.min_profit)}; "
        f"adjusted threshold > {cli.fmt_money(min_adjusted_profit)}; contracts={contracts}; "
        f"min profit after fees >= {cli.fmt_money(min_profit_after_fees)}; "
        f"source gap <= {source_gap_threshold:g}; target divergence <= {target_divergence_threshold:g}; "
        f"hold distance {hold_distance_multiplier:g}x; "
        f"take-profit exit >= {cli.fmt_display_cents(take_profit_exit_value)}c; "
        f"profit capture edge >= {cli.fmt_money(profit_capture_min_edge)}; "
        f"exit cushion >= {cli.fmt_money(exit_cushion)}; "
        f"max_trades={max_trades}; "
        f"{'HTTP polling' if args.disable_websocket else 'websocket event loop'} every {interval:g}s; "
        f"routine snapshot log every {log_interval:g}s",
    )
    print_startup_balances()
    if not args.disable_websocket:
        market_context = AsyncMarketContext(
            fetch_market_state,
            logger=cli.print_line,
            report_interval=max(0.25, args.ws_report_interval),
            stale_seconds=max(1.0, args.ws_stale_seconds),
        )
        await market_context.start()
    while True:
        started = time.monotonic()
        try:
            if market_context is not None:
                (
                    _kalshi_market,
                    kalshi_snapshot,
                    polymarket_market,
                    polymarket_snapshot,
                    source_snapshot,
                ) = await market_context.wait_for_update(timeout=max(0.25, args.ws_report_interval))
            else:
                (
                    _kalshi_market,
                    kalshi_snapshot,
                    polymarket_market,
                    polymarket_snapshot,
                    source_snapshot,
                ) = await asyncio.to_thread(fetch_market_state)
            arbitrage = cli.best_arbitrage(kalshi_snapshot, polymarket_snapshot)
            csv_path = cli.csv_path_for_contract(args.csv_dir, kalshi_snapshot)
            rows = pending_rows.setdefault(csv_path, [])
            rows.append(cli.csv_row(kalshi_snapshot, polymarket_snapshot, arbitrage))

            display_time = cli.fmt_display_time(kalshi_snapshot.get("timestamp_utc"))
            contract_key = str(kalshi_snapshot.get("ticker") or "")
            new_contract = bool(contract_key and contract_key != last_contract_key)
            if new_contract:
                kalshi_brtis.clear()
            ref_suffix, kalshi_direction_price = reference_delta_suffix(
                source_snapshot,
                kalshi_brtis,
                sma_window_size,
            )
            if open_position is not None:
                position_ticker = str(open_position.get("ticker") or "")
                if contract_key == position_ticker:
                    record_position_state(
                        open_position,
                        kalshi_snapshot,
                        polymarket_snapshot,
                        source_snapshot,
                    )
                pending_clear_reason = pending_exit_clear_reason(open_position, contract_key)
                if pending_clear_reason is not None:
                    cli.print_line(
                        f"{display_time:<10} | "
                        f"{format_pending_exit_clear(open_position, pending_clear_reason)}"
                    )
                    mark_contract_cooldown(
                        contract_cooldowns,
                        position_ticker or contract_key,
                        "pending exit cleared after expiry",
                    )
                    open_position = None
                elif (
                    contract_key
                    and position_ticker
                    and contract_key != position_ticker
                    and not position_has_pending_exit(open_position)
                ):
                    if not args.live:
                        cli.print_line(
                            f"{display_time:<10} | "
                            f"{format_dry_settlement(open_position, f'current contract is {contract_key}')}"
                        )
                    cli.print_line(
                        f"{display_time:<10} | "
                        f"{format_position_clear(open_position, f'current contract is {contract_key}')}"
                    )
                    open_position = None
                elif position_expired(open_position) and not position_has_pending_exit(open_position):
                    if not args.live:
                        cli.print_line(
                            f"{display_time:<10} | "
                            f"{format_dry_settlement(open_position, 'position contract reached expiry')}"
                        )
                    cli.print_line(
                        f"{display_time:<10} | "
                        f"{format_position_clear(open_position, 'position contract reached expiry')}"
                    )
                    open_position = None
            boundary_reason = no_trade_boundary_reason(kalshi_snapshot)
            if new_contract:
                last_contract_key = contract_key
                cli.trim_log_file(cli.TRADER_LOG_PATH, cli.TRADER_LOG_MAX_LINES - 2)
                cli.print_line(f"{display_time:<10} | {combined_balance_line()}")
                cli.print_line(f"{display_time:<10} | {format_contract_start(kalshi_snapshot, polymarket_snapshot, source_snapshot)}")

            now_monotonic = time.monotonic()
            snapshot_log_due = (
                args.once
                or new_contract
                or log_interval <= 0
                or now_monotonic - last_snapshot_log_at >= log_interval
            )
            snapshot_printed = False
            if snapshot_log_due:
                cli.print_snapshot(kalshi_snapshot, polymarket_snapshot, arbitrage, ref_suffix)
                snapshot_printed = True
                last_snapshot_log_at = now_monotonic

            if open_position is not None:
                if position_has_pending_exit(open_position):
                    exit_result, exit_complete = await execute_position_exit_async(
                        open_position,
                        kalshi_snapshot,
                        polymarket_snapshot,
                        polymarket_market,
                        args.live,
                        market_context,
                        chase_interval,
                        chase_max_steps,
                    )
                    cli.print_line(f"{display_time:<10} | {exit_result}")
                    if exit_complete:
                        mark_contract_cooldown(
                            contract_cooldowns,
                            position_ticker or contract_key,
                            "post-cleanup safety pause",
                        )
                        open_position = None
                elif boundary_reason is not None:
                    if not open_position.get("boundary_review_logged"):
                        liquidation, _liquidation_plans = executable_liquidation_value(
                            open_position,
                            kalshi_snapshot,
                            polymarket_snapshot,
                            polymarket_market,
                        )
                        cli.print_line(
                            f"{display_time:<10} | "
                            f"{format_position_review(open_position, boundary_reason, liquidation)}"
                        )
                        open_position["boundary_review_logged"] = True
                    hold_metrics = source_filter_metrics(
                        kalshi_snapshot,
                        polymarket_snapshot,
                        source_snapshot,
                        open_position["entry_cost"],
                        kalshi_direction_price,
                    )
                    hold_decision = evaluate_hold_filter(
                        hold_metrics,
                        source_gap_threshold,
                        target_divergence_threshold,
                        hold_distance_multiplier,
                    )
                    state_metrics = position_state_metrics(
                        open_position,
                        kalshi_snapshot,
                        polymarket_snapshot,
                        source_snapshot,
                        polymarket_market,
                    )
                    open_position["last_state_metrics"] = state_metrics
                    open_position["last_state_time"] = (
                        kalshi_snapshot.get("timestamp_utc") or source_snapshot.get("timestamp_utc")
                    )
                    strategy_decision = exit_strategy_decision(
                        open_position,
                        state_metrics,
                        hold_decision,
                        take_profit_exit_value,
                        profit_capture_min_edge,
                        exit_cushion,
                    )
                    should_log_hold = (
                        arbitrage
                        or not hold_decision["passed"]
                        or strategy_decision["action"] != "HOLD"
                        or state_metrics.get("held_winners") != 1
                    )
                    if should_log_hold:
                        cli.print_line(f"{display_time:<10} | {format_hold_decision(hold_decision)}")
                        cli.print_line(
                            f"{display_time:<10} | "
                            f"POSITION STATE {format_position_state(state_metrics)} | "
                            f"{strategy_decision['action']} {strategy_decision['reason']}"
                        )
                    if strategy_decision["action"] == "EXIT":
                        take_profit_exit = is_take_profit_exit(strategy_decision)
                        if (
                            take_profit_exit
                            and open_position.get("take_profit_exit_attempted")
                            and not position_has_exit_progress(open_position)
                        ):
                            cli.print_line(
                                f"{display_time:<10} | "
                                "EXIT_REVIEW TAKE_PROFIT already attempted once; holding position"
                            )
                        else:
                            liquidation = state_metrics.get("liquidation")
                            if liquidation is None:
                                exit_text = "EXIT_REVIEW executable liquidation unavailable"
                            else:
                                exit_pnl = liquidation - open_position["entry_cost"]
                                exit_text = (
                                    "EXIT_REVIEW "
                                    f"{strategy_decision['reason']}; "
                                    f"liquidation {cli.fmt_display_cents(liquidation)}c - "
                                    f"entry {cli.fmt_display_cents(open_position['entry_cost'])}c = "
                                    f"{cli.fmt_money(exit_pnl)} before exit fees"
                                )
                            cli.print_line(f"{display_time:<10} | {exit_text}")
                            if take_profit_exit:
                                open_position["take_profit_exit_attempted"] = True
                            exit_result, exit_complete = await execute_position_exit_async(
                                open_position,
                                kalshi_snapshot,
                                polymarket_snapshot,
                                polymarket_market,
                                args.live,
                                market_context,
                                chase_interval,
                                chase_max_steps,
                            )
                            cli.print_line(f"{display_time:<10} | {exit_result}")
                            if exit_complete:
                                mark_contract_cooldown(
                                    contract_cooldowns,
                                    contract_key,
                                    "post-exit safety pause",
                                )
                                open_position = None
                            elif take_profit_exit and not position_has_exit_progress(open_position):
                                open_position["exit_started"] = False
                else:
                    hold_metrics = source_filter_metrics(
                        kalshi_snapshot,
                        polymarket_snapshot,
                        source_snapshot,
                        open_position["entry_cost"],
                        kalshi_direction_price,
                    )
                    hold_decision = evaluate_hold_filter(
                        hold_metrics,
                        source_gap_threshold,
                        target_divergence_threshold,
                        hold_distance_multiplier,
                    )
                    state_metrics = position_state_metrics(
                        open_position,
                        kalshi_snapshot,
                        polymarket_snapshot,
                        source_snapshot,
                        polymarket_market,
                    )
                    open_position["last_state_metrics"] = state_metrics
                    open_position["last_state_time"] = (
                        kalshi_snapshot.get("timestamp_utc") or source_snapshot.get("timestamp_utc")
                    )
                    strategy_decision = exit_strategy_decision(
                        open_position,
                        state_metrics,
                        hold_decision,
                        take_profit_exit_value,
                        profit_capture_min_edge,
                        exit_cushion,
                    )
                    should_log_hold = (
                        arbitrage
                        or not hold_decision["passed"]
                        or strategy_decision["action"] != "HOLD"
                        or state_metrics.get("held_winners") != 1
                    )
                    if should_log_hold:
                        cli.print_line(f"{display_time:<10} | {format_hold_decision(hold_decision)}")
                        cli.print_line(
                            f"{display_time:<10} | "
                            f"POSITION STATE {format_position_state(state_metrics)} | "
                            f"{strategy_decision['action']} {strategy_decision['reason']}"
                        )
                    if strategy_decision["action"] == "EXIT":
                        take_profit_exit = is_take_profit_exit(strategy_decision)
                        if (
                            take_profit_exit
                            and open_position.get("take_profit_exit_attempted")
                            and not position_has_exit_progress(open_position)
                        ):
                            cli.print_line(
                                f"{display_time:<10} | "
                                "EXIT_REVIEW TAKE_PROFIT already attempted once; holding position"
                            )
                        else:
                            liquidation = state_metrics.get("liquidation")
                            if liquidation is None:
                                exit_text = "EXIT_REVIEW executable liquidation unavailable"
                            else:
                                exit_pnl = liquidation - open_position["entry_cost"]
                                exit_text = (
                                    "EXIT_REVIEW "
                                    f"{strategy_decision['reason']}; "
                                    f"liquidation {cli.fmt_display_cents(liquidation)}c - "
                                    f"entry {cli.fmt_display_cents(open_position['entry_cost'])}c = "
                                    f"{cli.fmt_money(exit_pnl)} before exit fees"
                                )
                            cli.print_line(f"{display_time:<10} | {exit_text}")
                            if take_profit_exit:
                                open_position["take_profit_exit_attempted"] = True
                            exit_result, exit_complete = await execute_position_exit_async(
                                open_position,
                                kalshi_snapshot,
                                polymarket_snapshot,
                                polymarket_market,
                                args.live,
                                market_context,
                                chase_interval,
                                chase_max_steps,
                            )
                            cli.print_line(f"{display_time:<10} | {exit_result}")
                            if exit_complete:
                                mark_contract_cooldown(
                                    contract_cooldowns,
                                    contract_key,
                                    "post-exit safety pause",
                                )
                                open_position = None
                            elif take_profit_exit and not position_has_exit_progress(open_position):
                                open_position["exit_started"] = False

            cooldown_reason = contract_cooldown_reason(contract_cooldowns, contract_key)
            entry_gate_reason = entry_prefilter_reason(
                open_position,
                boundary_reason,
                arbitrage,
                args.min_profit,
                trades_done,
                max_trades,
                cooldown_reason,
            )
            if entry_gate_reason is not None:
                if (
                    snapshot_log_due
                    and cooldown_reason is not None
                    and entry_gate_reason == cooldown_reason
                ):
                    cli.print_line(f"{display_time:<10} | ENTRY SKIP {cooldown_reason}")
            else:
                fallback_cost = arbitrage["kalshi_price"] + polymarket_execution_price(
                    arbitrage["polymarket_price"]
                )
                preliminary_metrics = source_filter_metrics(
                    kalshi_snapshot,
                    polymarket_snapshot,
                    source_snapshot,
                    fallback_cost,
                    kalshi_direction_price,
                )
                preliminary_decision = evaluate_entry_filter(
                    preliminary_metrics,
                    source_gap_threshold,
                    target_divergence_threshold,
                    min_profit_after_fees,
                )
                if not preliminary_decision["passed"]:
                    if (
                        snapshot_log_due
                        and filter_check_passed(preliminary_decision, "profit_after_fees")
                    ):
                        cli.print_line(f"{display_time:<10} | {format_entry_skip(preliminary_decision)}")
                    if len(rows) >= flush_every:
                        cli.append_rows(csv_path, rows)
                        rows.clear()
                    if args.once:
                        break
                    elapsed = time.monotonic() - started
                    await asyncio.sleep(max(0.0, interval - elapsed))
                    continue

                if not snapshot_printed:
                    cli.print_snapshot(kalshi_snapshot, polymarket_snapshot, arbitrage, ref_suffix)
                    snapshot_printed = True

                preflight = await trade_preflight_async(
                    kalshi_snapshot,
                    polymarket_market,
                    arbitrage,
                    contracts,
                    min_adjusted_profit,
                    market_context,
                )
                cli.print_line(
                    f"{cli.fmt_display_time(kalshi_snapshot.get('timestamp_utc')):<10} | "
                    f"{format_preflight(preflight)}"
                )
                executable_cost = (
                    preflight["kalshi_price"] + preflight["polymarket_price"]
                    if preflight.get("contracts", 0) > 0
                    else None
                )
                entry_metrics = source_filter_metrics(
                    kalshi_snapshot,
                    polymarket_snapshot,
                    source_snapshot,
                    executable_cost,
                    kalshi_direction_price,
                )
                entry_decision = evaluate_entry_filter(
                    entry_metrics,
                    source_gap_threshold,
                    target_divergence_threshold,
                    min_profit_after_fees,
                )
                if args.print_arb_orderbook:
                    for line in format_orderbook_debug(
                        preflight,
                        contracts,
                        args.book_depth_levels,
                    ):
                        cli.print_line(
                            f"{cli.fmt_display_time(kalshi_snapshot.get('timestamp_utc')):<10} | "
                            f"{line}"
                        )
                result = None
                if not entry_decision["passed"]:
                    if filter_check_passed(entry_decision, "profit_after_fees"):
                        result = format_entry_skip(entry_decision)
                elif preflight["decision"] != "PLACE":
                    result = f"ENTRY SKIP preflight: {preflight['reason']}"
                else:
                    partial_position = None
                    live_recheck_preflight = None
                    if args.live:
                        live_recheck_preflight = await trade_preflight_async(
                            kalshi_snapshot,
                            polymarket_market,
                            arbitrage,
                            contracts,
                            min_adjusted_profit,
                            market_context,
                        )
                    try:
                        result = await asyncio.to_thread(
                            execute_arbitrage,
                            kalshi_snapshot,
                            polymarket_market,
                            arbitrage,
                            contracts,
                            min_adjusted_profit,
                            args.live,
                            preflight,
                            live_recheck_preflight,
                        )
                    except PartialEntryError as exc:
                        result = str(exc)
                        partial_position = exc.position
                    if partial_position is not None:
                        open_position = partial_position
                        mark_contract_cooldown(
                            contract_cooldowns,
                            contract_key,
                            "hedge failure cleanup pending",
                            EXECUTION_FAILURE_COOLDOWN_SECONDS,
                        )
                        trades_done += 1
                    elif result.startswith("SKIP Kalshi hedge failed after Polymarket fill"):
                        mark_contract_cooldown(
                            contract_cooldowns,
                            contract_key,
                            "hedge failure cleanup completed",
                            EXECUTION_FAILURE_COOLDOWN_SECONDS,
                        )
                    elif result.startswith("SKIP live recheck:"):
                        mark_contract_cooldown(
                            contract_cooldowns,
                            contract_key,
                            "edge recheck failed",
                            EDGE_RECHECK_COOLDOWN_SECONDS,
                        )
                    elif result.startswith("SKIP Kalshi-first entry failed before Polymarket placement"):
                        mark_contract_cooldown(
                            contract_cooldowns,
                            contract_key,
                            "Kalshi-first execution failed before Polymarket",
                            EXECUTION_FAILURE_COOLDOWN_SECONDS,
                        )
                    elif result.startswith("SKIP Kalshi-first partial fill"):
                        mark_contract_cooldown(
                            contract_cooldowns,
                            contract_key,
                            "Kalshi-first partial fill cleanup completed",
                            EXECUTION_FAILURE_COOLDOWN_SECONDS,
                        )
                    elif result.startswith("SKIP Kalshi-first entry aborted"):
                        mark_contract_cooldown(
                            contract_cooldowns,
                            contract_key,
                            "Kalshi-first edge recheck cleanup completed",
                            EDGE_RECHECK_COOLDOWN_SECONDS,
                        )
                    elif result.startswith("SKIP Kalshi-first entry incomplete"):
                        mark_contract_cooldown(
                            contract_cooldowns,
                            contract_key,
                            "Kalshi-first execution failure cleanup completed",
                            EXECUTION_FAILURE_COOLDOWN_SECONDS,
                        )
                    elif not result.startswith("SKIP ") and not result.startswith("DRY RUN would skip"):
                        actual_entry_cost = (
                            result.entry_cost
                            if isinstance(result, TradeResult) and result.entry_cost is not None
                            else preflight["kalshi_price"] + preflight["polymarket_price"]
                        )
                        actual_contracts = (
                            result.contracts
                            if isinstance(result, TradeResult) and result.contracts is not None
                            else int(preflight["contracts"])
                        )
                        open_position = {
                            "ticker": kalshi_snapshot.get("ticker"),
                            "close_time": kalshi_snapshot.get("close_time"),
                            "kalshi_side": preflight["kalshi_side"],
                            "polymarket_contract": preflight["polymarket_contract"],
                            "entry_cost": actual_entry_cost,
                            "entry_time": kalshi_snapshot.get("timestamp_utc"),
                            "contracts": actual_contracts,
                            "kalshi_contracts": actual_contracts,
                            "polymarket_contracts": actual_contracts,
                            "kalshi_absent": False,
                            "polymarket_absent": False,
                            "boundary_review_logged": False,
                        }
                        trades_done += 1
                if result is not None:
                    cli.print_line(f"{cli.fmt_display_time(kalshi_snapshot.get('timestamp_utc')):<10} | {result}")

            if len(rows) >= flush_every:
                cli.append_rows(csv_path, rows)
                rows.clear()
        except KeyboardInterrupt:
            if pending_rows:
                cli.flush_pending(pending_rows)
            raise
        except FatalTradeError as exc:
            cli.print_line(f"{btc.iso_utc()} | FATAL {exc}")
            if pending_rows:
                cli.flush_pending(pending_rows)
            break
        except Exception as exc:
            cli.print_line(f"{btc.iso_utc()} | ERROR {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=2)

        if args.once:
            break
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(0.0, interval - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
